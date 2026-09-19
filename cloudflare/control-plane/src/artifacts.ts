import { bounded, digest, Fault, Json, receipt, requireThat, stable, State, validate } from "./shared";
import { apiView, commitApiView, prepareInventory, pruneApi } from "./prepared-api";


const MANIFEST_SCHEMA = "ct.artifact-manifest.v2";
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
}

/** Fence cleanup before an authenticated collector reads or writes an object. */
export function claimArtifactUpload(state: State, kind: string, sha256: string) {
  const now = Math.floor(Date.now() / 1000);
  state.sql.exec(`INSERT INTO artifact_upload_claims VALUES(?,?,?)
    ON CONFLICT(kind,sha256) DO UPDATE SET expires_at=excluded.expires_at`,
  kind, sha256, now + UPLOAD_CLAIM_SECONDS);
}

export interface ArtifactPublicationPlan {
  complete: true;
  releaseClaims: Array<{ kind: string; sha256: string }>;
  views: Json[];
  cards: Json[];
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
    const manifest = JSON.parse(row.manifest);
    for (const graph of manifest.graphs) {
      for (const object of [graph.facts, graph.summary, ...(graph.api_objects ?? [])]) {
        retained.set(`${object.kind}:${object.sha256}`, object.bytes);
      }
    }
  }
  const graphIds = new Set<string>();
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
      requireThat(!method.index || graph.api_objects.some((ref: Json) => ref.sha256 === method.index.sha256 && ref.bytes === method.index.bytes), "invalid_prepared_reference");
      if (method.index) {
        requireThat(method.index.bytes <= 64 * 1024, "invalid_prepared_reference");
        const stored = await env.ARTIFACTS.get(artifactKey(request.workspace_id, "api", method.index.sha256));
        requireThat(stored && stored.size === method.index.bytes, "artifact_upload_incomplete", 409);
        const body = await bounded(stored.body, method.index.bytes);
        requireThat(await digest(body) === method.index.sha256, "prepared_object_corrupt", 503);
        const index = JSON.parse(new TextDecoder().decode(body));
        requireThat(index.schema_version === "ct.prepared-api.v1" && index.source_manifest_sha256 === graph.fact_set_digest && index.method === method.method && index.method_version === method.method_version && index.scope === method.scope && index.turn_id === method.turn_id, "invalid_prepared_reference");
        requireThat(["exact", "page"].includes(index.mode), "invalid_prepared_reference");
        const refs = index.mode === "exact" ? [index.result] : [index.topology, ...index.packs.map((pack: Json) => pack.object)];
        requireThat(refs.every((ref: Json) => ref.kind === "api" && references.get(ref.sha256) === ref.bytes), "invalid_prepared_reference");
      }
    }
    for (const object of [graph.facts, graph.summary, ...graph.api_objects]) {
      if (retained.get(`${object.kind}:${object.sha256}`) === object.bytes) continue;
      const key = artifactKey(request.workspace_id, object.kind, object.sha256);
      const head = await env.ARTIFACTS.head(key);
      requireThat(head && head.size === object.bytes
        && head.customMetadata?.workspace_id === request.workspace_id
        && head.customMetadata?.kind === object.kind
        && head.customMetadata?.sha256 === object.sha256,
      "artifact_upload_incomplete", 409);
      releaseClaims.push({ kind: object.kind, sha256: object.sha256 });
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
  return { complete: true, releaseClaims, views, cards, inventory };
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
    graphs: request.graphs.map((graph: Json) => ({
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
  state.sql.exec("INSERT INTO artifact_manifests VALUES(?,?,?,?,?)",
    request.project_id, request.publication_sequence, sequence, request.agent_id, stable(manifest));
  request.graphs.forEach((graph: Json, index: number) => commitApiView(state, request.project_id, sequence, plan.views[index], graph.api_methods));
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
      state.sql.exec(`DELETE FROM artifact_upload_claims WHERE kind=? AND sha256 IN
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
    const manifest = JSON.parse(row.manifest);
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
