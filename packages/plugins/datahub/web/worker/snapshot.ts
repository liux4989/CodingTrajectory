import { configurationFor, verifyAccess, withSecurityHeaders } from "./access";

type Row = Record<string, unknown>;
type Params = Record<string, unknown>;
const PROTOCOL = "ct.datahub.v1";
const ENDPOINT = "/api/datahub/query";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const MAX_FILE_BYTES = 20 * 1024 * 1024;
const MAX_REQUEST_BYTES = 1024 * 1024;
const SUPPORTED = ["datahub.snapshot", "datahub.changes", "projects", "sessions", "session.graph", "session.tree", "session.items"] as const;
export const UNSUPPORTED = ["overview", "today", "project.detail", "sessions.timeline", "session.context-window", "session.evidence-timeline", "session.events", "model-usage", "token-efficiency.project", "code-time.report", "code-time.forecasts", "code-time.calibration", "datahub.refresh"] as const;

class RequestError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}

export async function handle(request: Request, env: Env, queryDispatch = dispatch): Promise<Response> {
    let response: Response;
    try {
      if (!configurationFor(env)) throw new RequestError(503, "unconfigured", "Datahub is not configured.");
      if (!await verifyAccess(request, env)) throw new RequestError(403, "authentication_required", "Cloudflare Access authentication required.");
      const url = new URL(request.url);
      if (url.pathname.startsWith("/_snapshot")) throw new RequestError(404, "not_found", "Not found.");
      if (url.pathname === ENDPOINT) {
        if (request.method !== "POST" || url.search) throw new RequestError(404, "not_found", "Not found.");
        const envelope = await requestEnvelope(request);
        response = Response.json(await queryDispatch(envelope, env));
      } else {
        if (url.pathname.startsWith("/api/") || !["GET", "HEAD"].includes(request.method)) {
          throw new RequestError(404, "not_found", "Not found.");
        }
        response = await env.ASSETS.fetch(request);
      }
    } catch (error) {
      const fault = error instanceof RequestError ? error : new RequestError(503, "unavailable", "Snapshot is unavailable.");
      response = Response.json({ protocol: PROTOCOL, id: null, method: null, ok: false, data: null,
        availability: { state: "unavailable", missing: [{ field: "$", reason: fault.code }] },
        error: { code: fault.code, message: fault.message } }, { status: fault.status });
    }
    return withSecurityHeaders(response);
}

export default { fetch: (request, env) => handle(request, env) } satisfies ExportedHandler<Env>;

async function requestEnvelope(request: Request): Promise<{ protocol: string; id: unknown; method: string; params: Params }> {
  const value = await readJson(request.body, MAX_REQUEST_BYTES, "Request body");
  if (!isRow(value)) throw new RequestError(400, "invalid_request", "Request body must be an object.");
  exact(value, ["protocol", "id", "method", "params"]);
  if (value.protocol !== PROTOCOL) throw new RequestError(400, "invalid_protocol", `Protocol must be ${PROTOCOL}.`);
  if (typeof value.method !== "string" || !value.method) throw new RequestError(400, "invalid_method", "Method is required.");
  if (!isRow(value.params)) throw new RequestError(400, "invalid_params", "Params must be an object.");
  return { protocol: PROTOCOL, id: value.id ?? null, method: value.method, params: value.params };
}

export async function dispatch(envelope: { protocol: string; id?: unknown; method: string; params: Params }, env: Env): Promise<unknown> {
  if (envelope.protocol !== PROTOCOL) throw new RequestError(400, "invalid_protocol", `Protocol must be ${PROTOCOL}.`);
  if (envelope.method === "datahub.capabilities") {
    exact(envelope.params, []);
    return success(envelope.id, envelope.method, { supported: [...SUPPORTED], unsupported: [...UNSUPPORTED] });
  }
  if ((UNSUPPORTED as readonly string[]).includes(envelope.method) || !(SUPPORTED as readonly string[]).includes(envelope.method)) {
    return unavailable(envelope.id, envelope.method, "unsupported", "Datahub method is unavailable in snapshot mode.");
  }
  return success(envelope.id, envelope.method, await query(envelope.method, envelope.params, env));
}

function success(id: unknown, method: string, data: unknown) {
  return { protocol: PROTOCOL, id: id ?? null, method, ok: true, data, availability: { state: "complete", missing: [] }, error: null };
}

function unavailable(id: unknown, method: string, reason: string, message: string) {
  return { protocol: PROTOCOL, id: id ?? null, method, ok: false, data: null,
    availability: { state: "unsupported", missing: [{ field: "$", reason }] },
    error: { code: reason, message } };
}

async function read(env: Env, path: string): Promise<unknown> {
  const response = await env.ASSETS.fetch(new Request(`https://snapshot.internal/_snapshot/${path}`));
  if (response.status !== 200 || !response.headers.get("content-type")?.includes("application/json")) {
    throw new RequestError(404, "not_found", "Not found.");
  }
  return readJson(response.body, MAX_FILE_BYTES, "Snapshot");
}

async function readJson(stream: ReadableStream<Uint8Array> | null, limit: number, label: string): Promise<unknown> {
  if (!stream) throw new RequestError(400, "invalid_json", `${label} is missing.`);
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let bytes = 0;
  let value = "";
  try {
    while (true) {
      const { done, value: chunk } = await reader.read();
      if (done) break;
      bytes += chunk.byteLength;
      if (bytes > limit) { await reader.cancel(); throw new RequestError(413, "request_too_large", `${label} exceeds byte limit.`); }
      value += decoder.decode(chunk, { stream: true });
    }
    try { return JSON.parse(value + decoder.decode()); }
    catch { throw new RequestError(400, "invalid_json", `${label} is not valid JSON.`); }
  } finally { reader.releaseLock(); }
}

function isRow(value: unknown): value is Row { return !!value && typeof value === "object" && !Array.isArray(value); }

function exact(value: Row, allowed: string[]) {
  if (Object.keys(value).some(key => !allowed.includes(key))) throw new RequestError(400, "invalid_params", "Request contains unknown fields.");
}

function params(value: Params, allowed: string[]): Params {
  exact(value, allowed);
  return value;
}

function integer(value: unknown, fallback: number, min: number, max: number): number {
  if (value === null || value === undefined) return fallback;
  if (!Number.isSafeInteger(value) || (value as number) < min || (value as number) > max) throw new RequestError(400, "invalid_params", "Invalid integer.");
  return value as number;
}

function id(value: unknown): string {
  if (typeof value !== "string" || !UUID.test(value)) throw new RequestError(400, "invalid_params", "Invalid identifier.");
  return value.toLowerCase();
}

function optionalText(value: unknown, max: number): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== "string" || !value.length || value.length > max) throw new RequestError(400, "invalid_params", "Invalid parameter.");
  return value;
}

function page(rows: Row[], values: Params, revision: number) {
  const limit = integer(values.limit, 50, 1, 200);
  let offset = 0;
  if (values.cursor !== null && values.cursor !== undefined) {
    if (typeof values.cursor !== "string") throw new RequestError(400, "invalid_cursor", "Invalid cursor.");
    let decoded;
    try {
      if (values.cursor.length > 4096 || !/^[\w-]+={0,2}$/.test(values.cursor)) throw new Error();
      decoded = JSON.parse(atob(values.cursor.replace(/-/g, "+").replace(/_/g, "/")));
      if (!decoded || Object.keys(decoded).sort().join() !== "offset,revision" || !Number.isSafeInteger(decoded.offset) || decoded.offset < 0 || decoded.offset > 100000 || !Number.isSafeInteger(decoded.revision) || decoded.revision < 0) throw new Error();
    } catch { throw new RequestError(400, "invalid_cursor", "Invalid cursor."); }
    if (decoded.revision !== revision) throw new RequestError(409, "snapshot_conflict", "Cursor snapshot is no longer current.");
    offset = decoded.offset;
  }
  const items = rows.slice(offset, offset + limit);
  const next = offset + items.length;
  const nextCursor = next < rows.length ? btoa(JSON.stringify({ revision, offset: next })).replace(/=/g, "").replace(/\+/g, "-").replace(/\//g, "_") : null;
  return { items, page: { revision, next_cursor: nextCursor, has_more: nextCursor !== null } };
}

export async function query(method: string, raw: Params, env: Env): Promise<unknown> {
  switch (method) {
    case "datahub.snapshot": params(raw, []); return read(env, "snapshot.json");
    case "datahub.changes": {
      const values = params(raw, ["after_revision"]);
      if (values.after_revision === null || values.after_revision === undefined) throw new RequestError(400, "invalid_params", "Missing revision.");
      const after = integer(values.after_revision, 0, 0, Number.MAX_SAFE_INTEGER);
      const changes = await read(env, "changes.json") as Row;
      const changed = after !== changes.to_revision;
      return { ...changes, from_revision: after, reset_required: changed, invalidations: changed ? ["sessions", "projects", "session-tree", "session-graph"] : [] };
    }
    case "projects":
    case "sessions": {
      const sessions = method === "sessions";
      const values = params(raw, sessions ? ["limit", "cursor", "agent_vendor", "project_name", "since_days"] : ["limit", "cursor", "agent_vendor"]);
      const days = integer(values.since_days, 7, 1, 7);
      const vendor = optionalText(values.agent_vendor, 64);
      const project = optionalText(values.project_name, 256);
      let rows = await read(env, sessions ? `sessions-${days}.json` : "projects.json") as Row[];
      if (vendor) rows = rows.filter(row => Array.isArray(row.vendors) && row.vendors.includes(vendor));
      if (project) rows = rows.filter(row => row.project === project);
      const snapshot = await read(env, "snapshot.json") as { revision: number };
      return page(rows, values, snapshot.revision);
    }
    case "session.graph":
    case "session.tree": {
      const values = params(raw, ["session_id"]);
      const session = id(values.session_id);
      return read(env, `${method === "session.tree" ? "trees" : "graphs"}/${session}.json`);
    }
    case "session.items": {
      const values = params(raw, ["item_ids", "include_content", "turn_id"]);
      if (values.include_content === true) throw new RequestError(404, "not_found", "Not found.");
      if (values.include_content !== null && values.include_content !== undefined && values.include_content !== false) throw new RequestError(400, "invalid_params", "Invalid parameter.");
      if (!Array.isArray(values.item_ids) || values.item_ids.length < 1 || values.item_ids.length > 200) throw new RequestError(400, "invalid_params", "Item identifiers are required.");
      const ids = values.item_ids.map(id);
      const turn = values.turn_id === null || values.turn_id === undefined ? null : id(values.turn_id);
      const buckets = new Map<string, Record<string, Row>>();
      for (const prefix of new Set(ids.map(value => value.slice(0, 1)))) buckets.set(prefix, await read(env, `items/${prefix}.json`) as Record<string, Row>);
      return ids.map(itemId => {
        const item = buckets.get(itemId.slice(0, 1))?.[itemId];
        if (!item || (turn && item.turn_id !== turn)) throw new RequestError(404, "not_found", "Not found.");
        return item;
      });
    }
    default: throw new RequestError(404, "unsupported", "Datahub method is unavailable in snapshot mode.");
  }
}
