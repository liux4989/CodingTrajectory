import { configurationFor, verifyAccess, withSecurityHeaders } from "./index";

type SnapshotEnv = Pick<Env, "ASSETS" | "CF_ACCESS_TEAM_DOMAIN" | "CF_ACCESS_AUD">;
type Row = Record<string, unknown>;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const MAX_FILE_BYTES = 20 * 1024 * 1024;

class RequestError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export default {
  async fetch(request, env): Promise<Response> {
    let response: Response;
    try {
      if (!configurationFor(env)) throw new RequestError(503, "Datahub is not configured.");
      if (!await verifyAccess(request, env)) throw new RequestError(403, "Cloudflare Access authentication required.");
      const url = new URL(request.url);
      if (url.pathname.startsWith("/_snapshot")) throw new RequestError(404, "Not found.");
      if (url.pathname.startsWith("/api/")) {
        if (request.method !== "GET") throw new RequestError(404, "Not found.");
        response = Response.json(await api(url, env));
      } else {
        if (!["GET", "HEAD"].includes(request.method)) throw new RequestError(404, "Not found.");
        response = await env.ASSETS.fetch(request);
      }
    } catch (error) {
      response = Response.json({ error: { message: error instanceof RequestError ? error.message : "Snapshot is unavailable." } },
        { status: error instanceof RequestError ? error.status : 503 });
    }
    // Every response is private, including direct navigation and error responses.
    return withSecurityHeaders(response, true);
  },
} satisfies ExportedHandler<SnapshotEnv>;

async function read(env: SnapshotEnv, path: string): Promise<unknown> {
  const response = await env.ASSETS.fetch(new Request(`https://snapshot.internal/_snapshot/${path}`));
  if (response.status !== 200 || !response.headers.get("content-type")?.includes("application/json")) {
    throw new RequestError(404, "Not found.");
  }
  if (!response.body) throw new Error("Missing snapshot body");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let bytes = 0;
  let text = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > MAX_FILE_BYTES) {
        await reader.cancel();
        throw new Error("Snapshot exceeds byte limit");
      }
      text += decoder.decode(value, { stream: true });
    }
    return JSON.parse(text + decoder.decode());
  } finally { reader.releaseLock(); }
}

function query(url: URL, allowed: string[]): URLSearchParams {
  const seen = new Set<string>();
  url.searchParams.forEach((_value, key) => {
    if (!allowed.includes(key) || seen.has(key)) throw new RequestError(400, "Invalid query.");
    seen.add(key);
  });
  return url.searchParams;
}

function integer(value: string | null, fallback: number, min: number, max: number): number {
  if (value === null) return fallback;
  if (!/^\d+$/.test(value)) throw new RequestError(400, "Invalid integer.");
  const result = Number(value);
  if (!Number.isSafeInteger(result) || result < min || result > max) throw new RequestError(400, "Invalid integer.");
  return result;
}

function id(value: string | null): string {
  if (!value || !UUID.test(value)) throw new RequestError(400, "Invalid identifier.");
  return value.toLowerCase();
}

function optionalText(params: URLSearchParams, key: string, max: number): string | null {
  const value = params.get(key);
  if (value !== null && (!value.length || value.length > max)) throw new RequestError(400, "Invalid query.");
  return value;
}

function page(rows: Row[], params: URLSearchParams, revision: number) {
  const limit = integer(params.get("limit"), 50, 1, 200);
  let offset = 0;
  const cursor = params.get("cursor");
  if (cursor !== null) {
    let decoded;
    try {
      if (cursor.length > 4096 || !/^[\w-]+={0,2}$/.test(cursor)) throw new Error();
      decoded = JSON.parse(atob(cursor.replace(/-/g, "+").replace(/_/g, "/")));
      if (!decoded || Object.keys(decoded).sort().join() !== "offset,revision" ||
          !Number.isSafeInteger(decoded.offset) || decoded.offset < 0 || decoded.offset > 100000 ||
          !Number.isSafeInteger(decoded.revision) || decoded.revision < 0) throw new Error();
    } catch { throw new RequestError(400, "Invalid cursor."); }
    if (decoded.revision !== revision) throw new RequestError(409, "Cursor snapshot is no longer current.");
    offset = decoded.offset;
  }
  const items = rows.slice(offset, offset + limit);
  const next = offset + items.length;
  const nextCursor = next < rows.length ? btoa(JSON.stringify({ revision, offset: next })).replace(/=/g, "").replace(/\+/g, "-").replace(/\//g, "_") : null;
  return { items, page: { revision, next_cursor: nextCursor, has_more: nextCursor !== null } };
}

export async function api(url: URL, env: SnapshotEnv): Promise<unknown> {
  switch (url.pathname) {
    case "/api/datahub/snapshot":
      query(url, []);
      return read(env, "snapshot.json");
    case "/api/datahub/changes": {
      const params = query(url, ["after_revision"]);
      if (!params.has("after_revision")) throw new RequestError(400, "Missing revision.");
      const after = integer(params.get("after_revision"), 0, 0, Number.MAX_SAFE_INTEGER);
      const changes = await read(env, "changes.json") as Row;
      const changed = after !== changes.to_revision;
      return { ...changes, from_revision: after, reset_required: changed,
        invalidations: changed ? ["sessions", "projects", "session-tree", "session-graph"] : [] };
    }
    case "/api/projects":
    case "/api/sessions": {
      const sessions = url.pathname === "/api/sessions";
      const params = query(url, sessions ? ["limit", "cursor", "agent_vendor", "project_name", "since_days"] : ["limit", "cursor", "agent_vendor"]);
      const days = integer(params.get("since_days"), 7, 1, 7);
      const vendor = optionalText(params, "agent_vendor", 64);
      const project = optionalText(params, "project_name", 256);
      let rows = await read(env, sessions ? `sessions-${days}.json` : "projects.json") as Row[];
      if (vendor) rows = rows.filter(row => Array.isArray(row.vendors) && row.vendors.includes(vendor));
      if (project) rows = rows.filter(row => row.project === project);
      const snapshot = await read(env, "snapshot.json") as { revision: number };
      return page(rows, params, snapshot.revision);
    }
    case "/api/sessions/graph":
    case "/api/sessions/tree": {
      const params = query(url, ["session_id"]);
      const session = id(params.get("session_id"));
      return read(env, `${url.pathname.endsWith("/tree") ? "trees" : "graphs"}/${session}.json`);
    }
    case "/api/sessions/items": {
      const params = query(url, ["item_ids", "include_content", "turn_id"]);
      const content = params.get("include_content")?.toLowerCase();
      if (content && ["true", "1", "yes", "on"].includes(content)) throw new RequestError(404, "Not found.");
      if (content !== undefined && !["false", "0", "no", "off"].includes(content)) throw new RequestError(400, "Invalid query.");
      const ids = (params.get("item_ids") ?? "").split(",").map(value => id(value.trim()));
      if (ids.length > 200) throw new RequestError(400, "Too many items.");
      const turn = params.has("turn_id") ? id(params.get("turn_id")) : null;
      const buckets = new Map<string, Record<string, Row>>();
      // At most one read per bucket, with no unbounded request fan-out.
      for (const prefix of new Set(ids.map(value => value.slice(0, 1)))) {
        buckets.set(prefix, await read(env, `items/${prefix}.json`) as Record<string, Row>);
      }
      return ids.map(itemId => {
        const item = buckets.get(itemId.slice(0, 1))?.[itemId];
        if (!item || (turn && item.turn_id !== turn)) throw new RequestError(404, "Not found.");
        return item;
      });
    }
    default: throw new RequestError(404, "Not found.");
  }
}
