-- Direct publication of locally assembled, bounded shareable graph artifacts.
--
-- Existing v1/v2 observations and revisions remain immutable. New source
-- observations contain checkpoint metadata only; graph bodies are validated
-- and published atomically by the authenticated project collector.

create extension if not exists pgcrypto with schema extensions;

create or replace function public.ct_jsonb_object_matches(
  value jsonb,
  allowed_keys text[],
  required_keys text[]
) returns boolean
language sql
immutable
parallel safe
set search_path = public, pg_temp
as $$
  select coalesce(
    jsonb_typeof(value) = 'object'
    and not exists (
      select 1 from jsonb_object_keys(value) as present(key)
      where not (present.key = any(allowed_keys))
    )
    and not exists (
      select 1 from unnest(required_keys) as required(key)
      where not (value ? required.key)
    ),
    false
  );
$$;

create or replace function public.ct_jsonb_nonnegative_integer(value jsonb)
returns boolean
language sql
immutable
parallel safe
set search_path = public, pg_temp
as $$
  select coalesce(
    jsonb_typeof(value) = 'number'
    and value #>> '{}' ~ '^(0|[1-9][0-9]*)$',
    false
  );
$$;

create or replace function public.ct_canonical_json(value jsonb)
returns text
language plpgsql
immutable
parallel safe
set search_path = public, pg_temp
as $$
declare
  rendered text;
begin
  case jsonb_typeof(value)
    when 'object' then
      select '{' || coalesce(string_agg(
        to_json(entry.key)::text || ':' || public.ct_canonical_json(entry.value),
        ',' order by entry.key
      ), '') || '}' into rendered
      from jsonb_each(value) as entry(key, value);
      return rendered;
    when 'array' then
      select '[' || coalesce(string_agg(
        public.ct_canonical_json(entry.value), ',' order by entry.ordinality
      ), '') || ']' into rendered
      from jsonb_array_elements(value) with ordinality as entry(value, ordinality);
      return rendered;
    when 'string' then
      return to_json(value #>> '{}')::text;
    else
      return value::text;
  end case;
end;
$$;

create or replace function public.ct_jsonb_sha256(value jsonb)
returns text
language sql
immutable
parallel safe
set search_path = public, pg_temp, extensions
as $$
  select encode(
    digest(convert_to(public.ct_canonical_json(value), 'UTF8'), 'sha256'),
    'hex'
  );
$$;

create or replace function public.ct_shareable_json_safe(value jsonb)
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
        from jsonb_each(ct_shareable_json_safe.value) kv loop
        normalized_key := lower(replace(entry.key, '-', '_'));
        if normalized_key = any(array[
          'input', 'output', 'command', 'cwd', 'vendor_data', 'event_ids',
          'source_event_id', 'user_request_event_id', 'tool_call_id',
          'input_summary', 'trace_id', 'turn_id_raw', 'reason', 'trigger'
        ]) then
          return false;
        end if;
        if normalized_key = any(array['title', 'preview', 'text_preview'])
          and entry.value <> 'null'::jsonb then return false; end if;
        if normalized_key = 'plan_actions' and entry.value <> '[]'::jsonb
          then return false; end if;
        if normalized_key = 'description' and entry.value not in (
          'null'::jsonb, '"tests"'::jsonb, '"checks"'::jsonb, '"command"'::jsonb
        ) then return false; end if;
        if normalized_key = 'content' and entry.value not in (
          'false'::jsonb, '"[content omitted]"'::jsonb
        ) then return false; end if;
        if normalized_key = 'events' and entry.value <> 'false'::jsonb then
          return false;
        end if;
        if normalized_key like '%data_uri%'
          or normalized_key like '%media%'
          or normalized_key like '%blob%' then
          return false;
        end if;
        if not public.ct_shareable_json_safe(entry.value) then
          return false;
        end if;
      end loop;
      return true;
    when 'array' then
      for child in select item from jsonb_array_elements(value) item loop
        if not public.ct_shareable_json_safe(child) then
          return false;
        end if;
      end loop;
      return true;
    when 'string' then
      text_value := value #>> '{}';
      if length(text_value) > 512
        or lower(ltrim(text_value)) like 'data:%'
        or text_value ~ '^(?:/Users/|/home/|/root/|/private/|/tmp/|/var/|/Volumes/|/workspace/|/workspaces/|/mnt/|/srv/|/opt/)'
        or text_value like '~/%'
        or text_value ~ '^[A-Za-z]:[\\/]'
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

create or replace function public.ct_shareable_graph_valid(value jsonb)
returns boolean
language plpgsql
immutable
set search_path = public, pg_temp
as $$
declare
  session_value jsonb;
  topology_value jsonb;
  origin_value jsonb;
  runtime_value jsonb;
  measurements_value jsonb;
  source_value jsonb;
  size_value jsonb;
  turn_value jsonb;
  request_value jsonb;
  usage_value jsonb;
  category_value jsonb;
  user_request_value jsonb;
  team_value jsonb;
  member_value jsonb;
  task_value jsonb;
  item_value jsonb;
  item_measurements_value jsonb;
  summary_value jsonb;
  semantic_value jsonb;
  edge_value jsonb;
  turn_count integer := 0;
  item_count integer := 0;
  distinct_count integer;
begin
  if not public.ct_jsonb_object_matches(
    ct_shareable_graph_valid.value,
    array['schema_version', 'graph', 'sessions', 'edges', 'coverage'],
    array['schema_version', 'graph', 'sessions', 'edges', 'coverage']
  ) or ct_shareable_graph_valid.value ->> 'schema_version' <> 'ct.shareable_graph.v1'
    or not public.ct_shareable_json_safe(ct_shareable_graph_valid.value) then
    return false;
  end if;

  if not public.ct_jsonb_object_matches(
    ct_shareable_graph_valid.value -> 'graph',
    array[
      'root_session_id', 'project', 'started_at', 'ended_at', 'status',
      'session_count', 'turn_count', 'item_count'
    ],
    array['root_session_id', 'session_count', 'turn_count', 'item_count']
  ) or not public.ct_jsonb_nonnegative_integer(
      ct_shareable_graph_valid.value -> 'graph' -> 'session_count'
    )
    or not public.ct_jsonb_nonnegative_integer(
      ct_shareable_graph_valid.value -> 'graph' -> 'turn_count'
    )
    or not public.ct_jsonb_nonnegative_integer(
      ct_shareable_graph_valid.value -> 'graph' -> 'item_count'
    )
    or (ct_shareable_graph_valid.value -> 'graph' ->> 'root_session_id')::uuid is null then
    return false;
  end if;

  if not public.ct_jsonb_object_matches(
    ct_shareable_graph_valid.value -> 'coverage',
    array[
      'content', 'events', 'topology', 'usage', 'measurements',
      'semantic_previews'
    ],
    array[
      'content', 'events', 'topology', 'usage', 'measurements',
      'semantic_previews'
    ]
  ) or ct_shareable_graph_valid.value -> 'coverage' <> '{
    "content": false,
    "events": false,
    "topology": true,
    "usage": true,
    "measurements": true,
    "semantic_previews": false
  }'::jsonb then
    return false;
  end if;

  if jsonb_typeof(ct_shareable_graph_valid.value -> 'sessions') <> 'array'
    or jsonb_array_length(ct_shareable_graph_valid.value -> 'sessions') = 0
    or jsonb_typeof(ct_shareable_graph_valid.value -> 'edges') <> 'array' then
    return false;
  end if;

  select count(distinct session_entry ->> 'session_id') into distinct_count
  from jsonb_array_elements(ct_shareable_graph_valid.value -> 'sessions') as sessions(session_entry);
  if distinct_count <> jsonb_array_length(ct_shareable_graph_valid.value -> 'sessions')
    or not exists (
      select 1
      from jsonb_array_elements(ct_shareable_graph_valid.value -> 'sessions') as sessions(session_entry)
      where session_entry ->> 'session_id' =
        ct_shareable_graph_valid.value -> 'graph' ->> 'root_session_id'
    ) then
    return false;
  end if;

  for session_value in
    select session_entry
    from jsonb_array_elements(ct_shareable_graph_valid.value -> 'sessions') as sessions(session_entry)
  loop
    if not public.ct_jsonb_object_matches(
      session_value,
      array[
        'session_id', 'parent_session_id', 'vendor', 'started_at', 'ended_at',
        'status', 'model', 'reasoning_effort', 'title', 'preview', 'topology',
        'runtime', 'measurements', 'turns'
      ],
      array[
        'session_id', 'vendor', 'started_at', 'status', 'topology', 'runtime',
        'measurements', 'turns'
      ]
    ) or (session_value ->> 'session_id')::uuid is null
      or session_value ->> 'vendor' not in ('codex_cli', 'claude_code', 'pi')
      or (
        session_value ? 'parent_session_id'
        and (session_value ->> 'parent_session_id')::uuid is null
      )
      or jsonb_typeof(session_value -> 'runtime') <> 'array'
      or jsonb_typeof(session_value -> 'turns') <> 'array' then
      return false;
    end if;

    topology_value := session_value -> 'topology';
    if not public.ct_jsonb_object_matches(
      topology_value,
      array[
        'sidechain', 'forked', 'spawned', 'spawn_depth',
        'multi_agent_version', 'multi_agent_mode', 'spawn_origins'
      ],
      array['sidechain', 'forked', 'spawned', 'spawn_origins']
    ) or jsonb_typeof(topology_value -> 'sidechain') <> 'boolean'
      or jsonb_typeof(topology_value -> 'forked') <> 'boolean'
      or jsonb_typeof(topology_value -> 'spawned') <> 'boolean'
      or jsonb_typeof(topology_value -> 'spawn_origins') <> 'array'
      or (
        topology_value ? 'spawn_depth'
        and not public.ct_jsonb_nonnegative_integer(
          topology_value -> 'spawn_depth'
        )
      ) then
      return false;
    end if;
    for origin_value in
      select origin_entry
      from jsonb_array_elements(topology_value -> 'spawn_origins')
        as origins(origin_entry)
    loop
      if not public.ct_jsonb_object_matches(
        origin_value,
        array['target_session_id', 'turn_id', 'item_id', 'tool_name'],
        array['target_session_id']
      ) or (origin_value ->> 'target_session_id')::uuid is null
        or (
          origin_value ? 'turn_id'
          and (origin_value ->> 'turn_id')::uuid is null
        )
        or (
          origin_value ? 'item_id'
          and (origin_value ->> 'item_id')::uuid is null
        ) then
        return false;
      end if;
    end loop;

    for runtime_value in
      select runtime_entry
      from jsonb_array_elements(session_value -> 'runtime') as runtime(runtime_entry)
    loop
      if not public.ct_jsonb_object_matches(
        runtime_value,
        array[
          'timestamp', 'kind', 'duration_ms', 'time_to_first_token_ms',
          'num_turns', 'pre_tokens', 'post_tokens',
          'cumulative_dropped_tokens', 'effort_from', 'effort_to'
        ],
        array['timestamp', 'kind']
      ) or exists (
        select 1
        from jsonb_each(runtime_value) numeric_entry
        where numeric_entry.key in (
            'duration_ms', 'time_to_first_token_ms', 'num_turns', 'pre_tokens',
            'post_tokens', 'cumulative_dropped_tokens'
          )
          and not public.ct_jsonb_nonnegative_integer(numeric_entry.value)
      ) then
        return false;
      end if;
    end loop;

    measurements_value := session_value -> 'measurements';
    if not public.ct_jsonb_object_matches(
      measurements_value,
      array['context_sources', 'llm_response_count', 'llm_response_text_sizes'],
      array['context_sources', 'llm_response_count', 'llm_response_text_sizes']
    ) or jsonb_typeof(measurements_value -> 'context_sources') <> 'array'
      or not public.ct_jsonb_nonnegative_integer(
        measurements_value -> 'llm_response_count'
      )
      or jsonb_typeof(measurements_value -> 'llm_response_text_sizes') <> 'array' then
      return false;
    end if;
    for source_value in
      select source_entry
      from jsonb_array_elements(measurements_value -> 'context_sources')
        as sources(source_entry)
    loop
      if not public.ct_jsonb_object_matches(
        source_value,
        array['timestamp', 'key', 'label', 'reported_tokens', 'chars', 'tokens'],
        array['timestamp', 'key', 'label', 'chars', 'tokens']
      ) or not public.ct_jsonb_nonnegative_integer(source_value -> 'chars')
        or not public.ct_jsonb_nonnegative_integer(source_value -> 'tokens')
        or (
          source_value ? 'reported_tokens'
          and not public.ct_jsonb_nonnegative_integer(
            source_value -> 'reported_tokens'
          )
        ) then
        return false;
      end if;
    end loop;
    for size_value in
      select size_entry
      from jsonb_array_elements(measurements_value -> 'llm_response_text_sizes')
        as sizes(size_entry)
    loop
      if not public.ct_jsonb_object_matches(
        size_value,
        array['timestamp', 'chars', 'tokens'],
        array['timestamp', 'chars', 'tokens']
      ) or not public.ct_jsonb_nonnegative_integer(size_value -> 'chars')
        or not public.ct_jsonb_nonnegative_integer(size_value -> 'tokens') then
        return false;
      end if;
    end loop;

    turn_count := turn_count + jsonb_array_length(session_value -> 'turns');
    for turn_value in
      select turn_entry
      from jsonb_array_elements(session_value -> 'turns') as turns(turn_entry)
    loop
      if not public.ct_jsonb_object_matches(
        turn_value,
        array[
          'turn_id', 'sequence', 'started_at', 'completed_at', 'status',
          'user_request', 'requests', 'items', 'team_state'
        ],
        array['turn_id', 'sequence', 'started_at', 'status', 'requests', 'items']
      ) or (turn_value ->> 'turn_id')::uuid is null
        or not public.ct_jsonb_nonnegative_integer(turn_value -> 'sequence')
        or jsonb_typeof(turn_value -> 'requests') <> 'array'
        or jsonb_typeof(turn_value -> 'items') <> 'array' then
        return false;
      end if;

      if turn_value ? 'user_request' then
        user_request_value := turn_value -> 'user_request';
        if not public.ct_jsonb_object_matches(
          user_request_value,
          array['request_id', 'type', 'source', 'content', 'chars', 'tokens'],
          array['request_id', 'type', 'source', 'content']
        ) or (user_request_value ->> 'request_id')::uuid is null
          or user_request_value ->> 'type' not in ('message', 'command')
          or (user_request_value ? 'chars') <> (user_request_value ? 'tokens')
          or (
            user_request_value ? 'chars'
            and (
              not public.ct_jsonb_nonnegative_integer(
                user_request_value -> 'chars'
              )
              or not public.ct_jsonb_nonnegative_integer(
                user_request_value -> 'tokens'
              )
            )
          ) then
          return false;
        end if;
      end if;

      for request_value in
        select request_entry
        from jsonb_array_elements(turn_value -> 'requests') as requests(request_entry)
      loop
        if not public.ct_jsonb_object_matches(
          request_value,
          array[
            'request_id', 'timestamp', 'source', 'model', 'provider',
            'context_window_tokens', 'used_input_tokens', 'usage', 'categories'
          ],
          array[
            'request_id', 'timestamp', 'source', 'used_input_tokens', 'usage',
            'categories'
          ]
        ) or (request_value ->> 'request_id')::uuid is null
          or jsonb_typeof(request_value -> 'usage') <> 'object'
          or jsonb_typeof(request_value -> 'categories') <> 'array'
          or jsonb_array_length(request_value -> 'categories') > 32
          or not public.ct_jsonb_nonnegative_integer(
            request_value -> 'used_input_tokens'
          )
          or (
            request_value ? 'context_window_tokens'
            and not public.ct_jsonb_nonnegative_integer(
              request_value -> 'context_window_tokens'
            )
          ) then
          return false;
        end if;
        usage_value := request_value -> 'usage';
        if not public.ct_jsonb_object_matches(
          usage_value,
          array[
            'input_tokens', 'cached_input_tokens', 'cache_creation_input_tokens',
            'output_tokens', 'reasoning_output_tokens', 'total_tokens',
            'uncached_input_tokens', 'cost_usd'
          ],
          array[
            'input_tokens', 'cached_input_tokens', 'cache_creation_input_tokens',
            'output_tokens', 'reasoning_output_tokens', 'total_tokens'
          ]
        ) or exists (
          select 1
          from jsonb_each(usage_value) usage_entry
          where usage_entry.key <> 'cost_usd'
            and not public.ct_jsonb_nonnegative_integer(usage_entry.value)
        ) or (
          usage_value ? 'cost_usd'
          and (
            jsonb_typeof(usage_value -> 'cost_usd') <> 'string'
            or usage_value ->> 'cost_usd' !~
              '^(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?$'
          )
        ) then
          return false;
        end if;
        for category_value in
          select category_entry
          from jsonb_array_elements(request_value -> 'categories')
            as categories(category_entry)
        loop
          if not public.ct_jsonb_object_matches(
            category_value,
            array['key', 'label', 'tokens', 'confidence', 'source'],
            array['key', 'label', 'tokens', 'confidence']
          ) or not public.ct_jsonb_nonnegative_integer(
            category_value -> 'tokens'
          ) then
            return false;
          end if;
        end loop;
      end loop;

      if turn_value ? 'team_state' then
        team_value := turn_value -> 'team_state';
        if not public.ct_jsonb_object_matches(
          team_value,
          array['members', 'tasks'],
          array['members', 'tasks']
        ) or jsonb_typeof(team_value -> 'members') <> 'array'
          or jsonb_typeof(team_value -> 'tasks') <> 'array' then
          return false;
        end if;
        for member_value in
          select member_entry
          from jsonb_array_elements(team_value -> 'members') as members(member_entry)
        loop
          if not public.ct_jsonb_object_matches(
            member_value,
            array['member_id', 'session_id', 'agent_type'],
            array['member_id']
          ) or (
            member_value ? 'session_id'
            and (member_value ->> 'session_id')::uuid is null
          ) then
            return false;
          end if;
        end loop;
        for task_value in
          select task_entry
          from jsonb_array_elements(team_value -> 'tasks') as tasks(task_entry)
        loop
          if not public.ct_jsonb_object_matches(
            task_value,
            array['task_id', 'status', 'member_id', 'blocked_by'],
            array['task_id', 'blocked_by']
          ) or jsonb_typeof(task_value -> 'blocked_by') <> 'array'
            or jsonb_array_length(task_value -> 'blocked_by') > 64 then
            return false;
          end if;
        end loop;
      end if;

      item_count := item_count + jsonb_array_length(turn_value -> 'items');
      for item_value in
        select item_entry
        from jsonb_array_elements(turn_value -> 'items') as items(item_entry)
      loop
        if not public.ct_jsonb_object_matches(
          item_value,
          array[
            'item_id', 'sequence', 'kind', 'started_at', 'completed_at', 'status',
            'tool_name', 'tool_category', 'operation', 'exit_code', 'path',
            'measurements', 'semantic'
          ],
          array['item_id', 'sequence', 'kind', 'started_at', 'measurements', 'semantic']
        ) or (item_value ->> 'item_id')::uuid is null
          or not public.ct_jsonb_nonnegative_integer(item_value -> 'sequence')
          or (
            item_value ? 'exit_code'
            and (
              jsonb_typeof(item_value -> 'exit_code') <> 'number'
              or item_value ->> 'exit_code' !~ '^-?(0|[1-9][0-9]*)$'
            )
          )
          or item_value ->> 'kind' not in (
            'agent_message', 'tool_call', 'command_execution', 'file_change',
            'reasoning', 'plan'
          ) or exists (
            select 1
            from jsonb_each(item_measurements_value) measurement_entry
            where measurement_entry.key in (
                'input_chars', 'input_tokens', 'output_chars', 'output_tokens',
                'text_chars', 'text_tokens', 'output_original_tokens'
              )
              and not public.ct_jsonb_nonnegative_integer(
                measurement_entry.value
              )
          ) or jsonb_typeof(
            item_measurements_value -> 'projection_only'
          ) <> 'boolean'
            or jsonb_typeof(
              item_measurements_value -> 'output_truncated'
            ) <> 'boolean' then
          return false;
        end if;
        item_measurements_value := item_value -> 'measurements';
        if not public.ct_jsonb_object_matches(
          item_measurements_value,
          array[
            'input_chars', 'input_tokens', 'output_chars', 'output_tokens',
            'text_chars', 'text_tokens', 'projection_only', 'output_truncated',
            'output_original_tokens', 'text_preview', 'tool_summary'
          ],
          array[
            'input_chars', 'input_tokens', 'output_chars', 'output_tokens',
            'text_chars', 'text_tokens', 'projection_only', 'output_truncated'
          ]
        ) then
          return false;
        end if;
        if item_measurements_value ? 'tool_summary' then
          summary_value := item_measurements_value -> 'tool_summary';
          if not public.ct_jsonb_object_matches(
            summary_value,
            array[
              'name', 'description', 'status', 'optimization_profile',
              'activity_hidden', 'activity_kind', 'activity_source',
              'activity_outcome', 'activity_fidelity', 'activity_wrapper_status'
            ],
            array['name']
          ) or (
            summary_value ? 'activity_hidden'
            and jsonb_typeof(summary_value -> 'activity_hidden') <> 'boolean'
          ) then
            return false;
          end if;
        end if;
        semantic_value := item_value -> 'semantic';
        if not public.ct_jsonb_object_matches(
          semantic_value,
          array['verification_kind', 'resolution_key', 'plan_actions'],
          array['plan_actions']
        ) or jsonb_typeof(semantic_value -> 'plan_actions') <> 'array'
          or jsonb_array_length(semantic_value -> 'plan_actions') > 10 then
          return false;
        end if;
      end loop;
    end loop;
  end loop;

  select count(distinct turn_entry ->> 'turn_id') into distinct_count
  from jsonb_array_elements(ct_shareable_graph_valid.value -> 'sessions') as sessions(session_entry)
  cross join lateral jsonb_array_elements(session_entry -> 'turns')
    as turns(turn_entry);
  if distinct_count <> turn_count then
    return false;
  end if;
  select count(distinct item_entry ->> 'item_id') into distinct_count
  from jsonb_array_elements(ct_shareable_graph_valid.value -> 'sessions') as sessions(session_entry)
  cross join lateral jsonb_array_elements(session_entry -> 'turns')
    as turns(turn_entry)
  cross join lateral jsonb_array_elements(turn_entry -> 'items') as items(item_entry);
  if distinct_count <> item_count
    or (ct_shareable_graph_valid.value -> 'graph' ->> 'session_count')::integer <>
      jsonb_array_length(ct_shareable_graph_valid.value -> 'sessions')
    or (ct_shareable_graph_valid.value -> 'graph' ->> 'turn_count')::integer <> turn_count
    or (ct_shareable_graph_valid.value -> 'graph' ->> 'item_count')::integer <> item_count then
    return false;
  end if;
  if exists (
    select 1
    from jsonb_array_elements(ct_shareable_graph_valid.value -> 'sessions') session_entry
    where (
      select count(*)
      from jsonb_array_elements(session_entry -> 'turns') turn_entry
    ) <> (
      select count(distinct (turn_entry ->> 'sequence')::integer)
      from jsonb_array_elements(session_entry -> 'turns') turn_entry
    )
  ) or exists (
    select 1
    from jsonb_array_elements(ct_shareable_graph_valid.value -> 'sessions') session_entry
    cross join lateral jsonb_array_elements(session_entry -> 'turns') turn_entry
    where (
      select count(*)
      from jsonb_array_elements(turn_entry -> 'items') item_entry
    ) <> (
      select count(distinct (item_entry ->> 'sequence')::integer)
      from jsonb_array_elements(turn_entry -> 'items') item_entry
    )
  ) then
    return false;
  end if;

  if exists (
    select 1
    from jsonb_array_elements(ct_shareable_graph_valid.value -> 'edges') edge_entry
    group by
      edge_entry ->> 'kind',
      edge_entry ->> 'source_session_id',
      edge_entry ->> 'target_session_id',
      edge_entry -> 'origin' ->> 'turn_id',
      edge_entry -> 'origin' ->> 'item_id'
    having count(*) > 1
  ) then
    return false;
  end if;

  for edge_value in
    select edge_entry
    from jsonb_array_elements(ct_shareable_graph_valid.value -> 'edges') as edges(edge_entry)
  loop
    if not public.ct_jsonb_object_matches(
      edge_value,
      array[
        'source_session_id', 'target_session_id', 'kind', 'origin', 'tool_name',
        'provenance', 'confidence'
      ],
      array[
        'source_session_id', 'target_session_id', 'kind', 'origin',
        'provenance', 'confidence'
      ]
    ) or edge_value ->> 'kind' not in (
      'spawned_subagent', 'sidechain_of', 'forked_from', 'handoff_to',
      'resumed_from', 'teammate_of'
    ) or edge_value ->> 'provenance' not in ('observed', 'derived')
      or edge_value ->> 'confidence' not in ('high', 'medium', 'low')
      or not exists (
      select 1
      from jsonb_array_elements(ct_shareable_graph_valid.value -> 'sessions') as sessions(session_entry)
      where session_entry ->> 'session_id' = edge_value ->> 'source_session_id'
    ) or not exists (
      select 1
      from jsonb_array_elements(ct_shareable_graph_valid.value -> 'sessions') as sessions(session_entry)
      where session_entry ->> 'session_id' = edge_value ->> 'target_session_id'
    ) then
      return false;
    end if;
    origin_value := edge_value -> 'origin';
    if not public.ct_jsonb_object_matches(
      origin_value,
      array['session_id', 'turn_id', 'item_id'],
      array['session_id']
    ) or origin_value ->> 'session_id' <> edge_value ->> 'source_session_id'
      or (
        origin_value ? 'turn_id'
        and (origin_value ->> 'turn_id')::uuid is null
      )
      or (
        origin_value ? 'item_id'
        and (origin_value ->> 'item_id')::uuid is null
      )
      or (
        origin_value ? 'turn_id'
        and not exists (
          select 1
          from jsonb_array_elements(ct_shareable_graph_valid.value -> 'sessions') session_entry,
            jsonb_array_elements(session_entry -> 'turns') turn_entry
          where session_entry ->> 'session_id' =
              edge_value ->> 'source_session_id'
            and turn_entry ->> 'turn_id' = origin_value ->> 'turn_id'
        )
      )
      or (
        origin_value ? 'item_id'
        and (
          not (origin_value ? 'turn_id')
          or not exists (
            select 1
            from jsonb_array_elements(ct_shareable_graph_valid.value -> 'sessions') session_entry,
              jsonb_array_elements(session_entry -> 'turns') turn_entry,
              jsonb_array_elements(turn_entry -> 'items') item_entry
            where session_entry ->> 'session_id' =
                edge_value ->> 'source_session_id'
              and turn_entry ->> 'turn_id' = origin_value ->> 'turn_id'
              and item_entry ->> 'item_id' = origin_value ->> 'item_id'
          )
        )
      ) then
      return false;
    end if;
  end loop;
  return true;
exception when others then
  return false;
end;
$$;

create or replace function public.ct_source_checkpoint_valid(value jsonb)
returns boolean
language plpgsql
immutable
set search_path = public, pg_temp
as $$
begin
  return coalesce(public.ct_jsonb_object_matches(
      value,
      array['kind', 'source_checkpoint', 'shareable_digest'],
      array['kind', 'source_checkpoint', 'shareable_digest']
    )
    and value - 'kind' - 'source_checkpoint' - 'shareable_digest' = '{}'::jsonb
    and value ->> 'kind' = 'ct.source_checkpoint.v1'
    and value ->> 'shareable_digest' ~ '^[0-9a-f]{64}$'
    and jsonb_typeof(value -> 'source_checkpoint') = 'object'
    and (value -> 'source_checkpoint') - 'segments' = '{}'::jsonb
    and jsonb_typeof(value -> 'source_checkpoint' -> 'segments') = 'array'
    and jsonb_array_length(value -> 'source_checkpoint' -> 'segments') > 0
    and not exists (
      select 1
      from jsonb_array_elements(value -> 'source_checkpoint' -> 'segments') entry
      where jsonb_typeof(entry) <> 'number'
        or entry #>> '{}' !~ '^[1-9][0-9]*$'
    ), false);
exception when others then
  return false;
end;
$$;

revoke all on function public.ct_jsonb_object_matches(jsonb, text[], text[])
  from public, anon, authenticated;
revoke all on function public.ct_jsonb_nonnegative_integer(jsonb)
  from public, anon, authenticated;
revoke all on function public.ct_canonical_json(jsonb)
  from public, anon, authenticated;
revoke all on function public.ct_jsonb_sha256(jsonb)
  from public, anon, authenticated;
revoke all on function public.ct_shareable_json_safe(jsonb) from public, anon, authenticated;
revoke all on function public.ct_shareable_graph_valid(jsonb) from public, anon, authenticated;
revoke all on function public.ct_source_checkpoint_valid(jsonb) from public, anon, authenticated;

-- Stop legacy projector work without deleting its observations or audit rows.
update public.ct_projection_outbox
set state = 'completed',
    lease_owner = null,
    lease_expires_at = null,
    completed_at = coalesce(completed_at, clock_timestamp()),
    last_error = 'superseded_by_direct_shareable_publication',
    payload = payload || jsonb_build_object('direct_publication_superseded', true)
where projection_name = 'project_source_observation'
  and state <> 'completed';

alter table public.ct_source_observations
  drop constraint if exists ct_source_observations_compact_v2_new;
alter table public.ct_source_observations
  add constraint ct_source_observations_checkpoint_v1_new
  check (
    schema_version = 'ct.source_checkpoint.v1'
    and octet_length(payload::text) <= 65536
    and content_sha256 = public.ct_jsonb_sha256(payload)
    and public.ct_source_checkpoint_valid(payload)
  ) not valid;

alter table public.ct_artifact_revisions
  add constraint ct_artifact_revisions_shareable_v1_new
  check (
    schema_version = 'ct.shareable_graph.v1'
    and source_vector = '{}'::jsonb
    and content_sha256 = public.ct_jsonb_sha256(payload)
    and octet_length(convert_to(public.ct_canonical_json(payload), 'UTF8')) <= 8388608
    and public.ct_shareable_graph_valid(payload)
  ) not valid;

-- One atomic project publication can contain multiple graph revisions at the
-- same workspace sequence, and a graph may deterministically return to an
-- earlier digest after an intervening revision. Keep both fields indexed, but
-- do not impose the legacy projector's uniqueness assumptions.
do $$
declare
  legacy_constraint record;
begin
  for legacy_constraint in
    select constraint_row.conname
    from pg_constraint constraint_row
    where constraint_row.conrelid =
        'public.ct_artifact_revisions'::regclass
      and constraint_row.contype = 'u'
      and pg_get_constraintdef(constraint_row.oid) in (
        'UNIQUE (workspace_id, published_sequence)',
        'UNIQUE (workspace_id, artifact_id, content_sha256)'
      )
  loop
    execute format(
      'alter table public.ct_artifact_revisions drop constraint %I',
      legacy_constraint.conname
    );
  end loop;
end;
$$;
create index if not exists ct_artifact_revisions_workspace_sequence_idx
  on public.ct_artifact_revisions (workspace_id, published_sequence);
create index if not exists ct_artifact_revisions_artifact_digest_idx
  on public.ct_artifact_revisions (workspace_id, artifact_id, content_sha256);

alter table public.ct_artifacts
  add column if not exists started_at timestamptz,
  add column if not exists ended_at timestamptz,
  add column if not exists session_count integer,
  add column if not exists turn_count integer,
  add column if not exists item_count integer,
  add column if not exists artifact_bytes integer;

create table public.ct_project_publishers (
  workspace_id uuid not null,
  project_id uuid not null,
  agent_id uuid not null,
  committed_publication_sequence bigint not null default -1
    check (committed_publication_sequence >= -1),
  last_request_sha256 text,
  last_committed_sequence bigint,
  updated_at timestamptz not null default now(),
  primary key (workspace_id, project_id, agent_id),
  foreign key (workspace_id, project_id)
    references public.ct_projects(workspace_id, project_id) on delete cascade,
  foreign key (workspace_id, agent_id)
    references public.ct_agents(workspace_id, agent_id) on delete cascade,
  check (last_request_sha256 is null or last_request_sha256 ~ '^[0-9a-f]{64}$')
);

create table public.ct_artifact_revision_sources (
  workspace_id uuid not null,
  artifact_id uuid not null,
  revision bigint not null,
  source_id uuid not null,
  source_epoch bigint not null,
  source_sequence bigint not null,
  content_sha256 text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
  primary key (workspace_id, artifact_id, revision, source_id),
  foreign key (workspace_id, artifact_id, revision)
    references public.ct_artifact_revisions(workspace_id, artifact_id, revision)
    on delete cascade,
  foreign key (workspace_id, source_id, source_epoch, source_sequence)
    references public.ct_source_observations(
      workspace_id, source_id, source_epoch, source_sequence
    )
);

create table public.ct_artifact_revision_resources (
  workspace_id uuid not null,
  artifact_id uuid not null,
  revision bigint not null,
  resource_kind text not null check (resource_kind in ('session', 'turn', 'item')),
  resource_id uuid not null,
  primary key (
    workspace_id, artifact_id, revision, resource_kind, resource_id
  ),
  foreign key (workspace_id, artifact_id, revision)
    references public.ct_artifact_revisions(workspace_id, artifact_id, revision)
    on delete cascade
);

create index ct_artifact_revision_resources_lookup_idx
  on public.ct_artifact_revision_resources (workspace_id, resource_id);

alter table public.ct_project_publishers enable row level security;
alter table public.ct_artifact_revision_sources enable row level security;
alter table public.ct_artifact_revision_resources enable row level security;

-- Checkpoint ingestion no longer enqueues remote graph reconstruction.
create or replace function public.ct_collector_publish_observation(
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
  target_source_id uuid := (request ->> 'source_id')::uuid;
  target_epoch bigint := (request ->> 'source_epoch')::bigint;
  target_sequence bigint := (request ->> 'source_sequence')::bigint;
  source_record public.ct_ingest_sources%rowtype;
  existing_receipt public.ct_ingest_receipts%rowtype;
  existing_observation public.ct_source_observations%rowtype;
  allocated_sequence bigint;
  contiguous_sequence bigint;
  receipt_id uuid := gen_random_uuid();
begin
  if not public.ct_collector_authorized(
    target_workspace_id, target_agent_id, 'ingest'
  ) then
    raise exception 'collector ingest capability is required' using errcode = '42501';
  end if;
  if not public.ct_jsonb_object_matches(
    request,
    array[
      'version', 'workspace_id', 'agent_id', 'source_id', 'source_epoch',
      'source_sequence', 'event_id', 'schema_version', 'parser_version',
      'content_sha256', 'observed_at', 'payload'
    ],
    array[
      'version', 'workspace_id', 'agent_id', 'source_id', 'source_epoch',
      'source_sequence', 'event_id', 'schema_version', 'parser_version',
      'content_sha256', 'observed_at', 'payload'
    ]
  ) or request ->> 'version' <> '1'
    or coalesce(btrim(request ->> 'parser_version'), '') = '' then
    raise exception 'invalid source checkpoint request' using errcode = '22023';
  end if;
  if idempotency_key is null or btrim(idempotency_key) = ''
    or request_sha256 is null
    or request_sha256 !~ '^[0-9a-f]{64}$'
    or request_sha256 <> public.ct_jsonb_sha256(request) then
    raise exception 'valid idempotency identity is required' using errcode = '22023';
  end if;
  if request ->> 'schema_version' <> 'ct.source_checkpoint.v1'
    or not public.ct_source_checkpoint_valid(request -> 'payload')
    or request ->> 'content_sha256' !~ '^[0-9a-f]{64}$'
    or request ->> 'content_sha256' <>
      public.ct_jsonb_sha256(request -> 'payload')
    or request ->> 'event_id' <>
      'checkpoint:' || (request ->> 'content_sha256') then
    raise exception 'invalid source checkpoint observation' using errcode = '22023';
  end if;

  select * into existing_receipt
  from public.ct_ingest_receipts receipt
  where receipt.workspace_id = target_workspace_id
    and receipt.agent_id = target_agent_id
    and receipt.idempotency_key = ct_collector_publish_observation.idempotency_key
  for update;
  if found then
    if existing_receipt.request_sha256 = request_sha256 then
      return jsonb_build_object(
        'receipt_id', existing_receipt.receipt_id,
        'outcome', existing_receipt.outcome,
        'committed_sequence', existing_receipt.committed_sequence,
        'details', existing_receipt.details
      );
    end if;
    insert into public.ct_ingest_receipt_conflicts (
      workspace_id, agent_id, idempotency_key, request_sha256,
      existing_request_sha256
    ) values (
      target_workspace_id, target_agent_id, idempotency_key,
      request_sha256, existing_receipt.request_sha256
    ) returning conflict_receipt_id into receipt_id;
    return jsonb_build_object(
      'receipt_id', receipt_id, 'outcome', 'conflict',
      'committed_sequence', null,
      'details', jsonb_build_object(
        'reason', 'idempotency_key_reused_with_different_request'
      )
    );
  end if;

  select * into source_record
  from public.ct_ingest_sources source
  where source.workspace_id = target_workspace_id
    and source.source_id = target_source_id
  for update;
  if not found or source_record.origin_agent_id <> target_agent_id then
    raise exception 'source is not registered to this collector agent'
      using errcode = '42501';
  end if;
  if source_record.current_epoch <> target_epoch then
    raise exception 'source epoch is not current' using errcode = '22023';
  end if;

  select * into existing_observation
  from public.ct_source_observations observation
  where observation.workspace_id = target_workspace_id
    and observation.source_id = target_source_id
    and observation.source_epoch = target_epoch
    and (
      observation.event_id = request ->> 'event_id'
      or observation.source_sequence = target_sequence
    )
  order by (observation.event_id = request ->> 'event_id') desc;
  if found then
    if existing_observation.event_id = request ->> 'event_id'
      and existing_observation.source_sequence = target_sequence
      and existing_observation.content_sha256 = request ->> 'content_sha256' then
      insert into public.ct_ingest_receipts (
        workspace_id, receipt_id, agent_id, idempotency_key, request_sha256,
        outcome, details
      ) values (
        target_workspace_id, receipt_id, target_agent_id, idempotency_key,
        request_sha256, 'duplicate',
        jsonb_build_object('reason', 'event_identity_already_accepted')
      );
      return jsonb_build_object(
        'receipt_id', receipt_id, 'outcome', 'duplicate',
        'committed_sequence', null,
        'details', jsonb_build_object('reason', 'event_identity_already_accepted')
      );
    end if;
    insert into public.ct_ingest_receipts (
      workspace_id, receipt_id, agent_id, idempotency_key, request_sha256,
      outcome, details
    ) values (
      target_workspace_id, receipt_id, target_agent_id, idempotency_key,
      request_sha256, 'conflict',
      jsonb_build_object(
        'reason', 'event_identity_or_sequence_reused_with_different_content'
      )
    );
    return jsonb_build_object(
      'receipt_id', receipt_id, 'outcome', 'conflict',
      'committed_sequence', null,
      'details', jsonb_build_object(
        'reason', 'event_identity_or_sequence_reused_with_different_content'
      )
    );
  end if;

  insert into public.ct_source_observations (
    workspace_id, source_id, source_epoch, source_sequence, event_id,
    schema_version, parser_version, content_sha256, observed_at, payload
  ) values (
    target_workspace_id, target_source_id, target_epoch, target_sequence,
    request ->> 'event_id', request ->> 'schema_version',
    request ->> 'parser_version', request ->> 'content_sha256',
    (request ->> 'observed_at')::timestamptz, request -> 'payload'
  );
  allocated_sequence := public.ct_next_workspace_sequence(target_workspace_id);
  insert into public.ct_change_log (
    workspace_id, sequence, authority, kind, resource_id, payload
  ) values (
    target_workspace_id, allocated_sequence, 'historical',
    'source_checkpoint', target_source_id::text,
    jsonb_build_object(
      'source_epoch', target_epoch, 'source_sequence', target_sequence
    )
  );
  contiguous_sequence := source_record.committed_source_sequence;
  while exists (
    select 1 from public.ct_source_observations observation
    where observation.workspace_id = target_workspace_id
      and observation.source_id = target_source_id
      and observation.source_epoch = target_epoch
      and observation.source_sequence = contiguous_sequence + 1
  ) loop
    contiguous_sequence := contiguous_sequence + 1;
  end loop;
  update public.ct_ingest_sources
  set committed_source_sequence = contiguous_sequence,
      last_observed_at = clock_timestamp()
  where workspace_id = target_workspace_id and source_id = target_source_id;
  insert into public.ct_ingest_receipts (
    workspace_id, receipt_id, agent_id, idempotency_key, request_sha256,
    outcome, committed_sequence
  ) values (
    target_workspace_id, receipt_id, target_agent_id, idempotency_key,
    request_sha256, 'accepted', allocated_sequence
  );
  return jsonb_build_object(
    'receipt_id', receipt_id, 'outcome', 'accepted',
    'committed_sequence', allocated_sequence, 'details', '{}'::jsonb
  );
end;
$$;

create or replace function public.ct_collector_publish_artifacts(
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
  target_project_id uuid := (request ->> 'project_id')::uuid;
  target_publication_sequence bigint :=
    (request ->> 'publication_sequence')::bigint;
  existing_receipt public.ct_ingest_receipts%rowtype;
  publisher public.ct_project_publishers%rowtype;
  existing_artifact public.ct_artifacts%rowtype;
  artifact_record record;
  next_revision bigint;
  allocated_sequence bigint;
  receipt_id uuid := gen_random_uuid();
  revision_count integer := 0;
  superseded_count integer := 0;
  omitted_count integer := 0;
  stale boolean := false;
  incomplete_scope boolean := false;
  details jsonb;
begin
  if not public.ct_collector_authorized(
    target_workspace_id, target_agent_id, 'ingest'
  ) then
    raise exception 'collector ingest capability is required' using errcode = '42501';
  end if;
  if not public.ct_jsonb_object_matches(
    request,
    array[
      'version', 'workspace_id', 'agent_id', 'project_id',
      'publication_sequence', 'source_vector', 'artifacts'
    ],
    array[
      'version', 'workspace_id', 'agent_id', 'project_id',
      'publication_sequence', 'source_vector', 'artifacts'
    ]
  ) or request ->> 'version' <> '1' then
    raise exception 'invalid artifact publication request' using errcode = '22023';
  end if;
  if idempotency_key is null or btrim(idempotency_key) = ''
    or request_sha256 is null
    or request_sha256 !~ '^[0-9a-f]{64}$'
    or request_sha256 <> public.ct_jsonb_sha256(request) then
    raise exception 'valid idempotency identity is required' using errcode = '22023';
  end if;

  select * into existing_receipt
  from public.ct_ingest_receipts receipt
  where receipt.workspace_id = target_workspace_id
    and receipt.agent_id = target_agent_id
    and receipt.idempotency_key = ct_collector_publish_artifacts.idempotency_key
  for update;
  if found then
    if existing_receipt.request_sha256 = request_sha256 then
      return jsonb_build_object(
        'receipt_id', existing_receipt.receipt_id,
        'outcome', existing_receipt.outcome,
        'committed_sequence', existing_receipt.committed_sequence,
        'details', existing_receipt.details
      );
    end if;
    insert into public.ct_ingest_receipt_conflicts (
      workspace_id, agent_id, idempotency_key, request_sha256,
      existing_request_sha256
    ) values (
      target_workspace_id, target_agent_id, idempotency_key,
      request_sha256, existing_receipt.request_sha256
    ) returning conflict_receipt_id into receipt_id;
    return jsonb_build_object(
      'receipt_id', receipt_id, 'outcome', 'conflict',
      'committed_sequence', null,
      'details', jsonb_build_object(
        'reason', 'idempotency_key_reused_with_different_request'
      )
    );
  end if;

  if target_publication_sequence < 0
    or jsonb_typeof(request -> 'source_vector') <> 'array'
    or jsonb_array_length(request -> 'source_vector') = 0
    or jsonb_typeof(request -> 'artifacts') <> 'array'
    or jsonb_array_length(request -> 'artifacts') = 0
    or octet_length(convert_to(public.ct_canonical_json(request), 'UTF8')) > 16777216
  then
    raise exception 'invalid bounded artifact publication' using errcode = '22023';
  end if;
  if exists (
    select 1 from jsonb_array_elements(request -> 'source_vector') source
    where not public.ct_jsonb_object_matches(
        source,
        array['source_id', 'source_epoch', 'source_sequence', 'content_sha256'],
        array['source_id', 'source_epoch', 'source_sequence', 'content_sha256']
      )
      or source ->> 'source_id' is null
      or source ->> 'source_epoch' !~ '^[1-9][0-9]*$'
      or source ->> 'source_sequence' !~ '^[0-9]+$'
      or source ->> 'content_sha256' !~ '^[0-9a-f]{64}$'
  ) or exists (
    select 1
    from jsonb_array_elements(request -> 'source_vector') source
    group by (source ->> 'source_id')::uuid
    having count(*) > 1
  ) then
    raise exception 'invalid or duplicate source vector entry' using errcode = '22023';
  end if;
  if exists (
    select 1
    from jsonb_array_elements(request -> 'source_vector') vector
    left join public.ct_ingest_sources source
      on source.workspace_id = target_workspace_id
      and source.source_id = (vector ->> 'source_id')::uuid
    left join public.ct_source_observations observation
      on observation.workspace_id = target_workspace_id
      and observation.source_id = (vector ->> 'source_id')::uuid
      and observation.source_epoch = (vector ->> 'source_epoch')::bigint
      and observation.source_sequence = (vector ->> 'source_sequence')::bigint
    where source.source_id is null
      or source.origin_agent_id <> target_agent_id
      or source.project_id <> target_project_id
      or observation.source_id is null
      or observation.state <> 'accepted'
      or observation.content_sha256 <> vector ->> 'content_sha256'
  ) then
    raise exception 'source vector is not backed by accepted project checkpoints'
      using errcode = '22023';
  end if;

  stale := exists (
    select 1
    from jsonb_array_elements(request -> 'source_vector') vector
    join public.ct_ingest_sources source
      on source.workspace_id = target_workspace_id
      and source.source_id = (vector ->> 'source_id')::uuid
    where source.current_epoch <> (vector ->> 'source_epoch')::bigint
      or source.committed_source_sequence <>
        (vector ->> 'source_sequence')::bigint
  );

  if exists (
    select 1 from jsonb_array_elements(request -> 'artifacts') artifact
    where not public.ct_jsonb_object_matches(
        artifact,
        array[
          'artifact_id', 'schema_version', 'payload', 'content_sha256',
          'serialized_bytes', 'source_ids', 'observed_at'
        ],
        array[
          'artifact_id', 'schema_version', 'payload', 'content_sha256',
          'serialized_bytes', 'source_ids', 'observed_at'
        ]
      )
      or artifact ->> 'artifact_id' is null
      or artifact ->> 'schema_version' <> 'ct.shareable_graph.v1'
      or artifact ->> 'content_sha256' !~ '^[0-9a-f]{64}$'
      or artifact ->> 'serialized_bytes' !~ '^[1-9][0-9]*$'
      or (artifact ->> 'serialized_bytes')::bigint > 8388608
      or (artifact ->> 'serialized_bytes')::bigint <>
        octet_length(convert_to(
          public.ct_canonical_json(artifact -> 'payload'), 'UTF8'
        ))
      or artifact ->> 'content_sha256' <>
        public.ct_jsonb_sha256(artifact -> 'payload')
      or not public.ct_shareable_graph_valid(artifact -> 'payload')
      or artifact ->> 'artifact_id' <>
        artifact -> 'payload' -> 'graph' ->> 'root_session_id'
      or jsonb_typeof(artifact -> 'source_ids') <> 'array'
      or jsonb_array_length(artifact -> 'source_ids') = 0
      or exists (
        select 1 from jsonb_array_elements(artifact -> 'source_ids') source_id
        where jsonb_typeof(source_id) <> 'string'
      )
  ) or exists (
    select 1 from jsonb_array_elements(request -> 'artifacts') artifact
    group by (artifact ->> 'artifact_id')::uuid
    having count(*) > 1
  ) or exists (
    select 1
    from jsonb_array_elements(request -> 'artifacts') artifact,
      jsonb_array_elements_text(artifact -> 'source_ids') source_entry(source_id)
    group by artifact ->> 'artifact_id', source_entry.source_id
    having count(*) > 1
  ) then
    raise exception 'invalid shareable graph artifact' using errcode = '22023';
  end if;
  if exists (
    select 1
    from jsonb_array_elements(request -> 'artifacts') artifact,
      jsonb_array_elements_text(artifact -> 'source_ids') artifact_source(source_id)
    where not exists (
      select 1
      from jsonb_array_elements(request -> 'source_vector') vector
      where vector ->> 'source_id' = artifact_source.source_id
    )
  ) then
    raise exception 'artifact source set is outside the publication vector'
      using errcode = '22023';
  end if;
  if exists (
    select 1
    from jsonb_array_elements(request -> 'source_vector') vector
    where not exists (
      select 1
      from jsonb_array_elements(request -> 'artifacts') artifact,
        jsonb_array_elements_text(artifact -> 'source_ids') source_entry(source_id)
      where source_entry.source_id = vector ->> 'source_id'
    )
  ) then
    raise exception 'publication vector contains an unrepresented source'
      using errcode = '22023';
  end if;

  perform 1 from public.ct_projects project
  where project.workspace_id = target_workspace_id
    and project.project_id = target_project_id
  for update;
  if not found then
    raise exception 'project not found in workspace' using errcode = '22023';
  end if;

  select * into publisher
  from public.ct_project_publishers record
  where record.workspace_id = target_workspace_id
    and record.project_id = target_project_id
    and record.agent_id = target_agent_id
  for update;
  if not found then
    if target_publication_sequence <> 0 then
      raise exception 'first project publication sequence must be zero'
        using errcode = '22023';
    end if;
    insert into public.ct_project_publishers (
      workspace_id, project_id, agent_id
    ) values (target_workspace_id, target_project_id, target_agent_id)
    returning * into publisher;
  end if;

  if target_publication_sequence <= publisher.committed_publication_sequence then
    details := jsonb_build_object('reason', 'stale_publication_sequence');
    insert into public.ct_ingest_receipts (
      workspace_id, receipt_id, agent_id, idempotency_key, request_sha256,
      outcome, committed_sequence, details
    ) values (
      target_workspace_id, receipt_id, target_agent_id, idempotency_key,
      request_sha256, 'conflict', publisher.last_committed_sequence, details
    );
    return jsonb_build_object(
      'receipt_id', receipt_id, 'outcome', 'conflict',
      'committed_sequence', publisher.last_committed_sequence,
      'details', details
    );
  end if;
  if target_publication_sequence <> publisher.committed_publication_sequence + 1 then
    raise exception 'project publication sequence contains a gap'
      using errcode = '22023';
  end if;

  -- Any overlapping current graph must be replaced in full. A filtered scan
  -- is not authority to remove its uncollected sessions or another host's data.
  incomplete_scope := exists (
    select 1 from public.ct_artifacts current_artifact
    join public.ct_artifact_revision_sources previous
      on previous.workspace_id = current_artifact.workspace_id
      and previous.artifact_id = current_artifact.artifact_id
      and previous.revision = current_artifact.current_revision
    where current_artifact.workspace_id = target_workspace_id
      and current_artifact.project_id = target_project_id
      and exists (
        select 1 from public.ct_artifact_revision_resources resource,
          jsonb_array_elements(request -> 'artifacts') incoming,
          jsonb_array_elements(incoming -> 'payload' -> 'sessions') session
        where resource.workspace_id = current_artifact.workspace_id
          and resource.artifact_id = current_artifact.artifact_id
          and resource.revision = current_artifact.current_revision
          and resource.resource_kind = 'session'
          and resource.resource_id = (session ->> 'session_id')::uuid
      )
      and not exists (
        select 1 from jsonb_array_elements(request -> 'source_vector') incoming
        where (incoming ->> 'source_id')::uuid = previous.source_id
      )
  );

  allocated_sequence := public.ct_next_workspace_sequence(target_workspace_id);
  if stale or incomplete_scope then
    details := case when incomplete_scope then jsonb_build_object(
      'reason', 'incomplete_graph_scope',
      'publication_outcome', 'rejected',
      'remedy', 'include all sources of overlapping published graphs'
    ) else jsonb_build_object('publication_outcome', 'superseded') end;
    insert into public.ct_change_log (
      workspace_id, sequence, authority, kind, resource_id, payload
    ) values (
      target_workspace_id, allocated_sequence, 'historical',
      'shareable_publication_superseded', target_project_id::text,
      jsonb_build_object(
        'publication_sequence', target_publication_sequence
      )
    );
    update public.ct_project_publishers
    set committed_publication_sequence = target_publication_sequence,
        last_request_sha256 = request_sha256,
        last_committed_sequence = allocated_sequence,
        updated_at = clock_timestamp()
    where workspace_id = target_workspace_id and project_id = target_project_id
      and agent_id = target_agent_id;
    insert into public.ct_ingest_receipts (
      workspace_id, receipt_id, agent_id, idempotency_key, request_sha256,
      outcome, committed_sequence, details
    ) values (
      target_workspace_id, receipt_id, target_agent_id, idempotency_key,
      request_sha256, case when incomplete_scope then 'rejected' else 'accepted' end,
      allocated_sequence, details
    );
    return jsonb_build_object(
      'receipt_id', receipt_id,
      'outcome', case when incomplete_scope then 'rejected' else 'accepted' end,
      'committed_sequence', allocated_sequence, 'details', details
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
    raise exception 'content digest was reused with a different artifact'
      using errcode = '23505';
  end if;

  for artifact_record in
    select
      (artifact ->> 'artifact_id')::uuid as artifact_id,
      artifact -> 'payload' as payload,
      artifact ->> 'content_sha256' as content_sha256,
      (artifact ->> 'serialized_bytes')::integer as serialized_bytes,
      artifact -> 'source_ids' as source_ids,
      (artifact ->> 'observed_at')::timestamptz as observed_at
    from jsonb_array_elements(request -> 'artifacts') artifact
    order by (artifact ->> 'artifact_id')::uuid
  loop
    existing_artifact := null;
    select * into existing_artifact
    from public.ct_artifacts artifact
    where artifact.workspace_id = target_workspace_id
      and artifact.artifact_id = artifact_record.artifact_id
    for update;

    if existing_artifact.artifact_id is not null
      and existing_artifact.current_revision > 0 then
      update public.ct_artifact_revisions
      set superseded_sequence = allocated_sequence
      where workspace_id = target_workspace_id
        and artifact_id = artifact_record.artifact_id
        and revision = existing_artifact.current_revision
        and superseded_sequence is null;
      superseded_count := superseded_count + 1;
    end if;

    insert into public.ct_artifacts (
      workspace_id, artifact_id, project_id, state
    ) values (
      target_workspace_id, artifact_record.artifact_id,
      target_project_id, 'accepting'
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
      'ct.shareable_graph.v1', artifact_record.payload,
      artifact_record.content_sha256, '{}'::jsonb,
      allocated_sequence, artifact_record.observed_at
    );

    insert into public.ct_artifact_revision_sources (
      workspace_id, artifact_id, revision, source_id, source_epoch,
      source_sequence, content_sha256
    )
    select
      target_workspace_id, artifact_record.artifact_id, next_revision,
      (vector ->> 'source_id')::uuid,
      (vector ->> 'source_epoch')::bigint,
      (vector ->> 'source_sequence')::bigint,
      vector ->> 'content_sha256'
    from jsonb_array_elements(request -> 'source_vector') vector
    where vector ->> 'source_id' in (
      select source_id
      from jsonb_array_elements_text(artifact_record.source_ids) source_id
    );

    insert into public.ct_artifact_revision_resources (
      workspace_id, artifact_id, revision, resource_kind, resource_id
    )
    select target_workspace_id, artifact_record.artifact_id, next_revision,
      'session', (session ->> 'session_id')::uuid
    from jsonb_array_elements(artifact_record.payload -> 'sessions') session
    union all
    select target_workspace_id, artifact_record.artifact_id, next_revision,
      'turn', (turn_entry ->> 'turn_id')::uuid
    from jsonb_array_elements(artifact_record.payload -> 'sessions') session
    cross join lateral jsonb_array_elements(session -> 'turns') turn_entry
    union all
    select target_workspace_id, artifact_record.artifact_id, next_revision,
      'item', (item_entry ->> 'item_id')::uuid
    from jsonb_array_elements(artifact_record.payload -> 'sessions') session
    cross join lateral jsonb_array_elements(session -> 'turns') turn_entry
    cross join lateral jsonb_array_elements(turn_entry -> 'items') item_entry
    on conflict do nothing;

    update public.ct_artifacts
    set current_revision = next_revision,
        current_published_sequence = allocated_sequence,
        state = 'accepting',
        started_at = (artifact_record.payload -> 'graph' ->> 'started_at')::timestamptz,
        ended_at = nullif(
          artifact_record.payload -> 'graph' ->> 'ended_at', ''
        )::timestamptz,
        session_count = (artifact_record.payload -> 'graph' ->> 'session_count')::integer,
        turn_count = (artifact_record.payload -> 'graph' ->> 'turn_count')::integer,
        item_count = (artifact_record.payload -> 'graph' ->> 'item_count')::integer,
        artifact_bytes = artifact_record.serialized_bytes,
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
      and exists (
        select 1 from public.ct_artifact_revision_sources previous
        where previous.workspace_id = artifact.workspace_id
          and previous.artifact_id = artifact.artifact_id
          and previous.revision = artifact.current_revision
      )
      and not exists (
        select 1 from public.ct_artifact_revision_sources previous
        where previous.workspace_id = artifact.workspace_id
          and previous.artifact_id = artifact.artifact_id
          and previous.revision = artifact.current_revision
          and not exists (
            select 1 from jsonb_array_elements(request -> 'source_vector') incoming
            where (incoming ->> 'source_id')::uuid = previous.source_id
          )
      )
      and not exists (
        select 1 from jsonb_array_elements(request -> 'artifacts') requested
        where (requested ->> 'artifact_id')::uuid = artifact.artifact_id
      )
    for update
  ), superseded as (
    update public.ct_artifact_revisions revision
    set superseded_sequence = allocated_sequence
    from omitted
    where revision.workspace_id = target_workspace_id
      and revision.artifact_id = omitted.artifact_id
      and revision.revision = omitted.current_revision
      and revision.superseded_sequence is null
    returning revision.artifact_id
  )
  update public.ct_artifacts artifact
  set current_revision = 0,
      current_published_sequence = allocated_sequence,
      state = 'tombstoned',
      updated_at = clock_timestamp()
  from superseded
  where artifact.workspace_id = target_workspace_id
    and artifact.artifact_id = superseded.artifact_id;
  get diagnostics omitted_count = row_count;
  superseded_count := superseded_count + omitted_count;

  insert into public.ct_change_log (
    workspace_id, sequence, authority, kind, resource_id, payload
  ) values (
    target_workspace_id, allocated_sequence, 'historical',
    'shareable_graphs_published', target_project_id::text,
    jsonb_build_object(
      'publication_sequence', target_publication_sequence,
      'revision_count', revision_count,
      'superseded_count', superseded_count
    )
  );
  update public.ct_projects
  set current_published_sequence = allocated_sequence,
      updated_at = clock_timestamp()
  where workspace_id = target_workspace_id and project_id = target_project_id;
  update public.ct_project_publishers
  set committed_publication_sequence = target_publication_sequence,
      last_request_sha256 = request_sha256,
      last_committed_sequence = allocated_sequence,
      updated_at = clock_timestamp()
  where workspace_id = target_workspace_id and project_id = target_project_id
      and agent_id = target_agent_id;

  details := jsonb_build_object(
    'publication_outcome', 'published',
    'revision_count', revision_count,
    'superseded_count', superseded_count
  );
  insert into public.ct_ingest_receipts (
    workspace_id, receipt_id, agent_id, idempotency_key, request_sha256,
    outcome, committed_sequence, details
  ) values (
    target_workspace_id, receipt_id, target_agent_id, idempotency_key,
    request_sha256, 'accepted', allocated_sequence, details
  );
  return jsonb_build_object(
    'receipt_id', receipt_id, 'outcome', 'accepted',
    'committed_sequence', allocated_sequence, 'details', details
  );
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
  requested_sequence bigint :=
    nullif(request ->> 'snapshot_sequence', '')::bigint;
  requested_resources jsonb := coalesce(request -> 'resource_ids', '[]'::jsonb);
  requested_project_name text := nullif(request ->> 'project_name', '');
  requested_vendor text := nullif(request ->> 'agent_vendor', '');
  requested_since_days integer := nullif(request ->> 'since_days', '')::integer;
  requested_modified_since timestamptz :=
    nullif(request ->> 'modified_since', '')::timestamptz;
  metadata_only boolean := coalesce(
    nullif(request ->> 'metadata_only', '')::boolean, false
  );
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

  if metadata_only then
    artifacts := '[]'::jsonb;
  else
    select coalesce(jsonb_agg(
      jsonb_build_object(
        'artifact_id', selected.artifact_id,
        'revision', selected.revision,
        'published_sequence', selected.published_sequence,
        'content_sha256', selected.content_sha256,
        'payload', selected.payload
      ) order by selected.artifact_id
    ), '[]'::jsonb) into artifacts
    from (
      select revision.*
      from public.ct_artifact_revisions revision
      join public.ct_artifacts artifact
        on artifact.workspace_id = revision.workspace_id
        and artifact.artifact_id = revision.artifact_id
      where revision.workspace_id = target_workspace_id
        and revision.schema_version = 'ct.shareable_graph.v1'
        and revision.published_sequence <= snapshot_sequence
        and (
          revision.superseded_sequence is null
          or revision.superseded_sequence > snapshot_sequence
        )
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
              and (
                project.superseded_sequence is null
                or project.superseded_sequence > snapshot_sequence
              )
          )
        )
        and (
          requested_vendor is null
          or exists (
            select 1
            from jsonb_array_elements(revision.payload -> 'sessions') session
            where session ->> 'vendor' = requested_vendor
          )
        )
        and (
          requested_since_days is null
          or revision.observed_at >= clock_timestamp()
            - make_interval(days => requested_since_days)
        )
        and (
          requested_modified_since is null
          or revision.observed_at >= requested_modified_since
        )
    ) selected;
  end if;

  return jsonb_build_object(
    'workspace_id', target_workspace_id,
    'snapshot_sequence', snapshot_sequence,
    'artifacts', artifacts
  );
end;
$$;

revoke all on function public.ct_collector_publish_artifacts(jsonb, text, text)
  from public, anon;
grant execute on function public.ct_collector_publish_artifacts(jsonb, text, text)
  to authenticated, service_role;
revoke all on function public.ct_collector_publish_observation(jsonb, text, text)
  from public, anon;
grant execute on function public.ct_collector_publish_observation(jsonb, text, text)
  to authenticated, service_role;
revoke all on function public.ct_historical_snapshot(jsonb) from public, anon;
grant execute on function public.ct_historical_snapshot(jsonb)
  to authenticated, service_role;

revoke all on function public.ct_projector_claim(jsonb)
  from public, anon, authenticated, service_role;
revoke all on function public.ct_projector_publish(jsonb)
  from public, anon, authenticated, service_role;
revoke all on function public.ct_projector_fail(jsonb)
  from public, anon, authenticated, service_role;
drop function public.ct_projector_claim(jsonb);
drop function public.ct_projector_publish(jsonb);
drop function public.ct_projector_fail(jsonb);

-- Read only the caller's authoritative stream watermarks. No native identity
-- or checkpoint body is returned; pending requests must be retried first.
create or replace function public.ct_collector_recover(request jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  target_workspace_id uuid := (request ->> 'workspace_id')::uuid;
  target_agent_id uuid := (request ->> 'agent_id')::uuid;
  target_project_id uuid := (request ->> 'project_id')::uuid;
  next_publication bigint;
  recovered_source jsonb;
  next_living bigint;
begin
  if not public.ct_collector_authorized(target_workspace_id, target_agent_id, 'ingest') then
    raise exception 'collector ingest capability is required' using errcode = '42501';
  end if;
  if not public.ct_jsonb_object_matches(request,
    array['workspace_id', 'agent_id', 'project_id', 'vendor', 'native_session_id', 'agent_instance_id'],
    array['workspace_id', 'agent_id', 'project_id'])
    or (request ? 'vendor') <> (request ? 'native_session_id') then
    raise exception 'invalid recovery request' using errcode = '22023';
  end if;
  if not exists (select 1 from public.ct_projects
    where workspace_id = target_workspace_id and project_id = target_project_id) then
    raise exception 'project not found in workspace' using errcode = '22023';
  end if;
  select committed_publication_sequence + 1 into next_publication
  from public.ct_project_publishers
  where workspace_id = target_workspace_id and project_id = target_project_id
    and agent_id = target_agent_id;
  if request ? 'vendor' then
    select jsonb_build_object(
      'source_id', source.source_id,
      'source_epoch', source.current_epoch,
      'next_source_sequence', source.committed_source_sequence + 1,
      'content_sha256', observation.content_sha256
    ) into recovered_source
    from public.ct_ingest_sources source
    left join public.ct_source_observations observation
      on observation.workspace_id = source.workspace_id
      and observation.source_id = source.source_id
      and observation.source_epoch = source.current_epoch
      and observation.source_sequence = source.committed_source_sequence
    where source.workspace_id = target_workspace_id
      and source.project_id = target_project_id
      and source.origin_agent_id = target_agent_id
      and source.vendor = request ->> 'vendor'
      and source.native_session_id = request ->> 'native_session_id';
  end if;
  if request ? 'agent_instance_id' then
    if not public.ct_collector_authorized(target_workspace_id, target_agent_id, 'living')
      or exists (select 1 from public.ct_agent_leases
        where workspace_id = target_workspace_id
          and agent_instance_id = (request ->> 'agent_instance_id')::uuid
          and agent_id <> target_agent_id) then
      raise exception 'living instance access denied' using errcode = '42501';
    end if;
    select coalesce(max(observation_sequence), 0) + 1 into next_living
    from public.ct_living_observations
    where workspace_id = target_workspace_id
      and agent_instance_id = (request ->> 'agent_instance_id')::uuid;
  end if;
  return jsonb_build_object(
    'next_living_sequence', next_living,
    'next_publication_sequence', coalesce(next_publication, 0),
    'source', recovered_source
  );
end;
$$;
revoke all on function public.ct_collector_recover(jsonb) from public, anon;
grant execute on function public.ct_collector_recover(jsonb) to authenticated, service_role;
