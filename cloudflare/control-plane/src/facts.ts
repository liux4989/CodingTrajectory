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
const MAX_FACT_ROWS_PER_GRAPH = 131_072;
const MAX_FACT_SET_BYTES = 16 * 1024 * 1024;
const MAX_FACT_DIGEST_BASIS_BYTES = 16 * 1024 * 1024;
const MAX_FACT_ROW_BYTES = 512 * 1024;
const FACT_READ_PAGE_MAX_BYTES = 1024 * 1024;
const FACT_PUBLICATION_MAX_BYTES = 96 * 1024 * 1024;
const MAX_STAGED_BATCH_BYTES = 2 * 1024 * 1024;
const FACT_SET_SCHEMA = "ct.published_facts.v1";
const PUBLISHED_COMMANDS = new Set([
  "bash", "bun", "cargo", "cat", "cmake", "command", "cp", "curl", "deno", "docker", "find",
  "gh", "git", "go", "grep", "kubectl", "ls", "make", "mkdir", "mv", "node", "npm", "npx",
  "pnpm", "python", "python3", "rg", "rm", "ruff", "sed", "sh", "terraform", "uv", "wget",
  "wrangler", "yarn", "zsh",
]);
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
      ON fact_rows(fact_id) WHERE kind = 'session';
    CREATE TABLE IF NOT EXISTS fact_schema (
      id INTEGER PRIMARY KEY CHECK (id=1), version INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS staged_fact_items (
      agent_id TEXT NOT NULL, graph_id TEXT NOT NULL, fact_set_digest TEXT NOT NULL,
      batch_index INTEGER NOT NULL, kind TEXT NOT NULL, fact_id TEXT NOT NULL,
      parent_id TEXT, order_index INTEGER, row_hash TEXT NOT NULL, payload TEXT NOT NULL,
      PRIMARY KEY (agent_id, graph_id, fact_set_digest, batch_index, kind, fact_id));
    CREATE INDEX IF NOT EXISTS staged_fact_items_graph
      ON staged_fact_items(agent_id, graph_id, fact_set_digest, kind, fact_id);
    -- Event ordering compares sequences; avoid scanning every event for each row.
    CREATE INDEX IF NOT EXISTS staged_fact_items_event_sequence
      ON staged_fact_items(agent_id, graph_id, fact_set_digest,
        json_extract(payload,'$.payload.sequence')) WHERE kind='event';
    CREATE TABLE IF NOT EXISTS staged_fact_generations (
      agent_id TEXT NOT NULL, graph_id TEXT NOT NULL, generation INTEGER NOT NULL,
      PRIMARY KEY (agent_id, graph_id));
    CREATE TABLE IF NOT EXISTS validated_fact_graphs (
      agent_id TEXT NOT NULL, graph_id TEXT NOT NULL, fact_set_digest TEXT NOT NULL,
      generation INTEGER NOT NULL, fact_count INTEGER NOT NULL, batch_count INTEGER NOT NULL,
      encoded_bytes INTEGER NOT NULL, manifest TEXT NOT NULL,
      PRIMARY KEY (agent_id, graph_id, fact_set_digest));`);
  const version = state.sql.exec<{ version: number }>(
    "SELECT version FROM fact_schema WHERE id=1").toArray()[0]?.version ?? 0;
  if (version < 1) {
    // Staging is retryable and invisible. Discard pre-upgrade batches so their
    // normalized rows are restaged. The enclosing storage transaction makes
    // table creation, cleanup, and the version marker one crash-safe migration.
    state.sql.exec(`DELETE FROM staged_fact_rows;
      DELETE FROM staged_fact_items;
      DELETE FROM staged_fact_generations;
      DELETE FROM validated_fact_graphs;
      INSERT INTO fact_schema VALUES(1,1)
        ON CONFLICT(id) DO UPDATE SET version=excluded.version;`);
  }
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
  const keys = new Set<string>();
  for (const row of request.rows) {
    requireThat(row.graph_id === graphId, "fact_row_graph_mismatch");
    const key = `${row.kind}:${row.fact_id}`;
    requireThat(!keys.has(key), "duplicate_fact_row");
    keys.add(key);
    requireThat(new TextEncoder().encode(stable(row)).length <= MAX_FACT_ROW_BYTES, "fact_row_too_large", 413);
    requireThat(await rowHash(row) === row.row_hash, "fact_row_hash_mismatch");
    requireNoFloats(row.payload);
    safeChronicle(row.payload);
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
    state.sql.exec("DELETE FROM staged_fact_items WHERE agent_id=? AND graph_id=?", request.agent_id, graphId);
  }
  const sameDigest = existing.filter(row => row.fact_set_digest === request.fact_set_digest);
  requireThat(!sameDigest.length || sameDigest.every(row => row.batch_count === request.batch_count), "fact_batch_count_conflict", 409);
  state.sql.exec(
    "DELETE FROM staged_fact_items WHERE agent_id=? AND graph_id=? AND fact_set_digest=? AND batch_index=?",
    request.agent_id, graphId, request.fact_set_digest, request.batch_index);
  for (const row of request.rows) {
    state.sql.exec(
      "INSERT INTO staged_fact_items VALUES(?,?,?,?,?,?,?,?,?,?)",
      request.agent_id, graphId, request.fact_set_digest, request.batch_index,
      row.kind, row.fact_id, row.parent_id ?? null, row.order_index ?? null,
      row.row_hash, stable(row));
  }
  state.sql.exec(
    "INSERT OR REPLACE INTO staged_fact_rows VALUES(?,?,?,?,?,?,?)",
    request.agent_id, graphId, request.fact_set_digest, request.batch_index,
    request.batch_count, request.rows.length, encoded);
  state.sql.exec(
    `INSERT INTO staged_fact_generations VALUES(?,?,1)
     ON CONFLICT(agent_id,graph_id) DO UPDATE SET generation=generation+1`,
    request.agent_id, graphId);
  state.sql.exec("DELETE FROM validated_fact_graphs WHERE agent_id=? AND graph_id=?", request.agent_id, graphId);
  return missingFactRows(state, {
    workspace_id: request.workspace_id, agent_id: request.agent_id, graph_id: graphId,
    fact_set_digest: request.fact_set_digest, batch_count: request.batch_count,
  });
}

export function missingFactRows(state: State, request: Json): Json {
  validate("ct_collector_missing_fact_rows", request);
  const graphId = uuid(request.graph_id);
  const staged = new Set(state.sql.exec<{ batch_index: number }>(
    `SELECT batch.batch_index FROM staged_fact_rows batch
     WHERE batch.agent_id=? AND batch.graph_id=? AND batch.fact_set_digest=?
       AND batch.row_count=(SELECT count(*) FROM staged_fact_items item
         WHERE item.agent_id=batch.agent_id AND item.graph_id=batch.graph_id
         AND item.fact_set_digest=batch.fact_set_digest AND item.batch_index=batch.batch_index)`,
    request.agent_id, graphId, request.fact_set_digest).toArray().map(row => row.batch_index));
  const missing: number[] = [];
  for (let index = 0; index < request.batch_count; index++) if (!staged.has(index)) missing.push(index);
  return { graph_id: graphId, fact_set_digest: request.fact_set_digest,
    staged_batches: request.batch_count - missing.length, missing_batches: missing };
}

function factSetView(publication: Json, rows: Json[]): Json {
  return deepStrip({
    schema_version: FACT_SET_SCHEMA,
    graph_id: publication.graph_id,
    fact_set_digest: publication.fact_set_digest,
    kind_counts: publication.kind_counts,
    rows,
  });
}

function stagedScope(agentId: string, publication: Json): [string, string, string] {
  return [agentId, publication.graph_id, publication.fact_set_digest];
}

function validationManifest(publication: Json): string {
  return stable({ fact_count: publication.fact_count, kind_counts: publication.kind_counts });
}

function hasRows(state: State, sql: string, bindings: unknown[]): boolean {
  return state.sql.exec(sql, ...bindings as any[]).toArray().length > 0;
}

function requireNoRows(state: State, sql: string, bindings: unknown[], code: string) {
  requireThat(!hasRows(state, sql, bindings), code);
}

/** Validate one graph using normalized SQL rows; only its compact digest manifest enters JS. */
async function validateStagedGraph(state: State, agentId: string, publication: Json): Promise<void> {
  const graphId = publication.graph_id;
  const scope = stagedScope(agentId, publication);
  const initialGeneration = state.sql.exec<{ generation: number }>(
    "SELECT generation FROM staged_fact_generations WHERE agent_id=? AND graph_id=?", agentId, graphId).one().generation;
  const batches = state.sql.exec<{ batch_index: number; batch_count: number; row_count: number; encoded_bytes: number }>(
    `SELECT batch_index, batch_count, row_count, length(CAST(rows_json AS BLOB)) AS encoded_bytes
     FROM staged_fact_rows WHERE agent_id=? AND graph_id=? AND fact_set_digest=? ORDER BY batch_index`,
    ...scope).toArray();
  requireThat(batches.length > 0, "fact_rows_must_be_staged");
  const batchCount = batches[0].batch_count;
  requireThat(batches.every(batch => batch.batch_count === batchCount), "fact_batch_count_conflict", 409);
  requireThat(batches.length === batchCount && batches.every((batch, index) => batch.batch_index === index),
    "fact_rows_incomplete", 409);
  const factCount = batches.reduce((total, batch) => total + batch.row_count, 0);
  requireThat(factCount === publication.fact_count, "fact_count_mismatch");
  requireThat(factCount <= MAX_FACT_ROWS_PER_GRAPH, "fact_row_cardinality_exceeded", 413);
  const normalizedCount = state.sql.exec<{ count: number }>(
    "SELECT count(*) AS count FROM staged_fact_items WHERE agent_id=? AND graph_id=? AND fact_set_digest=?",
    ...scope).one().count;
  requireThat(normalizedCount === factCount, "fact_rows_incomplete", 409);
  const combinedRowsBytes = batches.reduce((total, batch) => total + batch.encoded_bytes, 0)
    - batches.filter(batch => batch.row_count > 0).length + 1;
  const encodedBytes = new TextEncoder().encode(stable(factSetView(publication, []))).length - 2 + combinedRowsBytes;
  requireThat(encodedBytes <= MAX_FACT_SET_BYTES, "fact_set_too_large", 413);

  requireNoRows(state,
    `SELECT 1 FROM staged_fact_items WHERE agent_id=? AND graph_id=? AND fact_set_digest=?
     GROUP BY kind,fact_id HAVING count(*)>1 LIMIT 1`, scope, "duplicate_fact_row");
  const counts = Object.fromEntries(state.sql.exec<{ kind: string; count: number }>(
    `SELECT kind,count(*) AS count FROM staged_fact_items
     WHERE agent_id=? AND graph_id=? AND fact_set_digest=? GROUP BY kind`, ...scope)
    .toArray().map(row => [row.kind, row.count]));
  for (const [kind, count] of Object.entries(counts)) {
    requireThat(count <= FACT_KIND_LIMITS[kind], "fact_kind_cardinality_exceeded", 413);
    requireThat(counts[kind] === publication.kind_counts[kind], "fact_kind_counts_mismatch");
  }
  requireThat(Object.keys(counts).length === Object.keys(publication.kind_counts).length, "fact_kind_counts_mismatch");
  const where = "agent_id=? AND graph_id=? AND fact_set_digest=?";
  requireNoRows(state,
    `SELECT 1 FROM staged_fact_items child WHERE ${where} AND
       ((child.kind='graph' AND child.parent_id IS NOT NULL) OR
        (child.kind<>'graph' AND (child.parent_id IS NULL OR NOT EXISTS (
          SELECT 1 FROM staged_fact_items parent WHERE parent.agent_id=child.agent_id
            AND parent.graph_id=child.graph_id AND parent.fact_set_digest=child.fact_set_digest
            AND parent.fact_id=child.parent_id AND parent.kind IN
              (CASE child.kind WHEN 'session' THEN 'graph' WHEN 'turn' THEN 'session'
               WHEN 'item' THEN 'turn' WHEN 'event' THEN 'turn' WHEN 'output_evidence' THEN 'item'
               WHEN 'request' THEN 'turn' WHEN 'runtime' THEN 'session' WHEN 'measurement' THEN 'session'
               WHEN 'edge' THEN 'graph' WHEN 'model' THEN 'graph' END,
               CASE child.kind WHEN 'event' THEN 'session' ELSE '' END)
        )))) LIMIT 1`, scope, "fact_row_parent_missing");

  const invalid = (predicate: string, code: string, kind?: string) => requireNoRows(state,
    `SELECT 1 FROM staged_fact_items row WHERE ${where}${kind ? " AND row.kind=?" : ""} AND (${predicate}) LIMIT 1`,
    kind ? [...scope, kind] : scope, code);
  requireNoRows(state,
    `SELECT 1 FROM staged_fact_items row WHERE ${where} AND row.kind='graph'
     AND (row.fact_id<>? OR json_extract(row.payload,'$.payload.summary.root_session_id')<>?) LIMIT 1`,
    [...scope, graphId, graphId], "graph_fact_identity_mismatch");
  requireThat(counts.graph === 1, "graph_fact_count_mismatch");
  requireThat(hasRows(state,
    `SELECT 1 FROM staged_fact_items WHERE ${where} AND kind='session' AND fact_id=? LIMIT 1`,
    [...scope, graphId]), "graph_root_session_missing");
  requireNoRows(state,
    `SELECT 1 FROM staged_fact_items row WHERE ${where} AND row.kind='session'
     AND (row.fact_id<>json_extract(row.payload,'$.payload.session_id') OR row.parent_id<>?) LIMIT 1`,
    [...scope, graphId], "session_fact_identity_mismatch");
  requireNoRows(state,
    `SELECT 1 FROM staged_fact_items row WHERE ${where} AND row.kind='session'
     AND json_extract(row.payload,'$.payload.parent_session_id') IS NOT NULL AND NOT EXISTS (
       SELECT 1 FROM staged_fact_items parent WHERE parent.agent_id=row.agent_id AND parent.graph_id=row.graph_id
       AND parent.fact_set_digest=row.fact_set_digest AND parent.kind='session'
       AND parent.fact_id=json_extract(row.payload,'$.payload.parent_session_id')) LIMIT 1`,
    scope, "session_parent_missing");
  requireNoRows(state,
    `SELECT 1 FROM staged_fact_items row, json_each(row.payload,'$.payload.topology.spawn_origins') origin
     WHERE ${where} AND row.kind='session' AND
       ((json_extract(origin.value,'$.turn_id') IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM staged_fact_items turn_row WHERE turn_row.agent_id=row.agent_id AND turn_row.graph_id=row.graph_id
          AND turn_row.fact_set_digest=row.fact_set_digest AND turn_row.kind='turn'
          AND turn_row.fact_id=json_extract(origin.value,'$.turn_id') AND turn_row.parent_id=row.fact_id)) OR
        (json_extract(origin.value,'$.item_id') IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM staged_fact_items item_row WHERE item_row.agent_id=row.agent_id AND item_row.graph_id=row.graph_id
          AND item_row.fact_set_digest=row.fact_set_digest AND item_row.kind='item'
          AND item_row.fact_id=json_extract(origin.value,'$.item_id')
          AND item_row.parent_id=json_extract(origin.value,'$.turn_id')))) LIMIT 1`,
    scope, "session_spawn_reference_mismatch");

  for (const [kind, identityPath, identityCode] of [
    ["turn", "turn_id", "turn_fact_identity_mismatch"],
    ["item", "item_id", "item_fact_identity_mismatch"],
    ["event", "event_id", "event_fact_identity_mismatch"],
    ["request", "request_id", "request_fact_identity_mismatch"],
  ]) invalid(`row.fact_id<>json_extract(row.payload,'$.payload.${identityPath}')`, identityCode, kind);
  for (const kind of ["turn", "item"]) requireNoRows(state,
    `SELECT 1 FROM staged_fact_items row WHERE ${where} AND row.kind=? AND
       (row.order_index<>json_extract(row.payload,'$.payload.sequence') OR EXISTS (
         SELECT 1 FROM staged_fact_items other WHERE other.agent_id=row.agent_id AND other.graph_id=row.graph_id
         AND other.fact_set_digest=row.fact_set_digest AND other.kind=row.kind AND other.parent_id=row.parent_id
         AND other.fact_id<>row.fact_id AND json_extract(other.payload,'$.payload.sequence')=json_extract(row.payload,'$.payload.sequence')))
     LIMIT 1`, [...scope, kind], `${kind}_ordering_invalid`);
  requireNoRows(state,
    `SELECT 1 FROM staged_fact_items row WHERE ${where} AND row.kind='item' AND
      ((json_extract(row.payload,'$.payload.projection_parent_item_id') IS NULL
        AND json_extract(row.payload,'$.payload.nested_index') IS NOT NULL) OR
       (json_extract(row.payload,'$.payload.projection_parent_item_id') IS NOT NULL AND
        (coalesce(json_extract(row.payload,'$.payload.measurements.projection_only'),0)<>1 OR NOT EXISTS (
          SELECT 1 FROM staged_fact_items parent WHERE parent.agent_id=row.agent_id AND parent.graph_id=row.graph_id
          AND parent.fact_set_digest=row.fact_set_digest AND parent.kind='item' AND parent.parent_id=row.parent_id
          AND parent.fact_id=json_extract(row.payload,'$.payload.projection_parent_item_id') AND parent.fact_id<>row.fact_id
          AND coalesce(json_extract(parent.payload,'$.payload.measurements.projection_only'),0)<>1)))) LIMIT 1`,
    scope, "item_projection_parent_mismatch");
  requireNoRows(state,
    `SELECT 1 FROM staged_fact_items row, json_each(row.payload,'$.payload.event_ids') event_id
     WHERE ${where} AND row.kind='item' AND NOT EXISTS (
       SELECT 1 FROM staged_fact_items event_row WHERE event_row.agent_id=row.agent_id AND event_row.graph_id=row.graph_id
       AND event_row.fact_set_digest=row.fact_set_digest AND event_row.kind='event' AND event_row.fact_id=event_id.value
       AND json_extract(event_row.payload,'$.payload.item_id')=row.fact_id) LIMIT 1`, scope, "item_event_reference_mismatch");
  const commandTargets = state.sql.exec<{ target: string }>(
    `SELECT DISTINCT json_extract(payload,'$.payload.measurements.tool_summary.detail.target') AS target
     FROM staged_fact_items WHERE ${where} AND kind='item'
     AND json_extract(payload,'$.payload.measurements.tool_summary.detail.kind')='command'`, ...scope).toArray();
  requireThat(commandTargets.every(row => PUBLISHED_COMMANDS.has(row.target)), "unsafe_command_detail");

  requireNoRows(state,
    `SELECT 1 FROM staged_fact_items row WHERE ${where} AND row.kind='event' AND
      ((json_extract(row.payload,'$.payload.turn_id') IS NOT NULL AND
        (row.parent_id<>json_extract(row.payload,'$.payload.turn_id') OR NOT EXISTS (
          SELECT 1 FROM staged_fact_items turn_row WHERE turn_row.agent_id=row.agent_id AND turn_row.graph_id=row.graph_id
          AND turn_row.fact_set_digest=row.fact_set_digest AND turn_row.kind='turn'
          AND turn_row.fact_id=json_extract(row.payload,'$.payload.turn_id')))) OR
       (json_extract(row.payload,'$.payload.turn_id') IS NULL AND NOT EXISTS (
          SELECT 1 FROM staged_fact_items session_row WHERE session_row.agent_id=row.agent_id AND session_row.graph_id=row.graph_id
          AND session_row.fact_set_digest=row.fact_set_digest AND session_row.kind='session' AND session_row.fact_id=row.parent_id)) OR
       (json_extract(row.payload,'$.payload.item_id') IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM staged_fact_items item_row, json_each(item_row.payload,'$.payload.event_ids') event_id
          WHERE item_row.agent_id=row.agent_id AND item_row.graph_id=row.graph_id
          AND item_row.fact_set_digest=row.fact_set_digest AND item_row.kind='item'
          AND item_row.fact_id=json_extract(row.payload,'$.payload.item_id')
          AND item_row.parent_id=json_extract(row.payload,'$.payload.turn_id') AND event_id.value=row.fact_id))) LIMIT 1`,
    scope, "event_ownership_mismatch");
  requireNoRows(state,
    `SELECT 1 FROM staged_fact_items row WHERE ${where} AND row.kind='event' AND EXISTS (
       SELECT 1 FROM staged_fact_items other WHERE other.agent_id=row.agent_id AND other.graph_id=row.graph_id
       AND other.fact_set_digest=row.fact_set_digest AND other.kind='event' AND other.fact_id<>row.fact_id
       AND json_extract(other.payload,'$.payload.sequence')=json_extract(row.payload,'$.payload.sequence')
       AND coalesce((SELECT parent_id FROM staged_fact_items WHERE agent_id=row.agent_id AND graph_id=row.graph_id
         AND fact_set_digest=row.fact_set_digest AND kind='turn' AND fact_id=json_extract(other.payload,'$.payload.turn_id')),
         other.parent_id)=coalesce((SELECT parent_id FROM staged_fact_items WHERE agent_id=row.agent_id AND graph_id=row.graph_id
         AND fact_set_digest=row.fact_set_digest AND kind='turn' AND fact_id=json_extract(row.payload,'$.payload.turn_id')),row.parent_id)) LIMIT 1`,
    scope, "event_ordering_invalid");
  requireNoRows(state,
    `SELECT 1 FROM staged_fact_items row WHERE ${where} AND row.kind='output_evidence' AND
      (row.parent_id<>row.fact_id OR NOT EXISTS (SELECT 1 FROM staged_fact_items item_row
       WHERE item_row.agent_id=row.agent_id AND item_row.graph_id=row.graph_id
       AND item_row.fact_set_digest=row.fact_set_digest AND item_row.kind='item' AND item_row.fact_id=row.fact_id)) LIMIT 1`,
    scope, "output_evidence_item_mismatch");
  requireNoRows(state,
    `SELECT 1 FROM staged_fact_items row, json_each(row.payload,'$.payload.source_event_ids') event_id
     WHERE ${where} AND row.kind='output_evidence' AND NOT EXISTS (
       SELECT 1 FROM staged_fact_items event_row WHERE event_row.agent_id=row.agent_id AND event_row.graph_id=row.graph_id
       AND event_row.fact_set_digest=row.fact_set_digest AND event_row.kind='event'
       AND event_row.fact_id=event_id.value AND json_extract(event_row.payload,'$.payload.item_id')=row.fact_id) LIMIT 1`,
    scope, "output_evidence_event_mismatch");
  requireNoRows(state,
    `SELECT 1 FROM staged_fact_items row WHERE ${where} AND row.kind='edge' AND
      (row.parent_id<>? OR json_extract(row.payload,'$.payload.origin.session_id')<>json_extract(row.payload,'$.payload.source_session_id')
       OR NOT EXISTS (SELECT 1 FROM staged_fact_items source WHERE source.agent_id=row.agent_id AND source.graph_id=row.graph_id
         AND source.fact_set_digest=row.fact_set_digest AND source.kind='session' AND source.fact_id=json_extract(row.payload,'$.payload.source_session_id'))
       OR NOT EXISTS (SELECT 1 FROM staged_fact_items target WHERE target.agent_id=row.agent_id AND target.graph_id=row.graph_id
         AND target.fact_set_digest=row.fact_set_digest AND target.kind='session' AND target.fact_id=json_extract(row.payload,'$.payload.target_session_id')))
     LIMIT 1`, [...scope, graphId], "edge_session_ownership_mismatch");
  requireNoRows(state,
    `SELECT 1 FROM staged_fact_items row WHERE ${where} AND row.kind='edge' AND EXISTS (
       SELECT 1 FROM staged_fact_items other WHERE other.agent_id=row.agent_id AND other.graph_id=row.graph_id
       AND other.fact_set_digest=row.fact_set_digest AND other.kind='edge' AND other.fact_id<>row.fact_id
       AND json_array(json_extract(other.payload,'$.payload.kind'),json_extract(other.payload,'$.payload.source_session_id'),
         json_extract(other.payload,'$.payload.target_session_id'),json_extract(other.payload,'$.payload.origin.turn_id'),
         json_extract(other.payload,'$.payload.origin.item_id'))
        =json_array(json_extract(row.payload,'$.payload.kind'),json_extract(row.payload,'$.payload.source_session_id'),
         json_extract(row.payload,'$.payload.target_session_id'),json_extract(row.payload,'$.payload.origin.turn_id'),
         json_extract(row.payload,'$.payload.origin.item_id'))) LIMIT 1`, scope, "duplicate_edge_identity");
  requireNoRows(state,
    `SELECT 1 FROM staged_fact_items row WHERE ${where} AND row.kind='edge' AND
      ((json_extract(row.payload,'$.payload.origin.turn_id') IS NOT NULL AND NOT EXISTS (
         SELECT 1 FROM staged_fact_items turn_row WHERE turn_row.agent_id=row.agent_id AND turn_row.graph_id=row.graph_id
         AND turn_row.fact_set_digest=row.fact_set_digest AND turn_row.kind='turn'
         AND turn_row.fact_id=json_extract(row.payload,'$.payload.origin.turn_id')
         AND turn_row.parent_id=json_extract(row.payload,'$.payload.source_session_id')))
       OR (json_extract(row.payload,'$.payload.origin.item_id') IS NOT NULL AND NOT EXISTS (
         SELECT 1 FROM staged_fact_items item_row WHERE item_row.agent_id=row.agent_id AND item_row.graph_id=row.graph_id
         AND item_row.fact_set_digest=row.fact_set_digest AND item_row.kind='item'
         AND item_row.fact_id=json_extract(row.payload,'$.payload.origin.item_id')
         AND item_row.parent_id=json_extract(row.payload,'$.payload.origin.turn_id')))) LIMIT 1`,
    scope, "edge_origin_ownership_mismatch");
  requireNoRows(state,
    `SELECT 1 FROM staged_fact_items row,
       json_each(json_insert(coalesce(json_extract(row.payload,'$.payload.evidence_event_ids'),json('[]')),'$[#]',json_extract(row.payload,'$.payload.origin.event_id'))) event_id
     WHERE ${where} AND row.kind='edge' AND event_id.value IS NOT NULL AND NOT EXISTS (
       SELECT 1 FROM staged_fact_items event_row WHERE event_row.agent_id=row.agent_id AND event_row.graph_id=row.graph_id
       AND event_row.fact_set_digest=row.fact_set_digest AND event_row.kind='event' AND event_row.fact_id=event_id.value
       AND event_row.parent_id=coalesce(json_extract(row.payload,'$.payload.origin.turn_id'),json_extract(row.payload,'$.payload.source_session_id'))
       AND json_extract(event_row.payload,'$.payload.item_id') IS json_extract(row.payload,'$.payload.origin.item_id')) LIMIT 1`,
    scope, "edge_event_ownership_mismatch");
  const summary = state.sql.exec<{ sessions: number; turns: number; items: number }>(
    `SELECT json_extract(payload,'$.payload.summary.session_count') AS sessions,
       json_extract(payload,'$.payload.summary.turn_count') AS turns,
       json_extract(payload,'$.payload.summary.item_count') AS items
     FROM staged_fact_items WHERE ${where} AND kind='graph'`, ...scope).one();
  requireThat(summary.sessions === (counts.session ?? 0) && summary.turns === (counts.turn ?? 0)
    && summary.items === (counts.item ?? 0), "graph_summary_count_mismatch");

  const digestBasis = state.sql.exec<{ value: string; bytes: number }>(
    `SELECT value,length(CAST(value AS BLOB)) AS bytes FROM (
       SELECT '{"graph_id":'||json_quote(?)||',"rows":['||coalesce(group_concat(entry,','),'')||
         '],"schema_version":'||json_quote(?)||'}' AS value FROM (
         SELECT json_array(kind,fact_id,row_hash) AS entry FROM staged_fact_items
         WHERE ${where} ORDER BY kind,fact_id))`, graphId, FACT_SET_SCHEMA, ...scope).one();
  requireThat(digestBasis.bytes <= MAX_FACT_DIGEST_BASIS_BYTES,
    "fact_digest_basis_too_large", 413);
  requireThat(await digest(digestBasis.value) === publication.fact_set_digest, "fact_set_digest_mismatch");
  const generation = state.sql.exec<{ generation: number }>(
    "SELECT generation FROM staged_fact_generations WHERE agent_id=? AND graph_id=?", agentId, graphId).one().generation;
  requireThat(generation === initialGeneration, "fact_staging_changed", 409);
  state.sql.exec(
    `INSERT OR REPLACE INTO validated_fact_graphs VALUES(?,?,?,?,?,?,?,?)`,
    agentId, graphId, publication.fact_set_digest, generation, factCount, batchCount,
    encodedBytes, validationManifest(publication));
}

export interface PublicationPlan { validated: true }

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
  const graphIds = new Set<string>();
  for (const publication of request.graphs) {
    requireThat(!graphIds.has(publication.graph_id), "duplicate_graph_publication");
    graphIds.add(publication.graph_id);
    requireThat(new Set(publication.source_ids).size === publication.source_ids.length
      && publication.source_ids.every((id: string) => vector.has(id)), "invalid_graph_sources");
    const validated = state.sql.exec<{ present: number }>(
      `SELECT 1 AS present FROM validated_fact_graphs validated JOIN staged_fact_generations generation
       ON generation.agent_id=validated.agent_id AND generation.graph_id=validated.graph_id
       AND generation.generation=validated.generation
       WHERE validated.agent_id=? AND validated.graph_id=? AND validated.fact_set_digest=?
       AND validated.fact_count=? AND validated.manifest=? LIMIT 1`, request.agent_id,
      publication.graph_id, publication.fact_set_digest, publication.fact_count,
      validationManifest(publication)).toArray().length > 0;
    if (!validated) await validateStagedGraph(state, request.agent_id, publication);
  }
  const manifests = JSON.stringify(request.graphs.map((graph: Json) => ({
    graph_id: graph.graph_id, fact_set_digest: graph.fact_set_digest,
  })));
  requireNoRows(state,
    `WITH incoming AS (
       SELECT json_extract(value,'$.graph_id') AS graph_id,
         json_extract(value,'$.fact_set_digest') AS fact_set_digest FROM json_each(?)
     )
     SELECT 1 FROM staged_fact_items row JOIN incoming
       ON incoming.graph_id=row.graph_id AND incoming.fact_set_digest=row.fact_set_digest
     WHERE row.agent_id=? AND row.kind='session'
     GROUP BY row.fact_id HAVING count(*)>1 LIMIT 1`,
    [manifests, request.agent_id], "overlapping_incoming_graphs");
  const represented = new Set<string>();
  for (const publication of request.graphs) for (const id of publication.source_ids) represented.add(id);
  requireThat(represented.size === vector.size, "unrepresented_source");
  return { validated: true };
}

/** Synchronous commit phase: fencing, scope checks, atomic row application. */
export function commitPublication(state: State, request: Json, plan: PublicationPlan): Json {
  requireThat(plan.validated, "fact_rows_must_be_validated");
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
  let publicationBytes = 0;
  for (const publication of request.graphs) {
    const graphId = publication.graph_id;
    const validated = state.sql.exec<{ encoded_bytes: number; fact_count: number; generation: number; manifest: string }>(
      `SELECT validated.encoded_bytes,validated.fact_count,validated.generation,validated.manifest
       FROM validated_fact_graphs validated JOIN staged_fact_generations generation
         ON generation.agent_id=validated.agent_id AND generation.graph_id=validated.graph_id
         AND generation.generation=validated.generation
       WHERE validated.agent_id=? AND validated.graph_id=? AND validated.fact_set_digest=?`,
      request.agent_id, graphId, publication.fact_set_digest).toArray();
    requireThat(validated.length === 1 && validated[0].fact_count === publication.fact_count
      && validated[0].manifest === validationManifest(publication),
      "fact_rows_must_be_validated", 409);
    publicationBytes += validated[0].encoded_bytes;
    requireThat(publicationBytes <= FACT_PUBLICATION_MAX_BYTES, "publication_byte_budget", 413);
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

  // Project only affected graph IDs; each metadata record is loaded separately.
  const manifests = JSON.stringify(request.graphs.map((graph: Json) => ({
    graph_id: graph.graph_id, fact_set_digest: graph.fact_set_digest,
  })));
  const sourceIds = JSON.stringify([...vector.keys()]);
  const affectedIds = state.sql.exec<{ graph_id: string }>(
    `WITH latest AS (
       SELECT key,max(sequence) AS sequence FROM records
       WHERE kind='graph_publication' GROUP BY key
     ), incoming AS (
       SELECT json_extract(value,'$.graph_id') AS graph_id,
         json_extract(value,'$.fact_set_digest') AS fact_set_digest FROM json_each(?)
     )
     SELECT graph.key AS graph_id FROM records graph JOIN latest
       ON latest.key=graph.key AND latest.sequence=graph.sequence
     WHERE graph.kind='graph_publication' AND json_extract(graph.payload,'$.deleted')=0
       AND json_extract(graph.payload,'$.project_id')=? AND (
         EXISTS (SELECT 1 FROM json_each(graph.payload,'$.session_ids') prior_session
           WHERE EXISTS (SELECT 1 FROM staged_fact_items incoming_session JOIN incoming
             ON incoming.graph_id=incoming_session.graph_id
             AND incoming.fact_set_digest=incoming_session.fact_set_digest
             WHERE incoming_session.agent_id=? AND incoming_session.kind='session'
             AND incoming_session.fact_id=prior_session.value))
         OR (json_extract(graph.payload,'$.agent_id')=? AND NOT EXISTS (
           SELECT 1 FROM json_each(graph.payload,'$.source_ids') prior_source
           WHERE prior_source.value NOT IN (SELECT value FROM json_each(?)))))
     ORDER BY graph.key LIMIT 1001`,
    manifests, request.project_id, request.agent_id, request.agent_id, sourceIds).toArray().map(row => row.graph_id);
  requireThat(affectedIds.length <= 1000, "publication_scope_budget", 413);
  let incomplete = false;
  for (const graphId of affectedIds) {
    const row = state.get("graph_publication", graphId)!;
    const overlaps = hasRows(state,
      `WITH incoming AS (
         SELECT json_extract(value,'$.graph_id') AS graph_id,
           json_extract(value,'$.fact_set_digest') AS fact_set_digest FROM json_each(?)
       ) SELECT 1 FROM staged_fact_items item JOIN incoming
         ON incoming.graph_id=item.graph_id AND incoming.fact_set_digest=item.fact_set_digest
       WHERE item.agent_id=? AND item.kind='session' AND item.fact_id IN (SELECT value FROM json_each(?)) LIMIT 1`,
      [manifests, request.agent_id, JSON.stringify(row.session_ids)]);
    requireThat(!overlaps || row.agent_id === request.agent_id, "session_owner_conflict", 409);
    if (overlaps && row.source_ids.some((id: string) => !vector.has(id))) incomplete = true;
  }

  const sequence = state.next();
  state.put("publisher", publisherKey, { publication_sequence: request.publication_sequence, committed_sequence: sequence }, sequence);
  if (stale || incomplete) {
    return receipt("accepted", sequence, incomplete
      ? { reason: "incomplete_graph_scope", publication_outcome: "rejected", remedy: "include all sources of overlapping published graphs" }
      : { publication_outcome: "superseded" });
  }

  const publishedAt = new Date().toISOString();
  let inserted = 0, reused = 0, closed = 0, omitted = 0, superseded = 0;
  const incomingGraphIds = new Set<string>(request.graphs.map((graph: Json) => graph.graph_id));
  for (const publication of request.graphs) {
    const graphId = publication.graph_id;
    const counts = applyFactRows(state, request.agent_id, publication, sequence);
    inserted += counts.inserted; reused += counts.reused; closed += counts.closed;
    const prior = state.get("graph_publication", graphId);
    if (prior && !prior.deleted) superseded++;
    const sessionIds = state.sql.exec<{ fact_id: string }>(
      `SELECT fact_id FROM staged_fact_items WHERE agent_id=? AND graph_id=? AND fact_set_digest=?
       AND kind='session' ORDER BY fact_id`, request.agent_id, graphId, publication.fact_set_digest)
      .toArray().map(row => row.fact_id);
    const vendors = state.sql.exec<{ vendor: string }>(
      `SELECT DISTINCT json_extract(payload,'$.payload.vendor') AS vendor FROM staged_fact_items
       WHERE agent_id=? AND graph_id=? AND fact_set_digest=? AND kind='session' ORDER BY vendor`,
      request.agent_id, graphId, publication.fact_set_digest).toArray().map(row => row.vendor);
    state.put("graph_publication", graphId, {
      graph_id: graphId, project_id: request.project_id, agent_id: request.agent_id,
      schema_version: FACT_SET_SCHEMA, fact_set_digest: publication.fact_set_digest,
      fact_count: publication.fact_count, kind_counts: publication.kind_counts,
      source_ids: publication.source_ids, session_ids: sessionIds, vendors,
      observed_at: publication.observed_at, revision: (prior?.revision ?? 0) + 1,
      published_sequence: sequence, published_at: publishedAt, deleted: false,
    }, sequence);
    state.sql.exec("DELETE FROM staged_fact_rows WHERE agent_id=? AND graph_id=?", request.agent_id, graphId);
    state.sql.exec("DELETE FROM staged_fact_items WHERE agent_id=? AND graph_id=?", request.agent_id, graphId);
    state.sql.exec("DELETE FROM validated_fact_graphs WHERE agent_id=? AND graph_id=?", request.agent_id, graphId);
  }
  if ((request.replacement_scope ?? "complete_sources") === "complete_sources") {
    for (const graphId of affectedIds) {
      const row = state.get("graph_publication", graphId)!;
      if (row.agent_id === request.agent_id && !incomingGraphIds.has(row.graph_id)
        && row.source_ids.every((id: string) => vector.has(id))) {
        closed += closeFactRows(state, row.graph_id, sequence);
        state.put("graph_publication", row.graph_id, { ...row, deleted: true, published_at: publishedAt }, sequence);
        omitted++;
      }
    }
  }
  return receipt("accepted", sequence, { publication_outcome: "published",
    graphs_published: request.graphs.length, rows_inserted: inserted, rows_reused: reused,
    rows_closed: closed, superseded_revisions: superseded, omitted_graphs: omitted });
}

function applyFactRows(state: State, agentId: string, publication: Json, sequence: number): { inserted: number; reused: number; closed: number } {
  const scope = stagedScope(agentId, publication);
  const graphId = publication.graph_id;
  const reused = state.sql.exec<{ count: number }>(
    `SELECT count(*) AS count FROM fact_rows current JOIN staged_fact_items staged
       ON staged.kind=current.kind AND staged.fact_id=current.fact_id AND staged.row_hash=current.row_hash
     WHERE current.graph_id=? AND current.valid_to_sequence IS NULL
       AND staged.agent_id=? AND staged.graph_id=? AND staged.fact_set_digest=?`,
    graphId, ...scope).one().count;
  state.sql.exec(
    `UPDATE fact_rows SET valid_to_sequence=? WHERE graph_id=? AND valid_to_sequence IS NULL AND NOT EXISTS (
       SELECT 1 FROM staged_fact_items staged WHERE staged.agent_id=? AND staged.graph_id=?
       AND staged.fact_set_digest=? AND staged.kind=fact_rows.kind AND staged.fact_id=fact_rows.fact_id
       AND staged.row_hash=fact_rows.row_hash)`, sequence - 1, graphId, ...scope);
  const closed = state.sql.exec<{ count: number }>("SELECT changes() AS count").one().count;
  state.sql.exec(
    `INSERT INTO fact_rows(graph_id,kind,fact_id,parent_id,order_index,row_hash,payload,valid_from_sequence,valid_to_sequence)
     SELECT staged.graph_id,staged.kind,staged.fact_id,staged.parent_id,staged.order_index,staged.row_hash,staged.payload,?,NULL
     FROM staged_fact_items staged WHERE staged.agent_id=? AND staged.graph_id=? AND staged.fact_set_digest=?
       AND NOT EXISTS (SELECT 1 FROM fact_rows current WHERE current.graph_id=staged.graph_id
         AND current.kind=staged.kind AND current.fact_id=staged.fact_id AND current.valid_to_sequence IS NULL)`,
    sequence, ...scope);
  const inserted = state.sql.exec<{ count: number }>("SELECT changes() AS count").one().count;
  return { inserted, reused, closed };
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
  type PageRow = { graph_id: string; kind: string; fact_id: string; payload: string; payload_bytes: number };
  const page: PageRow[] = [];
  let pageBytes = 2; // JSON array brackets; each subsequent row also needs a comma.
  let hasMore = false;
  // Seek each primary-key prefix directly. A workspace-wide IN/OR cursor query
  // can rescan earlier facts; window functions also process the entire suffix.
  // Consume synchronously and stop after one lookahead, including at byte limits.
  readPage: for (const graphId of [...graphIds].sort()) {
    if (after && graphId < after[0]) continue;
    for (const kind of kinds ?? [...FACT_KINDS].sort()) {
      if (after && graphId === after[0] && kind < after[1]) continue;
      const afterId = after && graphId === after[0] && kind === after[1] ? after[2] : null;
      const bindings: unknown[] = [graphId, kind, sequence, sequence];
      if (afterId !== null) bindings.push(afterId);
      const candidates = state.sql.exec<PageRow>(
        `SELECT graph_id, kind, fact_id, payload, length(CAST(payload AS BLOB)) AS payload_bytes
         FROM fact_rows WHERE graph_id=? AND kind=?
         AND valid_from_sequence <= ? AND (valid_to_sequence IS NULL OR valid_to_sequence >= ?)
         ${afterId === null ? "" : "AND fact_id > ?"}
         ORDER BY fact_id LIMIT ?`, ...bindings as any[], limit - page.length + 1);
      for (const row of candidates) {
        const encodedBytes = row.payload_bytes + (page.length ? 1 : 0);
        if (page.length === limit || pageBytes + encodedBytes > FACT_READ_PAGE_MAX_BYTES) {
          hasMore = true;
          break readPage;
        }
        page.push(row);
        pageBytes += encodedBytes;
      }
    }
  }
  if (!page.length) {
    return { workspace_id: request.workspace_id, snapshot_sequence: sequence,
      rows: [], graph_digests: digests, graph_fact_counts: counts, next_cursor: null };
  }
  const rows = page.map(row => JSON.parse(row.payload));
  const last = page[page.length - 1];
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
