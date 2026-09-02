-- Snapshot-pinned portable project inventory for authenticated workspace reads.

create or replace function public.ct_project_register(request jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  target_workspace_id uuid := (request ->> 'workspace_id')::uuid;
  target_agent_id uuid := (request ->> 'agent_id')::uuid;
  target_display_name text := nullif(btrim(request ->> 'display_name'), '');
  target_repository_identity text := nullif(btrim(request ->> 'repository_identity'), '');
  target_aliases jsonb := coalesce(request -> 'aliases', '[]'::jsonb);
  target_project_id uuid;
  current_revision_record public.ct_project_revisions%rowtype;
  current_aliases jsonb;
  requested_aliases jsonb;
  next_revision bigint;
  allocated_sequence bigint;
  alias_value text;
begin
  if not public.ct_collector_authorized(target_workspace_id, target_agent_id, 'ingest') then
    raise exception 'collector ingest capability is required' using errcode = '42501';
  end if;
  if target_display_name is null or jsonb_typeof(target_aliases) <> 'array' then
    raise exception 'display_name and an aliases array are required' using errcode = '22023';
  end if;

  select revision.project_id into target_project_id
  from public.ct_project_revisions revision
  where revision.workspace_id = target_workspace_id
    and revision.superseded_sequence is null
    and (
      (target_repository_identity is not null
        and revision.repository_identity = target_repository_identity)
      or lower(revision.display_name) = lower(target_display_name)
    )
  order by (revision.repository_identity = target_repository_identity) desc
  limit 1;
  if target_project_id is null then
    insert into public.ct_projects (workspace_id)
    values (target_workspace_id)
    returning project_id into target_project_id;
  end if;

  perform 1 from public.ct_projects project
  where project.workspace_id = target_workspace_id
    and project.project_id = target_project_id
  for update;
  select revision.* into current_revision_record
  from public.ct_project_revisions revision
  where revision.workspace_id = target_workspace_id
    and revision.project_id = target_project_id
    and revision.superseded_sequence is null;
  select coalesce(jsonb_agg(alias.alias order by lower(alias.alias), alias.alias), '[]'::jsonb)
  into current_aliases
  from public.ct_project_aliases alias
  where alias.workspace_id = target_workspace_id
    and alias.project_id = target_project_id
    and alias.superseded_sequence is null;
  select coalesce(jsonb_agg(value order by lower(value), value), '[]'::jsonb)
  into requested_aliases
  from (
    select distinct btrim(raw.value) as value
    from jsonb_array_elements_text(target_aliases) raw
    where btrim(raw.value) <> ''
      and lower(btrim(raw.value)) <> lower(target_display_name)
  ) normalized;
  if current_revision_record.revision is not null
    and current_revision_record.display_name = target_display_name
    and current_revision_record.repository_identity is not distinct from target_repository_identity
    and current_aliases = requested_aliases then
    return jsonb_build_object(
      'project_id', target_project_id,
      'revision', current_revision_record.revision,
      'committed_sequence', current_revision_record.published_sequence
    );
  end if;
  select current_revision + 1 into next_revision
  from public.ct_projects
  where workspace_id = target_workspace_id and project_id = target_project_id;
  allocated_sequence := public.ct_next_workspace_sequence(target_workspace_id);

  update public.ct_project_revisions
  set superseded_sequence = allocated_sequence
  where workspace_id = target_workspace_id
    and project_id = target_project_id
    and superseded_sequence is null;
  update public.ct_project_aliases
  set superseded_sequence = allocated_sequence
  where workspace_id = target_workspace_id
    and project_id = target_project_id
    and superseded_sequence is null;
  insert into public.ct_change_log (
    workspace_id, sequence, authority, kind, resource_id, resource_revision, payload
  ) values (
    target_workspace_id, allocated_sequence, 'project_inventory',
    'project_registered', target_project_id::text, next_revision, '{}'::jsonb
  );
  insert into public.ct_project_revisions (
    workspace_id, project_id, revision, display_name, repository_identity,
    published_sequence
  ) values (
    target_workspace_id, target_project_id, next_revision, target_display_name,
    target_repository_identity, allocated_sequence
  );
  for alias_value in
    select distinct btrim(value)
    from jsonb_array_elements_text(target_aliases)
    where btrim(value) <> '' and lower(btrim(value)) <> lower(target_display_name)
  loop
    insert into public.ct_project_aliases (
      workspace_id, project_id, alias, published_sequence
    ) values (
      target_workspace_id, target_project_id, alias_value, allocated_sequence
    );
  end loop;
  update public.ct_projects
  set current_revision = next_revision,
      current_published_sequence = allocated_sequence,
      updated_at = clock_timestamp()
  where workspace_id = target_workspace_id and project_id = target_project_id;
  return jsonb_build_object(
    'project_id', target_project_id,
    'revision', next_revision,
    'committed_sequence', allocated_sequence
  );
end;
$$;

create or replace function public.ct_project_inventory_snapshot(request jsonb)
returns jsonb
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
declare
  target_workspace_id uuid := (request ->> 'workspace_id')::uuid;
  requested_sequence bigint := nullif(request ->> 'snapshot_sequence', '')::bigint;
  requested_project_name text := nullif(btrim(request ->> 'project_name'), '');
  requested_since_days integer := nullif(request ->> 'since_days', '')::integer;
  requested_modified_since timestamptz :=
    nullif(request ->> 'modified_since', '')::timestamptz;
  requested_vendor text := nullif(btrim(request ->> 'agent_vendor'), '');
  latest_sequence bigint;
  snapshot_sequence bigint;
  activity_cutoff timestamptz;
  projects jsonb;
begin
  if target_workspace_id is null then
    raise exception 'workspace_id is required' using errcode = '22023';
  end if;
  if coalesce(auth.role(), '') <> 'service_role'
    and not public.ct_is_workspace_member(target_workspace_id) then
    raise exception 'workspace membership is required' using errcode = '42501';
  end if;
  if requested_since_days is not null and requested_since_days < 1 then
    raise exception 'since_days must be positive' using errcode = '22023';
  end if;

  select coalesce(max(change.sequence), 0) into latest_sequence
  from public.ct_change_log change
  where change.workspace_id = target_workspace_id;
  snapshot_sequence := coalesce(requested_sequence, latest_sequence);
  if snapshot_sequence < 0 or snapshot_sequence > latest_sequence then
    raise exception 'snapshot_sequence must be between zero and the latest workspace sequence'
      using errcode = '22023';
  end if;

  activity_cutoff := coalesce(
    requested_modified_since,
    case
      when requested_since_days is not null
        then transaction_timestamp() - make_interval(days => requested_since_days)
    end
  );

  with visible_projects as (
    select
      revision.workspace_id,
      revision.project_id,
      revision.revision,
      revision.display_name,
      revision.repository_identity,
      revision.published_sequence,
      revision.committed_at
    from public.ct_project_revisions revision
    where revision.workspace_id = target_workspace_id
      and revision.published_sequence <= snapshot_sequence
      and (
        revision.superseded_sequence is null
        or revision.superseded_sequence > snapshot_sequence
      )
      and not revision.archived
  ),
  visible_aliases as (
    select
      alias.project_id,
      jsonb_agg(alias.alias order by lower(alias.alias), alias.alias) as aliases
    from public.ct_project_aliases alias
    where alias.workspace_id = target_workspace_id
      and alias.published_sequence <= snapshot_sequence
      and (
        alias.superseded_sequence is null
        or alias.superseded_sequence > snapshot_sequence
      )
    group by alias.project_id
  ),
  visible_sources as (
    select
      source.project_id,
      source.vendor,
      observation.observed_at
    from public.ct_projection_outbox outbox
    join public.ct_ingest_sources source
      on source.workspace_id = outbox.workspace_id
      and source.source_id::text = outbox.resource_id
    join public.ct_source_observations observation
      on observation.workspace_id = source.workspace_id
      and observation.source_id = source.source_id
      and observation.source_epoch = (outbox.payload ->> 'source_epoch')::bigint
      and observation.source_sequence = (outbox.payload ->> 'source_sequence')::bigint
    where outbox.workspace_id = target_workspace_id
      and outbox.projection_name = 'project_source_observation'
      and outbox.workspace_sequence <= snapshot_sequence
      and source.project_id is not null
  ),
  project_activity as (
    select
      project.project_id,
      greatest(
        project.committed_at,
        coalesce(max(source.observed_at), project.committed_at)
      ) as modified_at
    from visible_projects project
    left join visible_sources source on source.project_id = project.project_id
    group by project.project_id, project.committed_at
  ),
  project_vendors as (
    select
      source.project_id,
      jsonb_agg(distinct source.vendor order by source.vendor) as vendors
    from visible_sources source
    group by source.project_id
  ),
  matching_projects as (
    select
      project.project_id,
      project.revision,
      project.display_name,
      project.repository_identity,
      project.published_sequence,
      coalesce(alias.aliases, '[]'::jsonb) as aliases,
      activity.modified_at,
      coalesce(vendor.vendors, '[]'::jsonb) as vendors
    from visible_projects project
    join project_activity activity on activity.project_id = project.project_id
    left join visible_aliases alias on alias.project_id = project.project_id
    left join project_vendors vendor on vendor.project_id = project.project_id
    where (
        requested_project_name is null
        or lower(project.display_name) = lower(requested_project_name)
        or exists (
          select 1
          from jsonb_array_elements_text(coalesce(alias.aliases, '[]'::jsonb)) name
          where lower(name) = lower(requested_project_name)
        )
      )
      and (activity_cutoff is null or activity.modified_at >= activity_cutoff)
      and (
        requested_vendor is null
        or coalesce(vendor.vendors, '[]'::jsonb) ? requested_vendor
      )
  )
  select coalesce(
    jsonb_agg(
      jsonb_build_object(
        'project_id', project.project_id,
        'revision', project.revision,
        'display_name', project.display_name,
        'repository_identity', project.repository_identity,
        'aliases', project.aliases,
        'published_sequence', project.published_sequence,
        'modified_at', project.modified_at,
        'vendors', project.vendors
      ) order by lower(project.display_name), project.display_name, project.project_id
    ),
    '[]'::jsonb
  ) into projects
  from matching_projects project;

  return jsonb_build_object(
    'workspace_id', target_workspace_id,
    'snapshot_sequence', snapshot_sequence,
    'projects', projects
  );
end;
$$;

revoke all on function public.ct_project_register(jsonb) from public, anon;
revoke all on function public.ct_project_inventory_snapshot(jsonb) from public, anon;
grant execute on function public.ct_project_register(jsonb)
  to authenticated, service_role;
grant execute on function public.ct_project_inventory_snapshot(jsonb)
  to authenticated, service_role;
