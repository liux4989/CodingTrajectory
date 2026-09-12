import { configurationFor, verifyAccess, withSecurityHeaders } from "./access";

type Row = Record<string, unknown>;
type Params = Record<string, unknown>;
const PROTOCOL = "ct.datahub.v1";
const ENDPOINT = "/api/datahub/query";
const MAX_REQUEST_BYTES = 1024 * 1024;
// Vite emits content-hashed files under /assets; same scheme as the local
// Python server (_FINGERPRINTED_ASSET in datahub_plugin/serving/server.py).
const FINGERPRINTED_ASSET = /\/assets\/[^/]+-[A-Za-z0-9_-]{8,}\.[^.]+$/;
export const UNSUPPORTED = ["overview", "today", "project.detail", "sessions.timeline", "session.context-window", "session.evidence-timeline", "session.events", "model-usage", "token-efficiency.project", "code-time.report", "code-time.forecasts", "code-time.calibration", "datahub.refresh"] as const;

class RequestError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}

export async function handle(request: Request, env: Env, queryDispatch: (envelope: QueryEnvelope, env: Env) => Promise<unknown>): Promise<Response> {
    let response: Response;
    let cacheControl = "no-store";
    try {
      if (!configurationFor(env)) throw new RequestError(503, "unconfigured", "Datahub is not configured.");
      if (!await verifyAccess(request, env)) throw new RequestError(403, "authentication_required", "Cloudflare Access authentication required.");
      const url = new URL(request.url);
      // Never expose stale private export assets from a reused build directory.
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
        cacheControl = staticCacheControl(url.pathname);
      }
    } catch (error) {
      const fault = error instanceof RequestError ? error : new RequestError(503, "unavailable", "Datahub is unavailable.");
      response = Response.json({ protocol: PROTOCOL, id: null, method: null, ok: false, data: null,
        availability: { state: "unavailable", missing: [{ field: "$", reason: fault.code }] },
        error: { code: fault.code, message: fault.message } }, { status: fault.status });
    }
    return withSecurityHeaders(response, env.WORKER_VERSION?.id, cacheControl);
}

// Fingerprinted bundles never change under a URL, so let the browser cache
// them forever; every other static response may be the SPA shell and must
// revalidate. `private` keeps Access-gated content out of shared caches.
function staticCacheControl(pathname: string): string {
  return FINGERPRINTED_ASSET.test(pathname)
    ? "private, max-age=31536000, immutable"
    : "private, no-cache";
}

type QueryEnvelope = { protocol: string; id: unknown; method: string; params: Params };

async function requestEnvelope(request: Request): Promise<QueryEnvelope> {
  const value = await readJson(request.body, MAX_REQUEST_BYTES, "Request body");
  if (!isRow(value)) throw new RequestError(400, "invalid_request", "Request body must be an object.");
  exact(value, ["protocol", "id", "method", "params"]);
  if (value.protocol !== PROTOCOL) throw new RequestError(400, "invalid_protocol", `Protocol must be ${PROTOCOL}.`);
  if (typeof value.method !== "string" || !value.method) throw new RequestError(400, "invalid_method", "Method is required.");
  if (!isRow(value.params)) throw new RequestError(400, "invalid_params", "Params must be an object.");
  return { protocol: PROTOCOL, id: value.id ?? null, method: value.method, params: value.params };
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
