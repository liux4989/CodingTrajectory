-- CodingTrajectory remote control-plane foundation.
--
-- This migration owns durable control-plane state. Local SQLite databases and
-- vendor logs remain outside this schema and are never promoted as authority.

create table public.ct_workspaces (
  workspace_id uuid primary key default gen_random_uuid(),
  name text not null check (btrim(name) <> ''),
  created_by uuid not null references auth.users(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.ct_workspace_members (
  workspace_id uuid not null references public.ct_workspaces(workspace_id) on delete cascade,
  principal_id uuid not null references auth.users(id) on delete cascade,
  role text not null default 'member' check (role in ('owner', 'member')),
  created_at timestamptz not null default now(),
  primary key (workspace_id, principal_id)
);

create table public.ct_workspace_counters (
  workspace_id uuid primary key references public.ct_workspaces(workspace_id) on delete cascade,
  next_sequence bigint not null default 1 check (next_sequence > 0)
);

create or replace function public.ct_next_workspace_sequence(target_workspace_id uuid)
returns bigint
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  allocated_sequence bigint;
begin
  insert into public.ct_workspace_counters (workspace_id, next_sequence)
  values (target_workspace_id, 2)
  on conflict (workspace_id) do update
    set next_sequence = public.ct_workspace_counters.next_sequence + 1
  returning next_sequence - 1 into allocated_sequence;
  return allocated_sequence;
end;
$$;

revoke all on function public.ct_next_workspace_sequence(uuid) from public, anon, authenticated;
grant execute on function public.ct_next_workspace_sequence(uuid) to service_role;

create table public.ct_agents (
  workspace_id uuid not null references public.ct_workspaces(workspace_id) on delete cascade,
  agent_id uuid not null default gen_random_uuid(),
  principal_id uuid not null references auth.users(id),
  display_name text not null check (btrim(display_name) <> ''),
  created_at timestamptz not null default now(),
  revoked_at timestamptz,
  primary key (workspace_id, agent_id),
  unique (workspace_id, principal_id, agent_id)
);

create table public.ct_agent_capabilities (
  workspace_id uuid not null,
  agent_id uuid not null,
  capability text not null
    check (capability in ('read', 'ingest', 'living', 'estimate', 'admin')),
  created_at timestamptz not null default now(),
  primary key (workspace_id, agent_id, capability),
  foreign key (workspace_id, agent_id)
    references public.ct_agents(workspace_id, agent_id) on delete cascade
);

create table public.ct_projects (
  workspace_id uuid not null references public.ct_workspaces(workspace_id) on delete cascade,
  project_id uuid not null default gen_random_uuid(),
  current_revision bigint not null default 0 check (current_revision >= 0),
  current_published_sequence bigint,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (workspace_id, project_id)
);

create table public.ct_project_revisions (
  workspace_id uuid not null,
  project_id uuid not null,
  revision bigint not null check (revision > 0),
  display_name text not null check (btrim(display_name) <> ''),
  repository_identity text,
  published_sequence bigint not null check (published_sequence > 0),
  superseded_sequence bigint,
  archived boolean not null default false,
  committed_at timestamptz not null default now(),
  primary key (workspace_id, project_id, revision),
  foreign key (workspace_id, project_id)
    references public.ct_projects(workspace_id, project_id) on delete cascade,
  check (
    superseded_sequence is null
    or superseded_sequence > published_sequence
  )
);

create unique index ct_project_revisions_current_repository_idx
  on public.ct_project_revisions (workspace_id, repository_identity)
  where superseded_sequence is null and repository_identity is not null;

create table public.ct_project_aliases (
  workspace_id uuid not null,
  project_id uuid not null,
  alias text not null check (btrim(alias) <> ''),
  published_sequence bigint not null check (published_sequence > 0),
  superseded_sequence bigint,
  created_at timestamptz not null default now(),
  primary key (workspace_id, alias, published_sequence),
  foreign key (workspace_id, project_id)
    references public.ct_projects(workspace_id, project_id) on delete cascade,
  check (
    superseded_sequence is null
    or superseded_sequence > published_sequence
  )
);

create unique index ct_project_aliases_current_alias_idx
  on public.ct_project_aliases (workspace_id, alias)
  where superseded_sequence is null;

-- Host paths are private location hints, never portable project identity.
create table public.ct_agent_project_locations (
  workspace_id uuid not null,
  agent_id uuid not null,
  project_id uuid not null,
  location_uri text not null check (btrim(location_uri) <> ''),
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  primary key (workspace_id, agent_id, project_id, location_uri),
  foreign key (workspace_id, agent_id)
    references public.ct_agents(workspace_id, agent_id) on delete cascade,
  foreign key (workspace_id, project_id)
    references public.ct_projects(workspace_id, project_id) on delete cascade
);

create table public.ct_ingest_sources (
  workspace_id uuid not null,
  source_id uuid not null default gen_random_uuid(),
  origin_agent_id uuid not null,
  project_id uuid,
  vendor text not null check (btrim(vendor) <> ''),
  native_session_id text not null check (btrim(native_session_id) <> ''),
  current_epoch bigint not null default 1 check (current_epoch > 0),
  committed_source_sequence bigint not null default -1
    check (committed_source_sequence >= -1),
  created_at timestamptz not null default now(),
  last_observed_at timestamptz,
  primary key (workspace_id, source_id),
  unique (workspace_id, origin_agent_id, vendor, native_session_id),
  foreign key (workspace_id, origin_agent_id)
    references public.ct_agents(workspace_id, agent_id),
  foreign key (workspace_id, project_id)
    references public.ct_projects(workspace_id, project_id)
);

create table public.ct_source_observations (
  workspace_id uuid not null,
  source_id uuid not null,
  source_epoch bigint not null check (source_epoch > 0),
  source_sequence bigint not null check (source_sequence >= 0),
  event_id text not null check (btrim(event_id) <> ''),
  schema_version text not null,
  parser_version text not null,
  content_sha256 text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
  observed_at timestamptz not null,
  received_at timestamptz not null default now(),
  payload jsonb not null,
  state text not null default 'accepted'
    check (state in ('pending', 'accepted', 'rejected')),
  rejection_reason text,
  primary key (workspace_id, source_id, source_epoch, source_sequence),
  unique (workspace_id, source_id, source_epoch, event_id),
  foreign key (workspace_id, source_id)
    references public.ct_ingest_sources(workspace_id, source_id)
);

create table public.ct_artifacts (
  workspace_id uuid not null,
  artifact_id uuid not null,
  project_id uuid,
  current_revision bigint not null default 0 check (current_revision >= 0),
  current_published_sequence bigint,
  state text not null default 'accepting'
    check (state in ('accepting', 'terminal_observation', 'tombstoned')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (workspace_id, artifact_id),
  foreign key (workspace_id, project_id)
    references public.ct_projects(workspace_id, project_id)
);

create table public.ct_artifact_revisions (
  workspace_id uuid not null,
  artifact_id uuid not null,
  revision bigint not null check (revision > 0),
  schema_version text not null,
  payload jsonb not null,
  content_sha256 text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
  source_vector jsonb not null,
  published_sequence bigint not null check (published_sequence > 0),
  superseded_sequence bigint,
  observed_at timestamptz not null,
  committed_at timestamptz not null default now(),
  primary key (workspace_id, artifact_id, revision),
  unique (workspace_id, artifact_id, content_sha256),
  unique (workspace_id, published_sequence),
  foreign key (workspace_id, artifact_id)
    references public.ct_artifacts(workspace_id, artifact_id) on delete cascade,
  check (
    superseded_sequence is null
    or superseded_sequence > published_sequence
  )
);

create index ct_artifact_revisions_visible_at_idx
  on public.ct_artifact_revisions (
    workspace_id,
    artifact_id,
    published_sequence,
    superseded_sequence
  );

create table public.ct_ingest_receipts (
  workspace_id uuid not null,
  receipt_id uuid not null default gen_random_uuid(),
  agent_id uuid not null,
  idempotency_key text not null check (btrim(idempotency_key) <> ''),
  request_sha256 text not null check (request_sha256 ~ '^[0-9a-f]{64}$'),
  outcome text not null check (outcome in ('accepted', 'duplicate', 'rejected', 'conflict')),
  committed_sequence bigint,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  primary key (workspace_id, receipt_id),
  unique (workspace_id, agent_id, idempotency_key),
  foreign key (workspace_id, agent_id)
    references public.ct_agents(workspace_id, agent_id)
);

create table public.ct_change_log (
  workspace_id uuid not null,
  sequence bigint not null check (sequence > 0),
  authority text not null
    check (authority in ('historical', 'project_inventory', 'living', 'estimation')),
  kind text not null check (btrim(kind) <> ''),
  resource_id text not null check (btrim(resource_id) <> ''),
  resource_revision bigint,
  committed_at timestamptz not null default now(),
  payload jsonb not null default '{}'::jsonb,
  primary key (workspace_id, sequence),
  foreign key (workspace_id) references public.ct_workspaces(workspace_id) on delete cascade
);

alter table public.ct_project_revisions
  add foreign key (workspace_id, published_sequence)
  references public.ct_change_log(workspace_id, sequence)
  deferrable initially deferred;
alter table public.ct_project_aliases
  add foreign key (workspace_id, published_sequence)
  references public.ct_change_log(workspace_id, sequence)
  deferrable initially deferred;
alter table public.ct_artifact_revisions
  add foreign key (workspace_id, published_sequence)
  references public.ct_change_log(workspace_id, sequence)
  deferrable initially deferred;
alter table public.ct_ingest_receipts
  add foreign key (workspace_id, committed_sequence)
  references public.ct_change_log(workspace_id, sequence)
  deferrable initially deferred;

create table public.ct_projection_outbox (
  workspace_id uuid not null,
  outbox_id bigint generated always as identity,
  workspace_sequence bigint not null,
  projection_name text not null,
  resource_id text not null,
  payload jsonb not null default '{}'::jsonb,
  state text not null default 'pending'
    check (state in ('pending', 'leased', 'completed', 'failed')),
  available_at timestamptz not null default now(),
  lease_owner text,
  lease_expires_at timestamptz,
  attempts integer not null default 0 check (attempts >= 0),
  last_error text,
  created_at timestamptz not null default now(),
  completed_at timestamptz,
  primary key (workspace_id, outbox_id),
  unique (workspace_id, workspace_sequence, projection_name, resource_id),
  foreign key (workspace_id, workspace_sequence)
    references public.ct_change_log(workspace_id, sequence) on delete cascade
);

create index ct_projection_outbox_claim_idx
  on public.ct_projection_outbox (state, available_at)
  where state in ('pending', 'failed');

create table public.ct_agent_leases (
  workspace_id uuid not null,
  agent_id uuid not null,
  agent_instance_id uuid not null,
  heartbeat_at timestamptz not null,
  lease_expires_at timestamptz not null,
  source_watermarks jsonb not null default '{}'::jsonb,
  runtime_state text not null default 'unknown'
    check (runtime_state in ('living', 'idle', 'terminal', 'unknown')),
  updated_at timestamptz not null default now(),
  primary key (workspace_id, agent_instance_id),
  foreign key (workspace_id, agent_id)
    references public.ct_agents(workspace_id, agent_id) on delete cascade,
  check (lease_expires_at > heartbeat_at)
);

create table public.ct_living_observations (
  workspace_id uuid not null,
  agent_instance_id uuid not null,
  observation_sequence bigint not null check (observation_sequence > 0),
  workspace_sequence bigint not null check (workspace_sequence > 0),
  observed_at timestamptz not null,
  received_at timestamptz not null default now(),
  kind text not null check (btrim(kind) <> ''),
  payload jsonb not null,
  primary key (workspace_id, agent_instance_id, observation_sequence),
  unique (workspace_id, workspace_sequence),
  foreign key (workspace_id, agent_instance_id)
    references public.ct_agent_leases(workspace_id, agent_instance_id) on delete cascade,
  foreign key (workspace_id, workspace_sequence)
    references public.ct_change_log(workspace_id, sequence)
);

create table public.ct_estimation_jobs (
  workspace_id uuid not null,
  job_id uuid not null default gen_random_uuid(),
  requested_by uuid not null references auth.users(id),
  idempotency_key text not null check (btrim(idempotency_key) <> ''),
  kind text not null check (kind in ('predict', 'bind', 'backfill')),
  status text not null default 'pending'
    check (status in ('pending', 'leased', 'completed', 'failed', 'cancelled')),
  snapshot_sequence bigint not null check (snapshot_sequence > 0),
  spec jsonb not null,
  available_at timestamptz not null default now(),
  lease_owner text,
  lease_expires_at timestamptz,
  attempts integer not null default 0 check (attempts >= 0),
  created_at timestamptz not null default now(),
  started_at timestamptz,
  completed_at timestamptz,
  last_error text,
  primary key (workspace_id, job_id),
  unique (workspace_id, requested_by, idempotency_key),
  foreign key (workspace_id) references public.ct_workspaces(workspace_id) on delete cascade
);

create index ct_estimation_jobs_claim_idx
  on public.ct_estimation_jobs (status, available_at)
  where status in ('pending', 'failed');

create table public.ct_estimation_attempts (
  workspace_id uuid not null,
  job_id uuid not null,
  attempt_number integer not null check (attempt_number > 0),
  worker_id text not null check (btrim(worker_id) <> ''),
  status text not null check (status in ('running', 'succeeded', 'failed', 'abandoned')),
  started_at timestamptz not null default now(),
  heartbeat_at timestamptz,
  completed_at timestamptz,
  error jsonb,
  receipt jsonb,
  primary key (workspace_id, job_id, attempt_number),
  foreign key (workspace_id, job_id)
    references public.ct_estimation_jobs(workspace_id, job_id) on delete cascade
);

create table public.ct_forecast_events (
  workspace_id uuid not null,
  prediction_id uuid not null,
  event_sequence bigint not null check (event_sequence > 0),
  workspace_sequence bigint not null check (workspace_sequence > 0),
  job_id uuid,
  event_type text not null
    check (event_type in ('forecast_created', 'attempt_failed', 'forecast_bound', 'forecast_compared')),
  snapshot_sequence bigint not null check (snapshot_sequence > 0),
  payload jsonb not null,
  recorded_at timestamptz not null default now(),
  primary key (workspace_id, prediction_id, event_sequence),
  unique (workspace_id, workspace_sequence),
  foreign key (workspace_id, job_id)
    references public.ct_estimation_jobs(workspace_id, job_id),
  foreign key (workspace_id, workspace_sequence)
    references public.ct_change_log(workspace_id, sequence)
);

-- Membership checks are isolated in a security-definer function so policies on
-- ct_workspace_members do not recurse into themselves.
create or replace function public.ct_is_workspace_member(target_workspace_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select exists (
    select 1
    from public.ct_workspace_members member
    where member.workspace_id = target_workspace_id
      and member.principal_id = auth.uid()
  );
$$;

revoke all on function public.ct_is_workspace_member(uuid) from public;
grant execute on function public.ct_is_workspace_member(uuid) to authenticated, service_role;

alter table public.ct_workspaces enable row level security;
alter table public.ct_workspace_members enable row level security;
alter table public.ct_workspace_counters enable row level security;
alter table public.ct_agents enable row level security;
alter table public.ct_agent_capabilities enable row level security;
alter table public.ct_projects enable row level security;
alter table public.ct_project_revisions enable row level security;
alter table public.ct_project_aliases enable row level security;
alter table public.ct_agent_project_locations enable row level security;
alter table public.ct_ingest_sources enable row level security;
alter table public.ct_source_observations enable row level security;
alter table public.ct_artifacts enable row level security;
alter table public.ct_artifact_revisions enable row level security;
alter table public.ct_ingest_receipts enable row level security;
alter table public.ct_change_log enable row level security;
alter table public.ct_projection_outbox enable row level security;
alter table public.ct_agent_leases enable row level security;
alter table public.ct_living_observations enable row level security;
alter table public.ct_estimation_jobs enable row level security;
alter table public.ct_estimation_attempts enable row level security;
alter table public.ct_forecast_events enable row level security;

create policy ct_workspaces_member_read on public.ct_workspaces
  for select to authenticated
  using (public.ct_is_workspace_member(workspace_id));
create policy ct_workspace_members_member_read on public.ct_workspace_members
  for select to authenticated
  using (public.ct_is_workspace_member(workspace_id));
create policy ct_agents_member_read on public.ct_agents
  for select to authenticated
  using (public.ct_is_workspace_member(workspace_id));
create policy ct_agent_capabilities_member_read on public.ct_agent_capabilities
  for select to authenticated
  using (public.ct_is_workspace_member(workspace_id));
create policy ct_projects_member_read on public.ct_projects
  for select to authenticated
  using (public.ct_is_workspace_member(workspace_id));
create policy ct_project_revisions_member_read on public.ct_project_revisions
  for select to authenticated
  using (public.ct_is_workspace_member(workspace_id));
create policy ct_project_aliases_member_read on public.ct_project_aliases
  for select to authenticated
  using (public.ct_is_workspace_member(workspace_id));
create policy ct_ingest_sources_member_read on public.ct_ingest_sources
  for select to authenticated
  using (public.ct_is_workspace_member(workspace_id));
create policy ct_artifacts_member_read on public.ct_artifacts
  for select to authenticated
  using (public.ct_is_workspace_member(workspace_id));
create policy ct_artifact_revisions_member_read on public.ct_artifact_revisions
  for select to authenticated
  using (public.ct_is_workspace_member(workspace_id));
create policy ct_ingest_receipts_member_read on public.ct_ingest_receipts
  for select to authenticated
  using (public.ct_is_workspace_member(workspace_id));
create policy ct_change_log_member_read on public.ct_change_log
  for select to authenticated
  using (public.ct_is_workspace_member(workspace_id));
create policy ct_agent_leases_member_read on public.ct_agent_leases
  for select to authenticated
  using (public.ct_is_workspace_member(workspace_id));
create policy ct_living_observations_member_read on public.ct_living_observations
  for select to authenticated
  using (public.ct_is_workspace_member(workspace_id));
create policy ct_estimation_jobs_member_read on public.ct_estimation_jobs
  for select to authenticated
  using (public.ct_is_workspace_member(workspace_id));
create policy ct_estimation_attempts_member_read on public.ct_estimation_attempts
  for select to authenticated
  using (public.ct_is_workspace_member(workspace_id));
create policy ct_forecast_events_member_read on public.ct_forecast_events
  for select to authenticated
  using (public.ct_is_workspace_member(workspace_id));

-- Source payloads and operational queues are intentionally server-only in the
-- initial full-canonical workspace model. No authenticated SELECT policy is
-- defined for observations, counters, projection outbox, or host locations.
