import { artifactKey } from "./artifacts";
import { decode, digest, encode, Fault, Json, requireThat, stable, State, validate } from "./shared";

export const API_VERSIONS: Record<string, number> = {
  "project.list": 5, "project.sessions": 5, "session.overview": 4, "session.summary": 3,
  "session.tree": 4, "session.stats": 4, "session.usage": 4, "session.model_usage": 4,
  "session.request_usage": 4, "session.tool_usage": 4, "graph.stats": 4, "graph.usage": 4,
  "graph.overview": 4, "session.items": 5, "session.events": 5, "living.sessions": 3,
};
const SCHEMA = "ct.prepared-api.v1";
const bytes = (value: any) => new TextEncoder().encode(stable(value));
const projectKey = (value: string) => value.trim().replace(/([a-z0-9])([A-Z])/g, "$1-$2").replace(/[^a-zA-Z0-9]+/g, "-").replace(/^-|-$/g, "").toLowerCase();
const b64 = (value: Uint8Array) => encode(value).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
const un64 = (value: string) => decode(value.replaceAll("-", "+").replaceAll("_", "/") + "=".repeat((4 - value.length % 4) % 4));
async function cursorKey(secret: string) {
  requireThat(secret && secret.length >= 32, "cursor_key_unavailable", 503);
  return crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"]);
}
export async function readCursor(token: string, secret: string): Promise<Json> {
  try {
    requireThat(typeof token === "string" && token.length <= 4096, "invalid_cursor");
    const parts = token.split(".");
    requireThat(parts.length === 2 && await crypto.subtle.verify("HMAC", await cursorKey(secret), un64(parts[1]) as BufferSource, new TextEncoder().encode(parts[0])), "invalid_cursor");
    const value = JSON.parse(new TextDecoder().decode(un64(parts[0])));
    requireThat(Number.isSafeInteger(value.expires) && Number.isSafeInteger(value.position), "invalid_cursor");
    requireThat(value.expires > Date.now() / 1000, "view_expired", 409);
    return value;
  } catch (error) { if (error instanceof Fault) throw error; throw new Fault(400, "invalid_cursor"); }
}
export async function signCursor(value: Json, secret: string) {
  const body = b64(bytes(value));
  const token = body + "." + b64(new Uint8Array(await crypto.subtle.sign("HMAC", await cursorKey(secret), new TextEncoder().encode(body))));
  requireThat(token.length <= 4096, "remote_result_too_large", 413);
  return token;
}

export function initializeApi(state: State) {
  state.sql.exec(`CREATE TABLE IF NOT EXISTS api_views (
    hash TEXT PRIMARY KEY, project_id TEXT NOT NULL, sequence INTEGER NOT NULL, identity TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS api_methods (
    view_hash TEXT NOT NULL, method TEXT NOT NULL, scope TEXT NOT NULL, turn_id TEXT NOT NULL,
    descriptor TEXT NOT NULL, PRIMARY KEY(view_hash,method,scope,turn_id));
    CREATE INDEX IF NOT EXISTS api_scope ON api_methods(method,scope,turn_id);
    CREATE TABLE IF NOT EXISTS api_inventory_cards (project_id TEXT PRIMARY KEY, cards TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS api_inventory_objects (view_hash TEXT NOT NULL, hash TEXT NOT NULL,
    PRIMARY KEY(view_hash,hash));`);
}

export async function apiView(workspace: string, source: string, methods: Json[], project = "workspace") {
  const manifest = { schema_version: SCHEMA, workspace_id: workspace, project_id: project, source_manifest_sha256: source, methods };
  return { workspace_id: workspace, source_manifest_sha256: source,
    view_manifest_sha256: await digest(stable(manifest)) };
}
export function commitApiView(state: State, project: string, sequence: number, identity: Json, methods: Json[]) {
  const hash = identity.view_manifest_sha256;
  state.sql.exec("INSERT OR REPLACE INTO api_views VALUES(?,?,?,?)", hash, project, sequence,
    stable({ ...identity, source_snapshot_sequence: sequence, view_snapshot_sequence: sequence }));
  for (const method of methods) state.sql.exec("INSERT OR REPLACE INTO api_methods VALUES(?,?,?,?,?)",
    hash, method.method, method.scope, method.turn_id ?? "", stable(method));
}
export function apiLocator(state: State, request: Json): Json {
  const params = request.params;
  const scope = params.session_id ?? params.root_session_id ?? "workspace";
  const turn = ["session.items", "session.events"].includes(request.method) ? "" : params.turn_id ?? "";
  const hash = request.view_manifest_sha256 ?? params.view_manifest_sha256;
  const row = state.sql.exec<{ identity: string; descriptor: string; project_id: string }>(`SELECT v.identity,m.descriptor,v.project_id
    FROM api_methods m JOIN api_views v ON v.hash=m.view_hash
    WHERE m.method=? AND m.scope=? AND m.turn_id=? ${hash ? "AND v.hash=?" : `AND (v.project_id='workspace' OR v.sequence=(SELECT MAX(workspace_sequence) FROM artifact_manifests WHERE project_id=v.project_id))`}
    ORDER BY v.sequence DESC LIMIT 1`, request.method, scope, turn, ...(hash ? [hash] : [])).toArray()[0];
  requireThat(row, hash ? "view_expired" : "prepared_view_unavailable", 409);
  return { identity: JSON.parse(row.identity), descriptor: JSON.parse(row.descriptor), project_id: row.project_id };
}
export function pruneApi(state: State) {
  state.sql.exec(`DELETE FROM api_views WHERE project_id<>'workspace' AND sequence NOT IN
    (SELECT workspace_sequence FROM artifact_manifests);
    DELETE FROM api_views WHERE project_id='workspace' AND sequence NOT IN
    (SELECT sequence FROM api_views WHERE project_id='workspace' ORDER BY sequence DESC LIMIT 3);
    DELETE FROM api_methods WHERE view_hash NOT IN (SELECT hash FROM api_views);
    DELETE FROM api_inventory_objects WHERE view_hash NOT IN (SELECT hash FROM api_views);`);
}

/** Inventory is computed only at publication, never by enumerating graphs on a read. */
export async function prepareInventory(env: Env, workspace: string, projects: Json[], cards: Json[]) {
  projects.sort((a, b) => a.project_id.localeCompare(b.project_id));
  cards.sort((a, b) => a.root_session_id.localeCompare(b.root_session_id));
  const source = await digest(stable({ projects, sessions: cards }));
  const methods: Json[] = [], objects: string[] = [];
  async function put(value: Json, bound: number) {
    const body = bytes(value);
    requireThat(body.length <= bound, "remote_result_too_large", 413);
    const sha256 = await digest(body);
    await env.ARTIFACTS.put(artifactKey(workspace, "api", sha256), body,
      { customMetadata: { workspace_id: workspace, kind: "api", sha256 } });
    objects.push(sha256);
    return { kind: "api", sha256, bytes: body.length };
  }
  for (const [method, rows] of [["project.list", projects], ["project.sessions", cards]] as [string, Json[]][]) {
    const header = { schema_version: SCHEMA, method, method_version: API_VERSIONS[method], scope: "workspace", turn_id: null, source_manifest_sha256: source };
    const descriptor: Json = { method, method_version: API_VERSIONS[method], scope: "workspace", turn_id: null, index: null, error: null };
    try {
      const topology = await put({ ...header, data: {} }, 128 * 1024);
      const sizes = rows.map(row => bytes(row).length), packs: Json[] = [], postings: Json = {};
      rows.forEach((row, position) => {
        const values: Json = { project_id: [row.project_id], project_name: [projectKey(row.project ?? row.display_name ?? "")],
          modified: [row.modified ?? row.modified_at], agent_vendor: row.vendors ?? [] };
        for (const [key, entries] of Object.entries(values)) for (const value of entries as any[]) {
          if (value != null) ((postings[key] ??= {})[String(value)] ??= []).push(position);
        }
      });
      let start = 0;
      while (start < rows.length) {
        let end = start, size = bytes({ ...header, start: rows.length, rows: [] }).length;
        while (end < rows.length && size + sizes[end] + 1 <= 256 * 1024) size += sizes[end++] + 1;
        requireThat(end > start, "remote_result_too_large", 413);
        packs.push({ start, end, object: await put({ ...header, start, rows: rows.slice(start, end) }, 256 * 1024) }); start = end;
      }
      descriptor.index = await put({ ...header, mode: "page", field: "items", topology, total: rows.length, sizes, packs, postings }, 64 * 1024);
    } catch (error) {
      if (!(error instanceof Fault) || error.code !== "remote_result_too_large") throw error;
      descriptor.error = error.code;
    }
    methods.push(descriptor);
  }
  return { identity: await apiView(workspace, source, methods), methods, objects };
}

export async function servePrepared(env: Env, locator: Json, method: string, params: Json): Promise<Json> {
  const { identity, descriptor } = locator;
  requireThat(descriptor.method_version === API_VERSIONS[method], "unsupported_prepared_version", 409);
  requireThat(!descriptor.error, descriptor.error ?? "remote_result_too_large", 413);
  let reads = 0, fetched = 0;
  const header = { schema_version: SCHEMA, method, method_version: descriptor.method_version,
    scope: descriptor.scope, turn_id: descriptor.turn_id, source_manifest_sha256: identity.source_manifest_sha256 };
  async function load(ref: Json, bound: number): Promise<Json> {
    requireThat(ref?.kind === "api" && /^[0-9a-f]{64}$/.test(ref.sha256) && Number.isSafeInteger(ref.bytes) && ref.bytes > 0, "prepared_object_corrupt", 503);
    requireThat(ref.bytes <= bound && ++reads <= 4 && fetched + ref.bytes <= 768 * 1024, "remote_result_too_large", 413);
    const object = await env.ARTIFACTS.get(artifactKey(identity.workspace_id, "api", ref.sha256));
    requireThat(object, "prepared_object_missing", 503);
    requireThat(object.size === ref.bytes, "prepared_object_corrupt", 503);
    // R2's object size is checked before allocation; consume its native buffer
    // rather than copying stream chunks into a second JavaScript buffer.
    const body = new Uint8Array(await object.arrayBuffer()); fetched += body.length;
    requireThat(body.length === ref.bytes && await digest(body) === ref.sha256, "prepared_object_corrupt", 503);
    let value;
    try { value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body)); }
    catch { throw new Fault(503, "prepared_object_corrupt"); }
    requireThat(value && Object.entries(header).every(([key, item]) => value[key] === item), "prepared_object_corrupt", 503);
    return value;
  }
  const index = await load(descriptor.index, 64 * 1024);
  if (index.mode === "exact") return (await load(index.result, 440 * 1024)).data;
  requireThat(index.mode === "page" && ["turns", "items", "events"].includes(index.field), "prepared_object_corrupt", 503);
  requireThat(Array.isArray(index.packs) && index.postings && typeof index.postings === "object" && !Array.isArray(index.postings), "prepared_object_corrupt", 503);
  const base = (await load(index.topology, 128 * 1024)).data;
  if (index.field === "turns") base.project = { ...base.project, project_id: locator.project_id };
  const total = index.total;
  requireThat(Number.isSafeInteger(total) && total >= 0 && Array.isArray(index.sizes) && index.sizes.length === total && index.sizes.every((n: number) => Number.isSafeInteger(n) && n > 0), "prepared_object_corrupt", 503);
  const packFor: number[] = [];
  for (const [number, pack] of index.packs.entries()) {
    requireThat(pack.start === packFor.length && Number.isSafeInteger(pack.end) && pack.end > pack.start && pack.end <= total, "prepared_object_corrupt", 503);
    while (packFor.length < pack.end) packFor.push(number);
  }
  requireThat(packFor.length === total, "prepared_object_corrupt", 503);
  const normalized: Json = Object.fromEntries(Object.entries(params).filter(([k, v]) => !["cursor", "view_manifest_sha256"].includes(k) && v != null));
  for (const key of ["item_ids", "event_ids", "types"]) if (normalized[key]) normalized[key] = [...new Set(normalized[key])].sort();
  const binding = { workspace_id: identity.workspace_id, method, method_version: descriptor.method_version,
    binding: await digest(stable({ method, params: normalized })), view_manifest_sha256: identity.view_manifest_sha256,
    source_manifest_sha256: identity.source_manifest_sha256, index_sha256: descriptor.index.sha256 };
  const older = index.field === "turns";
  let position = older ? total : 0;
  if (params.cursor) {
    const cursor = await readCursor(params.cursor, env.CT_CURSOR_KEY);
    requireThat(Object.entries(binding).every(([key, value]) => cursor[key] === value) && cursor.position >= 0 && cursor.position <= total, "invalid_cursor");
    position = cursor.position;
  }
  let selected: Set<number> | undefined;
  const missing = new Set<string>();
  for (const name of ["turn_id", "item_id", "item_ids", "event_ids", "types", "status", "tool_name", "project_id", "project_name", "agent_vendor", "modified_since"]) {
    if (params[name] == null) continue;
    const key = ["item_ids", "event_ids"].includes(name) ? "id" : name;
    const values = Array.isArray(params[name]) ? params[name] : [params[name]];
    const matches = new Set<number>();
    if (name === "modified_since") {
      for (const [stamp, positions] of Object.entries(index.postings.modified ?? {})) if (Date.parse(stamp) >= Date.parse(params[name])) for (const p of positions as number[]) matches.add(p);
    } else for (let value of values) {
      if (name === "project_name") value = projectKey(value);
      const found = index.postings[key]?.[String(value)] ?? [];
      for (const p of found) matches.add(p);
    }
    if (name === "project_name") requireThat(Object.values(index.postings.project_id ?? {}).filter((entries: any) => entries.some((p: number) => matches.has(p))).length <= 1, "ambiguous_project_name");
    selected = selected === undefined ? matches : new Set([...selected].filter(p => matches.has(p)));
  }
  for (const name of ["item_ids", "event_ids"]) for (const id of params[name] ?? []) {
    if (!(index.postings.id?.[id] ?? []).length) missing.add(id);
  }
  const positions = [...(selected ?? Array.from({ length: total }, (_, i) => i))].sort((a, b) => a - b);
  requireThat(positions.every(p => Number.isSafeInteger(p) && p >= 0 && p < total), "prepared_object_corrupt", 503);
  const candidates = positions.filter(p => older ? p < position : p >= position); if (older) candidates.reverse();
  const chosen: number[] = [], packs = new Set<number>();
  let size = 2;
  // Key order affects signatures, not JSON byte length. Keep canonical
  // serialization for cursor bindings, but use native JSON for size checks.
  const budget = Math.min(440 * 1024 - new TextEncoder().encode(JSON.stringify(base)).length, older ? 320 * 1024 : Infinity);
  for (const p of candidates) {
    if (chosen.length === params.limit || size + index.sizes[p] + 1 > budget || (!packs.has(packFor[p]) && packs.size === 2)) break;
    chosen.push(p); packs.add(packFor[p]); size += index.sizes[p] + 1;
  }
  requireThat(!candidates.length || chosen.length, "remote_result_too_large", 413);
  const rows = new Map<number, Json>();
  for (const n of packs) {
    const pack = index.packs[n], value = await load(pack.object, 256 * 1024);
    requireThat(value.start === pack.start && Array.isArray(value.rows) && value.rows.length === pack.end - pack.start, "prepared_object_corrupt", 503);
    value.rows.forEach((row: Json, offset: number) => {
      const p = pack.start + offset;
      // Pack SHA verifies the original bytes; JSON numbers can serialize
      // differently in Python and JavaScript (1.0 vs 1). Bound the final result.
      requireThat(!older || row.global_ordinal === p, "prepared_object_corrupt", 503); rows.set(p, row);
    });
  }
  chosen.sort((a, b) => a - b);
  const more = chosen.length < candidates.length;
  const next = more ? await signCursor({ ...binding, position: older ? chosen[0] : chosen[chosen.length - 1] + 1, expires: Math.floor(Date.now() / 1000) + 86400 }, env.CT_CURSOR_KEY) : null;
  const result = { ...base, [index.field]: chosen.map(p => rows.get(p)) };
  if (older) result.page = { direction: "older", requested_limit: params.limit, start_ordinal: chosen[0] ?? 0,
    end_ordinal_exclusive: chosen.length ? chosen[chosen.length - 1] + 1 : 0, returned: chosen.length, total, has_more: more, next_cursor: next };
  else Object.assign(result, { total: positions.length, returned: chosen.length, next_cursor: next, unresolved_ids: [...missing].sort() });
  if (result.coverage) result.coverage.trimmed ||= more || chosen.length < positions.length;
  requireThat(new TextEncoder().encode(JSON.stringify(result)).length <= 440 * 1024, "remote_result_too_large", 413);
  return result;
}

export function validateApi(message: Json) {
  requireThat(message.protocol === "ct.api.v1", "unsupported_version");
  requireThat(!["session.search", "living.events"].includes(message.method), "method_unavailable", 501);
  requireThat(API_VERSIONS[message.method] === message.method_version, "unsupported_version");
  validate("api_request", message);
  validate(message.method === "living.sessions" ? "living_sessions_request" : "api_" + message.method.replaceAll(".", "_"), message.params);
  requireThat(!(message.params.project_id && message.params.project_name), "invalid_contract");
  if (message.params.project_id != null) {
    requireThat(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(message.params.project_id), "invalid_contract");
    message.params.project_id = message.params.project_id.toLowerCase();
  }
}
