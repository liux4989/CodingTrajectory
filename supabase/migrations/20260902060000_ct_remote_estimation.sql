-- Durable remote authority for the seven estimate.* methods.
--
-- The foundation tables intentionally left forecast facts in JSON. Two facts
-- cannot be reconstructed from those payloads: a database-enforced successful
-- forecast identity and parentage of backfill child jobs. Keep every contract
-- record and provider plan in immutable JSON while adding only those columns.

alter table public.ct_estimation_jobs
  add column parent_job_id uuid;

alter table public.ct_estimation_jobs
  drop constraint ct_estimation_jobs_snapshot_sequence_check;
alter table public.ct_estimation_jobs
  add constraint ct_estimation_jobs_snapshot_sequence_check
  check (snapshot_sequence >= 0);

alter table public.ct_estimation_jobs
  add constraint ct_estimation_jobs_parent_job_fk
  foreign key (workspace_id, parent_job_id)
  references public.ct_estimation_jobs(workspace_id, job_id) on delete cascade;

create index ct_estimation_jobs_parent_idx
  on public.ct_estimation_jobs (workspace_id, parent_job_id)
  where parent_job_id is not null;

alter table public.ct_forecast_events
  add column idempotency_key text;

alter table public.ct_forecast_events
  drop constraint ct_forecast_events_snapshot_sequence_check;
alter table public.ct_forecast_events
  add constraint ct_forecast_events_snapshot_sequence_check
  check (snapshot_sequence >= 0);

alter table public.ct_forecast_events
  add constraint ct_forecast_events_idempotency_key_valid
  check (idempotency_key is null or btrim(idempotency_key) <> '');

create unique index ct_forecast_events_success_identity_idx
  on public.ct_forecast_events (workspace_id, idempotency_key)
  where event_type = 'forecast_created';

create index ct_forecast_events_job_idx
  on public.ct_forecast_events (workspace_id, job_id, event_type);

create or replace function public.ct_estimation_require_access(
  target_workspace_id uuid,
  service_only boolean default false
)
returns void
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
begin
  if service_only then
    if coalesce(auth.role(), '') <> 'service_role' then
      raise exception 'service role is required' using errcode = '42501';
    end if;
  elsif coalesce(auth.role(), '') <> 'service_role'
    and not public.ct_is_workspace_member(target_workspace_id) then
    raise exception 'workspace membership is required' using errcode = '42501';
  end if;
end;
$$;

create or replace function public.ct_estimation_require_snapshot(
  target_workspace_id uuid,
  target_snapshot_sequence bigint
)
returns void
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
declare
  latest_sequence bigint;
begin
  select coalesce(max(change.sequence), 0) into latest_sequence
  from public.ct_change_log change
  where change.workspace_id = target_workspace_id;
  if target_snapshot_sequence is null
    or target_snapshot_sequence < 0
    or target_snapshot_sequence > latest_sequence then
    raise exception 'snapshot_sequence must be between zero and the latest workspace sequence'
      using errcode = '22023';
  end if;
end;
$$;

create or replace function public.ct_estimation_forecast(
  target_workspace_id uuid,
  target_prediction_id uuid,
  target_snapshot_sequence bigint default null
)
returns jsonb
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
declare
  created public.ct_forecast_events%rowtype;
  binding jsonb;
  comparison jsonb;
  result jsonb;
  target_role text;
  target_status text;
begin
  select event.* into created
  from public.ct_forecast_events event
  where event.workspace_id = target_workspace_id
    and event.prediction_id = target_prediction_id
    and event.event_type = 'forecast_created'
    and (target_snapshot_sequence is null or event.workspace_sequence <= target_snapshot_sequence)
  order by event.event_sequence
  limit 1;
  if not found then
    return null;
  end if;

  select event.payload into binding
  from public.ct_forecast_events event
  where event.workspace_id = target_workspace_id
    and event.prediction_id = target_prediction_id
    and event.event_type = 'forecast_bound'
    and (target_snapshot_sequence is null or event.workspace_sequence <= target_snapshot_sequence)
  order by event.event_sequence desc
  limit 1;
  select event.payload -> 'comparison' into comparison
  from public.ct_forecast_events event
  where event.workspace_id = target_workspace_id
    and event.prediction_id = target_prediction_id
    and event.event_type = 'forecast_compared'
    and (target_snapshot_sequence is null or event.workspace_sequence <= target_snapshot_sequence)
  order by event.event_sequence desc
  limit 1;

  target_role := case when exists (
    select 1
    from public.ct_forecast_events prior
    where prior.workspace_id = target_workspace_id
      and prior.event_type = 'forecast_created'
      and (target_snapshot_sequence is null or prior.workspace_sequence <= target_snapshot_sequence)
      and prior.prediction_id <> target_prediction_id
      and (prior.payload ->> 'turn_id') is not distinct from (created.payload ->> 'turn_id')
      and (prior.payload #>> '{estimator,provider}') is not distinct from (created.payload #>> '{estimator,provider}')
      and (prior.payload #>> '{estimator,model}') is not distinct from (created.payload #>> '{estimator,model}')
      and (prior.payload #>> '{estimator,effort}') is not distinct from (created.payload #>> '{estimator,effort}')
      and (prior.payload #>> '{estimator,prompt_version}') is not distinct from (created.payload #>> '{estimator,prompt_version}')
      and (prior.payload #>> '{estimator,schema_version}') is not distinct from (created.payload #>> '{estimator,schema_version}')
      and (prior.payload ->> 'issued_at', prior.prediction_id)
        < (created.payload ->> 'issued_at', created.prediction_id)
  ) then 'diagnostic' else 'primary' end;

  target_status := case
    when comparison is not null and comparison <> 'null'::jsonb
      and coalesce(comparison ->> 'exclusion', '') <> 'missing_terminal_time'
      then 'compared'
    when created.payload ->> 'forecast_kind' = 'prospective_unbound'
      and binding is null then 'unbound'
    else 'uncompared'
  end;

  result := created.payload
    || coalesce(binding - 'prediction_id', '{}'::jsonb)
    || jsonb_build_object(
      'role', target_role,
      'status', target_status,
      'comparison', comparison
    );
  return result;
end;
$$;

create or replace function public.ct_estimate_predict(request jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  target_workspace_id uuid := (request ->> 'workspace_id')::uuid;
  target_snapshot_sequence bigint := (request ->> 'snapshot_sequence')::bigint;
  plan jsonb := request -> 'plan';
  target_key text := plan ->> 'idempotency_key';
  record jsonb := plan -> 'record';
  existing_prediction_id uuid;
  existing_job public.ct_estimation_jobs%rowtype;
  requester_id uuid;
begin
  perform public.ct_estimation_require_access(target_workspace_id);
  perform public.ct_estimation_require_snapshot(target_workspace_id, target_snapshot_sequence);
  select coalesce(auth.uid(), workspace.created_by) into requester_id
  from public.ct_workspaces workspace
  where workspace.workspace_id = target_workspace_id;
  if plan is null or jsonb_typeof(plan) <> 'object'
    or target_key is null or target_key !~ '^[0-9a-f]{64}$'
    or record is null or jsonb_typeof(record) <> 'object'
    or record ->> 'idempotency_key' <> target_key
    or record ->> 'prediction_id' is null
    or plan ->> 'prompt' is null
    or btrim(plan ->> 'prompt') = '' then
    raise exception 'plan requires a valid identity, record, and prompt'
      using errcode = '22023';
  end if;

  select event.prediction_id into existing_prediction_id
  from public.ct_forecast_events event
  where event.workspace_id = target_workspace_id
    and event.event_type = 'forecast_created'
    and event.idempotency_key = target_key
    and event.workspace_sequence <= target_snapshot_sequence;
  if found then
    return jsonb_build_object(
      'forecast', public.ct_estimation_forecast(
        target_workspace_id, existing_prediction_id, target_snapshot_sequence
      ),
      'failure', null,
      'reused_existing', true
    );
  end if;

  select job.* into existing_job
  from public.ct_estimation_jobs job
  where job.workspace_id = target_workspace_id
    and job.requested_by = requester_id
    and job.idempotency_key = target_key;
  if not found then
    insert into public.ct_estimation_jobs (
      workspace_id, requested_by, idempotency_key, kind,
      snapshot_sequence, spec
    ) values (
      target_workspace_id, requester_id, target_key, 'predict',
      target_snapshot_sequence, jsonb_build_object('plan', plan, 'max_attempts', 2)
    ) returning * into existing_job;
  end if;

  if existing_job.status = 'cancelled' then
    return jsonb_build_object(
      'forecast', null,
      'failure', jsonb_build_object(
        'state', 'permanent_failed', 'reason', 'provider_error',
        'detail', existing_job.last_error
      ),
      'reused_existing', false
    );
  end if;
  return jsonb_build_object(
    'forecast', null,
    'failure', jsonb_build_object(
      'state', 'retryable_failed', 'reason', 'forecast_pending',
      'detail', existing_job.job_id::text
    ),
    'reused_existing', false
  );
end;
$$;

create or replace function public.ct_estimate_get(request jsonb)
returns jsonb
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
declare
  target_workspace_id uuid := (request ->> 'workspace_id')::uuid;
  target_prediction_id uuid := (request ->> 'prediction_id')::uuid;
  target_snapshot_sequence bigint := (request ->> 'snapshot_sequence')::bigint;
begin
  perform public.ct_estimation_require_access(target_workspace_id);
  perform public.ct_estimation_require_snapshot(target_workspace_id, target_snapshot_sequence);
  return jsonb_build_object(
    'forecast', public.ct_estimation_forecast(
      target_workspace_id, target_prediction_id, target_snapshot_sequence
    )
  );
end;
$$;

create or replace function public.ct_estimate_bind(request jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  target_workspace_id uuid := (request ->> 'workspace_id')::uuid;
  target_prediction_id uuid := (request ->> 'prediction_id')::uuid;
  target_snapshot_sequence bigint := (request ->> 'snapshot_sequence')::bigint;
  binding jsonb := request -> 'binding';
  comparison jsonb := request -> 'comparison';
  current_record jsonb;
  next_event_sequence bigint;
  allocated_sequence bigint;
begin
  perform public.ct_estimation_require_access(target_workspace_id);
  perform public.ct_estimation_require_snapshot(target_workspace_id, target_snapshot_sequence);
  perform 1 from public.ct_forecast_events event
  where event.workspace_id = target_workspace_id
    and event.prediction_id = target_prediction_id
    and event.event_type = 'forecast_created'
    and event.workspace_sequence <= target_snapshot_sequence
  for update;
  current_record := public.ct_estimation_forecast(
    target_workspace_id, target_prediction_id, target_snapshot_sequence
  );
  if current_record is null then
    return jsonb_build_object(
      'forecast', null,
      'failure', jsonb_build_object('state', 'not_applicable', 'reason', 'forecast_not_found', 'detail', target_prediction_id::text)
    );
  end if;
  if current_record ->> 'forecast_kind' <> 'prospective_unbound' then
    return jsonb_build_object(
      'forecast', current_record,
      'failure', jsonb_build_object('state', 'not_applicable', 'reason', 'not_unbound', 'detail', current_record ->> 'forecast_kind')
    );
  end if;
  if current_record ->> 'bound_at' is not null then
    return jsonb_build_object(
      'forecast', current_record,
      'failure', jsonb_build_object('state', 'not_applicable', 'reason', 'already_bound', 'detail', current_record ->> 'turn_id')
    );
  end if;
  if binding is null or jsonb_typeof(binding) <> 'object'
    or binding ->> 'bound_at' is null or binding ->> 'turn_id' is null then
    raise exception 'binding requires bound_at and turn_id' using errcode = '22023';
  end if;

  select coalesce(max(event.event_sequence), 0) + 1 into next_event_sequence
  from public.ct_forecast_events event
  where event.workspace_id = target_workspace_id
    and event.prediction_id = target_prediction_id;
  allocated_sequence := public.ct_next_workspace_sequence(target_workspace_id);
  insert into public.ct_change_log (
    workspace_id, sequence, authority, kind, resource_id, payload
  ) values (
    target_workspace_id, allocated_sequence, 'estimation', 'forecast_bound',
    target_prediction_id::text, '{}'::jsonb
  );
  insert into public.ct_forecast_events (
    workspace_id, prediction_id, event_sequence, workspace_sequence,
    event_type, snapshot_sequence, payload
  ) values (
    target_workspace_id, target_prediction_id, next_event_sequence,
    allocated_sequence, 'forecast_bound', target_snapshot_sequence,
    binding || jsonb_build_object('prediction_id', target_prediction_id)
  );

  if comparison is not null and comparison <> 'null'::jsonb then
    allocated_sequence := public.ct_next_workspace_sequence(target_workspace_id);
    insert into public.ct_change_log (
      workspace_id, sequence, authority, kind, resource_id, payload
    ) values (
      target_workspace_id, allocated_sequence, 'estimation', 'forecast_compared',
      target_prediction_id::text, '{}'::jsonb
    );
    insert into public.ct_forecast_events (
      workspace_id, prediction_id, event_sequence, workspace_sequence,
      event_type, snapshot_sequence, payload
    ) values (
      target_workspace_id, target_prediction_id, next_event_sequence + 1,
      allocated_sequence, 'forecast_compared', target_snapshot_sequence,
      jsonb_build_object('prediction_id', target_prediction_id, 'comparison', comparison)
    );
  end if;
  return jsonb_build_object(
    'forecast', public.ct_estimation_forecast(target_workspace_id, target_prediction_id),
    'failure', null
  );
end;
$$;

create or replace function public.ct_estimate_compare(request jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  target_workspace_id uuid := (request ->> 'workspace_id')::uuid;
  target_prediction_id uuid := (request ->> 'prediction_id')::uuid;
  target_snapshot_sequence bigint := (request ->> 'snapshot_sequence')::bigint;
  comparison jsonb := request -> 'comparison';
  next_event_sequence bigint;
  allocated_sequence bigint;
begin
  perform public.ct_estimation_require_access(target_workspace_id);
  perform public.ct_estimation_require_snapshot(target_workspace_id, target_snapshot_sequence);
  if comparison is null or jsonb_typeof(comparison) <> 'object'
    or comparison ->> 'compared_at' is null then
    raise exception 'comparison requires compared_at' using errcode = '22023';
  end if;
  perform 1 from public.ct_forecast_events event
  where event.workspace_id = target_workspace_id
    and event.prediction_id = target_prediction_id
    and event.event_type = 'forecast_created'
    and event.workspace_sequence <= target_snapshot_sequence
  for update;
  if not found then
    raise exception 'forecast not found' using errcode = '22023';
  end if;
  if exists (
    select 1 from public.ct_forecast_events event
    where event.workspace_id = target_workspace_id
      and event.prediction_id = target_prediction_id
      and event.event_type = 'forecast_compared'
      and event.payload -> 'comparison' = comparison
  ) then
    return jsonb_build_object('forecast', public.ct_estimation_forecast(target_workspace_id, target_prediction_id));
  end if;
  select coalesce(max(event.event_sequence), 0) + 1 into next_event_sequence
  from public.ct_forecast_events event
  where event.workspace_id = target_workspace_id
    and event.prediction_id = target_prediction_id;
  allocated_sequence := public.ct_next_workspace_sequence(target_workspace_id);
  insert into public.ct_change_log (
    workspace_id, sequence, authority, kind, resource_id, payload
  ) values (
    target_workspace_id, allocated_sequence, 'estimation', 'forecast_compared',
    target_prediction_id::text, '{}'::jsonb
  );
  insert into public.ct_forecast_events (
    workspace_id, prediction_id, event_sequence, workspace_sequence,
    event_type, snapshot_sequence, payload
  ) values (
    target_workspace_id, target_prediction_id, next_event_sequence,
    allocated_sequence, 'forecast_compared', target_snapshot_sequence,
    jsonb_build_object('prediction_id', target_prediction_id, 'comparison', comparison)
  );
  return jsonb_build_object('forecast', public.ct_estimation_forecast(target_workspace_id, target_prediction_id));
end;
$$;

create or replace function public.ct_estimate_list(request jsonb)
returns jsonb
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
declare
  target_workspace_id uuid := (request ->> 'workspace_id')::uuid;
  target_limit integer := coalesce((request ->> 'limit')::integer, 50);
  target_snapshot_sequence bigint := (request ->> 'snapshot_sequence')::bigint;
  records jsonb;
begin
  perform public.ct_estimation_require_access(target_workspace_id);
  perform public.ct_estimation_require_snapshot(target_workspace_id, target_snapshot_sequence);
  if target_limit < 1 or target_limit > 200 then
    raise exception 'limit must be between 1 and 200' using errcode = '22023';
  end if;
  select coalesce(jsonb_agg(limited.record order by limited.record ->> 'issued_at' desc, limited.prediction_id desc), '[]'::jsonb)
  into records
  from (
    select selected.prediction_id, selected.record
    from (
      select created.prediction_id,
        public.ct_estimation_forecast(
          target_workspace_id, created.prediction_id, target_snapshot_sequence
        ) as record
      from public.ct_forecast_events created
      where created.workspace_id = target_workspace_id
        and created.event_type = 'forecast_created'
        and created.workspace_sequence <= target_snapshot_sequence
    ) selected
    where (request ->> 'forecast_kind' is null or selected.record ->> 'forecast_kind' = request ->> 'forecast_kind')
      and (request ->> 'project_name' is null or selected.record ->> 'project_name' = request ->> 'project_name')
      and (request ->> 'target_harness_name' is null or selected.record #>> '{target,harness_name}' = request ->> 'target_harness_name')
      and (request ->> 'status' is null or selected.record ->> 'status' = request ->> 'status')
    order by selected.record ->> 'issued_at' desc, selected.prediction_id desc
    limit target_limit
  ) limited;
  return jsonb_build_object('items', records);
end;
$$;

create or replace function public.ct_estimate_calibration(request jsonb)
returns jsonb
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
declare
  target_workspace_id uuid := (request ->> 'workspace_id')::uuid;
  target_snapshot_sequence bigint := (request ->> 'snapshot_sequence')::bigint;
  records jsonb;
begin
  perform public.ct_estimation_require_access(target_workspace_id);
  perform public.ct_estimation_require_snapshot(target_workspace_id, target_snapshot_sequence);
  select coalesce(jsonb_agg(selected.record order by selected.record ->> 'issued_at', selected.prediction_id), '[]'::jsonb)
  into records
  from (
    select created.prediction_id,
      public.ct_estimation_forecast(
        target_workspace_id, created.prediction_id, target_snapshot_sequence
      ) as record
    from public.ct_forecast_events created
    where created.workspace_id = target_workspace_id
      and created.event_type = 'forecast_created'
      and created.workspace_sequence <= target_snapshot_sequence
  ) selected
  where (request ->> 'forecast_kind' is null or selected.record ->> 'forecast_kind' = request ->> 'forecast_kind')
    and (request ->> 'project_name' is null or selected.record ->> 'project_name' = request ->> 'project_name')
    and (request ->> 'target_harness_name' is null or selected.record #>> '{target,harness_name}' = request ->> 'target_harness_name')
    and (request ->> 'target_model' is null or selected.record #>> '{target,model}' = request ->> 'target_model')
    and (request ->> 'estimator_model' is null or selected.record #>> '{estimator,model}' = request ->> 'estimator_model')
    and (request ->> 'prompt_version' is null or selected.record #>> '{estimator,prompt_version}' = request ->> 'prompt_version')
    and (request ->> 'retrieval_policy_version' is null or selected.record #>> '{retrieval,policy_version}' = request ->> 'retrieval_policy_version');
  return jsonb_build_object('records', records);
end;
$$;

create or replace function public.ct_estimate_backfill_status(request jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  target_workspace_id uuid := (request ->> 'workspace_id')::uuid;
  target_job_id uuid := (request ->> 'job_id')::uuid;
  parent public.ct_estimation_jobs%rowtype;
  child_count integer;
  active_count integer;
  succeeded_count integer;
  skipped_count integer;
  retryable_count integer;
  permanent_count integer;
  excluded jsonb;
  response_status text;
  finished_at timestamptz;
  stop_reason text;
begin
  perform public.ct_estimation_require_access(target_workspace_id);
  select job.* into parent
  from public.ct_estimation_jobs job
  where job.workspace_id = target_workspace_id
    and job.job_id = target_job_id
    and job.kind = 'backfill'
  for update;
  if not found then
    raise exception 'backfill job not found' using errcode = '22023';
  end if;

  select
    count(*),
    count(*) filter (where child.status in ('pending', 'leased', 'failed')),
    count(*) filter (where child.status = 'completed' and child.spec #>> '{completion,outcome}' = 'succeeded'),
    count(*) filter (where child.status = 'completed' and child.spec #>> '{completion,outcome}' = 'skipped_existing'),
    count(*) filter (where child.status = 'failed'),
    count(*) filter (where child.status = 'cancelled')
  into child_count, active_count, succeeded_count, skipped_count, retryable_count, permanent_count
  from public.ct_estimation_jobs child
  where child.workspace_id = target_workspace_id
    and child.parent_job_id = target_job_id;
  excluded := coalesce(parent.spec -> 'excluded', '{}'::jsonb);

  if active_count = 0 then
    response_status := 'completed';
    finished_at := coalesce(parent.completed_at, clock_timestamp());
    stop_reason := case
      when child_count >= coalesce((parent.spec #>> '{request,max_forecasts}')::integer, 25)
        then 'budget_exhausted:max_forecasts'
      else 'inventory_exhausted'
    end;
    if parent.status <> 'completed' then
      update public.ct_estimation_jobs
      set status = 'completed', completed_at = finished_at
      where workspace_id = target_workspace_id and job_id = target_job_id;
    end if;
  else
    response_status := 'running';
    finished_at := null;
    stop_reason := null;
  end if;
  return jsonb_build_object('job', jsonb_build_object(
    'job_id', target_job_id::text,
    'status', response_status,
    'created_at', parent.created_at,
    'finished_at', finished_at,
    'spec', coalesce(parent.spec -> 'request', '{}'::jsonb),
    'counts', jsonb_build_object(
      'eligible', child_count,
      'succeeded', succeeded_count,
      'skipped_existing', skipped_count,
      'retryable_failed', retryable_count,
      'permanent_failed', permanent_count,
      'uncompared', 0,
      'excluded', excluded,
      'processed', succeeded_count + skipped_count + permanent_count
    ),
    'stop_reason', stop_reason
  ));
end;
$$;

create or replace function public.ct_estimate_backfill_start(request jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  target_workspace_id uuid := (request ->> 'workspace_id')::uuid;
  target_snapshot_sequence bigint := (request ->> 'snapshot_sequence')::bigint;
  target_job_id uuid := coalesce(nullif(request ->> 'job_id', '')::uuid, gen_random_uuid());
  target_spec jsonb := request -> 'spec';
  plans jsonb := request -> 'plans';
  existing public.ct_estimation_jobs%rowtype;
  plan jsonb;
  child_key text;
  requester_id uuid;
begin
  perform public.ct_estimation_require_access(target_workspace_id);
  perform public.ct_estimation_require_snapshot(target_workspace_id, target_snapshot_sequence);
  select coalesce(auth.uid(), workspace.created_by) into requester_id
  from public.ct_workspaces workspace
  where workspace.workspace_id = target_workspace_id;
  if target_spec is null or jsonb_typeof(target_spec) <> 'object'
    or plans is null or jsonb_typeof(plans) <> 'array'
    or jsonb_array_length(plans) > 1000 then
    raise exception 'spec and at most 1000 plans are required' using errcode = '22023';
  end if;
  select job.* into existing
  from public.ct_estimation_jobs job
  where job.workspace_id = target_workspace_id and job.job_id = target_job_id
  for update;
  if found then
    if existing.kind <> 'backfill'
      or (existing.spec -> 'request') - 'concurrency' <> target_spec - 'concurrency' then
      raise exception 'backfill resume parameters do not match the original job spec'
        using errcode = '22023';
    end if;
    return public.ct_estimate_backfill_status(jsonb_build_object('workspace_id', target_workspace_id, 'job_id', target_job_id));
  end if;

  insert into public.ct_estimation_jobs (
    workspace_id, job_id, requested_by, idempotency_key, kind,
    status, snapshot_sequence, spec
  ) values (
    target_workspace_id, target_job_id, requester_id, 'backfill:' || target_job_id,
    'backfill', 'pending', target_snapshot_sequence,
    jsonb_build_object('request', target_spec, 'excluded', coalesce(request -> 'excluded', '{}'::jsonb))
  );
  for plan in select value from jsonb_array_elements(plans)
  loop
    if plan ->> 'idempotency_key' is null or plan -> 'record' is null or plan ->> 'prompt' is null then
      raise exception 'each backfill plan requires idempotency_key, record, and prompt'
        using errcode = '22023';
    end if;
    child_key := 'backfill:' || target_job_id || ':' || (plan ->> 'idempotency_key');
    insert into public.ct_estimation_jobs (
      workspace_id, requested_by, idempotency_key, kind,
      snapshot_sequence, spec, parent_job_id
    ) values (
      target_workspace_id, requester_id, child_key, 'predict',
      target_snapshot_sequence, jsonb_build_object('plan', plan, 'max_attempts', 2),
      target_job_id
    );
  end loop;
  return public.ct_estimate_backfill_status(jsonb_build_object('workspace_id', target_workspace_id, 'job_id', target_job_id));
end;
$$;

create or replace function public.ct_estimator_claim(request jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  target_worker_id text := request ->> 'worker_id';
  target_lease_seconds integer := (request ->> 'lease_seconds')::integer;
  claimed public.ct_estimation_jobs%rowtype;
  plan jsonb;
begin
  perform public.ct_estimation_require_access(null, true);
  if target_worker_id is null or btrim(target_worker_id) = '' then
    raise exception 'worker_id is required' using errcode = '22023';
  end if;
  if target_lease_seconds is null or target_lease_seconds < 1 or target_lease_seconds > 3600 then
    raise exception 'lease_seconds must be between 1 and 3600' using errcode = '22023';
  end if;

  -- Claims are short transactions. Serializing this selection makes the
  -- parent-level concurrency budget exact across multiple worker processes.
  lock table public.ct_estimation_jobs in share row exclusive mode;
  select job.* into claimed
  from public.ct_estimation_jobs job
  where job.kind = 'predict'
    and job.attempts < coalesce((job.spec ->> 'max_attempts')::integer, 2)
    and (
      job.parent_job_id is null
      or (
        select count(*)
        from public.ct_estimation_jobs sibling
        where sibling.workspace_id = job.workspace_id
          and sibling.parent_job_id = job.parent_job_id
          and sibling.status = 'leased'
      ) < coalesce((
        select parent.spec #>> '{request,concurrency}'
        from public.ct_estimation_jobs parent
        where parent.workspace_id = job.workspace_id
          and parent.job_id = job.parent_job_id
      )::integer, 4)
    )
    and (
      (job.status in ('pending', 'failed') and job.available_at <= clock_timestamp())
      or (job.status = 'leased' and job.lease_expires_at <= clock_timestamp())
    )
  order by job.available_at, job.created_at, job.job_id
  for update skip locked
  limit 1;
  if not found then
    return '{}'::jsonb;
  end if;

  update public.ct_estimation_attempts
  set status = 'abandoned', completed_at = clock_timestamp()
  where workspace_id = claimed.workspace_id and job_id = claimed.job_id
    and status = 'running';
  update public.ct_estimation_jobs
  set status = 'leased', lease_owner = target_worker_id,
      lease_expires_at = clock_timestamp() + make_interval(secs => target_lease_seconds),
      attempts = attempts + 1, started_at = coalesce(started_at, clock_timestamp()),
      last_error = null
  where workspace_id = claimed.workspace_id and job_id = claimed.job_id;
  insert into public.ct_estimation_attempts (
    workspace_id, job_id, attempt_number, worker_id, status, heartbeat_at
  ) values (
    claimed.workspace_id, claimed.job_id, claimed.attempts + 1,
    target_worker_id, 'running', clock_timestamp()
  );
  plan := claimed.spec -> 'plan';
  return jsonb_build_object(
    'workspace_id', claimed.workspace_id,
    'job_id', claimed.job_id,
    'attempt_number', claimed.attempts + 1,
    'worker_id', target_worker_id,
    'prompt', plan ->> 'prompt',
    'model', plan #>> '{record,estimator,model}',
    'effort', plan #>> '{record,estimator,effort}'
  );
end;
$$;

create or replace function public.ct_estimator_complete(request jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  target_workspace_id uuid := (request ->> 'workspace_id')::uuid;
  target_job_id uuid := (request ->> 'job_id')::uuid;
  target_attempt integer := (request ->> 'attempt_number')::integer;
  target_worker_id text := request ->> 'worker_id';
  target_p50 double precision := (request ->> 'p50_minutes')::double precision;
  target_p80 double precision := (request ->> 'p80_minutes')::double precision;
  job public.ct_estimation_jobs%rowtype;
  plan jsonb;
  record jsonb;
  target_key text;
  target_prediction_id uuid;
  existing_prediction_id uuid;
  allocated_sequence bigint;
  completion_outcome text := 'succeeded';
begin
  perform public.ct_estimation_require_access(target_workspace_id, true);
  if target_worker_id is null or btrim(target_worker_id) = ''
    or target_attempt is null or target_attempt < 1
    or target_p50 is null or target_p80 is null
    or target_p50 < 1.0 / 60.0 or target_p50 > 10080
    or target_p80 < target_p50 or target_p80 > 10080
    or target_p50 = 'Infinity'::double precision
    or target_p80 = 'Infinity'::double precision then
    raise exception 'invalid p50_minutes or p80_minutes' using errcode = '22023';
  end if;
  select current.* into job
  from public.ct_estimation_jobs current
  where current.workspace_id = target_workspace_id and current.job_id = target_job_id
  for update;
  if not found or job.kind <> 'predict' then
    raise exception 'estimation job not found' using errcode = '22023';
  end if;
  if job.status = 'completed' then
    return coalesce(job.spec -> 'completion', jsonb_build_object('outcome', 'succeeded'));
  end if;
  if job.status <> 'leased' or job.lease_owner <> target_worker_id
    or job.lease_expires_at <= clock_timestamp() or job.attempts <> target_attempt then
    raise exception 'an active matching lease is required' using errcode = '42501';
  end if;
  plan := job.spec -> 'plan';
  record := plan -> 'record';
  target_key := plan ->> 'idempotency_key';
  target_prediction_id := (record ->> 'prediction_id')::uuid;
  select event.prediction_id into existing_prediction_id
  from public.ct_forecast_events event
  where event.workspace_id = target_workspace_id
    and event.event_type = 'forecast_created'
    and event.idempotency_key = target_key;
  if found then
    target_prediction_id := existing_prediction_id;
    completion_outcome := 'skipped_existing';
  else
    allocated_sequence := public.ct_next_workspace_sequence(target_workspace_id);
    record := record || jsonb_build_object('p50_minutes', target_p50, 'p80_minutes', target_p80);
    insert into public.ct_change_log (
      workspace_id, sequence, authority, kind, resource_id, payload
    ) values (
      target_workspace_id, allocated_sequence, 'estimation', 'forecast_created',
      target_prediction_id::text, jsonb_build_object('job_id', target_job_id)
    );
    insert into public.ct_forecast_events (
      workspace_id, prediction_id, event_sequence, workspace_sequence,
      job_id, event_type, snapshot_sequence, payload, idempotency_key
    ) values (
      target_workspace_id, target_prediction_id, 1, allocated_sequence,
      target_job_id, 'forecast_created', job.snapshot_sequence, record, target_key
    );
    if plan -> 'comparison' is not null
      and plan -> 'comparison' <> 'null'::jsonb then
      allocated_sequence := public.ct_next_workspace_sequence(target_workspace_id);
      insert into public.ct_change_log (
        workspace_id, sequence, authority, kind, resource_id, payload
      ) values (
        target_workspace_id, allocated_sequence, 'estimation', 'forecast_compared',
        target_prediction_id::text, jsonb_build_object('job_id', target_job_id)
      );
      insert into public.ct_forecast_events (
        workspace_id, prediction_id, event_sequence, workspace_sequence,
        job_id, event_type, snapshot_sequence, payload
      ) values (
        target_workspace_id, target_prediction_id, 2, allocated_sequence,
        target_job_id, 'forecast_compared', job.snapshot_sequence,
        jsonb_build_object('prediction_id', target_prediction_id, 'comparison', plan -> 'comparison')
      );
    end if;
  end if;
  update public.ct_estimation_attempts
  set status = 'succeeded', completed_at = clock_timestamp(),
      heartbeat_at = clock_timestamp(),
      receipt = jsonb_build_object('prediction_id', target_prediction_id, 'outcome', completion_outcome)
  where workspace_id = target_workspace_id and job_id = target_job_id
    and attempt_number = target_attempt and worker_id = target_worker_id
    and status = 'running';
  update public.ct_estimation_jobs
  set status = 'completed', lease_owner = null, lease_expires_at = null,
      completed_at = clock_timestamp(), last_error = null,
      spec = spec || jsonb_build_object('completion', jsonb_build_object(
        'prediction_id', target_prediction_id, 'outcome', completion_outcome
      ))
  where workspace_id = target_workspace_id and job_id = target_job_id;
  return jsonb_build_object('prediction_id', target_prediction_id, 'outcome', completion_outcome);
end;
$$;

create or replace function public.ct_estimator_fail(request jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  target_workspace_id uuid := (request ->> 'workspace_id')::uuid;
  target_job_id uuid := (request ->> 'job_id')::uuid;
  target_attempt integer := (request ->> 'attempt_number')::integer;
  target_worker_id text := request ->> 'worker_id';
  target_error text := request ->> 'error';
  target_permanent boolean := coalesce((request ->> 'permanent')::boolean, false);
  target_retry_seconds integer := coalesce((request ->> 'retry_seconds')::integer, 30);
  job public.ct_estimation_jobs%rowtype;
  next_status text;
  next_available_at timestamptz;
begin
  perform public.ct_estimation_require_access(target_workspace_id, true);
  if target_worker_id is null or btrim(target_worker_id) = ''
    or target_attempt is null or target_attempt < 1
    or target_error is null or btrim(target_error) = ''
    or target_retry_seconds < 1 or target_retry_seconds > 3600 then
    raise exception 'error and retry_seconds between 1 and 3600 are required'
      using errcode = '22023';
  end if;
  select current.* into job
  from public.ct_estimation_jobs current
  where current.workspace_id = target_workspace_id and current.job_id = target_job_id
  for update;
  if not found then
    raise exception 'estimation job not found' using errcode = '22023';
  end if;
  if job.status = 'completed' then
    return jsonb_build_object('state', 'completed', 'available_at', null);
  end if;
  if job.status <> 'leased' or job.lease_owner <> target_worker_id
    or job.lease_expires_at <= clock_timestamp() or job.attempts <> target_attempt then
    raise exception 'an active matching lease is required' using errcode = '42501';
  end if;
  next_status := case
    when target_permanent or job.attempts >= coalesce((job.spec ->> 'max_attempts')::integer, 2)
      then 'cancelled'
    else 'failed'
  end;
  next_available_at := case when next_status = 'failed'
    then clock_timestamp() + make_interval(secs => target_retry_seconds) else null end;
  update public.ct_estimation_attempts
  set status = 'failed', completed_at = clock_timestamp(),
      heartbeat_at = clock_timestamp(), error = jsonb_build_object(
        'message', target_error, 'permanent', target_permanent
      )
  where workspace_id = target_workspace_id and job_id = target_job_id
    and attempt_number = target_attempt and worker_id = target_worker_id
    and status = 'running';
  update public.ct_estimation_jobs
  set status = next_status, available_at = coalesce(next_available_at, available_at),
      lease_owner = null, lease_expires_at = null,
      completed_at = case when next_status = 'cancelled' then clock_timestamp() else null end,
      last_error = target_error
  where workspace_id = target_workspace_id and job_id = target_job_id;
  return jsonb_build_object('state', next_status, 'available_at', next_available_at);
end;
$$;

revoke all on function public.ct_estimation_require_access(uuid, boolean) from public, anon, authenticated;
revoke all on function public.ct_estimation_require_snapshot(uuid, bigint) from public, anon, authenticated;
revoke all on function public.ct_estimation_forecast(uuid, uuid, bigint) from public, anon, authenticated;

revoke all on function public.ct_estimator_claim(jsonb) from public, anon, authenticated;
revoke all on function public.ct_estimator_complete(jsonb) from public, anon, authenticated;
revoke all on function public.ct_estimator_fail(jsonb) from public, anon, authenticated;
grant execute on function public.ct_estimator_claim(jsonb) to service_role;
grant execute on function public.ct_estimator_complete(jsonb) to service_role;
grant execute on function public.ct_estimator_fail(jsonb) to service_role;

revoke all on function public.ct_estimate_predict(jsonb) from public, anon;
revoke all on function public.ct_estimate_bind(jsonb) from public, anon;
revoke all on function public.ct_estimate_compare(jsonb) from public, anon;
revoke all on function public.ct_estimate_get(jsonb) from public, anon;
revoke all on function public.ct_estimate_list(jsonb) from public, anon;
revoke all on function public.ct_estimate_calibration(jsonb) from public, anon;
revoke all on function public.ct_estimate_backfill_start(jsonb) from public, anon;
revoke all on function public.ct_estimate_backfill_status(jsonb) from public, anon;
grant execute on function public.ct_estimate_predict(jsonb) to authenticated, service_role;
grant execute on function public.ct_estimate_bind(jsonb) to authenticated, service_role;
grant execute on function public.ct_estimate_compare(jsonb) to authenticated, service_role;
grant execute on function public.ct_estimate_get(jsonb) to authenticated, service_role;
grant execute on function public.ct_estimate_list(jsonb) to authenticated, service_role;
grant execute on function public.ct_estimate_calibration(jsonb) to authenticated, service_role;
grant execute on function public.ct_estimate_backfill_start(jsonb) to authenticated, service_role;
grant execute on function public.ct_estimate_backfill_status(jsonb) to authenticated, service_role;
