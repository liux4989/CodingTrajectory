import { authorityFailure, bounded, DIGEST, digest, Fault, fields, Json, object, Principal, requireThat, text, uuid } from "./shared";
import { artifactKey } from "./artifacts";
import { publicationIndex, readCursor, servePrepared, validateApi } from "./prepared-api";
export { Workspace } from "./workspace";

const COLLECT = new Set(["ct_project_register", "ct_collector_register_source", "ct_collector_recover",
  "ct_collector_publish_observation", "ct_collector_publish_artifacts", "ct_collector_heartbeat", "ct_collector_publish_living_observation"]);
const READ = new Set(["ct_workspace_snapshot", "ct_legacy_fact_cleanup_status", "ct_artifact_manifest", "ct_artifact_read", "ct_project_inventory_snapshot", "ct_remote_living"]);
const REPLACE = "ct_workspace_replace";
const PROTOCOL = "ct.core.v1";
const MAX_ARTIFACT_BYTES = 16 * 1024 * 1024;

function responseHeaders(env: Env): Record<string, string> {
  return { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    ...(env.WORKER_VERSION?.id ? { "X-CT-Worker-Version": env.WORKER_VERSION.id } : {}) };
}

export default {
  async fetch(request, env): Promise<Response> {
    let requestId: unknown = null;
    let method: unknown = null;
    let protocol = new URL(request.url).pathname === "/v1/api" ? "ct.api.v1" : PROTOCOL;
    let methodVersion: unknown = null;
    try {
      const token = request.headers.get("authorization")?.match(/^Bearer ([A-Za-z0-9_-]{32,256})$/)?.[1];
      requireThat(token, "authentication_required", 401);
      let registry;
      try { registry = object(JSON.parse(env.CT_PRINCIPALS)); }
      catch { throw new Fault(503, "authentication_unavailable"); }
      const raw = registry[await digest(token)];
      requireThat(raw, "authentication_required", 401);
      const principal: Principal = { workspace_id: uuid(raw.workspace_id), agent_id: uuid(raw.agent_id), roles: raw.roles };
      requireThat(Array.isArray(principal.roles) && principal.roles.every(role => ["read", "collect", "owner"].includes(role)), "invalid_principal", 503);
      const url = new URL(request.url);
      if (url.pathname === "/v1/api") protocol = "ct.api.v1";
      const artifactUpload = url.pathname.match(/^\/v1\/artifacts\/(facts|summary|api)\/([0-9a-f]{64})$/);
      if (request.method === "PUT" && artifactUpload && url.search === "") {
        requireThat(principal.roles.includes("collect") || principal.roles.includes("owner"), "capability_required", 403);
        const [, kind, sha256] = artifactUpload;
        const body = await bounded(request.body, MAX_ARTIFACT_BYTES);
        requireThat(await digest(body) === sha256, "artifact_digest_mismatch");
        let value;
        try { value = object(JSON.parse(new TextDecoder().decode(body))); }
        catch (error) { if (error instanceof Fault) throw error; throw new Fault(400, "invalid_artifact_json"); }
        requireThat(kind === "facts"
          ? value.schema_version === "ct.published_facts.v2"
          : kind === "summary" ? value.schema_version === "ct.prepared-summary.v2"
          : value.schema_version === "ct.prepared-api.v1" && body.length <= 448 * 1024, "artifact_schema_mismatch");
        const index = kind === "api" ? publicationIndex(value, body.length) : null;
        const key = artifactKey(principal.workspace_id, kind, sha256);
        const workspace = env.WORKSPACES.getByName(principal.workspace_id);
        const claim = object(JSON.parse(await workspace.invoke(
          "ct_internal_artifact_claim",
          JSON.stringify({ request: { workspace_id: principal.workspace_id, kind, sha256 } }),
          JSON.stringify(principal),
        )));
        requireThat(claim.status === 200, "artifact_claim_failed", 503);
        const prior = await env.ARTIFACTS.get(key);
        if (prior) {
          requireThat(prior.size === body.length && prior.customMetadata?.sha256 === sha256
            && prior.customMetadata?.workspace_id === principal.workspace_id && prior.customMetadata?.kind === kind,
            "artifact_identity_conflict", 409);
          requireThat(await digest(new Uint8Array(await prior.arrayBuffer())) === sha256,
            "artifact_identity_conflict", 409);
        } else {
          await env.ARTIFACTS.put(key, body, { customMetadata: {
            workspace_id: principal.workspace_id, kind, sha256,
          }, httpMetadata: { contentType: "application/json" } });
        }
        const completion = object(JSON.parse(await workspace.invoke(
          "ct_internal_artifact_complete", JSON.stringify({ request: {
            workspace_id: principal.workspace_id, kind, sha256, bytes: body.length, index, token: claim.body.token,
          } }), JSON.stringify(principal),
        )));
        requireThat(completion.status === 200, completion.body?.error?.code ?? "artifact_completion_failed", completion.status);
        const result: Json = { ok: true, sha256, bytes: body.length };
        // Local benchmark subclasses may annotate the internal claim response.
        // Production Durable Objects never emit this field.
        if (claim.body?.__benchmark) result.__benchmark = claim.body.__benchmark;
        if (completion.body?.__benchmark) result.__benchmark_completion = completion.body.__benchmark;
        return Response.json(result, { headers: responseHeaders(env) });
      }
      if (request.method === "POST" && url.pathname === "/v1/api" && url.search === "") {
        requireThat(principal.roles.includes("read") || principal.roles.includes("owner"), "capability_required", 403);
        let message;
        try { message = object(JSON.parse(new TextDecoder().decode(await bounded(request.body, 64 * 1024)))); }
        catch (error) { if (error instanceof Fault) throw error; throw new Fault(400, "invalid_json"); }
        requestId = message.id ?? null; method = message.method; methodVersion = message.method_version;
        validateApi(message);
        const workspace = env.WORKSPACES.getByName(principal.workspace_id);
        let result, identity = null;
        if (method === "living.sessions") {
          const reply = object(JSON.parse(await workspace.invoke("ct_remote_living", JSON.stringify({ request: {
            workspace_id: principal.workspace_id, calls: [{ method, params: message.params }],
          } }), JSON.stringify(principal))));
          requireThat(reply.status === 200, reply.body?.error?.code ?? "method_failed", reply.status);
          result = reply.body.results[0].result;
        } else {
          const cursor = message.params.cursor ? await readCursor(message.params.cursor, env.CT_CURSOR_KEY) : null;
          requireThat(!cursor || cursor.workspace_id === principal.workspace_id, "invalid_cursor");
          requireThat(!cursor || !message.params.view_manifest_sha256 || cursor.view_manifest_sha256 === message.params.view_manifest_sha256, "invalid_cursor");
          const reply = object(JSON.parse(await workspace.invoke("ct_internal_api_locator", JSON.stringify({ request: {
            workspace_id: principal.workspace_id, method, params: message.params,
            view_manifest_sha256: cursor?.view_manifest_sha256 ?? message.params.view_manifest_sha256,
          } }), JSON.stringify(principal))));
          requireThat(reply.status === 200, reply.body?.error?.code ?? "method_failed", reply.status);
          identity = reply.body.identity;
          result = await servePrepared(env, reply.body, message.method, message.params);
        }
        const envelope = { protocol, id: requestId, method, method_version: methodVersion, ok: true, data: result,
          availability: { state: "complete", missing: [] }, error: null,
          meta: { source: "remote", freshness: "authoritative", content_scope: "facts", identity } };
        const body = new TextEncoder().encode(JSON.stringify(envelope));
        requireThat(body.length <= 448 * 1024, "remote_result_too_large", 413);
        return new Response(body, { headers: { ...responseHeaders(env), "Content-Type": "application/json" } });
      }
      requireThat(request.method === "POST" && url.search === "" && url.pathname === "/v1/core", "not_found", 404);
      let message;
      let bodyBytes: Uint8Array;
      try {
        bodyBytes = await bounded(request.body);
        message = object(JSON.parse(new TextDecoder().decode(bodyBytes)));
      }
      catch (error) { if (error instanceof Fault) throw error; throw new Fault(400, "invalid_json"); }
      fields(message, ["protocol", "id", "method", "params", "idempotency_key", "request_sha256"], ["protocol", "method", "params"]);
      requireThat(message.protocol === PROTOCOL, "invalid_protocol");
      requestId = message.id ?? null;
      const methodName = text(message.method, 128);
      method = methodName;
      const role = methodName === "ct_connection_status" ? "authenticated" : methodName === REPLACE ? "owner" : COLLECT.has(methodName) ? "collect" : READ.has(methodName) ? "read" : null;
      requireThat(role, "not_found", 404);
      requireThat(role === "authenticated" || principal.roles.includes(role) || principal.roles.includes("owner"), "capability_required", 403);
      const body = object(message.params);
      requireThat(body.workspace_id === principal.workspace_id, "workspace_denied", 403);
      if (methodName === REPLACE) {
        requireThat(env.CT_REPLACEMENT_WORKSPACE_ID && env.CT_REPLACEMENT_EXPORT_SHA256,
          "workspace_replacement_unavailable", 503);
        const replacementWorkspace = uuid(env.CT_REPLACEMENT_WORKSPACE_ID);
        requireThat(DIGEST.test(env.CT_REPLACEMENT_EXPORT_SHA256), "workspace_replacement_invalid", 503);
        fields(body, ["workspace_id", "mode", "expected_export_sha256", "confirmation"],
          ["workspace_id", "mode", "expected_export_sha256", "confirmation"]);
        requireThat(body.workspace_id === replacementWorkspace, "workspace_replacement_target_denied", 403);
        requireThat(body.expected_export_sha256 === env.CT_REPLACEMENT_EXPORT_SHA256,
          "workspace_replacement_export_denied", 403);
        requireThat(["preview", "execute"].includes(body.mode), "workspace_replacement_mode_invalid");
        requireThat(body.confirmation
          === `${body.mode}:${replacementWorkspace}:${env.CT_REPLACEMENT_EXPORT_SHA256}`,
        "workspace_replacement_confirmation_required", 403);
      }
      if (methodName === "ct_connection_status") {
        fields(body, ["workspace_id"], ["workspace_id"]);
        return Response.json({ protocol: PROTOCOL, id: requestId, method: methodName, ok: true,
          data: { protocol: PROTOCOL, workspace_id: principal.workspace_id, agent_id: principal.agent_id, roles: principal.roles },
          availability: { state: "complete", missing: [] }, error: null },
          { headers: responseHeaders(env) });
      }
      if (COLLECT.has(methodName)) requireThat(body.agent_id === principal.agent_id, "agent_denied", 403);
      if (message.idempotency_key != null) text(message.idempotency_key, 512);
      const envelope: Json = { request: body };
      if (message.idempotency_key != null) envelope.idempotency_key = message.idempotency_key;
      if (message.request_sha256 != null) envelope.request_sha256 = message.request_sha256;
      const workspace = env.WORKSPACES.getByName(principal.workspace_id);
      const result = object(JSON.parse(await workspace.invoke(methodName, JSON.stringify(envelope), JSON.stringify(principal))));
      if (result.status >= 200 && result.status < 300 && result.body?.__artifact_key) {
        const locator = result.body;
        const instrumentation = locator.__benchmark;
        const artifact = await env.ARTIFACTS.get(locator.__artifact_key);
        requireThat(artifact, "artifact_object_missing", 503);
        const bytes = new Uint8Array(await artifact.arrayBuffer());
        requireThat(await digest(bytes) === locator.sha256, "artifact_object_corrupt", 503);
        try { result.body = object(JSON.parse(new TextDecoder().decode(bytes))); }
        catch { throw new Fault(503, "artifact_object_corrupt"); }
        // Local benchmark subclasses may annotate the locator. Production
        // Durable Objects never emit this field.
        if (instrumentation) result.body.__benchmark = instrumentation;
      }
      const ok = result.status >= 200 && result.status < 300;
      const failureCode = responseErrorCode(result.body);
      const response: Json = ok
        ? { protocol: PROTOCOL, id: requestId, method: methodName, ok: true, data: result.body,
            availability: { state: "complete", missing: [] }, error: null }
        : { protocol: PROTOCOL, id: requestId, method: methodName, ok: false, data: null,
            availability: { state: "unavailable", missing: [{ field: "$", reason: failureCode }] },
            error: { code: failureCode } };
      return Response.json(response, { status: result.status, headers: responseHeaders(env) });
    } catch (error) {
      const failure = authorityFailure(error, "worker");
      const code = failure.code;
      return Response.json({ protocol, id: requestId, method, method_version: methodVersion, ok: false, data: null,
        availability: { state: code === "unsupported_version" ? "unsupported" : "unavailable", missing: [{ field: "$", reason: code }] }, error: { code, message: code }, meta: null },
        { status: failure.status, headers: responseHeaders(env) });
    }
  },
} satisfies ExportedHandler<Env>;

function responseErrorCode(body: Json): string {
  const error = body.error;
  return error && typeof error.code === "string" ? error.code : "method_failed";
}
