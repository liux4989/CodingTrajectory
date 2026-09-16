import { DIGEST, Fault, Json, UUID, decode, digest, encode, integer, receipt, requireThat, safeChronicle, stable, State, timestamp, uuid, validate } from "./shared";


/** Published-fact kinds mirrored from ct.published_facts.v1 (published_facts.py). */
export const FACT_KINDS = ["graph", "session", "turn", "item", "event", "edge", "request", "model", "runtime", "measurement", "output_evidence"];
export const FACT_KIND_LIMITS: Record<string, number> = {
  graph: 1, session: 512, turn: 32768, item: 131072, event: 131072, edge: 8192,
  request: 65536, model: 256, runtime: 32768, measurement: 512, output_evidence: 131072,
};
const FACT_ROW_BATCH_MAX = 512;
const FACT_READ_PAGE_MAX = 2048;
const FACT_PUBLICATION_MAX_GRAPHS = 512;
const MAX_FACT_SET_BYTES = 8 * 1024 * 1024;
const MAX_FACT_ROW_BYTES = 512 * 1024;
const FACT_READ_PAGE_MAX_BYTES = 1024 * 1024;
const FACT_PUBLICATION_MAX_BYTES = 16 * 1024 * 1024;
const MAX_STAGED_BATCH_BYTES = 2 * 1024 * 1024;
const FACT_SET_SCHEMA = "ct.published_facts.v1";
const PUBLISHED_COMMANDS = new Set([
  "bash", "bun", "cargo", "cat", "cmake", "command", "cp", "curl", "deno", "docker", "find",
  "gh", "git", "go", "grep", "kubectl", "ls", "make", "mkdir", "mv", "node", "npm", "npx",
  "pnpm", "python", "python3", "rg", "rm", "ruff", "sed", "sh", "terraform", "uv", "wget",
  "wrangler", "yarn", "zsh",
]);
/** Kinds whose rows must reference a retained parent fact, by parent kind. */
const PARENT_KINDS: Record<string, string[]> = {
  session: ["graph"], turn: ["session"], item: ["turn"], event: ["turn", "session"],
  output_evidence: ["item"], request: ["turn"], runtime: ["session"], measurement: ["session"],
  edge: ["graph"], model: ["graph"],
};

export function initializeFacts(state: State) {
  state.sql.exec(`CREATE TABLE IF NOT EXISTS staged_fact_rows (
    agent_id TEXT NOT NULL, graph_id TEXT NOT NULL, fact_set_digest TEXT NOT NULL,
    batch_index INTEGER NOT NULL, batch_count INTEGER NOT NULL,
    row_count INTEGER NOT NULL, rows_json TEXT NOT NULL,
    PRIMARY KEY (agent_id, graph_id, batch_index));
    CREATE TABLE IF NOT EXISTS fact_rows (
      graph_id TEXT NOT NULL, kind TEXT NOT NULL, fact_id TEXT NOT NULL,
      parent_id TEXT, order_index INTEGER, row_hash TEXT NOT NULL, payload TEXT NOT NULL,
      valid_from_sequence INTEGER NOT NULL, valid_to_sequence INTEGER,
      PRIMARY KEY (graph_id, kind, fact_id, valid_from_sequence));
    CREATE INDEX IF NOT EXISTS fact_rows_current
      ON fact_rows(graph_id, kind, fact_id) WHERE valid_to_sequence IS NULL;
    CREATE INDEX IF NOT EXISTS fact_rows_session
      ON fact_rows(fact_id) WHERE kind = 'session';`);
}

/** Strip null-valued object fields recursively, matching Pydantic exclude_none. */
function deepStrip(value: any): any {
  if (Array.isArray(value)) return value.map(deepStrip);
  if (value !== null && typeof value === "object") {
    const result: Json = {};
    for (const [key, child] of Object.entries(value)) if (child !== null) result[key] = deepStrip(child);
    return result;
  }
  return value;
}

function rowHashView(row: Json): Json {
  const view: Json = deepStrip(row);
  delete view.row_hash;
  return view;
}

async function rowHash(row: Json): Promise<string> {
  return digest(stable(rowHashView(row)));
}

export async function factSetDigest(graphId: string, rows: Json[]): Promise<string> {
  const ordered = [...rows].sort((a, b) => stable([a.kind, a.fact_id]) < stable([b.kind, b.fact_id]) ? -1 : 1);
  return digest(stable({
    schema_version: FACT_SET_SCHEMA, graph_id: graphId,
    rows: ordered.map(row => [row.kind, row.fact_id, row.row_hash]),
  }));
}

/** Async staging validation: contract shape, row hashes, float rejection. */
export async function verifyStageRows(request: Json): Promise<void> {
  validate("ct_collector_stage_fact_rows", request);
  requireThat(request.rows.length <= FACT_ROW_BATCH_MAX, "fact_batch_too_large", 413);
  request.rows = request.rows.map((row: Json) => deepStrip(row));
  const graphId = uuid(request.graph_id);
  for (const row of request.rows) {
    requireThat(row.graph_id === graphId, "fact_row_graph_mismatch");
    requireThat(new TextEncoder().encode(stable(row)).length <= MAX_FACT_ROW_BYTES, "fact_row_too_large", 413);
    requireThat(await rowHash(row) === row.row_hash, "fact_row_hash_mismatch");
    requireNoFloats(row.payload);
  }
}

/** Synchronous staging write, always inside the workspace transaction. */
export function writeStagedRows(state: State, request: Json): Json {
  const graphId = uuid(request.graph_id);
  const encoded = stable(deepStrip(request.rows));
  requireThat(new TextEncoder().encode(encoded).length <= MAX_STAGED_BATCH_BYTES, "fact_batch_too_large", 413);
  const existing = state.sql.exec<{ fact_set_digest: string; batch_count: number }>(
    "SELECT DISTINCT fact_set_digest, batch_count FROM staged_fact_rows WHERE agent_id=? AND graph_id=?",
    request.agent_id, graphId).toArray();
  if (existing.some(row => row.fact_set_digest !== request.fact_set_digest)) {
    // A republished graph supersedes previously staged batches atomically.
    state.sql.exec("DELETE FROM staged_fact_rows WHERE agent_id=? AND graph_id=?", request.agent_id, graphId);
  }
  const sameDigest = existing.filter(row => row.fact_set_digest === request.fact_set_digest);
  requireThat(!sameDigest.length || sameDigest.every(row => row.batch_count === request.batch_count), "fact_batch_count_conflict", 409);
  state.sql.exec(
    "INSERT OR REPLACE INTO staged_fact_rows VALUES(?,?,?,?,?,?,?)",
    request.agent_id, graphId, request.fact_set_digest, request.batch_index,
    request.batch_count, request.rows.length, encoded);
  return missingFactRows(state, {
    workspace_id: request.workspace_id, agent_id: request.agent_id, graph_id: graphId,
    fact_set_digest: request.fact_set_digest, batch_count: request.batch_count,
  });
}

export function missingFactRows(state: State, request: Json): Json {
  validate("ct_collector_missing_fact_rows", request);
  const graphId = uuid(request.graph_id);
  const staged = new Set(state.sql.exec<{ batch_index: number }>(
    "SELECT batch_index FROM staged_fact_rows WHERE agent_id=? AND graph_id=? AND fact_set_digest=?",
    request.agent_id, graphId, request.fact_set_digest).toArray().map(row => row.batch_index));
  const missing: number[] = [];
  for (let index = 0; index < request.batch_count; index++) if (!staged.has(index)) missing.push(index);
  return { graph_id: graphId, fact_set_digest: request.fact_set_digest,
    staged_batches: request.batch_count - missing.length, missing_batches: missing };
}

interface StagedGraph { rows: Json[]; vendors: string[]; sessionIds: string[] }

function factSetView(publication: Json, rows: Json[]): Json {
  return deepStrip({
    schema_version: FACT_SET_SCHEMA,
    graph_id: publication.graph_id,
    fact_set_digest: publication.fact_set_digest,
    kind_counts: publication.kind_counts,
    rows,
  });
}

async function stagedGraph(state: State, agentId: string, publication: Json): Promise<StagedGraph> {
  const graphId = publication.graph_id;
  const batches = state.sql.exec<{ batch_index: number; batch_count: number; rows_json: string }>(
    "SELECT batch_index, batch_count, rows_json FROM staged_fact_rows WHERE agent_id=? AND graph_id=? AND fact_set_digest=? ORDER BY batch_index",
    agentId, graphId, publication.fact_set_digest).toArray();
  requireThat(batches.length > 0, "fact_rows_must_be_staged");
  const batchCount = batches[0].batch_count;
  requireThat(batches.every(batch => batch.batch_count === batchCount), "fact_batch_count_conflict", 409);
  requireThat(batches.length === batchCount && batches.every((batch, index) => batch.batch_index === index),
    "fact_rows_incomplete", 409);
  const rows = batches.flatMap(batch => JSON.parse(batch.rows_json) as Json[]);
  requireThat(rows.length === publication.fact_count, "fact_count_mismatch");

  // Full integrity validation: uniqueness, canonical order, row hashes,
  // cardinality bounds, parent/reference integrity, kind counts, and digest.
  const keys = new Set<string>();
  const present = new Set<string>();
  const counts: Record<string, number> = {};
  for (const row of rows) {
    requireThat(row.graph_id === graphId && FACT_KINDS.includes(row.kind), "invalid_fact_row");
    const key = `${row.kind}:${row.fact_id}`;
    requireThat(!keys.has(key), "duplicate_fact_row");
    keys.add(key);
    present.add(key);
    counts[row.kind] = (counts[row.kind] ?? 0) + 1;
    requireThat(await rowHash(row) === row.row_hash, "fact_row_hash_mismatch");
  }
  for (const [kind, count] of Object.entries(counts)) {
    requireThat(count <= FACT_KIND_LIMITS[kind], "fact_kind_cardinality_exceeded", 413);
    requireThat(counts[kind] === publication.kind_counts[kind], "fact_kind_counts_mismatch");
  }
  requireThat(Object.keys(counts).length === Object.keys(publication.kind_counts).length, "fact_kind_counts_mismatch");
  for (const row of rows) {
    const expected = PARENT_KINDS[row.kind];
    if (row.parent_id == null) {
      requireThat(row.kind === "graph", "fact_row_parent_required");
      continue;
    }
    requireThat(expected, "fact_row_parent_forbidden");
    requireThat(expected.some(kind => present.has(`${kind}:${row.parent_id}`)), "fact_row_parent_missing");
  }
  validateFactRelationships(graphId, rows);
  requireThat(await factSetDigest(graphId, rows) === publication.fact_set_digest, "fact_set_digest_mismatch");
  requireThat(new TextEncoder().encode(stable(factSetView(publication, rows))).length <= MAX_FACT_SET_BYTES,
    "fact_set_too_large", 413);
  const sessionIds = rows.filter(row => row.kind === "session").map(row => row.fact_id);
  const vendors = [...new Set(rows.filter(row => row.kind === "session").map(row => String(row.payload.vendor)))].sort();
  requireThat(vendors.every(vendor => typeof vendor === "string" && vendor.length <= 512), "invalid_fact_row");
  for (const row of rows) safeChronicle(row.payload);
  return { rows, vendors, sessionIds };
}

function validateFactRelationships(graphId: string, rows: Json[]) {
  const byKind: Record<string, Map<string, Json>> = Object.fromEntries(
    FACT_KINDS.map(kind => [kind, new Map(rows.filter(row => row.kind === kind).map(row => [row.fact_id, row]))]),
  );
  const graphRows = [...byKind.graph.values()];
  requireThat(graphRows.length === 1, "graph_fact_count_mismatch");
  const graph = graphRows[0];
  requireThat(graph.fact_id === graphId && graph.parent_id == null
    && graph.payload.summary.root_session_id === graphId, "graph_fact_identity_mismatch");
  requireThat(byKind.session.has(graphId), "graph_root_session_missing");
  const validateSequences = (grouped: Map<string, Json[]>, label: string) => {
    for (const groupedRows of grouped.values()) {
      const sequences = groupedRows.map(row => row.payload.sequence);
      requireThat(new Set(sequences).size === sequences.length
        && groupedRows.every(row => row.order_index === row.payload.sequence), `${label}_ordering_invalid`);
    }
  };

  for (const row of byKind.session.values()) {
    requireThat(row.fact_id === row.payload.session_id && row.parent_id === graphId, "session_fact_identity_mismatch");
    if (row.payload.parent_session_id != null) requireThat(byKind.session.has(row.payload.parent_session_id), "session_parent_missing");
    for (const origin of row.payload.topology?.spawn_origins ?? []) {
      if (origin.turn_id != null) requireThat(byKind.turn.get(origin.turn_id)?.parent_id === row.fact_id, "session_spawn_turn_mismatch");
      if (origin.item_id != null) requireThat(byKind.item.get(origin.item_id)?.parent_id === origin.turn_id, "session_spawn_item_mismatch");
    }
  }
  for (const row of byKind.turn.values()) {
    requireThat(row.fact_id === row.payload.turn_id && byKind.session.has(row.parent_id), "turn_fact_identity_mismatch");
  }
  const turnsBySession = new Map<string, Json[]>();
  for (const row of byKind.turn.values()) {
    const grouped = turnsBySession.get(row.parent_id) ?? [];
    grouped.push(row);
    turnsBySession.set(row.parent_id, grouped);
  }
  validateSequences(turnsBySession, "turn");
  for (const row of byKind.item.values()) {
    requireThat(row.fact_id === row.payload.item_id && byKind.turn.has(row.parent_id), "item_fact_identity_mismatch");
    const projectionParentId = row.payload.projection_parent_item_id;
    if (projectionParentId == null) {
      requireThat(row.payload.nested_index == null, "item_nested_index_without_parent");
    } else {
      const parent = byKind.item.get(projectionParentId);
      requireThat(parent && parent.fact_id !== row.fact_id && parent.parent_id === row.parent_id,
        "item_projection_parent_mismatch");
      requireThat(parent.payload.measurements.projection_only !== true,
        "item_projection_parent_not_canonical");
      requireThat(row.payload.measurements.projection_only === true,
        "item_projection_child_owns_canonical_content");
    }
    for (const eventId of row.payload.event_ids) {
      requireThat(byKind.event.get(eventId)?.payload.item_id === row.fact_id, "item_event_reference_mismatch");
    }
    const detail = row.payload.measurements?.tool_summary?.detail;
    if (detail?.kind === "command") requireThat(PUBLISHED_COMMANDS.has(detail.target), "unsafe_command_detail");
  }
  const itemsByTurn = new Map<string, Json[]>();
  for (const row of byKind.item.values()) {
    const grouped = itemsByTurn.get(row.parent_id) ?? [];
    grouped.push(row);
    itemsByTurn.set(row.parent_id, grouped);
  }
  validateSequences(itemsByTurn, "item");
  const eventsBySession = new Map<string, Json[]>();
  for (const row of byKind.event.values()) {
    const event = row.payload;
    requireThat(row.fact_id === event.event_id, "event_fact_identity_mismatch");
    if (event.turn_id != null) requireThat(byKind.turn.has(event.turn_id) && row.parent_id === event.turn_id,
      "event_turn_ownership_mismatch");
    else requireThat(byKind.session.has(row.parent_id), "session_event_ownership_mismatch");
    const sessionId = event.turn_id != null ? byKind.turn.get(event.turn_id)!.parent_id : row.parent_id;
    const grouped = eventsBySession.get(sessionId) ?? [];
    grouped.push(row);
    eventsBySession.set(sessionId, grouped);
    if (event.item_id != null) {
      const item = byKind.item.get(event.item_id);
      requireThat(item && item.parent_id === event.turn_id && item.payload.event_ids.includes(event.event_id),
        "event_item_ownership_mismatch");
    }
  }
  validateSequences(eventsBySession, "event");
  for (const row of byKind.request.values()) {
    requireThat(row.fact_id === row.payload.request_id && byKind.turn.has(row.parent_id), "request_fact_identity_mismatch");
  }
  for (const row of byKind.output_evidence.values()) {
    requireThat(row.parent_id === row.fact_id && byKind.item.has(row.fact_id), "output_evidence_item_mismatch");
    for (const eventId of row.payload.source_event_ids ?? []) {
      requireThat(byKind.event.get(eventId)?.payload.item_id === row.fact_id, "output_evidence_event_mismatch");
    }
  }
  const edgeIdentities = new Set<string>();
  for (const row of byKind.edge.values()) {
    const edge = row.payload;
    requireThat(row.parent_id === graphId && byKind.session.has(edge.source_session_id)
      && byKind.session.has(edge.target_session_id) && edge.origin.session_id === edge.source_session_id,
    "edge_session_ownership_mismatch");
    if (edge.origin.turn_id != null) requireThat(byKind.turn.get(edge.origin.turn_id)?.parent_id === edge.source_session_id,
      "edge_turn_ownership_mismatch");
    if (edge.origin.item_id != null) requireThat(byKind.item.get(edge.origin.item_id)?.parent_id === edge.origin.turn_id,
      "edge_item_ownership_mismatch");
    const identity = stable([edge.kind, edge.source_session_id, edge.target_session_id,
      edge.origin.turn_id ?? null, edge.origin.item_id ?? null]);
    requireThat(!edgeIdentities.has(identity), "duplicate_edge_identity");
    edgeIdentities.add(identity);
    for (const eventId of [...(edge.evidence_event_ids ?? []), ...(edge.origin.event_id ? [edge.origin.event_id] : [])]) {
      const event = byKind.event.get(eventId);
      requireThat(event?.parent_id === (edge.origin.turn_id ?? edge.source_session_id)
        && event?.payload.item_id === (edge.origin.item_id ?? null),
        "edge_event_ownership_mismatch");
    }
  }
  requireThat(graph.payload.summary.session_count === byKind.session.size
    && graph.payload.summary.turn_count === byKind.turn.size
    && graph.payload.summary.item_count === byKind.item.size, "graph_summary_count_mismatch");
}

export interface PublicationPlan {
  graphs: Map<string, StagedGraph & { publication: Json }>;
  sessionIds: Set<string>;
}

/**
 * Async validation phase: verify staged completeness, row hashes, reference
 * integrity, cardinality, and the deterministic fact-set digest. All hashing
 * happens here because crypto.subtle is async; the fencing checks and the
 * commit itself run synchronously inside the workspace transaction.
 */
export async function preparePublication(state: State, request: Json): Promise<PublicationPlan> {
  validate("ct_collector_publish_facts", request);
  requireThat(request.source_vector.length <= 1000 && request.graphs.length <= FACT_PUBLICATION_MAX_GRAPHS, "publication_scope_budget", 413);
  const vector = new Map<string, Json>(request.source_vector.map((entry: Json) => [entry.source_id, entry]));
  requireThat(vector.size === request.source_vector.length, "duplicate_source_vector");
  let publicationBytes = 0;
  for (const publication of request.graphs) {
    const staged = state.sql.exec<{ encoded_bytes: number; nonempty_batches: number }>(
      `SELECT coalesce(sum(length(CAST(rows_json AS BLOB))),0) AS encoded_bytes,
         coalesce(sum(CASE WHEN row_count > 0 THEN 1 ELSE 0 END),0) AS nonempty_batches
       FROM staged_fact_rows WHERE agent_id=? AND graph_id=? AND fact_set_digest=?`,
      request.agent_id, publication.graph_id, publication.fact_set_digest).one();
    requireThat(staged.encoded_bytes > 0, "fact_rows_must_be_staged");
    const wrapperBytes = new TextEncoder().encode(stable(factSetView(publication, []))).length;
    const combinedRowsBytes = staged.encoded_bytes - staged.nonempty_batches + 1;
    publicationBytes += wrapperBytes - 2 + combinedRowsBytes;
    requireThat(publicationBytes <= FACT_PUBLICATION_MAX_BYTES, "publication_byte_budget", 413);
  }
  const graphs = new Map<string, StagedGraph & { publication: Json }>();
  const sessionIds = new Set<string>();
  for (const publication of request.graphs) {
    requireThat(!graphs.has(publication.graph_id), "duplicate_graph_publication");
    requireThat(new Set(publication.source_ids).size === publication.source_ids.length
      && publication.source_ids.every((id: string) => vector.has(id)), "invalid_graph_sources");
    const staged = await stagedGraph(state, request.agent_id, publication);
    graphs.set(publication.graph_id, { ...staged, publication });
    for (const id of staged.sessionIds) {
      requireThat(!sessionIds.has(id), "overlapping_incoming_graphs");
      sessionIds.add(id);
    }
  }
  const represented = new Set<string>();
  for (const publication of request.graphs) for (const id of publication.source_ids) represented.add(id);
  requireThat(represented.size === vector.size, "unrepresented_source");
  return { graphs, sessionIds };
}

/** Synchronous commit phase: fencing, scope checks, atomic row application. */
export function commitPublication(state: State, request: Json, plan: PublicationPlan): Json {
  requireThat(state.get("project", request.project_id), "project_not_found", 404);
  const vector = new Map<string, Json>(request.source_vector.map((entry: Json) => [entry.source_id, entry]));
  let stale = false;
  for (const entry of vector.values()) {
    const source = state.get("source", entry.source_id);
    const checkpoint = state.get("checkpoint", `${entry.source_id}:${entry.source_epoch}:${entry.source_sequence}`);
    requireThat(source && source.agent_id === request.agent_id && source.project_id === request.project_id && checkpoint?.content_sha256 === entry.content_sha256,
      "source_vector_requires_accepted_project_checkpoints");
    if (source.source_epoch !== entry.source_epoch || source.committed_source_sequence !== entry.source_sequence) stale = true;
  }
  for (const [graphId, staged] of plan.graphs) {
    // Re-verify the staged set did not change between prepare and commit.
    const count = state.sql.exec<{ batches: number; rows: number }>(
      "SELECT count(*) AS batches, coalesce(sum(row_count),0) AS rows FROM staged_fact_rows WHERE agent_id=? AND graph_id=? AND fact_set_digest=?",
      request.agent_id, graphId, staged.publication.fact_set_digest).one();
    requireThat(count.rows === staged.rows.length, "fact_rows_incomplete", 409);
    const prior = state.get("graph_publication", graphId);
    requireThat(!prior || prior.project_id === request.project_id, "graph_project_conflict", 409);
    requireThat(!prior || prior.agent_id === request.agent_id, "graph_owner_conflict", 409);
  }

  const publisherKey = `${request.agent_id}:${request.project_id}`;
  const publisher = state.get("publisher", publisherKey);
  const currentSequence = publisher?.publication_sequence ?? -1;
  if (request.publication_sequence <= currentSequence) {
    return receipt("conflict", publisher?.committed_sequence ?? null, { reason: "stale_publication_sequence" });
  }
  requireThat(request.publication_sequence === currentSequence + 1, "publication_sequence_gap", 409);

  // Select only affected project graphs, not every graph in the workspace.
  const affected = state.all("graph_publication").filter(row => !row.deleted
    && row.project_id === request.project_id
    && (row.session_ids.some((id: string) => plan.sessionIds.has(id))
      || (row.agent_id === request.agent_id && row.source_ids.every((id: string) => vector.has(id)))));
  requireThat(affected.length <= 1000, "publication_scope_budget", 413);
  requireThat(!affected.some(row => row.agent_id !== request.agent_id && row.session_ids.some((id: string) => plan.sessionIds.has(id))),
    "session_owner_conflict", 409);
  const incomplete = affected.some(row => row.session_ids.some((id: string) => plan.sessionIds.has(id))
    && row.source_ids.some((id: string) => !vector.has(id)));

  const sequence = state.next();
  state.put("publisher", publisherKey, { publication_sequence: request.publication_sequence, committed_sequence: sequence }, sequence);
  if (stale || incomplete) {
    return receipt("accepted", sequence, incomplete
      ? { reason: "incomplete_graph_scope", publication_outcome: "rejected", remedy: "include all sources of overlapping published graphs" }
      : { publication_outcome: "superseded" });
  }

  const publishedAt = new Date().toISOString();
  let inserted = 0, reused = 0, closed = 0, omitted = 0, superseded = 0;
  for (const [graphId, staged] of plan.graphs) {
    const publication = staged.publication;
    const counts = applyFactRows(state, graphId, staged.rows, sequence);
    inserted += counts.inserted; reused += counts.reused; closed += counts.closed;
    const prior = state.get("graph_publication", graphId);
    if (prior && !prior.deleted) superseded++;
    state.put("graph_publication", graphId, {
      graph_id: graphId, project_id: request.project_id, agent_id: request.agent_id,
      schema_version: FACT_SET_SCHEMA, fact_set_digest: publication.fact_set_digest,
      fact_count: publication.fact_count, kind_counts: publication.kind_counts,
      source_ids: publication.source_ids, session_ids: staged.sessionIds, vendors: staged.vendors,
      observed_at: publication.observed_at, revision: (prior?.revision ?? 0) + 1,
      published_sequence: sequence, published_at: publishedAt, deleted: false,
    }, sequence);
    state.sql.exec("DELETE FROM staged_fact_rows WHERE agent_id=? AND graph_id=?", request.agent_id, graphId);
  }
  if ((request.replacement_scope ?? "complete_sources") === "complete_sources") {
    for (const row of affected) {
      if (row.agent_id === request.agent_id && !plan.graphs.has(row.graph_id)
        && row.source_ids.every((id: string) => vector.has(id))) {
        closed += closeFactRows(state, row.graph_id, sequence);
        state.put("graph_publication", row.graph_id, { ...row, deleted: true, published_at: publishedAt }, sequence);
        omitted++;
      }
    }
  }
  return receipt("accepted", sequence, { publication_outcome: "published",
    graphs_published: plan.graphs.size, rows_inserted: inserted, rows_reused: reused,
    rows_closed: closed, superseded_revisions: superseded, omitted_graphs: omitted });
}

function applyFactRows(state: State, graphId: string, rows: Json[], sequence: number): { inserted: number; reused: number; closed: number } {
  const current = new Map(state.sql.exec<{ kind: string; fact_id: string; row_hash: string }>(
    "SELECT kind, fact_id, row_hash FROM fact_rows WHERE graph_id=? AND valid_to_sequence IS NULL", graphId)
    .toArray().map(row => [`${row.kind}:${row.fact_id}`, row]));
  let inserted = 0, reused = 0, closed = 0;
  for (const row of rows) {
    const key = `${row.kind}:${row.fact_id}`;
    const existing = current.get(key);
    if (existing && existing.row_hash === row.row_hash) { reused++; current.delete(key); continue; }
    if (existing) { closeFactRow(state, graphId, row.kind, row.fact_id, sequence); closed++; current.delete(key); }
    state.sql.exec("INSERT INTO fact_rows VALUES(?,?,?,?,?,?,?,?,NULL)",
      graphId, row.kind, row.fact_id, row.parent_id ?? null, row.order_index ?? null,
      row.row_hash, stable(deepStrip(row)), sequence);
    inserted++;
  }
  for (const row of current.values()) { closeFactRow(state, graphId, row.kind, row.fact_id, sequence); closed++; }
  return { inserted, reused, closed };
}

function closeFactRow(state: State, graphId: string, kind: string, factId: string, sequence: number) {
  state.sql.exec("UPDATE fact_rows SET valid_to_sequence=? WHERE graph_id=? AND kind=? AND fact_id=? AND valid_to_sequence IS NULL",
    sequence - 1, graphId, kind, factId);
}

function closeFactRows(state: State, graphId: string, sequence: number): number {
  state.sql.exec("UPDATE fact_rows SET valid_to_sequence=? WHERE graph_id=? AND valid_to_sequence IS NULL", sequence - 1, graphId);
  return state.sql.exec<{ count: number }>("SELECT changes() AS count").one().count;
}

async function cursorKey(secret: string): Promise<CryptoKey> {
  requireThat(typeof secret === "string" && new TextEncoder().encode(secret).length >= 32,
    "cursor_authentication_unavailable", 503);
  return crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"]);
}

async function factCursor(
  sequence: number,
  scopeDigest: string,
  after: [string, string, string],
  secret: string,
): Promise<string> {
  const payload = new TextEncoder().encode(stable({
    version: 3, kind: "fact_page", sequence, scope_digest: scopeDigest, after,
  }));
  const signature = await crypto.subtle.sign("HMAC", await cursorKey(secret), payload);
  return `${encode(payload)}.${encode(new Uint8Array(signature))}`;
}

async function parseFactCursor(
  value: unknown,
  secret: string,
): Promise<{ sequence: number; scope_digest: string; after: [string, string, string] }> {
  requireThat(typeof value === "string" && value.length <= 4096, "invalid_cursor");
  try {
    const parts = value.split(".");
    requireThat(parts.length === 2 && parts.every(part => part.length > 0), "invalid_cursor");
    const payload = new Uint8Array(decode(parts[0]));
    const signature = new Uint8Array(decode(parts[1]));
    requireThat(signature.length === 32
      && await crypto.subtle.verify(
        "HMAC", await cursorKey(secret), signature.buffer, payload.buffer), "invalid_cursor");
    const parsed = JSON.parse(new TextDecoder().decode(payload));
    requireThat(parsed && parsed.version === 3 && parsed.kind === "fact_page"
      && Number.isSafeInteger(parsed.sequence) && parsed.sequence >= 0
      && typeof parsed.scope_digest === "string" && DIGEST.test(parsed.scope_digest)
      && Array.isArray(parsed.after) && parsed.after.length === 3
      && parsed.after.every((part: unknown) => typeof part === "string"), "invalid_cursor");
    return parsed;
  } catch (error) {
    if (error instanceof Fault) throw error;
    throw new Fault(400, "invalid_cursor");
  }
}

/** Read one page of fact rows pinned to one workspace publication sequence. */
export async function factRead(state: State, request: Json, cursorSecret: string): Promise<Json> {
  validate("ct_fact_read", request);
  await cursorKey(cursorSecret);
  const sequence = state.pin(request.snapshot_sequence);
  const kinds = request.kinds == null ? null : [...new Set(request.kinds as string[])].sort();
  requireThat(!kinds || (kinds.length <= FACT_KINDS.length && kinds.every(kind => FACT_KINDS.includes(kind))), "invalid_fact_kinds");
  const scopeDigest = await digest(stable({
    workspace_id: uuid(request.workspace_id),
    sequence,
    graph_id: request.graph_id == null ? null : uuid(request.graph_id),
    session_id: request.session_id == null ? null : uuid(request.session_id),
    project_name: request.project_name ?? null,
    agent_vendor: request.agent_vendor ?? null,
    modified_since: request.modified_since == null ? null : new Date(timestamp(request.modified_since)).toISOString(),
    kinds,
  }));
  let after: [string, string, string] | null = null;
  if (request.cursor) {
    const cursor = await parseFactCursor(request.cursor, cursorSecret);
    requireThat(cursor.sequence === sequence, "snapshot_conflict", 409);
    requireThat(cursor.scope_digest === scopeDigest, "cursor_scope_conflict", 409);
    after = cursor.after;
  }
  const graphs = selectGraphs(state, request, sequence);
  requireThat(graphs.length <= FACT_PUBLICATION_MAX_GRAPHS, "fact_read_scope_budget", 413);
  const graphIds = graphs.map(graph => graph.graph_id);
  const digests = Object.fromEntries(graphs.map(graph => [graph.graph_id, graph.fact_set_digest]));
  const counts = Object.fromEntries(graphs.map(graph => [graph.graph_id, graph.fact_count]));
  if (!graphIds.length) {
    return { workspace_id: request.workspace_id, snapshot_sequence: sequence,
      rows: [], graph_digests: {}, graph_fact_counts: {}, next_cursor: null };
  }
  const limit = integer(request.limit ?? FACT_READ_PAGE_MAX, 1, FACT_READ_PAGE_MAX);
  const bindings: unknown[] = [JSON.stringify(graphIds), sequence, sequence];
  let where = `WHERE graph_id IN (SELECT value FROM json_each(?))
    AND valid_from_sequence <= ? AND (valid_to_sequence IS NULL OR valid_to_sequence >= ?)`;
  if (kinds) { where += " AND kind IN (SELECT value FROM json_each(?))"; bindings.push(JSON.stringify(kinds)); }
  if (after) {
    where += " AND (graph_id > ? OR (graph_id = ? AND (kind > ? OR (kind = ? AND fact_id > ?))))";
    bindings.push(after[0], after[0], after[1], after[1], after[2]);
  }
  const page = state.sql.exec<{ graph_id: string; kind: string; fact_id: string; payload: string }>(
    `WITH ordered AS (
       SELECT graph_id, kind, fact_id, payload, length(CAST(payload AS BLOB)) AS payload_bytes,
         row_number() OVER (ORDER BY graph_id, kind, fact_id) AS row_number
       FROM fact_rows ${where}
     ), budgeted AS (
       SELECT *, sum(payload_bytes) OVER (ORDER BY graph_id, kind, fact_id) AS cumulative_bytes
       FROM ordered
     )
     SELECT graph_id, kind, fact_id, payload FROM budgeted
     WHERE row_number <= ? AND cumulative_bytes + row_number <= ?
     ORDER BY graph_id, kind, fact_id`, ...bindings as any[], limit, FACT_READ_PAGE_MAX_BYTES - 1).toArray();
  if (!page.length) {
    return { workspace_id: request.workspace_id, snapshot_sequence: sequence,
      rows: [], graph_digests: digests, graph_fact_counts: counts, next_cursor: null };
  }
  const rows = page.map(row => JSON.parse(row.payload));
  const last = page[page.length - 1];
  const moreBindings = [...bindings, last.graph_id, last.graph_id, last.kind, last.kind, last.fact_id];
  const hasMore = state.sql.exec<{ present: number }>(
    `SELECT 1 AS present FROM fact_rows ${where}
     AND (graph_id > ? OR (graph_id = ? AND (kind > ? OR (kind = ? AND fact_id > ?)))) LIMIT 1`,
    ...moreBindings as any[]).toArray().length > 0;
  return { workspace_id: request.workspace_id, snapshot_sequence: sequence, rows,
    graph_digests: digests, graph_fact_counts: counts,
    next_cursor: hasMore
      ? await factCursor(sequence, scopeDigest, [last.graph_id, last.kind, last.fact_id], cursorSecret)
      : null };
}

type SelectedGraph = { graph_id: string; fact_set_digest: string; fact_count: number };

function selectGraphs(state: State, request: Json, sequence: number): SelectedGraph[] {
  let graphId = request.graph_id ? uuid(request.graph_id) : null;
  if (request.session_id) {
    const sessionId = uuid(request.session_id);
    const rows = state.sql.exec<{ graph_id: string }>(
      `SELECT graph_id FROM fact_rows WHERE kind='session' AND fact_id=?
       AND valid_from_sequence <= ? AND (valid_to_sequence IS NULL OR valid_to_sequence >= ?) LIMIT 2`,
      sessionId, sequence, sequence).toArray();
    requireThat(rows.length <= 1, "session_graph_conflict", 409);
    if (!rows.length) return [];
    graphId = rows[0].graph_id;
  }
  const bindings: unknown[] = [sequence];
  let filters = "";
  if (graphId) { filters += " AND graph.key=?"; bindings.push(graphId); }
  else {
    if (request.project_name) {
      filters += ` AND EXISTS (
        SELECT 1 FROM records project
        WHERE project.kind='project'
          AND project.key=json_extract(graph.payload,'$.project_id')
          AND project.sequence=(
            SELECT MAX(prior.sequence) FROM records prior
            WHERE prior.kind='project' AND prior.key=project.key AND prior.sequence<=?
          )
          AND json_extract(project.payload,'$.display_name')=?
      )`;
      bindings.push(sequence, request.project_name);
    }
    if (request.agent_vendor) {
      filters += " AND EXISTS (SELECT 1 FROM json_each(graph.payload,'$.vendors') vendor WHERE vendor.value=?)";
      bindings.push(request.agent_vendor);
    }
    if (request.modified_since) {
      filters += " AND julianday(json_extract(graph.payload,'$.observed_at'))>=julianday(?)";
      bindings.push(new Date(timestamp(request.modified_since)).toISOString());
    }
  }
  const rows = state.sql.exec<SelectedGraph>(
    `SELECT graph.key AS graph_id,
       json_extract(graph.payload,'$.fact_set_digest') AS fact_set_digest,
       json_extract(graph.payload,'$.fact_count') AS fact_count
     FROM records graph
     WHERE graph.kind='graph_publication'
       AND graph.sequence=(
         SELECT MAX(prior.sequence) FROM records prior
         WHERE prior.kind='graph_publication' AND prior.key=graph.key AND prior.sequence<=?
       )
       AND json_extract(graph.payload,'$.deleted')=0${filters}
     ORDER BY graph.key LIMIT ${FACT_PUBLICATION_MAX_GRAPHS + 1}`,
    ...bindings as any[]).toArray();
  for (const row of rows) {
    requireThat(UUID.test(row.graph_id) && DIGEST.test(row.fact_set_digest)
      && Number.isSafeInteger(row.fact_count) && row.fact_count > 0, "invalid_graph_publication");
  }
  return rows;
}

/** Guard: payloads never carry floats; decimal strings spell exact values. */
function requireNoFloats(value: any, field = "") {
  if (typeof value === "number") requireThat(Number.isSafeInteger(value), "fact_row_float_forbidden");
  else if (Array.isArray(value)) value.forEach(child => requireNoFloats(child));
  else if (value !== null && typeof value === "object") Object.values(value).forEach(child => requireNoFloats(child));
}

/** Recovery view: graph publication records visible to one collector. */
export function recoveredGraphs(state: State, request: Json): Json[] {
  return (request.graph_ids ?? []).flatMap((id: string) => {
    const graph = state.get("graph_publication", uuid(id));
    requireThat(!graph || graph.agent_id === request.agent_id, "graph_owner_conflict", 409);
    requireThat(!graph || graph.project_id === request.project_id, "graph_project_conflict", 409);
    return graph ? [{
      graph_id: id, schema_version: FACT_SET_SCHEMA, fact_set_digest: graph.fact_set_digest,
      fact_count: graph.fact_count, published_sequence: graph.published_sequence,
      observed_at: graph.observed_at,
    }] : [];
  });
}
