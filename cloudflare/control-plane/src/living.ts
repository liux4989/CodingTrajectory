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
  // Each living write also versions living_head. Read instance identities and
  // their pinned leases in SQL, not every heartbeat or observation in JS.
  const freshness = `WITH instances AS (
    SELECT key FROM records WHERE kind='living_head' AND sequence<=? GROUP BY key
  ), leases AS (
    SELECT key, (SELECT payload FROM records lease WHERE lease.kind='lease'
      AND lease.key=instances.key AND lease.sequence<=? ORDER BY lease.sequence DESC LIMIT 1) AS payload
    FROM instances
  ), freshness AS (
    SELECT key, COALESCE(julianday(json_extract(payload, '$.lease_expires_at')) > julianday(?), 0) AS fresh FROM leases
  )`;
  const freshnessArgs = [sequence, sequence, evaluatedAt];
  const coverage = state.sql.exec<{ fresh: number; unknown: number }>(`${freshness}
    SELECT COALESCE(SUM(fresh), 0) AS fresh, COUNT(*) - COALESCE(SUM(fresh), 0) AS unknown FROM freshness`, ...freshnessArgs).one();
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
    const canonical = `${freshness}, canonical AS (
      SELECT r.key, r.sequence,
        json_extract(r.payload, '$.request.agent_instance_id') AS instance,
        json_extract(r.payload, '$.request.payload.resource_kind') AS resource_kind,
        json_extract(r.payload, '$.request.payload.operation') AS operation,
        (SELECT json_group_object(key, value) FROM
          (SELECT key, value FROM json_each(r.payload, '$.request.payload.path') ORDER BY key)) AS path
      FROM records r WHERE r.kind='living' AND r.sequence<=?
        AND json_extract(r.payload, '$.request.kind')=?
        AND NOT EXISTS (SELECT 1 FROM json_each(?) scope
          WHERE json_extract(r.payload, '$.request.payload.path.' || scope.key) IS NOT scope.value)
    )`;
    const canonicalArgs = [...freshnessArgs, sequence, call.method, stable(scope)];
    const canonicalCount = state.sql.exec<{ count: number }>(`${canonical}
      SELECT COUNT(*) AS count FROM canonical`, ...canonicalArgs).one().count;
    const eligible = `${canonical}, eligible AS (
      SELECT c.* FROM canonical c JOIN freshness f
        ON f.key=c.instance WHERE f.fresh=1
    )`;
    const limit = integer(params.limit ?? 50, 1, 200);
    // Rank before applying the page cursor: an update/remove beyond the cursor
    // must still supersede its older resource. Sort path keys just as stable()
    // did, including the distinction between an absent field and explicit null.
    const selection = pageKind === "snapshot" ? `${eligible}, ranked AS (
      SELECT *, ROW_NUMBER() OVER (PARTITION BY instance, resource_kind, path
        ORDER BY sequence DESC) AS rank FROM eligible
    ) SELECT key, sequence FROM ranked WHERE rank=1 AND operation!='remove' AND sequence>?`
      : `${eligible} SELECT key, sequence FROM eligible WHERE sequence>?`;
    // Carry only scalar identities through ranking; load bodies for this page.
    const rows = state.sql.exec<{ payload: string }>(`SELECT r.payload FROM
      (${selection} ORDER BY sequence LIMIT ?) page JOIN records r
      ON r.kind='living' AND r.key=page.key AND r.sequence=page.sequence ORDER BY page.sequence`,
      ...canonicalArgs, Math.max(position, base), limit + 1).toArray().map(row => JSON.parse(row.payload));
    const hasMore = rows.length > limit;
    const changes = rows.slice(0, limit).map(row => ({ ...row.request.payload, revision: row.sequence,
      cursor: cursor({ ...binding, kind: pageKind, base_sequence: base, through_sequence: sequence, evaluated_at: evaluatedAt, position_sequence: row.sequence }) }));
    const unknown = coverage.unknown;
    const issues = [{ severity: "warning", code: "remote_living.observation_only", message: "Remote living authority exposes durable canonical observations only." }];
    if (unknown) issues.push({ severity: "warning", code: "remote_living.heartbeat_unknown", message: `${unknown} expired or missing agent leases; their state is unknown and their resources were omitted.` });
    if (!canonicalCount) issues.push({ severity: "warning", code: "remote_living.no_canonical_observations", message: "No canonical observations cover this method at the pinned sequence." });
    const result = { schema_version: call.method === "living.events" ? "ct.living_events.v1" : "ct.living_sessions.v2",
      mode: call.method === "living.events" ? params.mode ?? "view" : "view", page_kind: pageKind, through: watermark,
      next_cursor: hasMore ? changes[changes.length - 1].cursor : null, has_more: hasMore, changes, issues,
      coverage: { source: "ct_living_observations", snapshot_sequence: sequence, evaluated_at: evaluatedAt,
        fresh_agent_instances: coverage.fresh, unknown_agent_instances: unknown, canonical_observations: canonicalCount,
        completeness: !canonicalCount ? "heartbeat_only" : unknown ? "partial" : "canonical_observations" } };
    validate(call.method === "living.events" ? "living_events_response" : "living_sessions_response", result);
    return { method: call.method, result };
  });
  return { workspace_id: request.workspace_id, snapshot_sequence: sequence, evaluated_at: evaluatedAt, results };
}
