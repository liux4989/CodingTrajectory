import { fields, Json, requireThat, stable, State, validate } from "./shared";

export function initializeResourceProjections(state: State) {
  state.sql.exec(`CREATE TABLE IF NOT EXISTS resource_projections(
    identity TEXT NOT NULL, kind TEXT NOT NULL, resource_id TEXT NOT NULL,
    coverage TEXT NOT NULL, payload TEXT,
    PRIMARY KEY(identity,kind,resource_id));
    CREATE TABLE IF NOT EXISTS resource_projection_pages(
    identity TEXT NOT NULL, kind TEXT NOT NULL, resource_id TEXT NOT NULL,
    page_index INTEGER NOT NULL, page_count INTEGER NOT NULL,
    coverage TEXT NOT NULL, payload TEXT,
    PRIMARY KEY(identity,kind,resource_id,page_index));
    CREATE INDEX IF NOT EXISTS catalog_digest ON published_catalog(digest,artifact_id,sequence);`);
}

/** Validate independent canonical outputs before writing invisible staging rows. */
export function validateResourceProjections(value: Json, metadata: Json, resources: string[], artifact: Json) {
  validate("resource_projections", value);
  requireThat(new TextEncoder().encode(stable(value)).length <= 4*1024*1024, "resource_projection_budget", 413);
  const members = new Set(resources), keys = new Set<string>();
  const pages = new Map<string, {count: number, indexes: Set<number>}>();
  const items = new Map<string, string>();
  for (const session of artifact.sessions) for (const turn of session.turns ?? [])
    for (const item of turn.items ?? []) items.set(item.item_id, turn.turn_id);
  for (const row of value.rows) {
    const key = `${row.resource_kind}:${row.resource_id}:${row.page_index}`;
    requireThat(!keys.has(key) && members.has(row.resource_id), "resource_projection_membership");
    keys.add(key);
    requireThat(row.page_index < row.page_count, "resource_projection_page");
    requireThat((value.schema_version === "ct.resource_projections.v2" && row.resource_kind === "tree") || (row.page_index === 0 && row.page_count === 1), "resource_projection_page");
    const resource = `${row.resource_kind}:${row.resource_id}`;
    const group = pages.get(resource) ?? {count: row.page_count, indexes: new Set<number>()};
    requireThat(group.count === row.page_count && !group.indexes.has(row.page_index), "resource_projection_page");
    group.indexes.add(row.page_index); pages.set(resource, group);
    requireThat(row.coverage === "complete" ? row.payload != null : row.payload === null, "resource_projection_coverage");
    if (row.resource_kind === "graph") requireThat(row.resource_id === metadata.artifact_id, "resource_projection_identity");
    if (row.resource_kind === "tree") requireThat(metadata.session_ids.includes(row.resource_id), "resource_projection_identity");
    if (row.resource_kind === "item") requireThat(items.has(row.resource_id), "resource_projection_identity");
    if (row.payload == null) continue;
    if (row.resource_kind === "graph") {
      fields(row.payload, ["root_session_id","overview","stats","usage"], ["root_session_id","overview","stats","usage"]);
      requireThat(row.payload.root_session_id === row.resource_id, "resource_projection_identity");
      for (const part of ["overview","stats","usage"]) validate(`graph_${part}_response`, row.payload[part]);
    } else if (row.resource_kind === "tree") {
      validate("session_tree_response", row.payload);
    } else {
      validate("session_items_response", [row.payload]);
      requireThat(row.payload.item_id === row.resource_id && row.payload.turn_id === items.get(row.resource_id), "resource_projection_identity");
      requireThat(!("content" in row.payload), "resource_projection_content_forbidden");
    }
    requireThat(new TextEncoder().encode(stable(row.payload)).length <= 128*1024, "resource_projection_budget", 413);
  }
  for (const group of pages.values()) requireThat(group.indexes.size === group.count, "resource_projection_page");
}

/** Resolve exact published membership, then read only one independent payload. */
export function readResource(state: State, kind: string, id: string, revision: number, page = 0): Json {
  const manifests = state.sql.exec<Json>(`SELECT c.artifact_id,
    json_extract(c.payload,'$.resource_projection_identity') identity
    FROM resources r JOIN published_catalog c ON c.digest=r.digest
    WHERE r.resource_id=? AND c.sequence<=? AND c.deleted=0
    AND c.sequence=(SELECT max(v.sequence) FROM published_catalog v WHERE v.artifact_id=c.artifact_id AND v.sequence<=?)
    ORDER BY c.sequence DESC,c.artifact_id LIMIT 2`, id, revision, revision).toArray();
  // Publication ownership should make membership unique; never choose arbitrarily.
  requireThat(manifests.length <= 1, "resource_membership_conflict", 409);
  const manifest = manifests[0];
  let row: Json | null | undefined = null;
  if (manifest?.identity) row = state.sql.exec<Json>(
    "SELECT page_index,page_count,coverage,payload FROM resource_projection_pages WHERE identity=? AND kind=? AND resource_id=? AND page_index=?",
    manifest.identity, kind, kind === "graph" ? manifest.artifact_id : id, page).toArray()[0]
    ?? (page === 0 ? state.sql.exec<Json>("SELECT 0 page_index,1 page_count,coverage,payload FROM resource_projections WHERE identity=? AND kind=? AND resource_id=?",
      manifest.identity, kind, kind === "graph" ? manifest.artifact_id : id).toArray()[0] : null);
  return { kind: "resource", resource_kind: kind, resource_id: id,
    page_index: row?.page_index ?? 0, page_count: row?.page_count ?? 1,
    coverage: !manifest ? "not_found" : row?.coverage ?? "projection_unavailable",
    payload: row?.payload ? JSON.parse(row.payload) : null };
}
