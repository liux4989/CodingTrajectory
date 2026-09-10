import { bounded, digest, Fault, fields, Json, object, Principal, requireThat, text, uuid } from "./shared";
export { Workspace } from "./workspace";

const COLLECT = new Set(["ct_project_register", "ct_collector_register_source", "ct_collector_recover",
  "ct_collector_publish_observation", "ct_collector_stage_artifact_payload", "ct_collector_publish_artifacts",
  "ct_collector_heartbeat", "ct_collector_publish_living_observation"]);
const READ = new Set(["ct_workspace_snapshot", "ct_historical_snapshot", "ct_historical_artifacts",
  "ct_project_sessions_projection", "ct_project_inventory_snapshot", "ct_remote_living",
  "ct_estimate_get", "ct_estimate_list", "ct_estimate_calibration", "ct_estimate_backfill_status"]);
const ESTIMATE = new Set(["ct_estimate_predict", "ct_estimate_bind", "ct_estimate_compare", "ct_estimate_backfill_start"]);
const EXECUTE = new Set(["ct_estimator_claim", "ct_estimator_complete", "ct_estimator_fail"]);
const PROTOCOL = "ct.core.v1";

export default {
  async fetch(request, env): Promise<Response> {
    let requestId: unknown = null;
    let method: unknown = null;
    try {
      const token = request.headers.get("authorization")?.match(/^Bearer ([A-Za-z0-9_-]{32,256})$/)?.[1];
      requireThat(token, "authentication_required", 401);
      let registry;
      try { registry = object(JSON.parse(env.CT_PRINCIPALS)); }
      catch { throw new Fault(503, "authentication_unavailable"); }
      const raw = registry[await digest(token)];
      requireThat(raw, "authentication_required", 401);
      const principal: Principal = { workspace_id: uuid(raw.workspace_id), agent_id: uuid(raw.agent_id), roles: raw.roles };
      requireThat(Array.isArray(principal.roles) && principal.roles.every(role => ["read", "collect", "estimate", "estimate_worker", "owner"].includes(role)), "invalid_principal", 503);
      const url = new URL(request.url);
      requireThat(request.method === "POST" && url.search === "" && url.pathname === "/v1/core", "not_found", 404);
      let message;
      try { message = object(JSON.parse(new TextDecoder().decode(await bounded(request.body)))); }
      catch (error) { if (error instanceof Fault) throw error; throw new Fault(400, "invalid_json"); }
      fields(message, ["protocol", "id", "method", "params", "idempotency_key", "request_sha256"], ["protocol", "method", "params"]);
      requireThat(message.protocol === PROTOCOL, "invalid_protocol");
      requestId = message.id ?? null;
      const methodName = text(message.method, 128);
      method = methodName;
      const role = COLLECT.has(methodName) ? "collect" : READ.has(methodName) ? "read" : ESTIMATE.has(methodName) ? "estimate" : EXECUTE.has(methodName) ? "estimate_worker" : null;
      requireThat(role, "not_found", 404);
      requireThat(principal.roles.includes(role) || principal.roles.includes("owner"), "capability_required", 403);
      const body = object(message.params);
      if (methodName === "ct_estimator_claim" && !body.workspace_id) body.workspace_id = principal.workspace_id;
      requireThat(body.workspace_id === principal.workspace_id, "workspace_denied", 403);
      if (COLLECT.has(methodName)) requireThat(body.agent_id === principal.agent_id, "agent_denied", 403);
      if (message.idempotency_key != null) text(message.idempotency_key, 512);
      const envelope: Json = { request: body };
      if (message.idempotency_key != null) envelope.idempotency_key = message.idempotency_key;
      if (message.request_sha256 != null) envelope.request_sha256 = message.request_sha256;
      const encodedResult = await env.WORKSPACES.getByName(principal.workspace_id)
        .invoke(methodName, JSON.stringify(envelope), JSON.stringify(principal));
      const result = object(JSON.parse(encodedResult));
      const ok = result.status >= 200 && result.status < 300;
      const failureCode = responseErrorCode(result.body);
      const response: Json = ok
        ? { protocol: PROTOCOL, id: requestId, method: methodName, ok: true, data: result.body,
            availability: { state: "complete", missing: [] }, error: null }
        : { protocol: PROTOCOL, id: requestId, method: methodName, ok: false, data: null,
            availability: { state: "unavailable", missing: [{ field: "$", reason: failureCode }] },
            error: { code: failureCode } };
      return Response.json(response, { status: result.status, headers: { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" } });
    } catch (error) {
      const code = error instanceof Fault ? error.code : "authority_unavailable";
      return Response.json({ protocol: PROTOCOL, id: requestId, method, ok: false, data: null,
        availability: { state: "unavailable", missing: [{ field: "$", reason: code }] }, error: { code } },
        { status: error instanceof Fault ? error.status : 503, headers: { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" } });
    }
  },
} satisfies ExportedHandler<Env>;

function responseErrorCode(body: Json): string {
  const error = body.error;
  return error && typeof error.code === "string" ? error.code : "method_failed";
}
