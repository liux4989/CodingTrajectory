-- Transactional projector publication and point-in-time historical reads.

-- One projection publication is one workspace change, even when it contains
-- several graph artifacts. The original uniqueness constraint prevented that
-- atomic publication unit. Content may also legitimately reappear after an
-- artifact was absent from an intervening complete project projection.
alter table public.ct_artifact_revisions
  drop constraint ct_artifact_revisions_workspace_id_published_sequence_key;
do $$
declare
  target_constraint name;
begin
  select con.conname into target_constraint
  from pg_catalog.pg_constraint con
  where con.conrelid = 'public.ct_artifact_revisions'::regclass
    and con.contype = 'u'
    and (
      select array_agg(attribute.attname order by key.ordinality)
      from unnest(con.conkey) with ordinality key(attnum, ordinality)
      join pg_catalog.pg_attribute attribute
        on attribute.attrelid = con.conrelid
        and attribute.attnum = key.attnum
    ) = array['workspace_id', 'artifact_id', 'content_sha256']::name[];
  if target_constraint is null then
    raise exception 'artifact content uniqueness constraint not found';
  end if;
  execute format(
    'alter table public.ct_artifact_revisions drop constraint %I',
    target_constraint
  );
end;
$$;

create index ct_projection_outbox_expired_lease_idx
  on public.ct_projection_outbox (lease_expires_at)
  where state = 'leased';

-- Full v1 observations remain immutable evidence, but the compact remote
-- control plane never projects them or mixes them into compact graph history.
update public.ct_projection_outbox outbox
set state = 'completed',
    lease_owner = null,
    lease_expires_at = null,
    completed_at = clock_timestamp(),
    last_error = 'legacy_v1_not_projected',
    payload = outbox.payload || jsonb_build_object('legacy_unprojected', true)
from public.ct_source_observations observation
where outbox.projection_name = 'project_source_observation'
  and outbox.state <> 'completed'
  and observation.workspace_id = outbox.workspace_id
  and observation.source_id::text = outbox.resource_id
  and observation.source_epoch = (outbox.payload ->> 'source_epoch')::bigint
  and observation.source_sequence = (outbox.payload ->> 'source_sequence')::bigint
  and observation.schema_version = 'canonical_session_snapshot.v1';

-- Preserve accepted v1 evidence while forcing every future observation onto
-- the one compact wire version. A later wire upgrade replaces this constraint.
alter table public.ct_source_observations
  add constraint ct_source_observations_compact_v2_new
  check (schema_version = 'canonical_session_snapshot.v2') not valid;

create or replace function public.ct_projector_claim(request jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  target_worker_id text := request ->> 'worker_id';
  target_lease_seconds integer := (request ->> 'lease_seconds')::integer;
  claimed public.ct_projection_outbox%rowtype;
  target_project_id uuid;
  observations jsonb;
begin
  if target_worker_id is null or btrim(target_worker_id) = '' then
    raise exception 'worker_id is required' using errcode = '22023';
  end if;
  if target_lease_seconds is null or target_lease_seconds < 1 or target_lease_seconds > 3600 then
    raise exception 'lease_seconds must be between 1 and 3600' using errcode = '22023';
  end if;

  select outbox.* into claimed
  from public.ct_projection_outbox outbox
  join public.ct_ingest_sources source
    on source.workspace_id = outbox.workspace_id
    and source.source_id::text = outbox.resource_id
  join public.ct_source_observations trigger_observation
    on trigger_observation.workspace_id = outbox.workspace_id
    and trigger_observation.source_id = source.source_id
    and trigger_observation.source_epoch = (outbox.payload ->> 'source_epoch')::bigint
    and trigger_observation.source_sequence = (outbox.payload ->> 'source_sequence')::bigint
  where outbox.projection_name = 'project_source_observation'
    and trigger_observation.schema_version = 'canonical_session_snapshot.v2'
    and source.project_id is not null
    and (
      (outbox.state in ('pending', 'failed') and outbox.available_at <= clock_timestamp())
      or (outbox.state = 'leased' and outbox.lease_expires_at <= clock_timestamp())
    )
  order by outbox.workspace_sequence desc, outbox.outbox_id desc
  for update of outbox skip locked
  limit 1;

  if not found then
    return '{}'::jsonb;
  end if;

  update public.ct_projection_outbox
  set state = 'leased',
      lease_owner = target_worker_id,
      lease_expires_at = clock_timestamp() + make_interval(secs => target_lease_seconds),
      attempts = attempts + 1,
      last_error = null
  where workspace_id = claimed.workspace_id
    and outbox_id = claimed.outbox_id;

  select source.project_id into target_project_id
  from public.ct_ingest_sources source
  where source.workspace_id = claimed.workspace_id
    and source.source_id::text = claimed.resource_id;

  select coalesce(
    jsonb_agg(
      jsonb_build_object(
        'source_id', latest.source_id,
        'source_epoch', latest.source_epoch,
        'source_sequence', latest.source_sequence,
        'observed_at', latest.observed_at,
        'payload', latest.payload
      ) order by latest.source_id
    ),
    '[]'::jsonb
  ) into observations
  from (
    select distinct on (source.source_id)
      observation.source_id,
      observation.source_epoch,
      observation.source_sequence,
      observation.observed_at,
      observation.payload
    from public.ct_ingest_sources source
    join public.ct_source_observations observation
      on observation.workspace_id = source.workspace_id
      and observation.source_id = source.source_id
      and observation.source_epoch = source.current_epoch
      and observation.source_sequence <= source.committed_source_sequence
    where source.workspace_id = claimed.workspace_id
      and source.project_id = target_project_id
      and observation.state = 'accepted'
      and observation.schema_version = 'canonical_session_snapshot.v2'
    order by source.source_id, observation.source_sequence desc
  ) latest;

  return jsonb_build_object(
    'workspace_id', claimed.workspace_id,
    'outbox_id', claimed.outbox_id,
    'project_id', target_project_id,
    'workspace_sequence', claimed.workspace_sequence,
    'attempts', claimed.attempts + 1,
    'observations', observations
  );
end;
$$;

create or replace function public.ct_projector_publish(request jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  target_workspace_id uuid := (request ->> 'workspace_id')::uuid;
  target_outbox_id bigint := (request ->> 'outbox_id')::bigint;
  target_worker_id text := request ->> 'worker_id';
  target_project_id uuid := (request ->> 'project_id')::uuid;
  job public.ct_projection_outbox%rowtype;
  publication jsonb;
  artifact_manifest jsonb;
  artifact_record record;
  newer_publication jsonb;
  existing_artifact public.ct_artifacts%rowtype;
  existing_revision public.ct_artifact_revisions%rowtype;
  target_published_sequence bigint;
  next_revision bigint;
  revision_count integer := 0;
  superseded_count integer := 0;
  omitted_count integer := 0;
  publication_outcome text;
begin
  if target_worker_id is null or btrim(target_worker_id) = '' then
    raise exception 'worker_id is required' using errcode = '22023';
  end if;
  if request -> 'artifacts' is null or jsonb_typeof(request -> 'artifacts') <> 'array' then
    raise exception 'artifacts must be an array' using errcode = '22023';
  end if;
  if exists (
    select 1
    from jsonb_array_elements(request -> 'artifacts') artifact
    where jsonb_typeof(artifact) <> 'object'
      or artifact ->> 'artifact_id' is null
      or artifact ->> 'schema_version' is null
      or btrim(artifact ->> 'schema_version') = ''
      or artifact -> 'payload' is null
      or jsonb_typeof(artifact -> 'payload') <> 'object'
      or artifact ->> 'content_sha256' is null
      or artifact ->> 'content_sha256' !~ '^[0-9a-f]{64}$'
      or artifact -> 'source_vector' is null
      or jsonb_typeof(artifact -> 'source_vector') <> 'object'
      or artifact ->> 'observed_at' is null
  ) then
    raise exception 'each artifact requires artifact_id, schema_version, payload, content_sha256, source_vector, and observed_at'
      using errcode = '22023';
  end if;
  if exists (
    select 1
    from jsonb_array_elements(request -> 'artifacts') artifact
    group by (artifact ->> 'artifact_id')::uuid
    having count(*) > 1
  ) then
    raise exception 'artifact_id values must be unique within a publication'
      using errcode = '22023';
  end if;

  select coalesce(
    jsonb_agg(
      jsonb_build_object(
        'artifact_id', normalized.artifact_id,
        'schema_version', normalized.schema_version,
        'content_sha256', normalized.content_sha256,
        'source_vector', normalized.source_vector,
        'observed_at', normalized.observed_at
      ) order by normalized.artifact_id
    ),
    '[]'::jsonb
  ) into artifact_manifest
  from (
    select
      (artifact ->> 'artifact_id')::uuid as artifact_id,
      artifact ->> 'schema_version' as schema_version,
      artifact ->> 'content_sha256' as content_sha256,
      artifact -> 'source_vector' as source_vector,
      (artifact ->> 'observed_at')::timestamptz as observed_at
    from jsonb_array_elements(request -> 'artifacts') artifact
  ) normalized;

  select * into job
  from public.ct_projection_outbox outbox
  where outbox.workspace_id = target_workspace_id
    and outbox.outbox_id = target_outbox_id
  for update;
  if not found or job.projection_name <> 'project_source_observation' then
    raise exception 'projection outbox job not found' using errcode = '22023';
  end if;

  publication := job.payload -> 'projector_publication';
  if job.state = 'completed' then
    if publication is null
      or publication ->> 'worker_id' <> target_worker_id
      or (publication ->> 'project_id')::uuid <> target_project_id
      or publication -> 'artifact_manifest' <> artifact_manifest
      or (
        publication ->> 'outcome' <> 'superseded'
        and exists (
          select 1
          from jsonb_array_elements(request -> 'artifacts') requested
          where not exists (
            select 1
            from public.ct_artifact_revisions revision
            where revision.workspace_id = target_workspace_id
              and revision.artifact_id = (requested ->> 'artifact_id')::uuid
              and revision.content_sha256 = requested ->> 'content_sha256'
              and revision.payload = requested -> 'payload'
          )
        )
      ) then
      raise exception 'completed job does not match this publication request'
        using errcode = '23505';
    end if;
    return jsonb_build_object(
      'outcome', publication ->> 'outcome',
      'published_sequence', (publication ->> 'published_sequence')::bigint,
      'revision_count', (publication ->> 'revision_count')::integer
    );
  end if;
  if job.state <> 'leased'
    or job.lease_owner <> target_worker_id
    or job.lease_expires_at is null
    or job.lease_expires_at <= clock_timestamp() then
    raise exception 'an active lease owned by worker_id is required' using errcode = '42501';
  end if;
  if not exists (
    select 1
    from public.ct_ingest_sources source
    where source.workspace_id = target_workspace_id
      and source.source_id::text = job.resource_id
      and source.project_id = target_project_id
  ) then
    raise exception 'project does not match the workspace-scoped outbox source'
      using errcode = '22023';
  end if;

  perform 1
  from public.ct_projects project
  where project.workspace_id = target_workspace_id
    and project.project_id = target_project_id
  for update;
  if not found then
    raise exception 'project not found in workspace' using errcode = '22023';
  end if;

  -- Claims can be assembled concurrently. Never let an older observation job
  -- overwrite a project publication that a newer outbox job already committed.
  select newer.payload -> 'projector_publication' into newer_publication
  from public.ct_projection_outbox newer
  join public.ct_ingest_sources source
    on source.workspace_id = newer.workspace_id
    and source.source_id::text = newer.resource_id
  where newer.workspace_id = target_workspace_id
    and newer.projection_name = 'project_source_observation'
    and newer.state = 'completed'
    and newer.workspace_sequence > job.workspace_sequence
    and source.project_id = target_project_id
    and newer.payload -> 'projector_publication' is not null
  order by newer.workspace_sequence desc
  limit 1;
  if found then
    publication := jsonb_build_object(
      'worker_id', target_worker_id,
      'project_id', target_project_id,
      'artifact_manifest', artifact_manifest,
      'outcome', 'superseded',
      'published_sequence', (newer_publication ->> 'published_sequence')::bigint,
      'revision_count', 0
    );
    update public.ct_projection_outbox
    set state = 'completed',
        lease_owner = null,
        lease_expires_at = null,
        completed_at = clock_timestamp(),
        last_error = null,
        payload = payload || jsonb_build_object('projector_publication', publication)
    where workspace_id = target_workspace_id
      and outbox_id = target_outbox_id;
    return jsonb_build_object(
      'outcome', 'superseded',
      'published_sequence', (newer_publication ->> 'published_sequence')::bigint,
      'revision_count', 0
    );
  end if;

  if exists (
    select 1
    from jsonb_array_elements(request -> 'artifacts') requested
    join public.ct_artifacts artifact
      on artifact.workspace_id = target_workspace_id
      and artifact.artifact_id = (requested ->> 'artifact_id')::uuid
    where artifact.project_id is distinct from target_project_id
  ) then
    raise exception 'artifact_id belongs to another project in this workspace'
      using errcode = '23505';
  end if;
  if exists (
    select 1
    from jsonb_array_elements(request -> 'artifacts') requested
    join public.ct_artifact_revisions revision
      on revision.workspace_id = target_workspace_id
      and revision.artifact_id = (requested ->> 'artifact_id')::uuid
      and revision.content_sha256 = requested ->> 'content_sha256'
    where revision.payload <> requested -> 'payload'
  ) then
    raise exception 'content_sha256 was previously used with a different payload'
      using errcode = '23505';
  end if;

  target_published_sequence := public.ct_next_workspace_sequence(target_workspace_id);

  for artifact_record in
    select
      (artifact ->> 'artifact_id')::uuid as artifact_id,
      artifact ->> 'schema_version' as schema_version,
      artifact -> 'payload' as payload,
      artifact ->> 'content_sha256' as content_sha256,
      artifact -> 'source_vector' as source_vector,
      (artifact ->> 'observed_at')::timestamptz as observed_at
    from jsonb_array_elements(request -> 'artifacts') artifact
    order by (artifact ->> 'artifact_id')::uuid
  loop
    existing_artifact := null;
    existing_revision := null;
    select * into existing_artifact
    from public.ct_artifacts artifact
    where artifact.workspace_id = target_workspace_id
      and artifact.artifact_id = artifact_record.artifact_id
    for update;

    if found and existing_artifact.current_revision > 0 then
      select * into existing_revision
      from public.ct_artifact_revisions revision
      where revision.workspace_id = target_workspace_id
        and revision.artifact_id = artifact_record.artifact_id
        and revision.revision = existing_artifact.current_revision;
    end if;

    if existing_revision.schema_version = artifact_record.schema_version
      and existing_revision.content_sha256 = artifact_record.content_sha256
      and existing_revision.payload = artifact_record.payload
      and existing_revision.source_vector = artifact_record.source_vector
      and existing_revision.observed_at = artifact_record.observed_at then
      continue;
    end if;

    if existing_artifact.artifact_id is not null and existing_artifact.current_revision > 0 then
      update public.ct_artifact_revisions
      set superseded_sequence = target_published_sequence
      where workspace_id = target_workspace_id
        and artifact_id = artifact_record.artifact_id
        and revision = existing_artifact.current_revision
        and superseded_sequence is null;
      superseded_count := superseded_count + 1;
    end if;

    insert into public.ct_artifacts (
      workspace_id, artifact_id, project_id, current_revision,
      current_published_sequence, state
    ) values (
      target_workspace_id, artifact_record.artifact_id, target_project_id,
      0, null, 'accepting'
    ) on conflict (workspace_id, artifact_id) do nothing;

    select coalesce(max(revision.revision), 0) + 1 into next_revision
    from public.ct_artifact_revisions revision
    where revision.workspace_id = target_workspace_id
      and revision.artifact_id = artifact_record.artifact_id;

    insert into public.ct_artifact_revisions (
      workspace_id, artifact_id, revision, schema_version, payload,
      content_sha256, source_vector, published_sequence, observed_at
    ) values (
      target_workspace_id, artifact_record.artifact_id, next_revision,
      artifact_record.schema_version, artifact_record.payload,
      artifact_record.content_sha256, artifact_record.source_vector,
      target_published_sequence, artifact_record.observed_at
    );
    update public.ct_artifacts
    set current_revision = next_revision,
        current_published_sequence = target_published_sequence,
        state = 'accepting',
        updated_at = clock_timestamp()
    where workspace_id = target_workspace_id
      and artifact_id = artifact_record.artifact_id;
    revision_count := revision_count + 1;
  end loop;

  with omitted as (
    select artifact.artifact_id, artifact.current_revision
    from public.ct_artifacts artifact
    where artifact.workspace_id = target_workspace_id
      and artifact.project_id = target_project_id
      and artifact.current_revision > 0
      and not exists (
        select 1
        from jsonb_array_elements(request -> 'artifacts') requested
        where (requested ->> 'artifact_id')::uuid = artifact.artifact_id
      )
    for update
  ), superseded as (
    update public.ct_artifact_revisions revision
    set superseded_sequence = target_published_sequence
    from omitted
    where revision.workspace_id = target_workspace_id
      and revision.artifact_id = omitted.artifact_id
      and revision.revision = omitted.current_revision
      and revision.superseded_sequence is null
    returning revision.artifact_id
  )
  update public.ct_artifacts artifact
  set current_revision = 0,
      current_published_sequence = target_published_sequence,
      state = 'tombstoned',
      updated_at = clock_timestamp()
  from superseded
  where artifact.workspace_id = target_workspace_id
    and artifact.artifact_id = superseded.artifact_id;
  get diagnostics omitted_count = row_count;
  superseded_count := superseded_count + omitted_count;

  publication_outcome := case
    when revision_count = 0 and superseded_count = 0 then 'duplicate'
    else 'published'
  end;

  insert into public.ct_change_log (
    workspace_id, sequence, authority, kind, resource_id, payload
  ) values (
    target_workspace_id, target_published_sequence, 'historical',
    'graph_artifacts_published', target_project_id::text,
    jsonb_build_object(
      'outbox_id', target_outbox_id,
      'outcome', publication_outcome,
      'artifact_manifest', artifact_manifest,
      'revision_count', revision_count,
      'superseded_count', superseded_count
    )
  );

  publication := jsonb_build_object(
    'worker_id', target_worker_id,
    'project_id', target_project_id,
    'artifact_manifest', artifact_manifest,
    'outcome', publication_outcome,
    'published_sequence', target_published_sequence,
    'revision_count', revision_count
  );
  update public.ct_projection_outbox
  set state = 'completed',
      lease_owner = null,
      lease_expires_at = null,
      completed_at = clock_timestamp(),
      last_error = null,
      payload = payload || jsonb_build_object('projector_publication', publication)
  where workspace_id = target_workspace_id
    and outbox_id = target_outbox_id;

  return jsonb_build_object(
    'outcome', publication_outcome,
    'published_sequence', target_published_sequence,
    'revision_count', revision_count
  );
end;
$$;

create or replace function public.ct_projector_fail(request jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  target_workspace_id uuid := (request ->> 'workspace_id')::uuid;
  target_outbox_id bigint := (request ->> 'outbox_id')::bigint;
  target_worker_id text := request ->> 'worker_id';
  target_error text := request ->> 'error';
  target_retry_seconds integer := (request ->> 'retry_seconds')::integer;
  job public.ct_projection_outbox%rowtype;
  next_available_at timestamptz;
begin
  if target_worker_id is null or btrim(target_worker_id) = '' then
    raise exception 'worker_id is required' using errcode = '22023';
  end if;
  if target_error is null or btrim(target_error) = '' then
    raise exception 'error is required' using errcode = '22023';
  end if;
  if target_retry_seconds is null or target_retry_seconds < 1 or target_retry_seconds > 3600 then
    raise exception 'retry_seconds must be between 1 and 3600' using errcode = '22023';
  end if;

  select * into job
  from public.ct_projection_outbox outbox
  where outbox.workspace_id = target_workspace_id
    and outbox.outbox_id = target_outbox_id
    and outbox.projection_name = 'project_source_observation'
  for update;
  if not found then
    raise exception 'projection outbox job not found' using errcode = '22023';
  end if;
  if job.state = 'completed' then
    return jsonb_build_object('state', 'completed', 'available_at', null);
  end if;
  if job.state = 'failed' and job.last_error = target_error then
    return jsonb_build_object('state', job.state, 'available_at', job.available_at);
  end if;
  if job.state <> 'leased'
    or job.lease_owner <> target_worker_id
    or job.lease_expires_at is null
    or job.lease_expires_at <= clock_timestamp() then
    raise exception 'an active lease owned by worker_id is required' using errcode = '42501';
  end if;

  next_available_at := clock_timestamp() + make_interval(secs => target_retry_seconds);
  update public.ct_projection_outbox
  set state = 'failed',
      available_at = next_available_at,
      lease_owner = null,
      lease_expires_at = null,
      last_error = target_error
  where workspace_id = target_workspace_id
    and outbox_id = target_outbox_id;

  return jsonb_build_object('state', 'failed', 'available_at', next_available_at);
end;
$$;

create or replace function public.ct_historical_snapshot(request jsonb)
returns jsonb
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
declare
  target_workspace_id uuid := (request ->> 'workspace_id')::uuid;
  requested_sequence bigint := nullif(request ->> 'snapshot_sequence', '')::bigint;
  latest_sequence bigint;
  snapshot_sequence bigint;
  artifacts jsonb;
begin
  if coalesce(auth.role(), '') <> 'service_role'
    and not public.ct_is_workspace_member(target_workspace_id) then
    raise exception 'workspace membership is required' using errcode = '42501';
  end if;

  select coalesce(max(change.sequence), 0) into latest_sequence
  from public.ct_change_log change
  where change.workspace_id = target_workspace_id;
  snapshot_sequence := coalesce(requested_sequence, latest_sequence);
  if snapshot_sequence < 0 or snapshot_sequence > latest_sequence then
    raise exception 'snapshot_sequence must be between zero and the latest workspace sequence'
      using errcode = '22023';
  end if;

  select coalesce(
    jsonb_agg(
      jsonb_build_object(
        'artifact_id', revision.artifact_id,
        'revision', revision.revision,
        'published_sequence', revision.published_sequence,
        'payload', revision.payload
      ) order by revision.artifact_id
    ),
    '[]'::jsonb
  ) into artifacts
  from public.ct_artifact_revisions revision
  where revision.workspace_id = target_workspace_id
    and revision.published_sequence <= snapshot_sequence
    and (
      revision.superseded_sequence is null
      or revision.superseded_sequence > snapshot_sequence
    );

  return jsonb_build_object(
    'workspace_id', target_workspace_id,
    'snapshot_sequence', snapshot_sequence,
    'artifacts', artifacts
  );
end;
$$;

revoke all on function public.ct_projector_claim(jsonb) from public, anon, authenticated;
revoke all on function public.ct_projector_publish(jsonb) from public, anon, authenticated;
revoke all on function public.ct_projector_fail(jsonb) from public, anon, authenticated;
grant execute on function public.ct_projector_claim(jsonb) to service_role;
grant execute on function public.ct_projector_publish(jsonb) to service_role;
grant execute on function public.ct_projector_fail(jsonb) to service_role;

revoke all on function public.ct_historical_snapshot(jsonb) from public, anon;
grant execute on function public.ct_historical_snapshot(jsonb) to authenticated, service_role;
