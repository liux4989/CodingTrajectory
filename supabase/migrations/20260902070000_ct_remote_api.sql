-- Lightweight membership check and workspace snapshot pin for the Python API.

create or replace function public.ct_workspace_snapshot(request jsonb)
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
begin
  if target_workspace_id is null then
    raise exception 'workspace_id is required' using errcode = '22023';
  end if;
  if coalesce(auth.role(), '') <> 'service_role'
    and not public.ct_is_workspace_member(target_workspace_id) then
    raise exception 'workspace membership is required' using errcode = '42501';
  end if;
  select coalesce(max(sequence), 0) into latest_sequence
  from public.ct_change_log where workspace_id = target_workspace_id;
  snapshot_sequence := coalesce(requested_sequence, latest_sequence);
  if snapshot_sequence < 0 or snapshot_sequence > latest_sequence then
    raise exception 'snapshot_sequence must be between zero and the latest workspace sequence'
      using errcode = '22023';
  end if;
  return jsonb_build_object(
    'workspace_id', target_workspace_id,
    'snapshot_sequence', snapshot_sequence
  );
end;
$$;

revoke all on function public.ct_workspace_snapshot(jsonb) from public, anon;
grant execute on function public.ct_workspace_snapshot(jsonb)
  to authenticated, service_role;
