-- Stage content-addressed compressed Chronicle payloads and small read
-- projections alongside the legacy JSONB authority. The legacy payload remains
-- the rollback source until every historical revision is verified and backfilled.

create table public.ct_artifact_payloads (
  workspace_id uuid not null references public.ct_workspaces(workspace_id),
  content_sha256 text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
  schema_version text not null,
  encoding text not null check (encoding in ('legacy_jsonb', 'zstd')),
  uncompressed_bytes integer check (
    uncompressed_bytes is null or uncompressed_bytes between 1 and 8388608
  ),
  stored_bytes integer not null check (stored_bytes >= 0 and stored_bytes <= 8388608),
  compressed_payload bytea,
  staged_at timestamptz not null default clock_timestamp(),
  primary key (workspace_id, content_sha256),
  check (
    (encoding = 'legacy_jsonb' and compressed_payload is null)
    or
    (encoding = 'zstd' and compressed_payload is not null
      and uncompressed_bytes is not null
      and octet_length(compressed_payload) = stored_bytes)
  )
);

create table public.ct_artifact_read_projections (
  workspace_id uuid not null,
  content_sha256 text not null,
  projection_name text not null,
  projection_version integer not null check (projection_version > 0),
  variant text not null,
  payload jsonb not null,
  payload_bytes integer not null check (payload_bytes between 1 and 1048576),
  staged_at timestamptz not null default clock_timestamp(),
  primary key (
    workspace_id, content_sha256, projection_name, projection_version, variant
  ),
  foreign key (workspace_id, content_sha256)
    references public.ct_artifact_payloads(workspace_id, content_sha256)
    on delete cascade,
  check (octet_length(convert_to(payload::text, 'UTF8')) = payload_bytes)
);

alter table public.ct_artifact_payloads enable row level security;
alter table public.ct_artifact_read_projections enable row level security;

insert into public.ct_artifact_payloads (
  workspace_id, content_sha256, schema_version, encoding,
  uncompressed_bytes, stored_bytes
)
select distinct on (revision.workspace_id, revision.content_sha256)
  revision.workspace_id,
  revision.content_sha256,
  revision.schema_version,
  'legacy_jsonb',
  null,
  pg_column_size(revision.payload)
from public.ct_artifact_revisions revision
order by revision.workspace_id, revision.content_sha256, revision.revision;

create or replace function public.ct_ensure_artifact_payload_reference()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  insert into public.ct_artifact_payloads (
    workspace_id, content_sha256, schema_version, encoding,
    uncompressed_bytes, stored_bytes
  ) values (
    new.workspace_id, new.content_sha256, new.schema_version, 'legacy_jsonb',
    null, pg_column_size(new.payload)
  ) on conflict (workspace_id, content_sha256) do nothing;
  return new;
end;
$$;

create trigger ct_artifact_revisions_payload_reference
after insert on public.ct_artifact_revisions
for each row execute function public.ct_ensure_artifact_payload_reference();

create or replace function public.ct_collector_stage_artifact_payload(request jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  target_workspace_id uuid := (request ->> 'workspace_id')::uuid;
  target_agent_id uuid := (request ->> 'agent_id')::uuid;
  target_digest text := request ->> 'content_sha256';
  target_schema text := request ->> 'schema_version';
  target_encoding text := request ->> 'encoding';
  target_uncompressed_bytes integer :=
    (request ->> 'uncompressed_bytes')::integer;
  target_compressed_bytes integer := (request ->> 'compressed_bytes')::integer;
  encoded_payload text := request ->> 'payload_base64';
  compressed bytea;
  projections jsonb := request -> 'projections';
  projection record;
  existing public.ct_artifact_payloads%rowtype;
begin
  if not public.ct_collector_authorized(
    target_workspace_id, target_agent_id, 'ingest'
  ) then
    raise exception 'collector ingest capability is required' using errcode = '42501';
  end if;
  if not public.ct_jsonb_object_matches(
    request,
    array[
      'workspace_id', 'agent_id', 'schema_version', 'content_sha256',
      'encoding', 'uncompressed_bytes', 'compressed_bytes', 'payload_base64',
      'projections'
    ],
    array[
      'workspace_id', 'agent_id', 'schema_version', 'content_sha256',
      'encoding', 'uncompressed_bytes', 'compressed_bytes', 'payload_base64',
      'projections'
    ]
  ) or target_schema <> 'ct.chronicle_graph.v2'
    or target_digest !~ '^[0-9a-f]{64}$'
    or target_encoding <> 'zstd'
    or target_uncompressed_bytes not between 1 and 8388608
    or target_compressed_bytes not between 1 and 8388608
    or jsonb_typeof(projections) <> 'object'
    or not (projections ?& array['default', 'runtime', 'usage', 'runtime_usage'])
    or exists (
      select 1 from jsonb_object_keys(projections) key
      where key <> all(array['default', 'runtime', 'usage', 'runtime_usage'])
    )
  then
    raise exception 'invalid compressed artifact payload' using errcode = '22023';
  end if;

  compressed := decode(encoded_payload, 'base64');
  if octet_length(compressed) <> target_compressed_bytes then
    raise exception 'compressed artifact byte count mismatch' using errcode = '22023';
  end if;
  if exists (
    select 1 from jsonb_each(projections) entry
    where jsonb_typeof(entry.value) <> 'object'
      or jsonb_typeof(entry.value -> 'items') <> 'array'
      or octet_length(convert_to(entry.value::text, 'UTF8')) > 1048576
  ) then
    raise exception 'invalid artifact read projection' using errcode = '22023';
  end if;

  select * into existing
  from public.ct_artifact_payloads payload
  where payload.workspace_id = target_workspace_id
    and payload.content_sha256 = target_digest
  for update;
  if found and existing.encoding = 'zstd' and (
    existing.schema_version <> target_schema
    or existing.uncompressed_bytes <> target_uncompressed_bytes
    or existing.compressed_payload <> compressed
  ) then
    raise exception 'artifact digest was staged with different bytes'
      using errcode = '23505';
  end if;

  insert into public.ct_artifact_payloads (
    workspace_id, content_sha256, schema_version, encoding,
    uncompressed_bytes, stored_bytes, compressed_payload
  ) values (
    target_workspace_id, target_digest, target_schema, 'zstd',
    target_uncompressed_bytes, target_compressed_bytes, compressed
  ) on conflict (workspace_id, content_sha256) do update
    set schema_version = excluded.schema_version,
        encoding = excluded.encoding,
        uncompressed_bytes = excluded.uncompressed_bytes,
        stored_bytes = excluded.stored_bytes,
        compressed_payload = excluded.compressed_payload,
        staged_at = clock_timestamp()
    where ct_artifact_payloads.encoding = 'legacy_jsonb';

  for projection in select key, value from jsonb_each(projections) loop
    if exists (
      select 1 from public.ct_artifact_read_projections stored
      where stored.workspace_id = target_workspace_id
        and stored.content_sha256 = target_digest
        and stored.projection_name = 'project.sessions'
        and stored.projection_version = 1
        and stored.variant = projection.key
        and stored.payload <> projection.value
    ) then
      raise exception 'artifact projection is not deterministic'
        using errcode = '23505';
    end if;
    insert into public.ct_artifact_read_projections (
      workspace_id, content_sha256, projection_name, projection_version,
      variant, payload, payload_bytes
    ) values (
      target_workspace_id, target_digest, 'project.sessions', 1,
      projection.key, projection.value,
      octet_length(convert_to(projection.value::text, 'UTF8'))
    ) on conflict do nothing;
  end loop;

  return jsonb_build_object(
    'content_sha256', target_digest,
    'encoding', 'zstd',
    'stored_bytes', target_compressed_bytes,
    'projection_version', 1
  );
end;
$$;

create or replace function public.ct_historical_artifacts(request jsonb)
returns jsonb
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
declare
  target_workspace_id uuid := (request ->> 'workspace_id')::uuid;
  requested_sequence bigint := nullif(request ->> 'snapshot_sequence', '')::bigint;
  requested_resources jsonb := coalesce(request -> 'resource_ids', '[]'::jsonb);
  requested_project_name text := nullif(request ->> 'project_name', '');
  requested_vendor text := nullif(request ->> 'agent_vendor', '');
  requested_since_days integer := nullif(request ->> 'since_days', '')::integer;
  requested_modified_since timestamptz :=
    nullif(request ->> 'modified_since', '')::timestamptz;
  latest_sequence bigint;
  snapshot_sequence bigint;
  artifacts jsonb;
begin
  if coalesce(auth.role(), '') <> 'service_role'
    and not public.ct_is_workspace_member(target_workspace_id) then
    raise exception 'workspace membership is required' using errcode = '42501';
  end if;
  if jsonb_typeof(requested_resources) <> 'array' then
    raise exception 'resource_ids must be an array' using errcode = '22023';
  end if;
  select coalesce(max(change.sequence), 0) into latest_sequence
  from public.ct_change_log change
  where change.workspace_id = target_workspace_id;
  snapshot_sequence := coalesce(requested_sequence, latest_sequence);
  if snapshot_sequence < 0 or snapshot_sequence > latest_sequence then
    raise exception 'snapshot_sequence is outside the workspace history'
      using errcode = '22023';
  end if;

  select coalesce(jsonb_agg(
    jsonb_build_object(
      'artifact_id', selected.artifact_id,
      'revision', selected.revision,
      'published_sequence', selected.published_sequence,
      'content_sha256', selected.content_sha256
    ) || case when selected.encoding = 'zstd' then jsonb_build_object(
      'payload_encoding', 'zstd',
      'uncompressed_bytes', selected.uncompressed_bytes,
      'payload_base64', replace(
        encode(selected.compressed_payload, 'base64'), E'\n', ''
      )
    ) else jsonb_build_object('payload', selected.payload) end
    order by selected.artifact_id
  ), '[]'::jsonb) into artifacts
  from (
    select revision.*, stored.encoding, stored.uncompressed_bytes,
      stored.compressed_payload
    from public.ct_artifact_revisions revision
    join public.ct_artifacts artifact
      on artifact.workspace_id = revision.workspace_id
      and artifact.artifact_id = revision.artifact_id
    left join public.ct_artifact_payloads stored
      on stored.workspace_id = revision.workspace_id
      and stored.content_sha256 = revision.content_sha256
    where revision.workspace_id = target_workspace_id
      and revision.schema_version = 'ct.chronicle_graph.v2'
      and revision.published_sequence <= snapshot_sequence
      and (revision.superseded_sequence is null
        or revision.superseded_sequence > snapshot_sequence)
      and (
        jsonb_array_length(requested_resources) = 0
        or exists (
          select 1
          from public.ct_artifact_revision_resources resource,
            jsonb_array_elements_text(requested_resources) requested(resource_id)
          where resource.workspace_id = revision.workspace_id
            and resource.artifact_id = revision.artifact_id
            and resource.revision = revision.revision
            and resource.resource_id = requested.resource_id::uuid
        )
      )
      and (
        requested_project_name is null
        or exists (
          select 1 from public.ct_project_revisions project
          where project.workspace_id = artifact.workspace_id
            and project.project_id = artifact.project_id
            and project.display_name = requested_project_name
            and project.published_sequence <= snapshot_sequence
            and (project.superseded_sequence is null
              or project.superseded_sequence > snapshot_sequence)
        )
      )
      and (requested_vendor is null or exists (
        select 1 from jsonb_array_elements(revision.payload -> 'sessions') session
        where session ->> 'vendor' = requested_vendor
      ))
      and (requested_since_days is null or revision.observed_at >=
        clock_timestamp() - make_interval(days => requested_since_days))
      and (requested_modified_since is null
        or revision.observed_at >= requested_modified_since)
  ) selected;

  return jsonb_build_object(
    'workspace_id', target_workspace_id,
    'snapshot_sequence', snapshot_sequence,
    'artifacts', artifacts
  );
end;
$$;

create or replace function public.ct_project_sessions_projection(request jsonb)
returns jsonb
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
declare
  target_workspace_id uuid := (request ->> 'workspace_id')::uuid;
  requested_sequence bigint := nullif(request ->> 'snapshot_sequence', '')::bigint;
  requested_project_name text := nullif(request ->> 'project_name', '');
  requested_vendor text := nullif(request ->> 'agent_vendor', '');
  requested_since_days integer := nullif(request ->> 'since_days', '')::integer;
  requested_modified_since timestamptz :=
    nullif(request ->> 'modified_since', '')::timestamptz;
  requested_include jsonb := coalesce(request -> 'include', '[]'::jsonb);
  selected_variant text;
  latest_sequence bigint;
  snapshot_sequence bigint;
  candidate_count integer;
  projected_count integer;
  items jsonb;
begin
  if coalesce(auth.role(), '') <> 'service_role'
    and not public.ct_is_workspace_member(target_workspace_id) then
    raise exception 'workspace membership is required' using errcode = '42501';
  end if;
  if jsonb_typeof(requested_include) <> 'array' then
    raise exception 'include must be an array' using errcode = '22023';
  end if;
  selected_variant := case
    when requested_include ? 'runtime' and requested_include ? 'usage'
      then 'runtime_usage'
    when requested_include ? 'runtime' then 'runtime'
    when requested_include ? 'usage' then 'usage'
    else 'default'
  end;
  select coalesce(max(change.sequence), 0) into latest_sequence
  from public.ct_change_log change
  where change.workspace_id = target_workspace_id;
  snapshot_sequence := coalesce(requested_sequence, latest_sequence);
  if snapshot_sequence < 0 or snapshot_sequence > latest_sequence then
    raise exception 'snapshot_sequence is outside the workspace history'
      using errcode = '22023';
  end if;

  with candidates as materialized (
    select revision.workspace_id, revision.artifact_id, revision.revision,
      revision.content_sha256
    from public.ct_artifact_revisions revision
    join public.ct_artifacts artifact
      on artifact.workspace_id = revision.workspace_id
      and artifact.artifact_id = revision.artifact_id
    where revision.workspace_id = target_workspace_id
      and revision.schema_version = 'ct.chronicle_graph.v2'
      and revision.published_sequence <= snapshot_sequence
      and (revision.superseded_sequence is null
        or revision.superseded_sequence > snapshot_sequence)
      and (requested_project_name is null or exists (
        select 1 from public.ct_project_revisions project
        where project.workspace_id = artifact.workspace_id
          and project.project_id = artifact.project_id
          and project.display_name = requested_project_name
          and project.published_sequence <= snapshot_sequence
          and (project.superseded_sequence is null
            or project.superseded_sequence > snapshot_sequence)
      ))
      and (requested_since_days is null or revision.observed_at >=
        clock_timestamp() - make_interval(days => requested_since_days))
      and (requested_modified_since is null
        or revision.observed_at >= requested_modified_since)
  ), projected as materialized (
    select candidate.artifact_id, projection.payload
    from candidates candidate
    join public.ct_artifact_read_projections projection
      on projection.workspace_id = candidate.workspace_id
      and projection.content_sha256 = candidate.content_sha256
      and projection.projection_name = 'project.sessions'
      and projection.projection_version = 1
      and projection.variant = selected_variant
  ), flattened as (
    select projected.artifact_id, item.ordinality, item.value as item
    from projected
    cross join lateral jsonb_array_elements(projected.payload -> 'items')
      with ordinality item(value, ordinality)
    where requested_vendor is null
      or coalesce(item.value -> 'vendors', '[]'::jsonb) ? requested_vendor
  )
  select
    (select count(*) from candidates),
    (select count(*) from projected),
    coalesce(jsonb_agg(flattened.item order by
      flattened.item ->> 'project', flattened.artifact_id,
      flattened.ordinality), '[]'::jsonb)
  into candidate_count, projected_count, items
  from flattened;

  return jsonb_build_object(
    'workspace_id', target_workspace_id,
    'snapshot_sequence', snapshot_sequence,
    'complete', candidate_count = projected_count,
    'result', jsonb_build_object('items', items)
  );
end;
$$;

revoke all on function public.ct_ensure_artifact_payload_reference() from public;
revoke all on function public.ct_collector_stage_artifact_payload(jsonb)
  from public, anon;
revoke all on function public.ct_historical_artifacts(jsonb) from public, anon;
revoke all on function public.ct_project_sessions_projection(jsonb)
  from public, anon;
grant execute on function public.ct_collector_stage_artifact_payload(jsonb)
  to authenticated, service_role;
grant execute on function public.ct_historical_artifacts(jsonb)
  to authenticated, service_role;
grant execute on function public.ct_project_sessions_projection(jsonb)
  to authenticated, service_role;
