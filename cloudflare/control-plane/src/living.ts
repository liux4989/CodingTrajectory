import { decode, encode, Fault, integer, Json, requireThat, stable, State, timestamp, validate } from "./shared";

export function livingWrite(state: State, method: string, request: Json): Json {
  const instance = request.agent_instance_id;
  const heartbeat = method === "ct_collector_heartbeat";
  const head = state.get("living_head", instance);
  requireThat(!head || head.agent_id === request.agent_id, "agent_instance_conflict", 403);
  if (!heartbeat) {
    requireThat(state.get("lease", instance), "agent_lease_required", 403);
    validate(request.kind === "living.events" ? "living_events_change" : "living_sessions_change", request.payload);
  }
  const key = `${instance}:${request.observation_sequence}`;
  const prior = state.get("living", key);
  if (prior) {
    requireThat(prior.method === method && stable(prior.request) === stable(request), "living_identity_conflict", 409);
    return prior.receipt;
  }
  requireThat(request.observation_sequence > (head?.observation_sequence ?? 0), "living_sequence_conflict", 409);
  const sequence = state.next();
  const receipt: Json = { committed_sequence: sequence };
  if (heartbeat) {
    receipt.lease_expires_at = new Date(Date.now() + request.lease_seconds * 1000).toISOString();
    state.put("lease", instance, { agent_id: request.agent_id, lease_expires_at: receipt.lease_expires_at }, sequence);
  }
  state.put("living", key, { method, request, receipt, sequence }, sequence);
  state.put("living_head", instance, { agent_id: request.agent_id, observation_sequence: request.observation_sequence }, sequence);
  return receipt;
}

function cursor(value: Json): string { return encode(new TextEncoder().encode(stable(value))); }
function parse(value: unknown): Json {
  requireThat(typeof value === "string" && value.length <= 4096, "invalid_cursor");
  try {
    const result = JSON.parse(new TextDecoder().decode(decode(value)));
    requireThat(result && result.version === 1 && ["watermark", "snapshot", "delta"].includes(result.kind), "invalid_cursor");
    return result;
  } catch { throw new Fault(400, "invalid_cursor"); }
}

export function livingRead(state: State, request: Json): Json {
  requireThat(Array.isArray(request.calls) && request.calls.length > 0 && request.calls.length <= 100, "invalid_living_batch");
  let sequence = state.pin(request.snapshot_sequence);
  let evaluatedAt = new Date().toISOString();
  let through: Json | undefined;
  for (const call of request.calls) {
    requireThat(["living.events", "living.sessions"].includes(call.method), "invalid_living_method");
    validate(call.method === "living.events" ? "living_events_request" : "living_sessions_request", call.params);
    if (call.params.through) {
      const value = parse(call.params.through);
      requireThat(value.kind === "watermark", "invalid_through_cursor");
      if (through) requireThat(value.sequence === through.sequence && value.evaluated_at === through.evaluated_at, "batch_snapshot_conflict");
      through = value;
    }
  }
  if (through) {
    sequence = state.pin(through.sequence);
    requireThat(request.snapshot_sequence == null || request.snapshot_sequence === sequence, "snapshot_conflict");
    evaluatedAt = timestamp(through.evaluated_at);
    requireThat(Date.parse(evaluatedAt) <= Date.now(), "future_cursor");
  }
  const observations = state.all("living", sequence);
  const fresh = new Set<string>();
  const instances = new Set<string>(observations.map(row => row.request.agent_instance_id));
  for (const instance of instances) {
    // Leases are versioned here, unlike the old mutable lease table. A pinned
    // lease remains reconstructable even when a later heartbeat is committed.
    const lease = state.get("lease", instance, sequence);
    if (lease && Date.parse(lease.lease_expires_at) > Date.parse(evaluatedAt)) fresh.add(instance);
  }
  const results = request.calls.map((call: Json) => {
    const params = call.params;
    const scope = call.method === "living.events" ? Object.fromEntries(Object.entries(params.scope ?? {}).filter(([, value]) => value != null)) : {};
    const binding = { version: 1, workspace_id: request.workspace_id, method: call.method, scope: stable(scope) };
    const matches = (value: Json) => requireThat(Object.entries(binding).every(([key, item]) => value[key] === item), "cursor_scope_conflict");
    if (params.through) { const value = parse(params.through); matches(value); }
    const watermark = cursor({ ...binding, kind: "watermark", sequence, evaluated_at: evaluatedAt });
    let pageKind = "snapshot";
    let base = 0;
    let position = 0;
    if (params.after) {
      const after = parse(params.after);
      matches(after);
      if (after.kind === "watermark") { pageKind = "delta"; base = integer(after.sequence, 0, sequence); }
      else {
        requireThat(params.through && after.through_sequence === sequence && after.evaluated_at === evaluatedAt, "cursor_snapshot_conflict");
        pageKind = after.kind;
        base = pageKind === "delta" ? integer(after.base_sequence, 0, sequence) : 0;
        position = integer(after.position_sequence, 0, sequence);
      }
    }
    const canonical = observations.filter(row => row.request.kind === call.method && Object.entries(scope).every(([key, value]) => row.request.payload.path[key] === value));
    let rows = canonical.filter(row => fresh.has(row.request.agent_instance_id));
    if (pageKind === "snapshot") {
      const latest = new Map<string, Json>();
      for (const row of rows.sort((a, b) => a.sequence - b.sequence)) {
        const key = stable([row.request.agent_instance_id, row.request.payload.resource_kind, row.request.payload.path]);
        latest.set(key, row);
      }
      rows = [...latest.values()].filter(row => row.request.payload.operation !== "remove");
    } else rows = rows.filter(row => row.sequence > base);
    rows = rows.filter(row => row.sequence > position).sort((a, b) => a.sequence - b.sequence);
    const limit = integer(params.limit ?? 50, 1, 200);
    const hasMore = rows.length > limit;
    const changes = rows.slice(0, limit).map(row => ({ ...row.request.payload, revision: row.sequence,
      cursor: cursor({ ...binding, kind: pageKind, base_sequence: base, through_sequence: sequence, evaluated_at: evaluatedAt, position_sequence: row.sequence }) }));
    const unknown = instances.size - fresh.size;
    const issues = [{ severity: "warning", code: "remote_living.observation_only", message: "Remote living authority exposes durable canonical observations only." }];
    if (unknown) issues.push({ severity: "warning", code: "remote_living.heartbeat_unknown", message: `${unknown} expired or missing agent leases; their state is unknown and their resources were omitted.` });
    if (!canonical.length) issues.push({ severity: "warning", code: "remote_living.no_canonical_observations", message: "No canonical observations cover this method at the pinned sequence." });
    const result = { schema_version: call.method === "living.events" ? "ct.living_events.v1" : "ct.living_sessions.v2",
      mode: call.method === "living.events" ? params.mode ?? "view" : "view", page_kind: pageKind, through: watermark,
      next_cursor: hasMore ? changes[changes.length - 1].cursor : null, has_more: hasMore, changes, issues,
      coverage: { source: "ct_living_observations", snapshot_sequence: sequence, evaluated_at: evaluatedAt,
        fresh_agent_instances: fresh.size, unknown_agent_instances: unknown, canonical_observations: canonical.length,
        completeness: !canonical.length ? "heartbeat_only" : unknown ? "partial" : "canonical_observations" } };
    validate(call.method === "living.events" ? "living_events_response" : "living_sessions_response", result);
    return { method: call.method, result };
  });
  return { workspace_id: request.workspace_id, snapshot_sequence: sequence, evaluated_at: evaluatedAt, results };
}
