import { DIGEST, integer, Json, object, Principal, requireThat, stable, State, text, timestamp, uuid } from "./shared";
const now = () => new Date().toISOString();
function predictionId(value: unknown): string {
  const raw = text(value, 36);
  return uuid(/^[a-f0-9]{32}$/i.test(raw) ? raw.replace(/^(.{8})(.{4})(.{4})(.{4})(.{12})$/, "$1-$2-$3-$4-$5") : raw);
}
function plan(value: unknown): Json {
  const p = object(value), record = object(p.record);
  requireThat(DIGEST.test(p.idempotency_key) && record.idempotency_key === p.idempotency_key, "invalid_plan_identity");
  predictionId(record.prediction_id); text(p.prompt, 256000); timestamp(record.issued_at); object(record.estimator);
  requireThat(["prospective_unbound", "prospective_bound", "historical_backcast"].includes(record.forecast_kind), "invalid_forecast_kind");
  return p;
}
function read(state: State, id: string, sequence: number): Json | null {
  const created = state.get("forecast", predictionId(id), sequence);
  if (!created) return null;
  const record = created.record, binding = state.get("binding", created.id, sequence);
  const comparison = state.get("comparison", created.id, sequence)?.comparison ?? null;
  const prior = state.all("forecast", sequence).some(row => row.id !== created.id && row.record.turn_id === record.turn_id
    && stable(row.record.estimator) === stable(record.estimator) && `${row.record.issued_at}:${row.id}` < `${record.issued_at}:${created.id}`);
  return { ...record, ...binding, role: prior ? "diagnostic" : "primary", comparison,
    status: comparison && comparison.exclusion !== "missing_terminal_time" ? "compared" : record.forecast_kind === "prospective_unbound" && !binding ? "unbound" : "uncompared" };
}
function failure(reason: string, detail: string, state = "not_applicable"): Json { return { state, reason, detail }; }
function queue(state: State, p: Json, key: string, principal: Principal, snapshot: number, parent: string | null = null): Json {
  const prior = state.get("job_identity", key);
  if (prior) return state.get("job", prior.id)!;
  const id = crypto.randomUUID(), sequence = state.next();
  const job = { id, plan: p, requested_by: principal.agent_id, parent, snapshot_sequence: snapshot,
    status: "pending", attempts: 0, created_at: now(), available_at: now() };
  state.put("job", id, job, sequence); state.put("job_identity", key, { id }, sequence);
  return job;
}
function status(state: State, id: string): Json {
  const parent = state.get("backfill", uuid(id));
  requireThat(parent, "backfill_not_found", 404);
  const jobs = state.all("job").filter(row => row.parent === parent.id);
  const count = (predicate: (job: Json) => boolean) => jobs.filter(predicate).length;
  const active = count(row => ["pending", "leased", "failed"].includes(row.status));
  const succeeded = count(row => row.completion?.outcome === "succeeded"), skipped = count(row => row.completion?.outcome === "skipped_existing"), permanent = count(row => row.status === "cancelled");
  if (!active && !parent.finished_at) { parent.finished_at = now(); state.put("backfill", parent.id, parent, state.next()); }
  return { job: { job_id: parent.id, status: active ? "running" : "completed", created_at: parent.created_at,
    finished_at: active ? null : parent.finished_at, spec: parent.spec,
    counts: { eligible: jobs.length, succeeded, skipped_existing: skipped, retryable_failed: count(row => row.status === "failed"), permanent_failed: permanent,
      uncompared: 0, excluded: parent.excluded, processed: succeeded + skipped + permanent },
    stop_reason: active ? null : jobs.length >= parent.spec.max_forecasts ? "budget_exhausted:max_forecasts" : "inventory_exhausted" } };
}
export function estimation(state: State, method: string, request: Json, principal: Principal): Json {
  const sequence = state.pin(request.snapshot_sequence);
  if (method === "ct_estimate_predict") {
    const p = plan(request.plan), prior = state.get("forecast_identity", p.idempotency_key, sequence);
    if (prior) return { forecast: read(state, prior.id, sequence), failure: null, reused_existing: true };
    const job = queue(state, p, stable([principal.agent_id, p.idempotency_key]), principal, sequence);
    return { forecast: null, reused_existing: false, failure: failure(job.status === "cancelled" ? "provider_error" : "forecast_pending", job.id, job.status === "cancelled" ? "permanent_failed" : "retryable_failed") };
  }
  if (method === "ct_estimate_get") return { forecast: read(state, request.prediction_id, sequence) };
  if (["ct_estimate_bind", "ct_estimate_compare"].includes(method)) {
    const id = predictionId(request.prediction_id), current = read(state, id, state.head());
    if (!current) return { forecast: null, failure: failure("forecast_not_found", id) };
    if (method === "ct_estimate_bind") {
      if (current.forecast_kind !== "prospective_unbound" || current.bound_at) return { forecast: current, failure: failure(current.bound_at ? "already_bound" : "not_unbound", id) };
      const binding = object(request.binding);
      requireThat(Date.parse(timestamp(current.issued_at)) < Date.parse(timestamp(binding.target_execution_started_at)), "binding_window_missed", 409);
      predictionId(binding.turn_id); state.put("binding", id, binding, state.next());
    }
    if (request.comparison && stable(current.comparison) !== stable(request.comparison)) state.put("comparison", id, { comparison: object(request.comparison) }, state.next());
    return { forecast: read(state, id, state.head()), failure: null };
  }
  if (["ct_estimate_list", "ct_estimate_calibration"].includes(method)) {
    const items = state.all("forecast", sequence).map(row => read(state, row.id, sequence)!).filter(row =>
      ["forecast_kind", "project_name", "status"].every(key => request[key] == null || row[key] === request[key])
      && ["agent_vendor", "harness_name", "harness_version", "model", "effort"].every(key => request[`target_${key}`] == null || row.target?.[key] === request[`target_${key}`])
      && ["provider", "model", "effort", "prompt_version", "schema_version"].every(key => request[`estimator_${key}`] == null || row.estimator?.[key] === request[`estimator_${key}`])
    ).sort((a, b) => b.issued_at.localeCompare(a.issued_at) || b.prediction_id.localeCompare(a.prediction_id));
    return method === "ct_estimate_calibration" ? { records: items } : { items: items.slice(0, integer(request.limit ?? 50, 1, 200)) };
  }
  if (method === "ct_estimate_backfill_start") {
    const spec = object(request.spec); integer(spec.max_forecasts ?? 25, 1, 1000); integer(spec.concurrency ?? 4, 1, 32);
    requireThat(Array.isArray(request.plans) && request.plans.length <= Math.min(1000, spec.max_forecasts ?? 25), "invalid_backfill_plans");
    const id = request.job_id ? uuid(request.job_id) : crypto.randomUUID(), prior = state.get("backfill", id);
    if (prior) {
      const strip = (value: Json) => { const copy = { ...value }; delete copy.concurrency; return copy; };
      requireThat(stable(strip(prior.spec)) === stable(strip(spec)), "backfill_resume_conflict", 409);
    } else {
      const plans = request.plans.map(plan);
      requireThat(new Set(plans.map((p: Json) => p.idempotency_key)).size === plans.length, "duplicate_backfill_plan");
      state.put("backfill", id, { id, spec, excluded: request.excluded ?? {}, created_at: now() }, state.next());
      for (const p of plans) queue(state, p, stable([id, p.idempotency_key]), principal, sequence, id);
    }
    return status(state, id);
  }
  if (method === "ct_estimate_backfill_status") return status(state, request.job_id);
  if (method === "ct_estimator_claim") {
    const worker = text(request.worker_id, 512), lease = integer(request.lease_seconds ?? 300, 1, 3600);
    const jobs = state.all("job").sort((a, b) => a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id));
    for (const job of jobs) {
      if (!["pending", "failed", "leased"].includes(job.status)) continue;
      if (job.status === "leased" ? Date.parse(job.lease_expires_at) > Date.now() : Date.parse(job.available_at) > Date.now()) continue;
      if (job.attempts >= 2) { state.put("job", job.id, { ...job, status: "cancelled", failure_code: "attempt_budget_exhausted" }, state.next()); continue; }
      if (job.parent) {
        const parent = state.get("backfill", job.parent)!;
        if (jobs.filter(row => row.parent === job.parent && row.status === "leased" && Date.parse(row.lease_expires_at) > Date.now()).length >= (parent.spec.concurrency ?? 4)) continue;
      }
      const attempt = job.attempts + 1;
      state.put("job", job.id, { ...job, status: "leased", attempts: attempt, worker_id: worker, worker_agent: principal.agent_id,
        lease_expires_at: new Date(Date.now() + lease * 1000).toISOString() }, state.next());
      return { workspace_id: request.workspace_id, job_id: job.id, attempt_number: attempt, worker_id: worker,
        prompt: job.plan.prompt, model: job.plan.record.estimator.model ?? null, effort: job.plan.record.estimator.effort ?? null };
    }
    return {};
  }
  if (["ct_estimator_complete", "ct_estimator_fail"].includes(method)) {
    const id = uuid(request.job_id), job = state.get("job", id);
    requireThat(job && job.worker_id === request.worker_id && job.worker_agent === principal.agent_id && job.attempts === request.attempt_number, "job_lease_conflict", 409);
    if (method === "ct_estimator_complete" && job.status === "completed") {
      requireThat(job.completed_p50 === request.p50_minutes && job.completed_p80 === request.p80_minutes, "completion_conflict", 409); return job.completion;
    }
    requireThat(job.status === "leased" && Date.parse(job.lease_expires_at) > Date.now(), "job_lease_expired", 409);
    if (method === "ct_estimator_fail") {
      const retry = integer(request.retry_seconds ?? 1, 1, 3600); requireThat(typeof request.permanent === "boolean", "invalid_failure");
      const updated = { ...job, status: request.permanent || job.attempts >= 2 ? "cancelled" : "failed", available_at: new Date(Date.now() + retry * 1000).toISOString() };
      // Provider error bodies may contain private prompts. Persist only the failure state.
      state.put("job", id, updated, state.next()); return { status: updated.status, available_at: updated.available_at };
    }
    const p50 = request.p50_minutes, p80 = request.p80_minutes;
    requireThat(typeof p50 === "number" && typeof p80 === "number" && Number.isFinite(p50) && Number.isFinite(p80) && p50 >= 1 / 60 && p80 >= p50 && p80 <= 10080, "invalid_estimate");
    const existing = state.get("forecast_identity", job.plan.idempotency_key), forecastId = existing?.id ?? predictionId(job.plan.record.prediction_id), commit = state.next();
    if (!existing) {
      requireThat(!state.get("forecast", forecastId), "prediction_identity_conflict", 409);
      state.put("forecast", forecastId, { id: forecastId, record: { ...job.plan.record, p50_minutes: p50, p80_minutes: p80 } }, commit);
      state.put("forecast_identity", job.plan.idempotency_key, { id: forecastId }, commit);
      if (job.plan.comparison) state.put("comparison", forecastId, { comparison: job.plan.comparison }, commit);
    }
    const completion = { prediction_id: forecastId, outcome: existing ? "skipped_existing" : "succeeded" };
    state.put("job", id, { ...job, status: "completed", completion, completed_p50: p50, completed_p80: p80 }, commit); return completion;
  }
  requireThat(false, "not_found", 404);
}
