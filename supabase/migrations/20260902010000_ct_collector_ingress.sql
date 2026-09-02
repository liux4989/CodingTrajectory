-- Authenticated ingress for the host-local CT collector.
--
-- The RPC boundary receives already-normalized canonical observations. It does
-- not read paths, SQLite files, or raw vendor JSONL from any collector host.

create or replace function public.ct_collector_authorized(
  target_workspace_id uuid,
  target_agent_id uuid,
  required_capability text
) returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select exists (
    select 1
    from public.ct_agents agent
    join public.ct_agent_capabilities capability
      on capability.workspace_id = agent.workspace_id
      and capability.agent_id = agent.agent_id
    where agent.workspace_id = target_workspace_id
      and agent.agent_id = target_agent_id
      and agent.principal_id = auth.uid()
      and agent.revoked_at is null
      and capability.capability = required_capability
  );
$$;

revoke all on function public.ct_collector_authorized(uuid, uuid, text) from public;
grant execute on function public.ct_collector_authorized(uuid, uuid, text)
  to authenticated, service_role;

create or replace function public.ct_collector_register_source(
  request jsonb,
  idempotency_key text,
  request_sha256 text
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  target_workspace_id uuid := (request ->> 'workspace_id')::uuid;
  target_agent_id uuid := (request ->> 'agent_id')::uuid;
  requested_epoch bigint := coalesce((request ->> 'source_epoch')::bigint, 1);
  existing public.ct_ingest_sources%rowtype;
  result_source_id uuid;
  result_epoch bigint;
begin
  if not public.ct_collector_authorized(target_workspace_id, target_agent_id, 'ingest') then
    raise exception 'collector ingest capability is required' using errcode = '42501';
  end if;
  if request ->> 'vendor' is null or request ->> 'native_session_id' is null then
    raise exception 'vendor and native_session_id are required' using errcode = '22023';
  end if;
  if requested_epoch < 1 then
    raise exception 'source_epoch must be positive' using errcode = '22023';
  end if;

  select * into existing
  from public.ct_ingest_sources
  where workspace_id = target_workspace_id
    and origin_agent_id = target_agent_id
    and vendor = request ->> 'vendor'
    and native_session_id = request ->> 'native_session_id'
  for update;

  if found then
    result_source_id := existing.source_id;
    result_epoch := existing.current_epoch;
    if coalesce((request ->> 'rollover')::boolean, false) then
      if requested_epoch <> existing.current_epoch + 1 then
        raise exception 'source epoch rollover must advance by exactly one' using errcode = '22023';
      end if;
      update public.ct_ingest_sources
      set current_epoch = requested_epoch,
          committed_source_sequence = -1,
          last_observed_at = now()
      where workspace_id = target_workspace_id and source_id = existing.source_id;
      result_epoch := requested_epoch;
    elsif requested_epoch <> existing.current_epoch then
      raise exception 'source epoch does not match registered source' using errcode = '22023';
    end if;
  else
    if requested_epoch <> 1 then
      raise exception 'new sources must start at epoch one' using errcode = '22023';
    end if;
    insert into public.ct_ingest_sources (
      workspace_id, origin_agent_id, project_id, vendor, native_session_id, current_epoch
    ) values (
      target_workspace_id, target_agent_id,
      nullif(request ->> 'project_id', '')::uuid,
      request ->> 'vendor', request ->> 'native_session_id', requested_epoch
    ) returning source_id, current_epoch into result_source_id, result_epoch;
  end if;
  return jsonb_build_object('source_id', result_source_id, 'source_epoch', result_epoch);
end;
$$;

create table public.ct_ingest_receipt_conflicts (
  workspace_id uuid not null,
  conflict_receipt_id uuid primary key default gen_random_uuid(),
  agent_id uuid not null,
  idempotency_key text not null,
  request_sha256 text not null,
  existing_request_sha256 text not null,
  created_at timestamptz not null default now(),
  foreign key (workspace_id, agent_id)
    references public.ct_agents(workspace_id, agent_id) on delete cascade
);
alter table public.ct_ingest_receipt_conflicts enable row level security;
-- Conflict receipts are operational evidence, not client-readable tables.

create or replace function public.ct_collector_publish_observation(
  request jsonb,
  idempotency_key text,
  request_sha256 text
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  target_workspace_id uuid := (request ->> 'workspace_id')::uuid;
  target_agent_id uuid := (request ->> 'agent_id')::uuid;
  target_source_id uuid := (request ->> 'source_id')::uuid;
  target_epoch bigint := (request ->> 'source_epoch')::bigint;
  target_sequence bigint := (request ->> 'source_sequence')::bigint;
  source_record public.ct_ingest_sources%rowtype;
  existing_receipt public.ct_ingest_receipts%rowtype;
  existing_observation public.ct_source_observations%rowtype;
  allocated_sequence bigint;
  contiguous_sequence bigint;
  receipt_id uuid := gen_random_uuid();
begin
  if not public.ct_collector_authorized(target_workspace_id, target_agent_id, 'ingest') then
    raise exception 'collector ingest capability is required' using errcode = '42501';
  end if;
  if idempotency_key is null or btrim(idempotency_key) = '' then
    raise exception 'idempotency_key is required' using errcode = '22023';
  end if;
  if request_sha256 is null or request_sha256 !~ '^[0-9a-f]{64}$' then
    raise exception 'request_sha256 must be a SHA-256 digest' using errcode = '22023';
  end if;

  select * into existing_receipt from public.ct_ingest_receipts
  where workspace_id = target_workspace_id
    and agent_id = target_agent_id
    and ct_ingest_receipts.idempotency_key = ct_collector_publish_observation.idempotency_key
  for update;
  if found then
    if existing_receipt.request_sha256 = request_sha256 then
      return jsonb_build_object(
        'receipt_id', existing_receipt.receipt_id,
        'outcome', existing_receipt.outcome,
        'committed_sequence', existing_receipt.committed_sequence,
        'details', existing_receipt.details
      );
    end if;
    insert into public.ct_ingest_receipt_conflicts (
      workspace_id, agent_id, idempotency_key, request_sha256, existing_request_sha256
    ) values (
      target_workspace_id, target_agent_id, idempotency_key, request_sha256, existing_receipt.request_sha256
    ) returning conflict_receipt_id into receipt_id;
    return jsonb_build_object(
      'receipt_id', receipt_id, 'outcome', 'conflict', 'committed_sequence', null,
      'details', jsonb_build_object('reason', 'idempotency_key_reused_with_different_request')
    );
  end if;

  select * into source_record from public.ct_ingest_sources
  where workspace_id = target_workspace_id and source_id = target_source_id
  for update;
  if not found or source_record.origin_agent_id <> target_agent_id then
    raise exception 'source is not registered to this collector agent' using errcode = '42501';
  end if;
  if source_record.current_epoch <> target_epoch then
    raise exception 'source epoch is not current' using errcode = '22023';
  end if;

  select * into existing_observation from public.ct_source_observations
  where workspace_id = target_workspace_id
    and source_id = target_source_id
    and source_epoch = target_epoch
    and (
      event_id = request ->> 'event_id'
      or source_sequence = target_sequence
    )
  order by (event_id = request ->> 'event_id') desc;
  if found then
    if existing_observation.event_id = request ->> 'event_id'
      and existing_observation.source_sequence = target_sequence
      and existing_observation.content_sha256 = request ->> 'content_sha256' then
      insert into public.ct_ingest_receipts (
        workspace_id, receipt_id, agent_id, idempotency_key, request_sha256, outcome, details
      ) values (
        target_workspace_id, receipt_id, target_agent_id, idempotency_key, request_sha256,
        'duplicate', jsonb_build_object('reason', 'event_identity_already_accepted')
      );
      return jsonb_build_object(
        'receipt_id', receipt_id, 'outcome', 'duplicate', 'committed_sequence', null,
        'details', jsonb_build_object('reason', 'event_identity_already_accepted')
      );
    end if;
    insert into public.ct_ingest_receipts (
      workspace_id, receipt_id, agent_id, idempotency_key, request_sha256, outcome, details
    ) values (
      target_workspace_id, receipt_id, target_agent_id, idempotency_key, request_sha256,
      'conflict', jsonb_build_object('reason', 'event_identity_or_sequence_reused_with_different_content')
    );
    return jsonb_build_object(
      'receipt_id', receipt_id, 'outcome', 'conflict', 'committed_sequence', null,
      'details', jsonb_build_object('reason', 'event_identity_or_sequence_reused_with_different_content')
    );
  end if;

  insert into public.ct_source_observations (
    workspace_id, source_id, source_epoch, source_sequence, event_id,
    schema_version, parser_version, content_sha256, observed_at, payload
  ) values (
    target_workspace_id, target_source_id, target_epoch, target_sequence, request ->> 'event_id',
    request ->> 'schema_version', request ->> 'parser_version', request ->> 'content_sha256',
    (request ->> 'observed_at')::timestamptz, request -> 'payload'
  );
  allocated_sequence := public.ct_next_workspace_sequence(target_workspace_id);
  insert into public.ct_change_log (
    workspace_id, sequence, authority, kind, resource_id, payload
  ) values (
    target_workspace_id, allocated_sequence, 'historical', 'source_observation', target_source_id::text,
    jsonb_build_object('source_epoch', target_epoch, 'source_sequence', target_sequence)
  );
  insert into public.ct_projection_outbox (
    workspace_id, workspace_sequence, projection_name, resource_id, payload
  ) values (
    target_workspace_id, allocated_sequence, 'project_source_observation', target_source_id::text,
    jsonb_build_object('source_epoch', target_epoch, 'source_sequence', target_sequence)
  );
  contiguous_sequence := source_record.committed_source_sequence;
  while exists (
    select 1 from public.ct_source_observations
    where workspace_id = target_workspace_id and source_id = target_source_id
      and source_epoch = target_epoch and source_sequence = contiguous_sequence + 1
  ) loop
    contiguous_sequence := contiguous_sequence + 1;
  end loop;
  update public.ct_ingest_sources
  set committed_source_sequence = contiguous_sequence, last_observed_at = now()
  where workspace_id = target_workspace_id and source_id = target_source_id;
  insert into public.ct_ingest_receipts (
    workspace_id, receipt_id, agent_id, idempotency_key, request_sha256, outcome, committed_sequence
  ) values (
    target_workspace_id, receipt_id, target_agent_id, idempotency_key, request_sha256,
    'accepted', allocated_sequence
  );
  return jsonb_build_object(
    'receipt_id', receipt_id, 'outcome', 'accepted', 'committed_sequence', allocated_sequence, 'details', '{}'::jsonb
  );
end;
$$;

create or replace function public.ct_collector_heartbeat(request jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  target_workspace_id uuid := (request ->> 'workspace_id')::uuid;
  target_agent_id uuid := (request ->> 'agent_id')::uuid;
  target_instance_id uuid := (request ->> 'agent_instance_id')::uuid;
  target_observation_sequence bigint := (request ->> 'observation_sequence')::bigint;
  existing public.ct_living_observations%rowtype;
  allocated_sequence bigint;
  expires_at timestamptz := now() + make_interval(secs => (request ->> 'lease_seconds')::integer);
begin
  if not public.ct_collector_authorized(target_workspace_id, target_agent_id, 'living') then
    raise exception 'collector living capability is required' using errcode = '42501';
  end if;
  select * into existing from public.ct_living_observations
  where workspace_id = target_workspace_id and agent_instance_id = target_instance_id
    and observation_sequence = target_observation_sequence;
  if found then
    return jsonb_build_object('committed_sequence', existing.workspace_sequence, 'lease_expires_at', expires_at);
  end if;
  if exists (
    select 1 from public.ct_agent_leases
    where workspace_id = target_workspace_id and agent_instance_id = target_instance_id
      and agent_id <> target_agent_id
  ) then
    raise exception 'agent instance belongs to another agent' using errcode = '42501';
  end if;
  allocated_sequence := public.ct_next_workspace_sequence(target_workspace_id);
  insert into public.ct_agent_leases (
    workspace_id, agent_id, agent_instance_id, heartbeat_at, lease_expires_at, source_watermarks, runtime_state
  ) values (
    target_workspace_id, target_agent_id, target_instance_id, now(), expires_at,
    coalesce(request -> 'source_watermarks', '{}'::jsonb), coalesce(request ->> 'runtime_state', 'unknown')
  ) on conflict (workspace_id, agent_instance_id) do update set
    heartbeat_at = excluded.heartbeat_at, lease_expires_at = excluded.lease_expires_at,
    source_watermarks = excluded.source_watermarks, runtime_state = excluded.runtime_state, updated_at = now();
  insert into public.ct_change_log (workspace_id, sequence, authority, kind, resource_id, payload)
  values (
    target_workspace_id, allocated_sequence, 'living', 'collector_heartbeat', target_instance_id::text,
    jsonb_build_object('agent_id', target_agent_id, 'runtime_state', request ->> 'runtime_state')
  );
  insert into public.ct_living_observations (
    workspace_id, agent_instance_id, observation_sequence, workspace_sequence, observed_at, kind, payload
  ) values (
    target_workspace_id, target_instance_id, target_observation_sequence, allocated_sequence,
    (request ->> 'observed_at')::timestamptz, 'collector_heartbeat',
    jsonb_build_object('runtime_state', request ->> 'runtime_state')
  );
  return jsonb_build_object('committed_sequence', allocated_sequence, 'lease_expires_at', expires_at);
end;
$$;

revoke all on function public.ct_collector_register_source(jsonb, text, text) from public;
revoke all on function public.ct_collector_publish_observation(jsonb, text, text) from public;
revoke all on function public.ct_collector_heartbeat(jsonb) from public;
grant execute on function public.ct_collector_register_source(jsonb, text, text) to authenticated, service_role;
grant execute on function public.ct_collector_publish_observation(jsonb, text, text) to authenticated, service_role;
grant execute on function public.ct_collector_heartbeat(jsonb) to authenticated, service_role;
