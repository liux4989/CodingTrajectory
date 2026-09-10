import { fields, Fault, integer, Json, object, requireThat, stable, State, text, uuid, validate } from "./shared";

/** Materialized metadata follows the same SQLite transaction as artifact commits. */
export function initializeCatalog(state: State) {
  state.sql.exec(`CREATE TABLE IF NOT EXISTS published_catalog(
    artifact_id TEXT NOT NULL, sequence INTEGER NOT NULL, project_id TEXT NOT NULL,
    digest TEXT NOT NULL, observed_at TEXT NOT NULL, deleted INTEGER NOT NULL,
    payload TEXT NOT NULL, PRIMARY KEY(artifact_id,sequence));
    CREATE INDEX IF NOT EXISTS catalog_project ON published_catalog(project_id,artifact_id,sequence);
    CREATE INDEX IF NOT EXISTS catalog_changes ON published_catalog(sequence,artifact_id);
    CREATE INDEX IF NOT EXISTS catalog_activity ON published_catalog(observed_at DESC,artifact_id,sequence);
    CREATE INDEX IF NOT EXISTS resources_identity ON resources(resource_id,digest);
    CREATE TABLE IF NOT EXISTS catalog_migration(id INTEGER PRIMARY KEY, after_row INTEGER NOT NULL, complete INTEGER NOT NULL);
    INSERT OR IGNORE INTO catalog_migration VALUES(1,0,0);
    CREATE TRIGGER IF NOT EXISTS catalog_insert AFTER INSERT ON records WHEN NEW.kind='artifact' BEGIN
      INSERT OR REPLACE INTO published_catalog VALUES(NEW.key,NEW.sequence,
      json_extract(NEW.payload,'$.project_id'),json_extract(NEW.payload,'$.content_sha256'),
      coalesce(json_extract(NEW.payload,'$.observed_at'),''),coalesce(json_extract(NEW.payload,'$.deleted'),0),NEW.payload);
    END;
    CREATE TRIGGER IF NOT EXISTS catalog_update AFTER UPDATE ON records WHEN NEW.kind='artifact' BEGIN
      INSERT OR REPLACE INTO published_catalog VALUES(NEW.key,NEW.sequence,
      json_extract(NEW.payload,'$.project_id'),json_extract(NEW.payload,'$.content_sha256'),
      coalesce(json_extract(NEW.payload,'$.observed_at'),''),coalesce(json_extract(NEW.payload,'$.deleted'),0),NEW.payload);
    END;`);
  // Fresh authorities need no backfill. Existing authorities use explicit bounded migration.
  if (!state.sql.exec("SELECT 1 FROM records WHERE kind='artifact' LIMIT 1").toArray().length)
    state.sql.exec("UPDATE catalog_migration SET complete=1 WHERE id=1");
}

export function migrateCatalog(state: State): Json {
  const prior = state.sql.exec<{ after_row: number; complete: number }>("SELECT after_row,complete FROM catalog_migration WHERE id=1").one();
  if (prior.complete) return { complete: true, migrated: 0 };
  const rows = state.sql.exec<{ rowid: number; key: string; sequence: number; payload: string }>(
    "SELECT rowid,key,sequence,payload FROM records WHERE kind='artifact' AND rowid>? ORDER BY rowid LIMIT 500", prior.after_row).toArray();
  for (const row of rows) {
    const value = JSON.parse(row.payload);
    state.sql.exec("INSERT OR IGNORE INTO published_catalog VALUES(?,?,?,?,?,?,?)", row.key,row.sequence,value.project_id,
      value.content_sha256,value.observed_at ?? "",value.deleted ? 1 : 0,row.payload);
  }
  const complete = rows.length < 500;
  state.sql.exec("UPDATE catalog_migration SET after_row=?,complete=? WHERE id=1", rows.at(-1)?.rowid ?? prior.after_row,complete ? 1 : 0);
  return { complete, migrated: rows.length };
}

function head(state: State): number {
  return state.sql.exec<{ revision: number }>("SELECT coalesce(max(sequence),0) revision FROM published_catalog").one().revision;
}
function publicationStatus(state: State, sequence: number): Json {
  const row = state.sql.exec<{ stamp: string | null }>(
    "SELECT json_extract(payload,'$.published_at') stamp FROM published_catalog WHERE sequence<=? ORDER BY sequence DESC LIMIT 1", sequence).toArray()[0];
  return { last_publication_at: row?.stamp ?? null, authority_observed_at: new Date().toISOString() };
}
function ready(state: State) {
  requireThat(state.sql.exec<{ complete: number }>("SELECT complete FROM catalog_migration WHERE id=1").one().complete, "catalog_migration_required",503);
}
function projection(state: State, row: Json): Json | null | undefined {
  const items=state.get("projections",row.content_sha256)?.runtime_usage?.items;
  // Core intentionally excludes empty/non-reportable graphs. Preserve that
  // semantic distinction from an absent legacy projection.
  if(Array.isArray(items) && !items.length)return undefined;
  return items?.find((item:Json)=>item.root_session_id===row.artifact_id) ?? null;
}
function pack(value: Json) { return btoa(unescape(encodeURIComponent(stable(value)))).replace(/=/g, "").replace(/\+/g,"-").replace(/\//g,"_"); }
function unpack(value: unknown): Json {
  try { return object(JSON.parse(decodeURIComponent(escape(atob(text(value,4096).replace(/-/g,"+").replace(/_/g,"/")))))); }
  catch { throw new Fault(400,"invalid_cursor"); }
}

export function catalogRead(state: State, method: string, request: Json): Json {
  ready(state);
  if (method === "ct_publication_changes") {
    fields(request,["workspace_id","after_revision","snapshot_sequence","cursor","limit"],["workspace_id","after_revision"]);
    const current = head(state);
    const after = integer(request.after_revision);
    const limit = request.limit == null ? 100 : integer(request.limit,1,200);
    const cursor = request.cursor ? unpack(request.cursor) : null;
    const sequence = cursor ? integer(cursor.sequence,0,current) : request.snapshot_sequence == null ? current : integer(request.snapshot_sequence,0,current);
    const scope = stable({ workspace_id: request.workspace_id, after });
    if (cursor) requireThat(cursor.scope === scope && (request.snapshot_sequence == null || request.snapshot_sequence === sequence),"cursor_scope_mismatch",409);
    const lastSequence = cursor ? integer(cursor.last_sequence,after,sequence) : after;
    const lastId = cursor ? text(cursor.last_id,36) : "";
    if (after > current) return { workspace_id: request.workspace_id, published_sequence: current, ...publicationStatus(state,current), changes: [], next_cursor: null, reset_required: true };
    const rows = state.sql.exec<{ payload: string; sequence: number; artifact_id: string }>(
      "SELECT payload,sequence,artifact_id FROM published_catalog WHERE sequence>? AND sequence<=? AND (sequence>? OR (sequence=? AND artifact_id>?)) ORDER BY sequence,artifact_id LIMIT ?",
      after,sequence,lastSequence,lastSequence,lastId,limit+1).toArray();
    const selected = rows.slice(0,limit);
    const tail = selected.at(-1);
    return { workspace_id: request.workspace_id, published_sequence: sequence, ...publicationStatus(state,sequence), reset_required: false,
      changes: selected.map(row=> { const body=JSON.parse(row.payload); return {artifact_id:row.artifact_id,revision:row.sequence,deleted:!!body.deleted}; }),
      next_cursor: rows.length>limit && tail ? pack({ scope, sequence,last_sequence:tail.sequence,last_id:tail.artifact_id }) : null };
  }
  fields(request,["workspace_id","snapshot_sequence","cursor","limit","project_name","agent_vendor","since_days","resource_id","kind"],["workspace_id"]);
  const current = head(state);
  const cursor = request.cursor ? unpack(request.cursor) : null;
  const sequence = cursor ? integer(cursor.sequence,0,current) : request.snapshot_sequence == null ? current : integer(request.snapshot_sequence,0,current);
  const limit = request.limit == null ? 50 : integer(request.limit,1,200);
  const kind = request.kind ?? "sessions";
  requireThat(["sessions","projects","detail","status"].includes(kind),"invalid_catalog_kind");
  if (kind === "status") {
    requireThat(!request.cursor && !request.project_name && !request.agent_vendor && request.since_days == null && !request.resource_id, "invalid_status_scope");
    return { workspace_id: request.workspace_id, published_sequence: sequence, ...publicationStatus(state,sequence), minimum_available_revision: 0 };
  }
  if(kind==="detail")requireThat(request.resource_id,"resource_scope_required");
  const scope = stable({ workspace_id: request.workspace_id, kind, project_name: request.project_name ?? null,
    agent_vendor: request.agent_vendor ?? null, since_days: request.since_days ?? null, resource_id: request.resource_id ?? null });
  if (cursor) requireThat(cursor.scope === scope && (request.snapshot_sequence == null || request.snapshot_sequence===sequence),"cursor_scope_mismatch",409);
  const last = cursor ? text(cursor.last,256) : "";
  const evaluatedAt = cursor ? integer(cursor.evaluated_at) : Date.now();
  let where = "c.sequence<=? AND c.sequence=(SELECT max(v.sequence) FROM published_catalog v WHERE v.artifact_id=c.artifact_id AND v.sequence<=?) AND c.deleted=0";
  const bindings: (string | number)[] = [sequence,sequence];
  if(request.project_name!=null) { where+=" AND json_extract(p.payload,'$.display_name')=?"; bindings.push(text(request.project_name,256)); }
  if(request.agent_vendor!=null) { where+=" AND EXISTS(SELECT 1 FROM json_each(c.payload,'$.vendors') WHERE value=?)"; bindings.push(text(request.agent_vendor,64)); }
  if(request.since_days!=null) { where+=" AND julianday(c.observed_at)>=julianday(?)"; bindings.push(new Date(evaluatedAt-integer(request.since_days,1,36500)*86400000).toISOString()); }
  if(request.resource_id!=null) { where+=" AND EXISTS(SELECT 1 FROM resources r WHERE r.digest=c.digest AND r.resource_id=?)"; bindings.push(uuid(request.resource_id)); }
  const join = " LEFT JOIN records p ON p.kind='project' AND p.key=c.project_id AND p.sequence=(SELECT max(x.sequence) FROM records x WHERE x.kind='project' AND x.key=c.project_id AND x.sequence<=?)";
  const columns = kind === "projects" ? "c.project_id AS identity,json_extract(p.payload,'$.display_name') AS name" : "c.artifact_id AS identity,c.observed_at AS sort_time,c.payload";
  const group = kind === "projects" ? " GROUP BY c.project_id" : "";
  const identity = kind === "projects" ? "c.project_id" : "c.artifact_id";
  let seek=` AND ${identity}>?`;
  const pagingBindings:(string|number)[]=[last];
  let order=identity;
  if(kind!=="projects") {
    order="c.observed_at DESC,c.artifact_id";
    seek=cursor ? " AND (c.observed_at<? OR (c.observed_at=? AND c.artifact_id>?))" : "";
    pagingBindings.splice(0);
    if(cursor) { const time=text(cursor.last_time,64);pagingBindings.push(time,time,last); }
  }
  const rows = state.sql.exec<Json>(`SELECT ${columns} FROM published_catalog c ${join} WHERE ${where} ${seek} ${group} ORDER BY ${order} LIMIT ?`,sequence,...bindings,...pagingBindings,limit+1).toArray();
  const selected = rows.slice(0,limit);
  const projectRow = (row:Json) => {
    if(kind === "projects") {
      const vendors=state.sql.exec<{value:string}>("SELECT DISTINCT j.value FROM published_catalog c,json_each(c.payload,'$.vendors') j WHERE c.project_id=? AND c.sequence<=? AND c.deleted=0 AND c.sequence=(SELECT max(v.sequence) FROM published_catalog v WHERE v.artifact_id=c.artifact_id AND v.sequence<=?) ORDER BY j.value LIMIT 16",row.identity,sequence,sequence).toArray().map(value=>value.value);
      return {project_id:row.identity,name:row.name,path:null,vendors};
    }
    const value=JSON.parse(row.payload);
    const listProjection=projection(state,value);
    if(kind==="sessions" && listProjection===undefined)return null;
    return { artifact_id:value.artifact_id,project_id:value.project_id,revision:value.revision,
      published_sequence:value.published_sequence,session_ids:value.session_ids,projection:listProjection ?? null,
      canonical:kind === "detail" ? state.get("projections",value.content_sha256)?.canonical ?? null : undefined };
  };
  const items:Json[]=[];let consumed=0;let bytes=4096;
  for(const row of selected) {
    const value=projectRow(row);
    const size=value ? new TextEncoder().encode(JSON.stringify(value)).length : 0;
    requireThat(size<=512*1024-4096,"catalog_resource_budget",413);
    if(bytes+size>512*1024)break;
    consumed++;bytes+=size;
    if(value)items.push(value);
  }
  return {workspace_id:request.workspace_id,published_sequence:sequence,items,
    next_cursor:rows.length>consumed && consumed ? pack({scope,sequence,last:selected[consumed-1].identity,last_time:selected[consumed-1].sort_time??null,evaluated_at:evaluatedAt}):null};
}

export function validateReadProjections(raw: unknown) {
  const value=object(raw);
  fields(value,["schema_version","content_sha256","root_session_id","graph","trees","items"],["schema_version","content_sha256","root_session_id","graph","trees","items"]);
  requireThat(value.schema_version==="ct.canonical_read.v1" && /^[0-9a-f]{64}$/.test(value.content_sha256),"invalid_read_projection");
  uuid(value.root_session_id);
  requireThat(new TextEncoder().encode(stable(value)).length<=128*1024,"read_projection_budget",413);
  if(value.graph!=null) {
    fields(object(value.graph),["root_session_id","overview","stats","usage"],["root_session_id","overview","stats","usage"]);
    requireThat(value.graph.root_session_id===value.root_session_id,"read_projection_identity");
    for(const key of ["overview","stats","usage"]) validate(`graph_${key}_response`,value.graph[key]);
  }
  for(const [key,tree] of Object.entries(object(value.trees))) { uuid(key); validate("session_tree_response",tree); }
  requireThat(Array.isArray(value.items),"invalid_read_projection");
  validate("session_items_response",value.items);
}
