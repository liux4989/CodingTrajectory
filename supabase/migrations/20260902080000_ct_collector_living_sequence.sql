-- Make heartbeat and canonical living publication one ordered, retry-safe stream.

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
  target_observed_at timestamptz := (request ->> 'observed_at')::timestamptz;
  target_lease_seconds integer := coalesce((request ->> 'lease_seconds')::integer, 90);
  target_runtime_state text := coalesce(request ->> 'runtime_state', 'unknown');
  target_source_watermarks jsonb := coalesce(request -> 'source_watermarks', '{}'::jsonb);
  target_payload jsonb;
  existing public.ct_living_observations%rowtype;
  existing_lease_expires_at timestamptz;
  allocated_sequence bigint;
  expires_at timestamptz;
begin
  if not public.ct_collector_authorized(target_workspace_id, target_agent_id, 'living') then
    raise exception 'collector living capability is required' using errcode = '42501';
  end if;
  if target_observation_sequence is null or target_observation_sequence < 1
    or target_observed_at is null
    or target_lease_seconds < 15 or target_lease_seconds > 3600
    or target_runtime_state not in ('living', 'idle', 'terminal', 'unknown')
    or jsonb_typeof(target_source_watermarks) <> 'object' then
    raise exception 'a valid collector heartbeat is required' using errcode = '22023';
  end if;

  perform pg_advisory_xact_lock(
    hashtextextended(target_workspace_id::text || ':' || target_instance_id::text, 0)
  );
  if exists (
    select 1 from public.ct_agent_leases lease
    where lease.workspace_id = target_workspace_id
      and lease.agent_instance_id = target_instance_id
      and lease.agent_id <> target_agent_id
  ) then
    raise exception 'agent instance belongs to another agent' using errcode = '42501';
  end if;

  target_payload := jsonb_build_object(
    'agent_id', target_agent_id,
    'observed_at', target_observed_at,
    'lease_seconds', target_lease_seconds,
    'runtime_state', target_runtime_state,
    'source_watermarks', target_source_watermarks
  );
  select * into existing from public.ct_living_observations observation
  where observation.workspace_id = target_workspace_id
    and observation.agent_instance_id = target_instance_id
    and observation.observation_sequence = target_observation_sequence;
  if found then
    if existing.kind <> 'collector_heartbeat'
      or (
        existing.payload ? 'agent_id'
        and existing.payload - 'lease_expires_at' <> target_payload
      )
      or (
        not (existing.payload ? 'agent_id')
        and existing.payload ->> 'runtime_state' is distinct from target_runtime_state
      ) then
      raise exception 'living observation identity was reused with different content'
        using errcode = '23505';
    end if;
    select lease.lease_expires_at into existing_lease_expires_at
    from public.ct_agent_leases lease
    where lease.workspace_id = target_workspace_id
      and lease.agent_instance_id = target_instance_id;
    return jsonb_build_object(
      'committed_sequence', existing.workspace_sequence,
      'lease_expires_at', coalesce(
        (existing.payload ->> 'lease_expires_at')::timestamptz,
        existing_lease_expires_at,
        clock_timestamp() + make_interval(secs => target_lease_seconds)
      )
    );
  end if;
  if exists (
    select 1 from public.ct_living_observations observation
    where observation.workspace_id = target_workspace_id
      and observation.agent_instance_id = target_instance_id
      and observation.observation_sequence >= target_observation_sequence
  ) then
    raise exception 'living observation sequence must increase monotonically'
      using errcode = '22023';
  end if;

  expires_at := clock_timestamp() + make_interval(secs => target_lease_seconds);
  target_payload := target_payload || jsonb_build_object(
    'lease_expires_at', expires_at
  );
  allocated_sequence := public.ct_next_workspace_sequence(target_workspace_id);
  insert into public.ct_agent_leases (
    workspace_id, agent_id, agent_instance_id, heartbeat_at, lease_expires_at,
    source_watermarks, runtime_state
  ) values (
    target_workspace_id, target_agent_id, target_instance_id, clock_timestamp(),
    expires_at, target_source_watermarks, target_runtime_state
  ) on conflict (workspace_id, agent_instance_id) do update set
    heartbeat_at = excluded.heartbeat_at,
    lease_expires_at = excluded.lease_expires_at,
    source_watermarks = excluded.source_watermarks,
    runtime_state = excluded.runtime_state,
    updated_at = clock_timestamp();
  insert into public.ct_change_log (
    workspace_id, sequence, authority, kind, resource_id, payload
  ) values (
    target_workspace_id, allocated_sequence, 'living', 'collector_heartbeat',
    target_instance_id::text,
    jsonb_build_object('agent_id', target_agent_id, 'runtime_state', target_runtime_state)
  );
  insert into public.ct_living_observations (
    workspace_id, agent_instance_id, observation_sequence, workspace_sequence,
    observed_at, kind, payload
  ) values (
    target_workspace_id, target_instance_id, target_observation_sequence,
    allocated_sequence, target_observed_at, 'collector_heartbeat', target_payload
  );
  return jsonb_build_object(
    'committed_sequence', allocated_sequence,
    'lease_expires_at', expires_at
  );
end;
$$;

revoke all on function public.ct_collector_heartbeat(jsonb) from public, anon;
grant execute on function public.ct_collector_heartbeat(jsonb)
  to authenticated, service_role;
