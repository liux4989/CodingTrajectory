-- Make collector retries truthful and safe after a response is lost.

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
      if requested_epoch = existing.current_epoch then
        -- The previous rollover committed but its response was lost. Return
        -- the existing epoch without advancing it a second time.
        null;
      elsif requested_epoch = existing.current_epoch + 1 then
        update public.ct_ingest_sources
        set current_epoch = requested_epoch,
            committed_source_sequence = -1,
            last_observed_at = now()
        where workspace_id = target_workspace_id and source_id = existing.source_id;
        result_epoch := requested_epoch;
      else
        raise exception 'source epoch rollover must advance by exactly one'
          using errcode = '22023';
      end if;
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
  existing_lease_expires_at timestamptz;
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
    select lease_expires_at into existing_lease_expires_at
    from public.ct_agent_leases
    where workspace_id = target_workspace_id and agent_instance_id = target_instance_id;
    return jsonb_build_object(
      'committed_sequence', existing.workspace_sequence,
      'lease_expires_at', existing_lease_expires_at
    );
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
    workspace_id, agent_id, agent_instance_id, heartbeat_at, lease_expires_at,
    source_watermarks, runtime_state
  ) values (
    target_workspace_id, target_agent_id, target_instance_id, now(), expires_at,
    coalesce(request -> 'source_watermarks', '{}'::jsonb),
    coalesce(request ->> 'runtime_state', 'unknown')
  ) on conflict (workspace_id, agent_instance_id) do update set
    heartbeat_at = excluded.heartbeat_at,
    lease_expires_at = excluded.lease_expires_at,
    source_watermarks = excluded.source_watermarks,
    runtime_state = excluded.runtime_state,
    updated_at = now();
  insert into public.ct_change_log (
    workspace_id, sequence, authority, kind, resource_id, payload
  ) values (
    target_workspace_id, allocated_sequence, 'living', 'collector_heartbeat',
    target_instance_id::text,
    jsonb_build_object('agent_id', target_agent_id, 'runtime_state', request ->> 'runtime_state')
  );
  insert into public.ct_living_observations (
    workspace_id, agent_instance_id, observation_sequence, workspace_sequence,
    observed_at, kind, payload
  ) values (
    target_workspace_id, target_instance_id, target_observation_sequence,
    allocated_sequence, (request ->> 'observed_at')::timestamptz,
    'collector_heartbeat',
    jsonb_build_object('runtime_state', request ->> 'runtime_state')
  );
  return jsonb_build_object(
    'committed_sequence', allocated_sequence,
    'lease_expires_at', expires_at
  );
end;
$$;
