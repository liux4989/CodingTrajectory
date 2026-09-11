import { Json, Principal, requireThat, stable, State, UUID, validate } from "./shared";
import { initializeResourceProjections, readResource } from "./resource-projections";

const LIFETIME_MS = 30 * 60 * 1000;
const MAX_PAGE_BYTES = 512 * 1024;
const MAX_TOKENS = 10000;

/** Selection state is bounded independently of retained publication payloads. */
export function initializeCatalogSelections(state: State) {
  initializeResourceProjections(state);
  state.sql.exec(`CREATE TABLE IF NOT EXISTS catalog_selections (
    token TEXT PRIMARY KEY, workspace TEXT NOT NULL, principal TEXT NOT NULL,
    publication INTEGER NOT NULL, metadata INTEGER NOT NULL,
    evaluated INTEGER NOT NULL, expires INTEGER NOT NULL);
    CREATE INDEX IF NOT EXISTS catalog_selection_expiry ON catalog_selections(expires);
    CREATE TABLE IF NOT EXISTS catalog_cursors (
    token TEXT PRIMARY KEY, selection TEXT NOT NULL, scope TEXT NOT NULL,
    position TEXT NOT NULL, expires INTEGER NOT NULL,
    UNIQUE(selection,scope,position));
    CREATE INDEX IF NOT EXISTS catalog_cursor_expiry ON catalog_cursors(expires);`);
  if (!state.sql.exec<{ name: string }>("PRAGMA table_info(catalog_selections)").toArray().some(row => row.name === "workspace_sequence")) {
    state.sql.exec("ALTER TABLE catalog_selections ADD COLUMN workspace_sequence INTEGER");
  }
}

function reserve(state: State, table: "catalog_selections" | "catalog_cursors", now: number) {
  state.sql.exec(`DELETE FROM ${table} WHERE token IN
    (SELECT token FROM ${table} WHERE expires<=? ORDER BY expires LIMIT 200)`, now);
  requireThat(state.sql.exec<{ count: number }>(`SELECT count(*) count FROM ${table}`).one().count < MAX_TOKENS,
    "selection_capacity_exceeded", 429);
}

function checkToken(token: string, incarnation: string, now: number) {
  const parts = token.split(".");
  requireThat(parts.length === 3 && UUID.test(parts[0]) && /^\d{13}$/.test(parts[1]) && UUID.test(parts[2]), "invalid_cursor");
  requireThat(parts[0] === incarnation, "selection_reset", 409);
  // The visible expiry permits a typed reset after token-state cleanup. It grants
  // no authority: a live token must still match the exact server-held record.
  requireThat(Number(parts[1]) > now, "selection_expired", 409);
}

function continuation(state: State, selected: Json, scope: string, position: Json, now: number): string {
  const encoded = stable(position);
  const existing = state.sql.exec<{ token: string }>("SELECT token FROM catalog_cursors WHERE selection=? AND scope=? AND position=?",
    selected.token, scope, encoded).toArray()[0]?.token;
  if (existing) return existing;
  reserve(state, "catalog_cursors", now);
  const token = `${selected.token.split('.')[0]}.${selected.expires}.${crypto.randomUUID()}`;
  state.sql.exec("INSERT INTO catalog_cursors VALUES(?,?,?,?,?)", token, selected.token, scope, encoded, selected.expires);
  return token;
}

/** Called inside transactionSync, after current workspace/role authorization. */
export function catalogReadV2(state: State, request: Json, principal: Principal): Json {
  validate("ct_catalog_read_v2", request);
  requireThat(request.workspace_id === principal.workspace_id, "workspace_denied", 403);
  requireThat(state.sql.exec<{ complete: number }>("SELECT complete FROM catalog_migration WHERE id=1").one().complete,
    "catalog_migration_required", 503);
  const now = Date.now();
  const incarnation = state.sql.exec<{ incarnation: string }>("SELECT incarnation FROM upload_authority WHERE id=1").one().incarnation;
  const kind = request.kind;
  const include: string[] = [...new Set<string>(request.include ?? [])].sort();
  const population = request.population ?? "published";
  const ids: string[] = (request.resource_ids ?? []).map((id: string) => id.toLowerCase());
  requireThat(kind === "resources" ? request.resource_kind && ids.length : !request.resource_kind && !ids.length, "invalid_resource_scope");
  if (kind === "resources") requireThat(!request.project_name && !request.agent_vendor && request.since_days == null && !request.modified_since, "invalid_resource_scope");
  requireThat(kind === "projects" || population === "published", "invalid_population");
  requireThat(kind === "sessions" || !include.length, "invalid_include");
  requireThat(kind === "changes" ? request.after_revision != null : request.after_revision == null, "invalid_change_scope");
  if (kind === "changes") requireThat(!request.project_name && !request.agent_vendor && request.since_days == null && !request.modified_since, "invalid_change_scope");
  if (kind === "status") requireThat(!request.cursor && !request.project_name && !request.agent_vendor
    && request.since_days == null && !request.modified_since, "invalid_status_scope");
  const scope = stable({ version: 2, kind, population, include,
    ...(kind === "resources" ? {resource_kind: request.resource_kind, resource_ids: ids} : {}),
    after_revision: request.after_revision ?? null, project_name: request.project_name ?? null,
    agent_vendor: request.agent_vendor ?? null, since_days: request.since_days ?? null,
    modified_since: request.modified_since ? new Date(request.modified_since).toISOString() : null });
  let cursor: Json | undefined;
  if (request.cursor) {
    checkToken(request.cursor, incarnation, now);
    cursor = state.sql.exec<Json>("SELECT * FROM catalog_cursors WHERE token=?", request.cursor).toArray()[0];
    requireThat(cursor, "invalid_cursor", 400);
    requireThat(cursor.expires > now, "selection_expired", 409);
    requireThat(cursor.scope === scope && (!request.selection || request.selection === cursor.selection), "invalid_cursor", 400);
  }
  const token = request.selection ?? cursor?.selection;
  let selected: Json;
  if (token) {
    checkToken(token, incarnation, now);
    const row = state.sql.exec<Json>("SELECT * FROM catalog_selections WHERE token=?", token).toArray()[0];
    requireThat(row && row.expires > now, "selection_expired", 409);
    requireThat(row.workspace === principal.workspace_id && row.principal === principal.agent_id, "selection_scope_denied", 403);
    selected = row;
  } else {
    reserve(state, "catalog_selections", now);
    const publication = state.sql.exec<{ revision: number }>("SELECT coalesce(max(sequence),0) revision FROM published_catalog").one().revision;
    const metadata = state.sql.exec<{ revision: number }>("SELECT coalesce(max(sequence),0) revision FROM records WHERE kind='project'").one().revision;
    selected = { token: `${incarnation}.${now + LIFETIME_MS}.${crypto.randomUUID()}`, workspace: principal.workspace_id,
      principal: principal.agent_id, publication, metadata, evaluated: now, expires: now + LIFETIME_MS, workspace_sequence: state.head() };
    state.sql.exec("INSERT INTO catalog_selections VALUES(?,?,?,?,?,?,?,?)", selected.token, selected.workspace,
      selected.principal, publication, metadata, selected.evaluated, selected.expires, selected.workspace_sequence);
  }
  const stamp = state.sql.exec<{ stamp: string | null }>(
    "SELECT json_extract(payload,'$.published_at') stamp FROM published_catalog WHERE sequence<=? ORDER BY sequence DESC LIMIT 1",
    selected.publication).toArray()[0]?.stamp ?? null;
  const response: Json = { schema_version: "ct.catalog.v2", workspace_id: principal.workspace_id,
    selection: { token: selected.token, authority_incarnation: incarnation, publication_revision: selected.publication,
      project_metadata_revision: selected.metadata, workspace_sequence: selected.workspace_sequence, evaluated_at: new Date(selected.evaluated).toISOString(),
      expires_at: new Date(selected.expires).toISOString() }, last_publication_at: stamp,
    authority_observed_at: new Date(now).toISOString(),
    // No payload/history GC is enabled. Revision zero remains readable in both domains.
    minimum_available_revision: 0, minimum_project_metadata_revision: 0,
    coverage: kind === "status" ? "unknown" : "complete", items: [], next_cursor: null };
  if (kind === "status") {
    validate("catalog_read_v2_response", response);
    return response;
  }

  const position = cursor ? JSON.parse(cursor.position) : null;
  const limit = request.limit ?? 50;
  if (kind === "resources") {
    let offset = position?.offset ?? 0, page = position?.page ?? 0;
    let bytes = new TextEncoder().encode(JSON.stringify(response)).length + 256;
    while (offset < ids.length && response.items.length < limit) {
      const value = readResource(state, request.resource_kind, ids[offset], selected.publication, page);
      const size = new TextEncoder().encode(JSON.stringify(value)).length + 1;
      if (bytes + size > MAX_PAGE_BYTES) break;
      response.items.push(value); bytes += size;
      if (value.coverage === "complete" && page + 1 < value.page_count) page++;
      else { offset++; page = 0; }
      if (value.coverage !== "complete") response.coverage = "partial";
    }
    if (offset < ids.length) response.next_cursor = continuation(state, selected, scope, {offset,page}, now);
    validate("catalog_read_v2_response", response);
    return response;
  }
  if (kind === "changes") {
    const after = request.after_revision;
    response.reset_required = after > selected.publication || after < response.minimum_available_revision;
    if (!response.reset_required) {
      const rows = state.sql.exec<Json>(`SELECT artifact_id,sequence,deleted FROM published_catalog
        WHERE sequence>? AND sequence<=? AND (sequence>? OR (sequence=? AND artifact_id>?))
        ORDER BY sequence,artifact_id LIMIT ?`, after, selected.publication,
        position?.revision ?? after, position?.revision ?? after, position?.id ?? "", limit+1).toArray();
      response.items = rows.slice(0,limit).map(row => ({kind: "change", artifact_id: row.artifact_id, revision: row.sequence, deleted: !!row.deleted}));
      if (rows.length > limit) {
        const tail = rows[limit-1];
        response.next_cursor = continuation(state, selected, scope, {id: tail.artifact_id, revision: tail.sequence}, now);
      }
    }
    validate("catalog_read_v2_response", response);
    return response;
  }
  const bindings: (string | number)[] = [];
  const published = `c.sequence<=? AND c.sequence=(SELECT max(v.sequence) FROM published_catalog v
    WHERE v.artifact_id=c.artifact_id AND v.sequence<=?) AND c.deleted=0`;
  const project = `p.kind='project' AND p.sequence=(SELECT max(v.sequence) FROM records v
    WHERE v.kind='project' AND v.key=p.key AND v.sequence<=?) AND coalesce(json_extract(p.payload,'$.deleted'),0)=0`;
  let query: string;
  if (kind === "projects") {
    query = `SELECT p.key identity,p.payload FROM records p WHERE ${project}`;
    bindings.push(selected.metadata);
    if (population === "published" || request.agent_vendor || request.since_days != null) {
      query += ` AND EXISTS(SELECT 1 FROM published_catalog c WHERE c.project_id=p.key AND ${published}`;
      bindings.push(selected.publication, selected.publication);
      if (request.agent_vendor) { query += " AND EXISTS(SELECT 1 FROM json_each(c.payload,'$.vendors') WHERE value=?)"; bindings.push(request.agent_vendor); }
      if (request.since_days != null) { query += " AND julianday(c.observed_at)>=julianday(?)"; bindings.push(new Date(selected.evaluated-request.since_days*86400000).toISOString()); }
      query += ")";
    }
    if (request.modified_since) { query += " AND julianday(json_extract(p.payload,'$.modified_at'))>=julianday(?)"; bindings.push(request.modified_since); }
  } else {
    query = `SELECT c.artifact_id identity,c.sequence,c.observed_at sort_time,c.payload,json_extract(p.payload,'$.display_name') name
      FROM published_catalog c LEFT JOIN records p ON p.key=c.project_id AND ${project} WHERE ${published}`;
    bindings.push(selected.metadata, selected.publication, selected.publication);
    if (request.agent_vendor) { query += " AND EXISTS(SELECT 1 FROM json_each(c.payload,'$.vendors') WHERE value=?)"; bindings.push(request.agent_vendor); }
    if (request.since_days != null) { query += " AND julianday(c.observed_at)>=julianday(?)"; bindings.push(new Date(selected.evaluated-request.since_days*86400000).toISOString()); }
    if (request.modified_since) { query += " AND julianday(c.observed_at)>=julianday(?)"; bindings.push(request.modified_since); }
  }
  if (request.project_name) { query += " AND json_extract(p.payload,'$.display_name')=?"; bindings.push(request.project_name); }
  if (kind === "projects") {
    query += " AND p.key>? ORDER BY p.key LIMIT ?";
    bindings.push(position?.id ?? "", limit+1);
  } else {
    if (position) { query += " AND (c.observed_at<? OR (c.observed_at=? AND c.artifact_id>?))"; bindings.push(position.time, position.time, position.id); }
    query += " ORDER BY c.observed_at DESC,c.artifact_id LIMIT ?";
    bindings.push(limit+1);
  }
  const rows = state.sql.exec<Json>(query, ...bindings).toArray();
  let consumed = 0;
  let bytes = new TextEncoder().encode(JSON.stringify(response)).length + 256;
  for (const row of rows.slice(0, limit)) {
    const body = JSON.parse(row.payload);
    let value: Json | null;
    if (kind === "projects") {
      const vendors = state.sql.exec<{ value: string }>(`SELECT DISTINCT j.value FROM published_catalog c,json_each(c.payload,'$.vendors') j
        WHERE c.project_id=? AND ${published} ORDER BY j.value LIMIT 65`, row.identity, selected.publication, selected.publication).toArray();
      requireThat(vendors.length <= 64, "catalog_resource_budget", 413);
      value = { kind: "project", project_id: row.identity, name: body.display_name, modified_at: body.modified_at ?? null,
        vendors: vendors.map(v => v.value) };
    } else {
      const variant = include.includes("runtime") ? include.includes("usage") ? "runtime_usage" : "runtime" : include.includes("usage") ? "usage" : "default";
      // Select only the requested summary variant, not the canonical detail bundle.
      const projection = state.sql.exec<{ items: string | null }>(`SELECT json_extract(payload,?) items FROM records
        WHERE kind='projections' AND key=? AND sequence<=? ORDER BY sequence DESC LIMIT 1`,
        `$.${variant}.items`, body.content_sha256, selected.publication).toArray()[0];
      const summaries: Json[] | null = projection?.items ? JSON.parse(projection.items) : null;
      const summary = summaries?.find(item => item.root_session_id === body.artifact_id);
      requireThat(!summaries?.length || summary, "projection_identity_mismatch", 503);
      // Apply canonical schema defaults before calculating the response byte budget.
      if (summary) validate("project_sessions_response", { items: [summary] });
      value = summaries && !summaries.length ? null : { kind: "session", artifact_id: body.artifact_id,
        project_id: body.project_id, revision: row.sequence,
        coverage: summary ? "complete" : "projection_unavailable",
        projection: summary ? { ...summary, project: row.name ?? null } : null };
    }
    const size = value ? new TextEncoder().encode(JSON.stringify(value)).length + 1 : 0;
    requireThat(size < MAX_PAGE_BYTES - 4096, "catalog_resource_budget", 413);
    if (bytes + size > MAX_PAGE_BYTES) break;
    consumed++; bytes += size;
    if (value) {
      response.items.push(value);
      if (value.coverage === "projection_unavailable") response.coverage = "partial";
    }
  }
  if (rows.length > consumed && consumed) {
    const tail = rows[consumed-1];
    response.next_cursor = continuation(state, selected, scope, { id: tail.identity, time: tail.sort_time ?? null }, now);
  }
  validate("catalog_read_v2_response", response);
  return response;
}
