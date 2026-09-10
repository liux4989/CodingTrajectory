import { bounded, digest, Fault, fields, object, Principal, requireThat, text, uuid } from "./shared";
export { Workspace } from "./workspace";

const COLLECT = new Set(["ct_project_register", "ct_collector_register_source", "ct_collector_recover",
  "ct_collector_publish_observation", "ct_collector_stage_artifact_payload", "ct_collector_publish_artifacts",
  "ct_collector_heartbeat", "ct_collector_publish_living_observation"]);
const READ = new Set(["ct_workspace_snapshot", "ct_historical_snapshot", "ct_historical_artifacts",
  "ct_project_sessions_projection", "ct_project_inventory_snapshot", "ct_remote_living",
  "ct_estimate_get", "ct_estimate_list", "ct_estimate_calibration", "ct_estimate_backfill_status"]);
const ESTIMATE = new Set(["ct_estimate_predict", "ct_estimate_bind", "ct_estimate_compare", "ct_estimate_backfill_start"]);
const EXECUTE = new Set(["ct_estimator_claim", "ct_estimator_complete", "ct_estimator_fail"]);

export default {
  async fetch(request, env): Promise<Response> {
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
      requireThat(request.method === "POST" && url.search === "" && /^\/rpc\/ct_[a-z_]+$/.test(url.pathname), "not_found", 404);
      const method = url.pathname.slice(5);
      const role = COLLECT.has(method) ? "collect" : READ.has(method) ? "read" : ESTIMATE.has(method) ? "estimate" : EXECUTE.has(method) ? "estimate_worker" : null;
      requireThat(role, "not_found", 404);
      requireThat(principal.roles.includes(role) || principal.roles.includes("owner"), "capability_required", 403);
      let envelope;
      try { envelope = object(JSON.parse(new TextDecoder().decode(await bounded(request.body)))); }
      catch (error) { if (error instanceof Fault) throw error; throw new Fault(400, "invalid_json"); }
      fields(envelope, ["request", "idempotency_key", "request_sha256"], ["request"]);
      const body = object(envelope.request);
      if (method === "ct_estimator_claim" && !body.workspace_id) body.workspace_id = principal.workspace_id;
      requireThat(body.workspace_id === principal.workspace_id, "workspace_denied", 403);
      if (COLLECT.has(method)) requireThat(body.agent_id === principal.agent_id, "agent_denied", 403);
      if (envelope.idempotency_key !== undefined) text(envelope.idempotency_key, 512);
      const result = await env.WORKSPACES.getByName(principal.workspace_id).rpc(method, envelope, principal);
      return Response.json(result.body, { status: result.status, headers: { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" } });
    } catch (error) {
      return Response.json({ error: { code: error instanceof Fault ? error.code : "authority_unavailable" } },
        { status: error instanceof Fault ? error.status : 503, headers: { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" } });
    }
  },
} satisfies ExportedHandler<Env>;
