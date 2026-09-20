import { digest, Fault, Json, receipt, requireThat, stable, State, validate } from "./shared";
import { apiView, commitApiView, prepareInventory, pruneApi } from "./prepared-api";
import { compactGraph, expandManifest } from "./artifact-manifest";


const MANIFEST_SCHEMA = "ct.artifact-manifest.v3";
const RETAINED_MANIFESTS = 3;
const CLEANUP_PAGES_PER_PUBLICATION = 4;
const UPLOAD_CLAIM_SECONDS = 7 * 24 * 60 * 60;

export function artifactKey(workspaceId: string, kind: string, sha256: string): string {
  return `workspaces/${workspaceId}/artifacts/${kind}/${sha256}`;
}

export function initializeArtifacts(state: State) {
  state.sql.exec(`CREATE TABLE IF NOT EXISTS artifact_manifests (
    project_id TEXT NOT NULL, publication_sequence INTEGER NOT NULL,
    workspace_sequence INTEGER NOT NULL UNIQUE, agent_id TEXT NOT NULL,
    manifest TEXT NOT NULL,
    PRIMARY KEY(project_id, publication_sequence));
    CREATE INDEX IF NOT EXISTS artifact_manifests_snapshot
      ON artifact_manifests(workspace_sequence);
    CREATE TABLE IF NOT EXISTS artifact_cleanup (
      workspace_id TEXT PRIMARY KEY, cursor TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS artifact_upload_claims (
      kind TEXT NOT NULL, sha256 TEXT NOT NULL, expires_at INTEGER NOT NULL,
      PRIMARY KEY(kind, sha256));`);
  const columns = state.sql.exec<{ name: string }>("PRAGMA table_info(artifact_upload_claims)").toArray();
  if (!columns.some(column => column.name === "token")) state.sql.exec(`
    ALTER TABLE artifact_upload_claims ADD COLUMN token TEXT;
    ALTER TABLE artifact_upload_claims ADD COLUMN completion TEXT;`);
}

/** Fence cleanup before an authenticated collector reads or writes an object. */
export function claimArtifactUpload(state: State, kind: string, sha256: string) {
  const now = Math.floor(Date.now() / 1000);
  // A duplicate must not downgrade a completed receipt. Expired generations
  // cannot complete a replacement claim or resurrect a claim released at commit.
  return state.sql.exec<{ token: string }>(`INSERT INTO artifact_upload_claims(kind,sha256,expires_at,token)
    VALUES(?,?,?,?) ON CONFLICT(kind,sha256) DO UPDATE SET
    token=CASE WHEN expires_at<=? OR token IS NULL THEN excluded.token ELSE token END,
    completion=CASE WHEN expires_at<=? THEN NULL ELSE completion END,
    expires_at=excluded.expires_at RETURNING token`,
  kind, sha256, now + UPLOAD_CLAIM_SECONDS, crypto.randomUUID(), now, now).one().token;
}

export function completeArtifactUpload(state: State, request: Json) {
  const updated = state.sql.exec(`UPDATE artifact_upload_claims SET completion=?
    WHERE kind=? AND sha256=? AND token=? AND expires_at>?`,
  stable({ workspace_id: request.workspace_id, bytes: request.bytes, index: request.index }),
  request.kind, request.sha256, request.token, Math.floor(Date.now() / 1000));
  requireThat(updated.rowsWritten === 1, "artifact_claim_expired", 409);
}

/** Read-only hints: publication must revalidate after expiry/pruning/cleanup. */
export function artifactReadiness(state: State, request: Json): Json {
  const completed = new Map<string, Json>();
  for (const kind of ["facts", "summary", "api"]) {
    const hashes = [...new Set<string>(request.objects.filter((ref: Json) => ref.kind === kind).map((ref: Json) => ref.sha256))];
    for (let offset = 0; offset < hashes.length; offset += 50) {
      const batch = hashes.slice(offset, offset + 50);
      for (const row of state.sql.exec<{ sha256: string; completion: string }>(
        `SELECT sha256,completion FROM artifact_upload_claims WHERE kind=? AND sha256 IN (${batch.map(() => "?").join(",")}) AND expires_at>? AND completion IS NOT NULL`,
        kind, ...batch, Math.floor(Date.now() / 1000)).toArray()) {
        const completion = JSON.parse(row.completion);
        if (completion.workspace_id === request.workspace_id) completed.set(`${kind}:${row.sha256}`, completion);
      }
    }
  }
  const ready = request.objects.map((ref: Json) => {
    const completion = completed.get(`${ref.kind}:${ref.sha256}`);
    return Boolean(completion && completion.bytes === ref.bytes && (!ref.requires_index || completion.index));
  });
  if (ready.every(Boolean)) return { ready };
  // Parse retained manifests once per bounded batch, never once per object.
  const retained = new Map<string, number>(), indexes = new Set<string>();
  for (const row of state.sql.exec<{ manifest: string }>("SELECT manifest FROM artifact_manifests").toArray()) {
    for (const graph of expandManifest(JSON.parse(row.manifest)).graphs) {
      for (const ref of [graph.facts, graph.summary, ...(graph.api_objects ?? [])]) retained.set(`${ref.kind}:${ref.sha256}`, ref.bytes);
    }
  }
  const hashes = request.objects.filter((ref: Json, index: number) => !ready[index] && ref.requires_index
    && retained.get(`${ref.kind}:${ref.sha256}`) === ref.bytes).map((ref: Json) => ref.sha256);
  for (let offset = 0; offset < hashes.length; offset += 50) {
    const batch = hashes.slice(offset, offset + 50);
    for (const row of state.sql.exec<{ descriptor: string }>(`SELECT descriptor FROM api_methods
      WHERE json_extract(descriptor,'$.index.sha256') IN (${batch.map(() => "?").join(",")})`, ...batch).toArray()) {
      const descriptor = JSON.parse(row.descriptor);
      if (descriptor.publication_index) indexes.add(descriptor.index.sha256);
    }
  }
  return { ready: request.objects.map((ref: Json, index: number) => ready[index]
    || (retained.get(`${ref.kind}:${ref.sha256}`) === ref.bytes && (!ref.requires_index || indexes.has(ref.sha256)))) };
}

export interface ArtifactPublicationPlan {
  complete: true;
  releaseClaims: Array<{ kind: string; sha256: string }>;
  views: Json[];
  cards: Json[];
  indexes: Map<string, Json>;
  inventory: Awaited<ReturnType<typeof prepareInventory>>;
}

/** Verify all immutable objects and source fences before making a manifest visible. */
export async function prepareArtifactPublication(
  state: State,
  env: Env,
  request: Json,
): Promise<ArtifactPublicationPlan> {
  validate("ct_collector_publish_artifacts", request);
  requireThat(request.inventory_state === "complete", "artifact_inventory_incomplete");
  requireThat(state.get("project", request.project_id), "project_not_found", 404);
  const owner = state.get("artifact_project_publisher", request.project_id);
  requireThat(!owner || owner.agent_id === request.agent_id, "project_publisher_conflict", 409);
  const vector = new Map<string, Json>(request.source_vector.map((entry: Json) => [entry.source_id, entry]));
  requireThat(vector.size === request.source_vector.length, "duplicate_source_vector");
  for (const entry of vector.values()) {
    const source = state.get("source", entry.source_id);
    const checkpoint = state.get("checkpoint", `${entry.source_id}:${entry.source_epoch}:${entry.source_sequence}`);
    requireThat(source && source.agent_id === request.agent_id
      && source.project_id === request.project_id
      && source.source_epoch === entry.source_epoch
      && source.committed_source_sequence === entry.source_sequence
      && checkpoint?.content_sha256 === entry.content_sha256,
    "source_vector_requires_accepted_project_checkpoints");
  }
  const retained = new Map<string, number>();
  for (const row of state.sql.exec<{ manifest: string }>("SELECT manifest FROM artifact_manifests").toArray()) {
    const manifest = expandManifest(JSON.parse(row.manifest));
    for (const graph of manifest.graphs) {
      for (const object of [graph.facts, graph.summary, ...(graph.api_objects ?? [])]) {
        retained.set(`${object.kind}:${object.sha256}`, object.bytes);
      }
    }
  }
  const graphIds = new Set<string>();
  const completed = new Map<string, Json>(), indexes = new Map<string, Json>();
  // Keyed batches, never one table scan per reference or an unbounded claim dump.
  for (const kind of ["facts", "summary", "api"]) {
    const hashes = [...new Set<string>(request.graphs.flatMap((graph: Json) =>
      [graph.facts, graph.summary, ...graph.api_objects].filter(ref => ref.kind === kind).map(ref => ref.sha256)))];
    for (let offset = 0; offset < hashes.length; offset += 50) {
      const batch = hashes.slice(offset, offset + 50), placeholders = batch.map(() => "?").join(",");
      for (const row of state.sql.exec<{ sha256: string; completion: string }>(
        `SELECT sha256,completion FROM artifact_upload_claims WHERE kind=? AND sha256 IN (${placeholders}) AND expires_at>? AND completion IS NOT NULL`,
        kind, ...batch, Math.floor(Date.now() / 1000)).toArray()) {
        const completion = JSON.parse(row.completion);
        if (completion.workspace_id !== request.workspace_id) continue;
        completed.set(`${kind}:${row.sha256}`, completion);
        if (completion.index) indexes.set(row.sha256, completion.index);
      }
    }
  }
  const indexHashes = [...new Set<string>(request.graphs.flatMap((graph: Json) =>
    graph.api_methods.filter((method: Json) => method.index && !indexes.has(method.index.sha256)).map((method: Json) => method.index.sha256)))];
  for (let offset = 0; offset < indexHashes.length; offset += 50) {
    const batch = indexHashes.slice(offset, offset + 50);
    for (const row of state.sql.exec<{ descriptor: string }>(`SELECT descriptor FROM api_methods
      WHERE json_extract(descriptor,'$.index.sha256') IN (${batch.map(() => "?").join(",")})`, ...batch).toArray()) {
      const descriptor = JSON.parse(row.descriptor);
      if (descriptor.publication_index) indexes.set(descriptor.index.sha256, descriptor.publication_index);
    }
  }
  const releaseClaims: Array<{ kind: string; sha256: string }> = [];
  const views: Json[] = [], cards: Json[] = [];
  for (const graph of request.graphs) {
    requireThat(!graphIds.has(graph.graph_id), "duplicate_graph_publication");
    graphIds.add(graph.graph_id);
    requireThat(graph.source_ids.every((sourceId: string) => vector.has(sourceId)), "invalid_graph_sources");
    const keys = new Set<string>();
    const references = new Map<string, number>(graph.api_objects.map((ref: Json) => [ref.sha256, ref.bytes]));
    for (const method of graph.api_methods) {
      const key = stable([method.method, method.scope, method.turn_id]);
      requireThat(!keys.has(key) && Boolean(method.index) !== Boolean(method.error), "invalid_prepared_method");
      keys.add(key);
      requireThat(!method.index || references.get(method.index.sha256) === method.index.bytes, "invalid_prepared_reference");
      if (method.index) {
        requireThat(method.index.bytes <= 64 * 1024, "invalid_prepared_reference");
        const index = indexes.get(method.index.sha256);
        requireThat(index, "artifact_upload_incomplete", 409);
        requireThat(index.source_manifest_sha256 === graph.fact_set_digest && index.method === method.method && index.method_version === method.method_version && index.scope === method.scope && index.turn_id === method.turn_id, "invalid_prepared_reference");
        requireThat(index.references.every((ref: Json) => references.get(ref.sha256) === ref.bytes), "invalid_prepared_reference");
      }
    }
    for (const object of [graph.facts, graph.summary, ...graph.api_objects]) {
      const key = `${object.kind}:${object.sha256}`;
      const completion = completed.get(key);
      requireThat(retained.get(key) === object.bytes || completion?.bytes === object.bytes,
      "artifact_upload_incomplete", 409);
      if (completion) releaseClaims.push({ kind: object.kind, sha256: object.sha256 });
    }
    const summaryObject = await env.ARTIFACTS.get(artifactKey(request.workspace_id, "summary", graph.summary.sha256));
    requireThat(summaryObject && summaryObject.size === graph.summary.bytes, "artifact_upload_incomplete", 409);
    const body = new Uint8Array(await summaryObject.arrayBuffer());
    requireThat(await digest(body) === graph.summary.sha256, "artifact_object_corrupt", 503);
    const summary = JSON.parse(new TextDecoder().decode(body));
    requireThat(summary.schema_version === "ct.prepared-summary.v2" && summary.fact_set_digest === graph.fact_set_digest && summary.graph_id === graph.graph_id && Array.isArray(summary.project_sessions), "unsupported_prepared_version", 409);
    const view = await apiView(request.workspace_id, graph.fact_set_digest, graph.api_methods, request.project_id);
    cards.push(...summary.project_sessions.map((card: Json) => ({ ...card, project_id: request.project_id, view_manifest_sha256: view.view_manifest_sha256 })));
    views.push(view);
  }
  const allCards = state.sql.exec<{ cards: string }>("SELECT cards FROM api_inventory_cards WHERE project_id<>?", request.project_id)
    .toArray().flatMap(row => JSON.parse(row.cards)).concat(cards);
  const projects = state.all("project").map(project => ({ project_id: project.project_id,
    display_name: project.display_name, vendors: [...new Set(allCards.filter(card => card.project_id === project.project_id).flatMap(card => card.vendors ?? []))].sort(),
    modified: allCards.filter(card => card.project_id === project.project_id).map(card => card.modified).filter(Boolean).sort().at(-1) ?? null }));
  const inventory = await prepareInventory(env, request.workspace_id, projects, allCards);
  return { complete: true, releaseClaims, views, cards, indexes, inventory };
}

/** Commit one complete inventory and retain only a bounded rollback window. */
export function commitArtifactPublication(
  state: State,
  request: Json,
  plan: ArtifactPublicationPlan,
): Json {
  requireThat(plan.complete, "artifact_upload_incomplete", 409);
  const publisher = state.get("artifact_project_publisher", request.project_id);
  const current = publisher?.publication_sequence ?? -1;
  if (request.publication_sequence <= current) {
    return receipt("conflict", publisher?.committed_sequence ?? null,
      { reason: "stale_publication_sequence" });
  }
  requireThat(request.publication_sequence === current + 1, "publication_sequence_gap", 409);
  const sequence = state.next();
  const manifest = {
    schema_version: MANIFEST_SCHEMA,
    preparation_version: request.preparation_version,
    workspace_id: request.workspace_id,
    project_id: request.project_id,
    publisher_agent_id: request.agent_id,
    publication_sequence: request.publication_sequence,
    snapshot_sequence: sequence,
    published_at: new Date().toISOString(),
    inventory_state: "complete",
    graphs: request.graphs.map((graph: Json) => compactGraph({
      graph_id: graph.graph_id,
      fact_set_digest: graph.fact_set_digest,
      fact_count: graph.fact_count,
      observed_at: graph.observed_at,
      vendors: graph.vendors,
      facts: graph.facts,
      summary: graph.summary,
      api_methods: graph.api_methods,
      api_objects: graph.api_objects,
    })),
  };
  const stored = stable(manifest);
  // Leave headroom for the other columns and SQLite row encoding under 2 MiB.
  requireThat(new TextEncoder().encode(stored).byteLength <= 2 * 1024 * 1024 - 4096,
    "artifact_manifest_too_large", 413);
  state.sql.exec("INSERT INTO artifact_manifests VALUES(?,?,?,?,?)",
    request.project_id, request.publication_sequence, sequence, request.agent_id, stored);
  request.graphs.forEach((graph: Json, index: number) => commitApiView(state, request.project_id, sequence, plan.views[index], graph.api_methods, plan.indexes));
  state.sql.exec("INSERT OR REPLACE INTO api_inventory_cards VALUES(?,?)", request.project_id, stable(plan.cards));
  commitApiView(state, "workspace", sequence, plan.inventory.identity, plan.inventory.methods);
  for (const hash of plan.inventory.objects) state.sql.exec("INSERT OR IGNORE INTO api_inventory_objects VALUES(?,?)", plan.inventory.identity.view_manifest_sha256, hash);
  state.sql.exec(`DELETE FROM artifact_manifests WHERE project_id=? AND publication_sequence NOT IN (
    SELECT publication_sequence FROM artifact_manifests WHERE project_id=?
    ORDER BY publication_sequence DESC LIMIT ?)`, request.project_id, request.project_id, RETAINED_MANIFESTS);
  state.put("artifact_project_publisher", request.project_id, {
    project_id: request.project_id,
    agent_id: request.agent_id,
    publication_sequence: request.publication_sequence,
    committed_sequence: sequence,
    first_sequence: publisher?.first_sequence ?? sequence,
  }, sequence);
  // Preserve one migration boundary plus the retained rollback window; older
  // publisher versions would otherwise make metadata grow without bound.
  state.sql.exec(`DELETE FROM records WHERE kind='artifact_project_publisher' AND key=?
    AND sequence NOT IN (
      SELECT MIN(sequence) FROM records WHERE kind='artifact_project_publisher' AND key=?
      UNION SELECT workspace_sequence FROM artifact_manifests WHERE project_id=?)`,
  request.project_id, request.project_id, request.project_id);
  pruneApi(state);
  for (const kind of ["facts", "summary", "api"]) {
    const hashes = plan.releaseClaims.filter(claim => claim.kind === kind)
      .map(claim => claim.sha256);
    for (let offset = 0; offset < hashes.length; offset += 50) {
      const batch = hashes.slice(offset, offset + 50);
      state.sql.exec(`DELETE FROM artifact_upload_claims WHERE completion IS NOT NULL AND kind=? AND sha256 IN
        (${batch.map(() => "?").join(",")})`, kind, ...batch);
    }
  }
  return receipt("accepted", sequence, {
    publication_outcome: "published",
    graphs_published: request.graphs.length,
    retention: RETAINED_MANIFESTS,
  });
}

/** Keep retry receipts only while their artifact snapshots remain retained. */
export function pruneArtifactReceipts(state: State) {
  state.sql.exec(`DELETE FROM records WHERE kind='receipt'
    AND json_extract(payload, '$.result.details.retention')=?
    AND json_extract(payload, '$.result.committed_sequence') NOT IN (
      SELECT workspace_sequence FROM artifact_manifests)`, RETAINED_MANIFESTS);
}

export function artifactManifests(state: State, request: Json): Json {
  validate("ct_artifact_manifest", request);
  const sequence = state.pin(request.snapshot_sequence);
  const args: unknown[] = [sequence];
  let projectFilter = "";
  if (request.project_id) {
    projectFilter = " AND project_id=?";
    args.push(request.project_id);
  }
  const rows = state.sql.exec<{ manifest: string }>(`SELECT manifest FROM artifact_manifests current
    WHERE workspace_sequence=(SELECT MAX(candidate.workspace_sequence) FROM artifact_manifests candidate
      WHERE candidate.project_id=current.project_id AND candidate.workspace_sequence<=?)${projectFilter}
    ORDER BY project_id`, ...args as any[]).toArray();
  const expected = state.all("artifact_project_publisher", sequence).filter(row =>
    row.first_sequence <= sequence && (!request.project_id || row.project_id === request.project_id));
  if (rows.length !== expected.length) throw new Fault(410, "artifact_snapshot_expired");
  if (!rows.length) throw new Fault(404, "artifact_snapshot_unavailable");
  return { workspace_id: request.workspace_id, snapshot_sequence: sequence,
    manifests: rows.map(row => JSON.parse(row.manifest)) };
}

export function artifactReadLocator(state: State, request: Json): Json {
  validate("ct_artifact_read", request);
  const manifests = artifactManifests(state, {
    workspace_id: request.workspace_id,
    snapshot_sequence: request.snapshot_sequence,
  }).manifests as Json[];
  const referenced = manifests.some(manifest => manifest.graphs.some((graph: Json) => {
    const reference = request.kind === "facts" ? graph.facts : graph.summary;
    return reference.sha256 === request.sha256;
  }));
  requireThat(referenced, "artifact_not_in_snapshot", 404);
  return { __artifact_key: artifactKey(request.workspace_id, request.kind, request.sha256),
    sha256: request.sha256, kind: request.kind };
}

/** Remove only orphaned objects in this workspace after a committed manifest. */
export async function cleanupArtifactObjects(state: State, env: Env, workspaceId: string) {
  const referenced = new Set<string>();
  for (const row of state.sql.exec<{ manifest: string }>("SELECT manifest FROM artifact_manifests").toArray()) {
    const manifest = expandManifest(JSON.parse(row.manifest));
    for (const graph of manifest.graphs) {
      referenced.add(artifactKey(workspaceId, "facts", graph.facts.sha256));
      referenced.add(artifactKey(workspaceId, "summary", graph.summary.sha256));
      for (const ref of graph.api_objects ?? []) referenced.add(artifactKey(workspaceId, "api", ref.sha256));
    }
  }
  for (const row of state.sql.exec<{ hash: string }>("SELECT hash FROM api_inventory_objects").toArray()) referenced.add(artifactKey(workspaceId, "api", row.hash));
  const now = Math.floor(Date.now() / 1000);
  state.sql.exec("DELETE FROM artifact_upload_claims WHERE expires_at<=?", now);
  for (const claim of state.sql.exec<{ kind: string; sha256: string }>(
    "SELECT kind,sha256 FROM artifact_upload_claims",
  ).toArray()) {
    referenced.add(artifactKey(workspaceId, claim.kind, claim.sha256));
  }
  let cursor = state.sql.exec<{ cursor: string }>(
    "SELECT cursor FROM artifact_cleanup WHERE workspace_id=?", workspaceId,
  ).toArray()[0]?.cursor;
  for (let pageNumber = 0; pageNumber < CLEANUP_PAGES_PER_PUBLICATION; pageNumber++) {
    const page = await env.ARTIFACTS.list({
      prefix: `workspaces/${workspaceId}/artifacts/`, cursor, limit: 1000,
    });
    const stale = page.objects.map(object => object.key).filter(key => !referenced.has(key));
    if (stale.length) await env.ARTIFACTS.delete(stale);
    if (!page.truncated || !page.cursor) {
      state.sql.exec("DELETE FROM artifact_cleanup WHERE workspace_id=?", workspaceId);
      return;
    }
    cursor = page.cursor;
  }
  state.sql.exec("INSERT INTO artifact_cleanup VALUES(?,?) ON CONFLICT(workspace_id) DO UPDATE SET cursor=excluded.cursor",
    workspaceId, cursor);
}
