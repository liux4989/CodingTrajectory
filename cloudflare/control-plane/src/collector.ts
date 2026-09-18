import { DIGEST, Json, receipt, requireThat, stable, State, text, uuid } from "./shared";


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
  const publisher = state.get("artifact_project_publisher", request.project_id);
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
    next_living_sequence: request.agent_instance_id ? (living?.observation_sequence ?? 0) + 1 : null, source,
    authority_sequence: state.head(),
    publication_receipt: request.publication_idempotency_key
      ? state.get("receipt", stable([request.agent_id, "ct_collector_publish_artifacts", request.publication_idempotency_key]))?.result
        ?? null
      : null };
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
