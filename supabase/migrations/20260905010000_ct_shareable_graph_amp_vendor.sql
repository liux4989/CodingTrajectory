-- Allow Amp sessions in otherwise unchanged shareable graph validation.
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
      or session_value ->> 'vendor' not in ('codex_cli', 'claude_code', 'pi', 'amp')
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
