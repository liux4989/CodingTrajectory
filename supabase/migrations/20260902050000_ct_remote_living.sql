-- Remote living authority over durable collector observations.
--
-- Canonical living observations use kind = 'living.events' or
-- 'living.sessions' and carry one corresponding LivingChange object directly
-- in payload. Collector heartbeats remain useful freshness/coverage evidence,
-- but are never promoted into invented session or event resources.

create or replace function public.ct_collector_publish_living_observation(request jsonb)
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
  target_kind text := request ->> 'kind';
  target_payload jsonb := request -> 'payload';
  existing public.ct_living_observations%rowtype;
  allocated_sequence bigint;
begin
  if not public.ct_collector_authorized(target_workspace_id, target_agent_id, 'living') then
    raise exception 'collector living capability is required' using errcode = '42501';
  end if;
  if target_observation_sequence is null or target_observation_sequence < 1
    or target_kind not in ('living.events', 'living.sessions')
    or jsonb_typeof(target_payload) <> 'object'
    or target_payload ->> 'operation' not in ('upsert', 'remove', 'reset')
    or target_payload ->> 'resource_kind' is null
    or jsonb_typeof(target_payload -> 'path') <> 'object' then
    raise exception 'a valid living observation is required' using errcode = '22023';
  end if;
  perform 1 from public.ct_agent_leases lease
    where lease.workspace_id = target_workspace_id
      and lease.agent_instance_id = target_instance_id
      and lease.agent_id = target_agent_id
    for update;
  if not found then
    raise exception 'agent instance lease is required' using errcode = '42501';
  end if;

  select * into existing from public.ct_living_observations observation
  where observation.workspace_id = target_workspace_id
    and observation.agent_instance_id = target_instance_id
    and observation.observation_sequence = target_observation_sequence;
  if found then
    if existing.kind <> target_kind or existing.payload <> target_payload then
      raise exception 'living observation identity was reused with different content'
        using errcode = '23505';
    end if;
    return jsonb_build_object('committed_sequence', existing.workspace_sequence);
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

  allocated_sequence := public.ct_next_workspace_sequence(target_workspace_id);
  insert into public.ct_change_log (
    workspace_id, sequence, authority, kind, resource_id, payload
  ) values (
    target_workspace_id, allocated_sequence, 'living', target_kind,
    target_instance_id::text,
    jsonb_build_object('observation_sequence', target_observation_sequence)
  );
  insert into public.ct_living_observations (
    workspace_id, agent_instance_id, observation_sequence, workspace_sequence,
    observed_at, kind, payload
  ) values (
    target_workspace_id, target_instance_id, target_observation_sequence,
    allocated_sequence, (request ->> 'observed_at')::timestamptz,
    target_kind, target_payload
  );
  return jsonb_build_object('committed_sequence', allocated_sequence);
end;
$$;

create or replace function public.ct_remote_living_encode_cursor(value jsonb)
returns text
language sql
immutable
set search_path = public, pg_temp
as $$
  select rtrim(
    translate(
      replace(replace(encode(convert_to(value::text, 'utf8'), 'base64'), E'\n', ''), E'\r', ''),
      '+/',
      '-_'
    ),
    '='
  );
$$;

create or replace function public.ct_remote_living_decode_cursor(value text)
returns jsonb
language plpgsql
immutable
set search_path = public, pg_temp
as $$
declare
  padded text;
  decoded jsonb;
begin
  if value is null or value = '' or length(value) > 4096
    or value !~ '^[A-Za-z0-9_-]+$' then
    raise exception 'invalid remote living cursor' using errcode = '22023';
  end if;
  padded := translate(value, '-_', '+/')
    || repeat('=', (4 - length(value) % 4) % 4);
  begin
    decoded := convert_from(decode(padded, 'base64'), 'utf8')::jsonb;
  exception when others then
    raise exception 'invalid remote living cursor' using errcode = '22023';
  end;
  if jsonb_typeof(decoded) <> 'object' then
    raise exception 'invalid remote living cursor' using errcode = '22023';
  end if;
  return decoded;
end;
$$;

create or replace function public.ct_remote_living_page(
  target_workspace_id uuid,
  target_method text,
  params jsonb,
  snapshot_sequence bigint,
  evaluated_at timestamptz
) returns jsonb
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
declare
  schema_version text;
  result_mode text;
  scope jsonb := case
    when target_method = 'living.events' then coalesce(params -> 'scope', '{}'::jsonb)
    else '{}'::jsonb
  end;
  scope_fingerprint text;
  after_value text := nullif(params ->> 'after', '');
  after_cursor jsonb;
  through_value text := nullif(params ->> 'through', '');
  through_cursor jsonb;
  page_kind text := 'snapshot';
  base_sequence bigint := 0;
  position_sequence bigint := 0;
  position_instance uuid := '00000000-0000-0000-0000-000000000000'::uuid;
  position_observation bigint := 0;
  requested_limit integer := coalesce((params ->> 'limit')::integer, 50);
  selected_changes jsonb := '[]'::jsonb;
  selected_count integer := 0;
  has_more boolean := false;
  next_cursor text;
  watermark text;
  fresh_agents integer := 0;
  unknown_agents integer := 0;
  canonical_observations integer := 0;
  issues jsonb := '[]'::jsonb;
  completeness text;
begin
  if target_method = 'living.events' then
    schema_version := 'ct.living_events.v1';
    result_mode := coalesce(params ->> 'mode', 'view');
    if result_mode not in ('view', 'details') then
      raise exception 'living.events mode must be view or details' using errcode = '22023';
    end if;
  elsif target_method = 'living.sessions' then
    schema_version := 'ct.living_sessions.v2';
    result_mode := 'view';
  else
    raise exception 'unsupported remote living method: %', target_method
      using errcode = '22023';
  end if;
  if requested_limit < 1 or requested_limit > 200 then
    raise exception 'remote living limit must be between 1 and 200'
      using errcode = '22023';
  end if;

  scope_fingerprint := md5(scope::text);
  watermark := public.ct_remote_living_encode_cursor(jsonb_build_object(
    'version', 1,
    'kind', 'watermark',
    'workspace_id', target_workspace_id,
    'method', target_method,
    'sequence', snapshot_sequence,
    'evaluated_at', evaluated_at,
    'scope', scope_fingerprint
  ));

  if through_value is not null then
    through_cursor := public.ct_remote_living_decode_cursor(through_value);
    if through_cursor ->> 'kind' <> 'watermark'
      or through_cursor ->> 'workspace_id' <> target_workspace_id::text
      or through_cursor ->> 'method' <> target_method
      or through_cursor ->> 'scope' <> scope_fingerprint
      or (through_cursor ->> 'sequence')::bigint <> snapshot_sequence
      or (through_cursor ->> 'evaluated_at')::timestamptz <> evaluated_at then
      raise exception 'through cursor does not match remote living request'
        using errcode = '22023';
    end if;
  end if;

  if after_value is not null then
    after_cursor := public.ct_remote_living_decode_cursor(after_value);
    if after_cursor ->> 'workspace_id' <> target_workspace_id::text
      or after_cursor ->> 'method' <> target_method
      or after_cursor ->> 'scope' <> scope_fingerprint then
      raise exception 'remote living cursor does not match request scope'
        using errcode = '22023';
    end if;
    if after_cursor ->> 'kind' = 'snapshot' then
      if (after_cursor ->> 'through_sequence')::bigint <> snapshot_sequence
        or (after_cursor ->> 'evaluated_at')::timestamptz <> evaluated_at then
        raise exception 'snapshot cursor does not match through cursor'
          using errcode = '22023';
      end if;
      position_sequence := (after_cursor ->> 'position_sequence')::bigint;
      position_instance := (after_cursor ->> 'position_instance')::uuid;
      position_observation := (after_cursor ->> 'position_observation')::bigint;
    elsif after_cursor ->> 'kind' in ('watermark', 'delta') then
      page_kind := 'delta';
      base_sequence := case
        when after_cursor ->> 'kind' = 'watermark'
          then (after_cursor ->> 'sequence')::bigint
        else (after_cursor ->> 'base_sequence')::bigint
      end;
      if base_sequence > snapshot_sequence then
        raise exception 'remote living cursor is ahead of through cursor'
          using errcode = '22023';
      end if;
      if after_cursor ->> 'kind' = 'delta' then
        if (after_cursor ->> 'through_sequence')::bigint <> snapshot_sequence
          or (after_cursor ->> 'evaluated_at')::timestamptz <> evaluated_at then
          raise exception 'delta cursor does not match through cursor'
            using errcode = '22023';
        end if;
        position_sequence := (after_cursor ->> 'position_sequence')::bigint;
        position_instance := (after_cursor ->> 'position_instance')::uuid;
        position_observation := (after_cursor ->> 'position_observation')::bigint;
      end if;
    else
      raise exception 'remote living cursor has an unsupported kind'
        using errcode = '22023';
    end if;
  end if;

  -- A lease row is mutable. It is usable at S only when no later observation
  -- proves that the row represents a future heartbeat, and only until expiry.
  with agents_at_snapshot as (
    select distinct observation.agent_instance_id
    from public.ct_living_observations observation
    where observation.workspace_id = target_workspace_id
      and observation.workspace_sequence <= snapshot_sequence
  ), lease_state as (
    select
      agent.agent_instance_id,
      lease.lease_expires_at,
      not exists (
        select 1
        from public.ct_living_observations future
        where future.workspace_id = target_workspace_id
          and future.agent_instance_id = agent.agent_instance_id
          and future.workspace_sequence > snapshot_sequence
      ) as reconstructable
    from agents_at_snapshot agent
    left join public.ct_agent_leases lease
      on lease.workspace_id = target_workspace_id
      and lease.agent_instance_id = agent.agent_instance_id
  )
  select
    count(*) filter (where reconstructable and lease_expires_at > evaluated_at),
    count(*) filter (
      where not reconstructable
        or lease_expires_at is null
        or lease_expires_at <= evaluated_at
    )
  into fresh_agents, unknown_agents
  from lease_state;

  select count(*) into canonical_observations
  from public.ct_living_observations observation
  where observation.workspace_id = target_workspace_id
    and observation.workspace_sequence <= snapshot_sequence
    and observation.kind = target_method;

  if page_kind = 'snapshot' then
    with canonical as (
      select distinct on (
        observation.agent_instance_id,
        observation.payload ->> 'resource_kind',
        coalesce(observation.payload -> 'path', '{}'::jsonb)::text
      ) observation.*
      from public.ct_living_observations observation
      join public.ct_agent_leases lease
        on lease.workspace_id = observation.workspace_id
        and lease.agent_instance_id = observation.agent_instance_id
        and lease.lease_expires_at > evaluated_at
      where observation.workspace_id = target_workspace_id
        and observation.workspace_sequence <= snapshot_sequence
        and observation.kind = target_method
        and jsonb_typeof(observation.payload) = 'object'
        and observation.payload ->> 'operation' in ('upsert', 'remove', 'reset')
        and observation.payload ->> 'resource_kind' is not null
        and jsonb_typeof(observation.payload -> 'path') = 'object'
        and not exists (
          select 1 from public.ct_living_observations future
          where future.workspace_id = observation.workspace_id
            and future.agent_instance_id = observation.agent_instance_id
            and future.workspace_sequence > snapshot_sequence
        )
        and not exists (
          select 1 from jsonb_each_text(scope) filter
          where filter.value <> coalesce(observation.payload -> 'path' ->> filter.key, '')
        )
      order by observation.agent_instance_id,
        observation.payload ->> 'resource_kind',
        coalesce(observation.payload -> 'path', '{}'::jsonb)::text,
        observation.workspace_sequence desc,
        observation.observation_sequence desc
    ), page as (
      select canonical.*
      from canonical
      where canonical.payload ->> 'operation' <> 'remove'
        and (
          canonical.workspace_sequence,
          canonical.agent_instance_id,
          canonical.observation_sequence
        ) > (position_sequence, position_instance, position_observation)
      order by canonical.workspace_sequence,
        canonical.agent_instance_id,
        canonical.observation_sequence
      limit requested_limit + 1
    ), numbered as (
      select page.*, row_number() over () as row_number
      from page
    )
    select coalesce(jsonb_agg(
      (numbered.payload - 'cursor' - 'revision') || jsonb_build_object(
        'cursor', public.ct_remote_living_encode_cursor(jsonb_build_object(
          'version', 1,
          'kind', 'snapshot',
          'workspace_id', target_workspace_id,
          'method', target_method,
          'through_sequence', snapshot_sequence,
          'evaluated_at', evaluated_at,
          'scope', scope_fingerprint,
          'position_sequence', numbered.workspace_sequence,
          'position_instance', numbered.agent_instance_id,
          'position_observation', numbered.observation_sequence
        )),
        'revision', numbered.workspace_sequence
      ) order by numbered.workspace_sequence, numbered.agent_instance_id,
        numbered.observation_sequence
    ) filter (where numbered.row_number <= requested_limit), '[]'::jsonb),
      count(*)
    into selected_changes, selected_count
    from numbered;
  else
    with page as (
      select observation.*
      from public.ct_living_observations observation
      join public.ct_agent_leases lease
        on lease.workspace_id = observation.workspace_id
        and lease.agent_instance_id = observation.agent_instance_id
        and lease.lease_expires_at > evaluated_at
      where observation.workspace_id = target_workspace_id
        and observation.workspace_sequence > base_sequence
        and observation.workspace_sequence <= snapshot_sequence
        and observation.kind = target_method
        and jsonb_typeof(observation.payload) = 'object'
        and observation.payload ->> 'operation' in ('upsert', 'remove', 'reset')
        and observation.payload ->> 'resource_kind' is not null
        and jsonb_typeof(observation.payload -> 'path') = 'object'
        and not exists (
          select 1 from public.ct_living_observations future
          where future.workspace_id = observation.workspace_id
            and future.agent_instance_id = observation.agent_instance_id
            and future.workspace_sequence > snapshot_sequence
        )
        and not exists (
          select 1 from jsonb_each_text(scope) filter
          where filter.value <> coalesce(observation.payload -> 'path' ->> filter.key, '')
        )
        and (
          observation.workspace_sequence,
          observation.agent_instance_id,
          observation.observation_sequence
        ) > (position_sequence, position_instance, position_observation)
      order by observation.workspace_sequence,
        observation.agent_instance_id,
        observation.observation_sequence
      limit requested_limit + 1
    ), numbered as (
      select page.*, row_number() over () as row_number
      from page
    )
    select coalesce(jsonb_agg(
      (numbered.payload - 'cursor' - 'revision') || jsonb_build_object(
        'cursor', public.ct_remote_living_encode_cursor(jsonb_build_object(
          'version', 1,
          'kind', 'delta',
          'workspace_id', target_workspace_id,
          'method', target_method,
          'base_sequence', base_sequence,
          'through_sequence', snapshot_sequence,
          'evaluated_at', evaluated_at,
          'scope', scope_fingerprint,
          'position_sequence', numbered.workspace_sequence,
          'position_instance', numbered.agent_instance_id,
          'position_observation', numbered.observation_sequence
        )),
        'revision', numbered.workspace_sequence
      ) order by numbered.workspace_sequence, numbered.agent_instance_id,
        numbered.observation_sequence
    ) filter (where numbered.row_number <= requested_limit), '[]'::jsonb),
      count(*)
    into selected_changes, selected_count
    from numbered;
  end if;

  has_more := selected_count > requested_limit;
  if has_more and jsonb_array_length(selected_changes) > 0 then
    next_cursor := selected_changes -> (jsonb_array_length(selected_changes) - 1) ->> 'cursor';
  end if;

  issues := issues || jsonb_build_array(jsonb_build_object(
    'severity', 'warning',
    'code', 'remote_living.observation_only',
    'message', 'Remote living authority exposes durable canonical observations only; host-local discovery is not used as fallback.'
  ));
  if unknown_agents > 0 then
    issues := issues || jsonb_build_array(jsonb_build_object(
      'severity', 'warning',
      'code', 'remote_living.heartbeat_unknown',
      'message', unknown_agents || ' agent lease(s) were expired or not reconstructable at the pinned workspace sequence; their state is unknown and their resources were omitted.'
    ));
  end if;
  if canonical_observations = 0 then
    issues := issues || jsonb_build_array(jsonb_build_object(
      'severity', 'warning',
      'code', 'remote_living.no_canonical_observations',
      'message', 'No durable canonical observations cover this method at the pinned workspace sequence.'
    ));
  end if;
  completeness := case
    when canonical_observations = 0 then 'heartbeat_only'
    when unknown_agents > 0 then 'partial'
    else 'canonical_observations'
  end;

  return jsonb_build_object(
    'schema_version', schema_version,
    'mode', result_mode,
    'page_kind', page_kind,
    'through', watermark,
    'next_cursor', next_cursor,
    'has_more', has_more,
    'changes', selected_changes,
    'issues', issues,
    'coverage', jsonb_build_object(
      'source', 'ct_living_observations',
      'snapshot_sequence', snapshot_sequence,
      'evaluated_at', evaluated_at,
      'fresh_agent_instances', fresh_agents,
      'unknown_agent_instances', unknown_agents,
      'canonical_observations', canonical_observations,
      'completeness', completeness
    )
  );
end;
$$;

create or replace function public.ct_remote_living(request jsonb)
returns jsonb
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
declare
  target_workspace_id uuid := (request ->> 'workspace_id')::uuid;
  requested_sequence bigint := nullif(request ->> 'snapshot_sequence', '')::bigint;
  snapshot_sequence bigint;
  latest_sequence bigint;
  evaluated_at timestamptz := transaction_timestamp();
  calls jsonb := request -> 'calls';
  call jsonb;
  call_params jsonb;
  through_cursor jsonb;
  pinned_sequence bigint;
  pinned_evaluated_at timestamptz;
  results jsonb := '[]'::jsonb;
begin
  if coalesce(auth.role(), '') <> 'service_role'
    and not public.ct_is_workspace_member(target_workspace_id) then
    raise exception 'workspace membership is required' using errcode = '42501';
  end if;
  if jsonb_typeof(calls) <> 'array' or jsonb_array_length(calls) = 0 then
    raise exception 'remote living calls must be a non-empty array'
      using errcode = '22023';
  end if;

  select coalesce(max(change.sequence), 0) into latest_sequence
  from public.ct_change_log change
  where change.workspace_id = target_workspace_id;

  for call in select value from jsonb_array_elements(calls) loop
    call_params := coalesce(call -> 'params', '{}'::jsonb);
    if nullif(call_params ->> 'through', '') is not null then
      through_cursor := public.ct_remote_living_decode_cursor(call_params ->> 'through');
      if through_cursor ->> 'kind' <> 'watermark'
        or through_cursor ->> 'workspace_id' <> target_workspace_id::text
        or through_cursor ->> 'method' <> call ->> 'method' then
        raise exception 'through must be a matching remote living watermark cursor'
          using errcode = '22023';
      end if;
      if pinned_sequence is not null
        and pinned_sequence <> (through_cursor ->> 'sequence')::bigint then
        raise exception 'all remote living calls in a batch must use one workspace sequence'
          using errcode = '22023';
      end if;
      if pinned_evaluated_at is not null
        and pinned_evaluated_at <> (through_cursor ->> 'evaluated_at')::timestamptz then
        raise exception 'all remote living calls in a batch must use one freshness instant'
          using errcode = '22023';
      end if;
      pinned_sequence := (through_cursor ->> 'sequence')::bigint;
      pinned_evaluated_at := (through_cursor ->> 'evaluated_at')::timestamptz;
    end if;
  end loop;

  snapshot_sequence := coalesce(requested_sequence, pinned_sequence, latest_sequence);
  evaluated_at := coalesce(pinned_evaluated_at, evaluated_at);
  if snapshot_sequence < 0 or snapshot_sequence > latest_sequence then
    raise exception 'snapshot_sequence must be between zero and the latest workspace sequence'
      using errcode = '22023';
  end if;
  if requested_sequence is not null and pinned_sequence is not null
    and requested_sequence <> pinned_sequence then
    raise exception 'snapshot_sequence does not match through cursor'
      using errcode = '22023';
  end if;

  for call in select value from jsonb_array_elements(calls) loop
    call_params := coalesce(call -> 'params', '{}'::jsonb);
    results := results || jsonb_build_array(jsonb_build_object(
      'method', call ->> 'method',
      'result', public.ct_remote_living_page(
        target_workspace_id,
        call ->> 'method',
        call_params,
        snapshot_sequence,
        evaluated_at
      )
    ));
  end loop;

  return jsonb_build_object(
    'workspace_id', target_workspace_id,
    'snapshot_sequence', snapshot_sequence,
    'evaluated_at', evaluated_at,
    'results', results
  );
end;
$$;

revoke all on function public.ct_remote_living_encode_cursor(jsonb)
  from public, anon, authenticated;
revoke all on function public.ct_remote_living_decode_cursor(text)
  from public, anon, authenticated;
revoke all on function public.ct_remote_living_page(uuid, text, jsonb, bigint, timestamptz)
  from public, anon, authenticated;
revoke all on function public.ct_remote_living(jsonb) from public, anon;
revoke all on function public.ct_collector_publish_living_observation(jsonb)
  from public, anon;
grant execute on function public.ct_remote_living(jsonb)
  to authenticated, service_role;
grant execute on function public.ct_collector_publish_living_observation(jsonb)
  to authenticated, service_role;
