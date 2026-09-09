-- Retain bounded user-request and assistant-response previews in Chronicle.
--
-- The graph schema already permits user_request.content and
-- item.measurements.text_preview. This migration narrows those two prose fields
-- to 280 characters while continuing to reject transcript/tool bodies and
-- unsafe strings everywhere else in the artifact.

create or replace function public.ct_chronicle_json_safe(value jsonb)
returns boolean
language plpgsql
immutable
set search_path = public, pg_temp
as $$
declare
  entry record;
  child jsonb;
  text_value text;
  normalized_key text;
begin
  case jsonb_typeof(value)
    when 'object' then
      for entry in select kv.key, kv.value
        from jsonb_each(ct_chronicle_json_safe.value) kv loop
        normalized_key := lower(replace(entry.key, '-', '_'));
        if normalized_key = any(array[
          'input', 'output', 'command', 'cwd', 'vendor_data', 'event_ids',
          'source_event_id', 'user_request_event_id', 'tool_call_id',
          'input_summary', 'trace_id', 'turn_id_raw', 'reason', 'trigger'
        ]) then
          return false;
        end if;
        if normalized_key = 'text_preview' then
          if jsonb_typeof(entry.value) <> 'string' then
            return false;
          end if;
          text_value := entry.value #>> '{}';
          if length(text_value) = 0 or length(text_value) > 280 then
            return false;
          end if;
          continue;
        end if;
        if normalized_key = 'content'
          and jsonb_typeof(entry.value) = 'string' then
          text_value := entry.value #>> '{}';
          if length(text_value) = 0 or length(text_value) > 280 then
            return false;
          end if;
          continue;
        end if;
        if normalized_key = any(array['title', 'preview'])
          and entry.value <> 'null'::jsonb then return false; end if;
        if normalized_key = 'description' and entry.value not in (
          'null'::jsonb, '"tests"'::jsonb, '"checks"'::jsonb, '"command"'::jsonb
        ) then return false; end if;
        if normalized_key = 'content' and entry.value <> 'false'::jsonb then
          return false;
        end if;
        if normalized_key = 'events' and entry.value <> 'false'::jsonb then
          return false;
        end if;
        if normalized_key like '%data_uri%'
          or normalized_key like '%media%'
          or normalized_key like '%blob%' then
          return false;
        end if;
        if not public.ct_chronicle_json_safe(entry.value) then
          return false;
        end if;
      end loop;
      return true;
    when 'array' then
      for child in select item from jsonb_array_elements(value) item loop
        if not public.ct_chronicle_json_safe(child) then
          return false;
        end if;
      end loop;
      return true;
    when 'string' then
      text_value := value #>> '{}';
      if length(text_value) > 512
        or lower(ltrim(text_value)) like 'data:%'
        or text_value ~ '(?:^|[^A-Za-z0-9])(?:~/|/Users/|/home/|/root/|/private/|/tmp/|/var/|/Volumes/|/workspace/|/workspaces/|/mnt/|/srv/|/opt/|[A-Za-z]:[\\/])'
        or (
          length(text_value) >= 128
          and text_value ~ '^[A-Za-z0-9+/]+={0,2}$'
        ) then
        return false;
      end if;
      return true;
    when 'null' then
      return false;
    else
      return true;
  end case;
end;
$$;

revoke all on function public.ct_chronicle_json_safe(jsonb)
from public, anon, authenticated;
