import { DIGEST, fields, integer, Json, object, receipt, requireThat, stable, State, text, timestamp, uuid, validate } from "./shared";

export function registerProject(state: State, request: Json): Json {
  const name = text(request.display_name).trim();
  requireThat(name && !/[\\/]/.test(name), "portable_project_name_required");
  const repository = request.repository_identity ?? null;
  const aliases = [...new Set<string>((request.aliases ?? []).map((alias: string) => alias.trim()).filter((alias: string) => alias && alias.toLowerCase() !== name.toLowerCase()))].sort();
  const projects = state.all("project");
  const existing = projects.find(p => repository && p.repository_identity === repository) ?? projects.find(p => p.display_name.toLowerCase() === name.toLowerCase());
  if (existing && existing.display_name === name && existing.repository_identity === repository && stable(existing.aliases) === stable(aliases)) {
    return { project_id: existing.project_id, revision: existing.revision, committed_sequence: existing.published_sequence };
  }
  const id = existing?.project_id ?? crypto.randomUUID();
  const sequence = state.next();
  const revision = (existing?.revision ?? 0) + 1;
  state.put("project", id, { project_id: id, revision, display_name: name, repository_identity: repository, aliases,
    published_sequence: sequence, modified_at: new Date().toISOString() }, sequence);
  return { project_id: id, revision, committed_sequence: sequence };
}

export function registerSource(state: State, request: Json): Json {
  if (request.project_id) requireThat(state.get("project", request.project_id), "project_not_found", 404);
  const identity = stable([request.agent_id, request.vendor, request.native_session_id]);
  const existing = state.get("source_identity", identity);
  const source = existing ? state.get("source", existing.source_id) : undefined;
  const epoch = request.source_epoch ?? 1;
  if (source) {
    requireThat(source.project_id === (request.project_id ?? null), "source_project_conflict", 409);
    requireThat(epoch === source.source_epoch || (request.rollover && epoch === source.source_epoch + 1), "source_epoch_conflict", 409);
    if (epoch !== source.source_epoch) state.put("source", source.source_id, { ...source, source_epoch: epoch, committed_source_sequence: -1 }, state.next());
    return { source_id: source.source_id, source_epoch: epoch };
  }
  requireThat(epoch === 1, "new_source_epoch_must_be_one");
  const id = crypto.randomUUID();
  const sequence = state.next();
  state.put("source_identity", identity, { source_id: id }, sequence);
  state.put("source", id, { source_id: id, agent_id: request.agent_id, project_id: request.project_id ?? null,
    source_epoch: 1, vendor: request.vendor, native_session_id: request.native_session_id, committed_source_sequence: -1 }, sequence);
  return { source_id: id, source_epoch: 1 };
}

export function recovery(state: State, request: Json): Json {
  const publisher = state.get("publisher", `${request.agent_id}:${request.project_id}`);
  let source = null;
  if (request.vendor && request.native_session_id) {
    const identity = state.get("source_identity", stable([request.agent_id, request.vendor, request.native_session_id]));
    const current = identity ? state.get("source", identity.source_id) : undefined;
    if (current) {
      requireThat(current.project_id === request.project_id, "source_project_conflict", 409);
      const checkpoint = state.get("checkpoint", `${current.source_id}:${current.source_epoch}:${current.committed_source_sequence}`);
      source = { source_id: current.source_id, source_epoch: current.source_epoch,
        next_source_sequence: current.committed_source_sequence + 1, content_sha256: checkpoint?.content_sha256 ?? null };
    }
  }
  const living = request.agent_instance_id ? state.get("living_head", request.agent_instance_id) : undefined;
  if (living) requireThat(living.agent_id === request.agent_id, "agent_instance_conflict", 403);
  return { next_publication_sequence: (publisher?.publication_sequence ?? -1) + 1,
    next_living_sequence: request.agent_instance_id ? (living?.observation_sequence ?? 0) + 1 : null, source };
}

export function checkpoint(state: State, request: Json): Json {
  const source = state.get("source", request.source_id);
  requireThat(source && source.agent_id === request.agent_id, "source_agent_denied", 403);
  requireThat(source.source_epoch === request.source_epoch, "source_epoch_conflict", 409);
  const key = `${request.source_id}:${request.source_epoch}:${request.source_sequence}`;
  const eventKey = `${request.source_id}:${request.source_epoch}:${request.event_id}`;
  const existing = state.get("checkpoint", key) ?? state.get("checkpoint_event", eventKey);
  if (existing) {
    const duplicate = existing.event_id === request.event_id && existing.source_sequence === request.source_sequence && existing.content_sha256 === request.content_sha256;
    return receipt(duplicate ? "duplicate" : "conflict", null, { reason: duplicate ? "event_identity_already_accepted" : "event_identity_or_sequence_reused_with_different_content" });
  }
  const sequence = state.next();
  state.put("checkpoint", key, request, sequence);
  state.put("checkpoint_event", eventKey, request, sequence);
  let contiguous = source.committed_source_sequence;
  while (state.get("checkpoint", `${source.source_id}:${source.source_epoch}:${contiguous + 1}`)) contiguous++;
  state.put("source", source.source_id, { ...source, committed_source_sequence: contiguous }, sequence);
  return receipt("accepted", sequence);
}

export function publication(state: State, request: Json): Json {
  requireThat(state.get("project", request.project_id), "project_not_found", 404);
  const vector = new Map<string, Json>(request.source_vector.map((entry: Json) => [entry.source_id, entry]));
  requireThat(vector.size === request.source_vector.length, "duplicate_source_vector");
  let stale = false;
  for (const entry of vector.values()) {
    const source = state.get("source", entry.source_id);
    const checkpoint = state.get("checkpoint", `${entry.source_id}:${entry.source_epoch}:${entry.source_sequence}`);
    requireThat(source && source.agent_id === request.agent_id && source.project_id === request.project_id && checkpoint?.content_sha256 === entry.content_sha256,
      "source_vector_requires_accepted_project_checkpoints");
    if (source.source_epoch !== entry.source_epoch || source.committed_source_sequence !== entry.source_sequence) stale = true;
  }
  const incoming = new Map<string, Json>();
  const represented = new Set<string>();
  const sessions = new Set<string>();
  for (const artifact of request.artifacts) {
    requireThat(!incoming.has(artifact.artifact_id), "duplicate_artifact");
    requireThat(new Set(artifact.source_ids).size === artifact.source_ids.length && artifact.source_ids.every((id: string) => vector.has(id)), "invalid_artifact_sources");
    const staged = state.get("staged", `${request.agent_id}:${artifact.content_sha256}`);
    requireThat(staged && staged.artifact_id === artifact.artifact_id && staged.uncompressed_bytes === artifact.serialized_bytes && staged.schema_version === artifact.schema_version,
      "artifact_must_be_staged");
    const prior = state.get("artifact", artifact.artifact_id);
    requireThat(!prior || prior.project_id === request.project_id, "artifact_project_conflict", 409);
    incoming.set(artifact.artifact_id, { ...staged, ...artifact });
    for (const id of artifact.source_ids) represented.add(id);
    for (const id of staged.session_ids) {
      requireThat(!sessions.has(id), "overlapping_incoming_graphs");
      sessions.add(id);
    }
  }
  requireThat(represented.size === vector.size, "unrepresented_source");
  const publisherKey = `${request.agent_id}:${request.project_id}`;
  const publisher = state.get("publisher", publisherKey);
  const currentSequence = publisher?.publication_sequence ?? -1;
  if (request.publication_sequence <= currentSequence) return receipt("conflict", publisher?.committed_sequence ?? null, { reason: "stale_publication_sequence" });
  requireThat(request.publication_sequence === currentSequence + 1, "publication_sequence_gap", 409);
  const current = state.all("artifact").filter(row => !row.deleted && row.project_id === request.project_id);
  const incomplete = current.some(row => row.session_ids.some((id: string) => sessions.has(id)) && row.source_ids.some((id: string) => !vector.has(id)));
  const sequence = state.next();
  state.put("publisher", publisherKey, { publication_sequence: request.publication_sequence, committed_sequence: sequence }, sequence);
  if (stale || incomplete) {
    return receipt("accepted", sequence, incomplete ? { reason: "incomplete_graph_scope", publication_outcome: "rejected", remedy: "include all sources of overlapping published graphs" } : { publication_outcome: "superseded" });
  }
  let superseded = 0;
  let omitted = 0;
  for (const [id, artifact] of incoming) {
    const prior = state.get("artifact", id);
    if (prior && !prior.deleted) superseded++;
    state.put("artifact", id, { ...artifact, project_id: request.project_id, agent_id: request.agent_id,
      revision: (prior?.revision ?? 0) + 1, published_sequence: sequence, deleted: false }, sequence);
  }
  for (const row of current) {
    if (!incoming.has(row.artifact_id) && row.source_ids.every((id: string) => vector.has(id))) {
      state.put("artifact", row.artifact_id, { ...row, deleted: true }, sequence);
      omitted++;
    }
  }
  return receipt("accepted", sequence, { publication_outcome: "published", artifacts_published: incoming.size, revisions_published: incoming.size,
    superseded_revisions: superseded, omitted_artifacts: omitted });
}

export function chronicleMetadata(payload: Json): { metadata: Json; resources: string[] } {
  validate("chronicle", payload);
  const sessions: Json[] = payload.sessions;
  const sessionIds = new Set<string>(sessions.map(session => session.session_id));
  requireThat(sessions.length > 0 && sessionIds.size === sessions.length && sessionIds.has(payload.graph.root_session_id) && payload.graph.session_count === sessions.length, "invalid_graph_sessions");
  const turns = new Map<string, string>();
  const items = new Map<string, string>();
  const owner = (origin: Json, session: string) => {
    requireThat(!origin.turn_id || turns.get(origin.turn_id) === session, "invalid_origin_turn");
    requireThat(!origin.item_id || (origin.turn_id && items.get(origin.item_id) === `${session}:${origin.turn_id}`), "invalid_origin_item");
  };
  for (const session of sessions) {
    let priorTurn = -1;
    for (const turn of session.turns ?? []) {
      requireThat(!turns.has(turn.turn_id) && turn.sequence > priorTurn, "invalid_turn_order");
      turns.set(turn.turn_id, session.session_id);
      priorTurn = turn.sequence;
      const turnItems = new Map<string, Json>((turn.items ?? []).map((item: Json) => [item.item_id, item]));
      let priorItem = -1;
      for (const item of turn.items ?? []) {
        requireThat(!items.has(item.item_id) && item.sequence > priorItem, "invalid_item_order");
        items.set(item.item_id, `${session.session_id}:${turn.turn_id}`);
        priorItem = item.sequence;
        if (item.projection_parent_item_id) {
          const parent = turnItems.get(item.projection_parent_item_id);
          requireThat(parent && parent.item_id !== item.item_id && !parent.measurements?.projection_only && item.measurements?.projection_only, "invalid_projection_parent");
        } else requireThat(item.nested_index == null, "projection_parent_required");
      }
    }
    for (const origin of session.topology?.spawn_origins ?? []) owner(origin, session.session_id);
  }
  requireThat(turns.size === payload.graph.turn_count && items.size === payload.graph.item_count, "invalid_graph_counts");
  const edges = new Set<string>();
  for (const edge of payload.edges ?? []) {
    requireThat(sessionIds.has(edge.source_session_id) && sessionIds.has(edge.target_session_id) && edge.origin.session_id === edge.source_session_id, "invalid_edge");
    owner(edge.origin, edge.source_session_id);
    const identity = stable([edge.kind, edge.source_session_id, edge.target_session_id, edge.origin.turn_id ?? null, edge.origin.item_id ?? null]);
    requireThat(!edges.has(identity), "duplicate_edge");
    edges.add(identity);
  }
  safeChronicle(payload);
  return { metadata: { artifact_id: payload.graph.root_session_id, schema_version: payload.schema_version,
    session_ids: [...sessionIds], vendors: [...new Set(sessions.map(session => session.vendor))], graph: payload.graph }, resources: [...sessionIds, ...turns.keys(), ...items.keys()] };
}

function safeChronicle(value: any, field = "") {
  if (typeof value === "string") {
    requireThat(value.length <= 512, "unbounded_chronicle_string");
    if (["content", "text_preview"].includes(field)) { requireThat(value.length > 0 && value.length <= 280, "invalid_preview"); return; }
    requireThat(!/^\s*data:/i.test(value) && !/(?:\/Users\/|\/home\/|[A-Za-z]:\\|~\/)/.test(value) && !(value.length >= 128 && /^[A-Za-z0-9+/]+={0,2}$/.test(value)), "private_chronicle_content");
  } else if (Array.isArray(value)) for (const child of value) safeChronicle(child, field);
  else if (value && typeof value === "object") for (const [key, child] of Object.entries(value)) {
    const empty = child == null || child === "" || (Array.isArray(child) && child.length === 0) || (typeof child === "object" && Object.keys(child as object).length === 0);
    requireThat(empty || !/(data_uri|blob|media)/.test(key.toLowerCase().replace(/-/g, "_")), "embedded_chronicle_content");
    safeChronicle(child, key);
  }
}
