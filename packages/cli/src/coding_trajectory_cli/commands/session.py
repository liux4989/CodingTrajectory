"""Session command registration and renderers."""

from __future__ import annotations

import argparse
from typing import Any

from coding_trajectory_cli._shared import (
    GhFormatter,
    add_global_scope_flag,
    add_json_output_flag,
    add_output_flags,
    add_session_source,
    add_turn_window_flags,
    display_value,
    format_cost,
    format_duration,
    format_percent,
    format_tokens,
    one_line,
    render_usage_line,
)

EVENT_SCAN_EPILOG = """\
EVENT TYPES
  user.prompt.submitted    A user prompt submission
  tool.call.requested      A tool invocation request
  tool.call.succeeded      A tool call that succeeded
  tool.call.failed         A tool call that failed
  llm.response             An LLM response
  usage                    Provider request-usage observation
  vendor.raw               A vendor-specific raw event

FILTER SYNTAX
  key=value     Exact match on a payload field
  key=*         Field must exist
  key=!         Field must be absent/null
  Dot-paths supported: result.error=*
"""

CONTEXT_CATEGORY_WIDTH = 48
CONTEXT_USAGE_WIDTH = 34


def _session_turn_window_params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {"session_id": args.session_id}
    if args.num_turns is not None:
        params["num_turns"] = args.num_turns
    if args.drop_turns is not None:
        params["drop_turns"] = args.drop_turns
    return params


def _session_stats_params(args: argparse.Namespace) -> dict[str, Any]:
    return {"session_id": args.session_id}


def _session_usage_params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {"session_id": args.session_id}
    if args.turn_id:
        params["turn_id"] = args.turn_id
    return params


def _session_request_usage_params(args: argparse.Namespace) -> dict[str, Any]:
    params = _session_usage_params(args)
    include = [
        value
        for enabled, value in (
            (args.include_context, "context"),
            (args.include_causality, "causality"),
        )
        if enabled
    ]
    if include:
        params["include"] = include
    return params


def _session_events_params(args: argparse.Namespace) -> dict[str, Any]:
    return {
        **({"session_id": args.session_id} if args.session_id else {}),
        **({"event_ids": args.event_ids} if args.event_ids else {}),
        **({"turn_id": args.turn_id} if args.turn_id else {}),
        **({"type": args.event_type} if args.event_type else {}),
        **({"filters": args.filters} if args.filters is not None else {}),
    }


def _session_items_params(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "session_id": args.session_id,
        **({"item_ids": args.resource_ids} if args.resource_ids else {}),
        **({"turn_id": args.turn_id} if args.turn_id else {}),
        **({"types": args.item_types} if args.item_types else {}),
        **({"include_content": True} if args.include_content else {}),
    }


def _overview_request_label(request: Any) -> str:
    if not isinstance(request, dict):
        return "-"
    content = request.get("content") or request.get("summary") or request.get("text")
    return one_line(content, limit=88)


def _overview_activity_label(activity: dict[str, Any]) -> str:
    if "compaction" in activity:
        return f"compaction: {activity.get('summary') or 'compaction'}"
    if "tool" in activity:
        tool = str(activity.get("tool") or "tool")
        count = activity.get("count")
        suffix = f" x{count}" if count and count != 1 else ""
        annotations: list[str] = []
        if activity.get("status") == "failed":
            annotations.append("failed")
        if activity.get("wrapper_status") == "failed":
            annotations.append("wrapper failed")
        if activity.get("outcome") == "unknown":
            annotations.append("outcome unavailable")
        annotation = f" [{'; '.join(annotations)}]" if annotations else ""
        if tool == "RunCommand" and count and count != 1:
            command_word = "command" if count == 1 else "commands"
            return f"Ran {count} {command_word}{annotation}"
        for key in ("cmd", "path", "query", "url"):
            if activity.get(key):
                return (
                    f"{tool}{suffix}: {one_line(activity[key], limit=72)}{annotation}"
                )
        for key in ("commands", "paths", "queries", "urls", "targets"):
            values = activity.get(key)
            if isinstance(values, list) and values:
                joined = ", ".join(one_line(item, limit=32) for item in values[:3])
                more = f" +{len(values) - 3}" if len(values) > 3 else ""
                return f"{tool}{suffix}: {joined}{more}{annotation}"
        return f"{tool}{suffix}{annotation}"
    if "teammate_summary" in activity:
        return "teammate summary"
    if "text" in activity:
        return f"assistant: {one_line(activity.get('text'), limit=84)}"
    return one_line(activity, limit=80)


def _render_session_overview_text(payload: dict[str, Any]) -> str:
    sessions = payload.get("sessions") or []
    turn_count = sum(len(session.get("turns") or []) for session in sessions)
    lines = [
        f"# Session `{payload.get('root_session_id') or '-'}`",
        "",
        f"{len(sessions)} session{'s' if len(sessions) != 1 else ''}, {turn_count} visible turn{'s' if turn_count != 1 else ''}",
        "",
    ]

    by_id = {
        str(session.get("session_id")): session
        for session in sessions
        if session.get("session_id")
    }
    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    roots: list[dict[str, Any]] = []
    for session in sessions:
        relationship = session.get("relationship") or {}
        parent_id = (
            relationship.get("parent_session_id")
            or relationship.get("parent")
            or session.get("parent_session_id")
        )
        if parent_id and str(parent_id) in by_id:
            children_by_parent.setdefault(str(parent_id), []).append(session)
        else:
            roots.append(session)
    # Stable child ordering by started_at/session_id when available.
    for kids in children_by_parent.values():
        kids.sort(
            key=lambda s: (s.get("started_at") or "", str(s.get("session_id") or ""))
        )

    if roots:
        for root in roots:
            _render_session_tree_node(root, children_by_parent, depth=0, lines=lines)
    else:  # no parent linkage resolved - fall back to flat rendering
        for session in sessions:
            _render_session_tree_node(session, {}, depth=0, lines=lines)

    return "\n".join(lines).rstrip()


def _render_session_tree_node(
    session: dict[str, Any],
    children_by_parent: dict[str, list[dict[str, Any]]],
    *,
    depth: int,
    lines: list[str],
) -> None:
    indent = "  " * depth
    relationship = session.get("relationship") or {}
    role = (
        relationship.get("role")
        or relationship.get("relationship")
        or session.get("edge_type")
        or "session"
    )
    header = f"{indent}- session `{session.get('session_id') or '-'}`"
    if session.get("title"):
        header += f"  {one_line(session['title'], limit=64)}"
    header += f"  {role}"
    header += f", {session.get('vendor') or '-'}, {display_value(session.get('status')) or '-'}"
    if session.get("agent_name"):
        header += f", {session['agent_name']}"
    if depth > 0:
        header += f", depth {depth}"
    if session.get("compactions"):
        header += f", {session['compactions']} compactions"
    lines.append(header)
    if session.get("cwd"):
        lines.append(f"{indent}   cwd: {session['cwd']}")

    turns = session.get("turns") or []
    for turn in turns:
        lines.append(
            f"{indent}  - turn {turn.get('turn_id') or '-'}  "
            f"{display_value(turn.get('status')) or '-'}  {_overview_request_label(turn.get('user_request'))}"
        )

        activities = turn.get("activity") or []
        if turn.get("teammate_summary"):
            activities = [{"teammate_summary": turn.get("teammate_summary")}]
        for activity in activities:
            if isinstance(activity, dict):
                lines.append(f"{indent}    - {_overview_activity_label(activity)}")

    for child in children_by_parent.get(str(session.get("session_id")), []):
        _render_session_tree_node(
            child, children_by_parent, depth=depth + 1, lines=lines
        )


def _render_conversation_tree_text(payload: dict[str, Any]) -> str:
    branches = payload.get("branches") or []
    children: dict[str, list[dict[str, Any]]] = {}
    roots: list[dict[str, Any]] = []
    for branch in branches:
        parent_id = branch.get("parent_session_id")
        if parent_id:
            children.setdefault(str(parent_id), []).append(branch)
        else:
            roots.append(branch)
    for siblings in children.values():
        siblings.sort(
            key=lambda item: (
                item.get("started_at") or "",
                item.get("session_id") or "",
            )
        )
    roots.sort(
        key=lambda item: (item.get("started_at") or "", item.get("session_id") or "")
    )

    lines = [
        f"# Conversation tree `{payload.get('root_session_id') or '-'}`",
        "",
    ]
    if not branches:
        lines.append("No conversation branches observed.")
        return "\n".join(lines)

    def render(branch: dict[str, Any], depth: int) -> None:
        indent = "  " * depth
        session_id = branch.get("session_id") or "-"
        agents = int(branch.get("spawned_agent_count") or 0)
        turns = int(branch.get("turn_count") or 0)
        lines.append(
            f"{indent}- branch `{session_id}` — {turns} turns, {agents} spawned agents"
        )
        source_turn_id = branch.get("source_turn_id")
        if source_turn_id:
            lines.append(f"{indent}  forked at turn `{source_turn_id}`")
        lines.append(f"{indent}  graph: `ct session graph overview {session_id}`")
        for child in children.get(str(session_id), []):
            render(child, depth + 1)

    for root in roots:
        render(root, 0)
    return "\n".join(lines)


def _render_context_category(
    lines: list[str],
    category: dict[str, Any],
    *,
    indent: int = 0,
    include_allocated_usage: bool = False,
) -> None:
    label = str(category.get("label") or category.get("key") or "-")
    display_width = max(CONTEXT_CATEGORY_WIDTH - indent, 16)
    label = one_line(label, limit=display_width)
    if include_allocated_usage:
        allocated_usage = category.get("allocated_usage")
        if allocated_usage is None:
            allocated_usage = category.get("real_tokens")
        lines.append(
            f"{' ' * indent}{label:<{display_width}} {format_tokens(category.get('tokens')):>7} "
            f"{_format_allocated_usage(allocated_usage):>{CONTEXT_USAGE_WIDTH}} "
            f"{format_percent(category.get('percent')):>8}"
        )
    else:
        lines.append(
            f"{' ' * indent}{label:<{display_width}} {format_tokens(category.get('tokens')):>7} "
            f"{format_percent(category.get('percent')):>8}"
        )
    for child in category.get("children") or []:
        if isinstance(child, dict):
            _render_context_category(
                lines,
                child,
                indent=indent + 2,
                include_allocated_usage=include_allocated_usage,
            )


def _format_optional_tokens(value: Any) -> str:
    return "-" if value is None else format_tokens(value)


def _compaction_line(compaction: Any) -> str | None:
    """Render a ``- Compactions:`` summary line, or ``None`` when absent.

    Drops any sub-part the session graph could not observe: Codex compactions
    (full eviction, no pre/post in the event) render as a bare count; Claude Code
    compactions add the cumulative dropped total and the last event's
    pre→post delta and trigger.
    """
    if not isinstance(compaction, dict) or not compaction.get("count"):
        return None
    count = compaction.get("count") or 0
    parts: list[str] = [f"{count} compaction{'s' if count != 1 else ''}"]
    cumulative = compaction.get("cumulative_dropped_tokens")
    if cumulative is not None:
        parts.append(f"{format_tokens(cumulative)} tokens dropped")
    last = compaction.get("last") or {}
    delta_parts: list[str] = []
    if last.get("pre_tokens") is not None and last.get("post_tokens") is not None:
        delta_parts.append(
            f"{format_tokens(last['pre_tokens'])} → {format_tokens(last['post_tokens'])}"
        )
    elif last.get("dropped_tokens") is not None:
        delta_parts.append(f"{format_tokens(last['dropped_tokens'])} dropped")
    if last.get("trigger"):
        delta_parts.append(str(last["trigger"]))
    detail = ", ".join(delta_parts)
    body = ", ".join(parts)
    if not detail:
        return f"- Compactions: {body}"
    return f"- Compactions: {body} (last: {detail})"


def _compaction_has_detail(compaction: Any) -> bool:
    """Whether the compaction summary carries info beyond a bare count.

    `session stats` already appends the compaction count to the Execution line,
    so the standalone ``- Compactions:`` line is only worth emitting when it adds
    dropped-token totals or a last-event delta (Claude Code). Codex
    compactions carry neither in the event, so a count-only line would just
    duplicate the Execution line.
    """
    if not isinstance(compaction, dict):
        return False
    if compaction.get("cumulative_dropped_tokens") is not None:
        return True
    last = compaction.get("last") or {}
    return bool(
        isinstance(last, dict)
        and (
            last.get("pre_tokens") is not None
            or last.get("dropped_tokens") is not None
            or last.get("trigger")
        )
    )


def _should_render_compaction_timeline(compaction: Any) -> bool:
    """Show the per-event table only when it adds information.

    A single bare Codex compaction (no pre/post) renders as a useless one-row
    table — the summary line already covers it. Show the table when there are
    ≥2 events, or when any single event carries pre/post metadata.
    """
    if not isinstance(compaction, dict):
        return False
    events = compaction.get("events") or []
    if not isinstance(events, list) or len(events) < 1:
        return False
    if len(events) >= 2:
        return True
    return any(
        isinstance(event, dict) and event.get("pre_tokens") is not None
        for event in events
    )


def _format_compaction_timestamp(value: Any) -> str:
    if not value:
        return "-"
    text = str(value)
    # Trim to ``YYYY-MM-DD HH:MM`` for column compactness.
    return text[:16].replace("T", " ")


def _render_compaction_timeline(lines: list[str], compaction: Any) -> None:
    if not _should_render_compaction_timeline(compaction):
        return
    events = compaction.get("events") or []
    lines.extend(["", "Compaction timeline", "```"])
    lines.append(
        f"{'#':>2}  {'Timestamp':<16} {'Mechanism':<18} {'Trigger':<10} "
        f"{'Pre → Post':>15} {'Dropped':>10}"
    )
    for index, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            continue
        mechanism = event.get("mechanism") or "-"
        trigger = event.get("trigger") or "-"
        pre = event.get("pre_tokens")
        post = event.get("post_tokens")
        if pre is not None and post is not None:
            delta = f"{format_tokens(pre)} → {format_tokens(post)}"
        else:
            delta = "-"
        dropped = event.get("dropped_tokens")
        dropped_label = format_tokens(dropped) if dropped is not None else "-"
        lines.append(
            f"{index:>2}  {_format_compaction_timestamp(event.get('timestamp')):<16} "
            f"{mechanism:<18} {trigger:<10} {delta:>15} {dropped_label:>10}"
        )
    lines.append("```")


def _format_allocated_usage(value: Any) -> str:
    if not isinstance(value, dict):
        return _format_optional_tokens(value)
    uncached = value.get("uncached_prompt_tokens")
    cached = value.get("cached_prompt_tokens")
    cache_creation = value.get("cache_write_tokens")
    output = value.get("completion_tokens")
    reasoning = value.get("reasoning_tokens")
    if (
        uncached is None
        and cached is None
        and cache_creation is None
        and output is None
        and reasoning is None
    ):
        return "-"
    return (
        f"{format_tokens(uncached)}/"
        f"{format_tokens(cached)}/"
        f"{format_tokens(cache_creation)}/"
        f"{format_tokens(output)}/"
        f"{format_tokens(reasoning)}"
    )


def _render_session_stats_text(payload: dict[str, Any]) -> str:
    session_sections = [
        session
        for session in payload.get("sessions") or []
        if isinstance(session, dict)
    ]
    if len(session_sections) > 1:
        return _render_session_stats_sections(payload, session_sections)

    model = payload.get("model") or {}
    context_window = payload.get("context_window") or {}
    runtime = payload.get("runtime") or {}
    messages = payload.get("messages") or {}
    usage = payload.get("usage") or {}
    billed_token_usage = payload.get("billed_token_usage") or {}

    model_name = model.get("name") or "-"
    context_tokens = model.get("context_window_tokens")
    lines = [
        "# Session Stats",
        "",
        f"Model: {model_name} ({format_tokens(context_tokens)} context)",
        "",
        "```",
        f"{'Observed composition':<{CONTEXT_CATEGORY_WIDTH}} {'Est tokens':>10} "
        f"{'Billed Unc/Cache/Create/Out/Reason':>{CONTEXT_USAGE_WIDTH}} "
        f"{'Share':>8}",
    ]

    for category in context_window.get("categories") or []:
        if isinstance(category, dict):
            _render_context_category(lines, category, include_allocated_usage=True)

    lines.append("```")

    provider_buckets = payload.get("provider_usage_buckets") or []
    if provider_buckets:
        lines.extend(["", "Provider usage buckets", "```"])
        lines.append(
            f"{'Bucket':<{CONTEXT_CATEGORY_WIDTH}} {'Tokens':>7} {'Context':>8}"
        )
        for category in provider_buckets:
            if isinstance(category, dict):
                _render_context_category(lines, category)
        lines.append("```")

    used_tokens = context_window.get("used_tokens") or usage.get("prompt_tokens")
    used_percent = context_window.get("used_percent")
    tool_calls_total = runtime.get("tool_calls") or 0
    failed_tool_calls = runtime.get("failed_tool_calls") or 0
    runtime_line = (
        f"Execution: {format_duration(runtime.get('execution_seconds'))}, "
        f"wait {format_duration(runtime.get('wait_seconds'))}, "
        f"{runtime.get('turns') or 0} turns, "
        f"{runtime.get('items') or 0} items, "
        f"{tool_calls_total} tool calls"
    )
    if failed_tool_calls:
        runtime_line += f" ({failed_tool_calls} failed)"
    runtime_line += f", {runtime.get('subagent_sessions') or 0} subagent sessions"
    lines.append("")
    lines.append(
        f"- Context window: {format_tokens(used_tokens)} tokens "
        f"{format_percent(used_percent)} of context window"
    )
    if billed_token_usage:
        lines.append(
            f"- Billed tokens (all API calls): {render_usage_line(billed_token_usage)}"
        )
    lines.append(f"- {runtime_line}")
    if runtime.get("compactions"):
        lines[-1] += f", {runtime['compactions']} compactions"
    if runtime.get("interrupted_turns"):
        lines[-1] += f", {runtime['interrupted_turns']} interrupted"
    if runtime.get("rollbacks"):
        lines[-1] += f", {runtime['rollbacks']} rolled back"
    compaction = payload.get("compaction")
    # The Execution line already carries the compaction count; only emit the
    # standalone line when it adds detail (dropped totals / last delta).
    if _compaction_has_detail(compaction):
        compaction_line = _compaction_line(compaction)
        if compaction_line:
            lines.append(compaction_line)
    _render_compaction_timeline(lines, compaction)
    if runtime.get("average_time_to_first_token_ms") is not None:
        lines.append(
            f"- Average time to first token: "
            f"{runtime['average_time_to_first_token_ms'] / 1000:.2f}s"
        )
    if tool_calls_total:
        success_rate = round(
            ((tool_calls_total - failed_tool_calls) / tool_calls_total) * 100, 1
        )
        lines.append(f"- Tool Success Rate: {success_rate}%")
    if messages:
        messages_line = (
            f"- Messages: "
            f"{messages.get('user') or 0} user, "
            f"{messages.get('assistant') or 0} assistant, "
            f"{messages.get('tool_outputs') or 0} tool outputs, "
            f"{messages.get('reasoning_items') or 0} reasoning items"
        )
        if messages.get("compacted_contexts"):
            messages_line += f", {messages['compacted_contexts']} compacted"
        lines.append(messages_line)
    return "\n".join(lines).rstrip()


def _render_session_stats_sections(
    payload: dict[str, Any],
    session_sections: list[dict[str, Any]],
) -> str:
    lines = ["# Session Stats", "", "Session context sections"]
    name_by_id, depth_by_id = _section_label_maps(session_sections)
    for section in session_sections:
        model = section.get("model") or {}
        context_window = section.get("context_window") or {}
        runtime = section.get("runtime") or {}
        billed_token_usage = section.get("billed_token_usage") or {}
        model_name = model.get("name") or "-"
        context_tokens = model.get("context_window_tokens")
        lines.extend(
            [
                "",
                f"## {_session_section_label(section, name_by_id=name_by_id, depth_by_id=depth_by_id)}",
                f"Model: {model_name} ({format_tokens(context_tokens)} context)",
                "",
                "```",
                f"{'Observed composition':<{CONTEXT_CATEGORY_WIDTH}} {'Est tokens':>10} "
                f"{'Billed Unc/Cache/Create/Out/Reason':>{CONTEXT_USAGE_WIDTH}} "
                f"{'Share':>8}",
            ]
        )
        for category in context_window.get("categories") or []:
            if isinstance(category, dict):
                _render_context_category(lines, category, include_allocated_usage=True)
        lines.append("```")
        used_tokens = context_window.get("used_tokens") or (
            section.get("usage") or {}
        ).get("prompt_tokens")
        used_percent = context_window.get("used_percent")
        lines.append(
            f"- Context window: {format_tokens(used_tokens)} tokens "
            f"{format_percent(used_percent)} of context window"
        )
        if billed_token_usage:
            lines.append(f"- Billed tokens: {render_usage_line(billed_token_usage)}")
        lines.append(
            "- Runtime: "
            f"{runtime.get('turns') or 0} turns, "
            f"{runtime.get('items') or 0} items, "
            f"{runtime.get('tool_calls') or 0} tool calls"
        )

    graph_context = payload.get("context_window") or {}
    runtime = payload.get("runtime") or {}
    graph_billed = payload.get("billed_token_usage") or {}
    lines.extend(["", "Graph aggregate", ""])
    lines.append(
        f"- Aggregate context composition: {format_tokens(graph_context.get('used_tokens'))} "
        f"tokens {format_percent(graph_context.get('used_percent'))}"
    )
    if graph_billed:
        lines.append(f"- Aggregate billed tokens: {render_usage_line(graph_billed)}")
    lines.append(
        "- Graph runtime: "
        f"{runtime.get('turns') or 0} turns, "
        f"{runtime.get('items') or 0} items, "
        f"{runtime.get('tool_calls') or 0} tool calls, "
        f"{runtime.get('subagent_sessions') or 0} subagent sessions"
    )
    for warning in payload.get("warnings") or []:
        lines.append(f"- Warning: {warning}")
    return "\n".join(lines).rstrip()


def _render_session_usage_text(
    payload: dict[str, Any], args: argparse.Namespace | None = None
) -> str:
    session_sections = [
        session
        for session in payload.get("sessions") or []
        if isinstance(session, dict)
    ]
    if len(session_sections) > 1:
        return _render_session_usage_sections(payload, args, session_sections)

    lines = ["# Session Usage", "", "```", "Total"]
    lines.append(f"  {render_usage_line(payload.get('total_usage') or {})}")
    total_cost = payload.get("estimated_cost") or {}
    if total_cost.get("value_usd") is not None:
        lines.append(
            f"  cost {format_cost(total_cost.get('value_usd'))} "
            f"({total_cost.get('confidence', 'estimated')})"
        )
    _append_model_usage(lines, payload.get("models"), indent="  ")
    runtime = payload.get("runtime") or {}
    if runtime:
        lines.append(
            f"  execution {format_duration(runtime.get('execution_seconds'))}  "
            f"wait {format_duration(runtime.get('wait_seconds'))}"
        )

    compaction_line = _compaction_line(payload.get("compaction"))
    if compaction_line:
        lines.append(f"  {compaction_line}")

    show_all = bool(getattr(args, "all_turns", False))
    turns = payload.get("turns") or []
    rendered_turns = [t for t in turns if show_all or _turn_has_usage(t)]
    skipped = len(turns) - len(rendered_turns)
    if rendered_turns:
        lines.extend(["", "Turns"])
    for turn in rendered_turns:
        lines.append(f"  turn {turn.get('turn_id') or '-'}")
        lines.append(f"    {render_usage_line(turn.get('usage') or {})}")
        turn_cost = turn.get("estimated_cost") or {}
        if turn_cost.get("value_usd") is not None:
            lines.append(
                f"    cost {format_cost(turn_cost.get('value_usd'))} "
                f"({turn_cost.get('confidence', 'estimated')})"
            )
        runtime = turn.get("runtime") or {}
        if runtime:
            timing_parts = []
            if runtime.get("execution_seconds") is not None:
                timing_parts.append(
                    f"execution {format_duration(runtime.get('execution_seconds'))}"
                )
            if runtime.get("wait_before_seconds") is not None:
                timing_parts.append(
                    f"wait before {format_duration(runtime.get('wait_before_seconds'))}"
                )
            if timing_parts:
                lines.append("    " + "  ".join(timing_parts))
    if skipped:
        lines.append(
            f"  (skipped {skipped} turn(s) with no recorded token usage; "
            f"use --all to show)"
        )

    lines.append("```")
    return "\n".join(lines).rstrip()


def _render_session_usage_sections(
    payload: dict[str, Any],
    args: argparse.Namespace | None,
    session_sections: list[dict[str, Any]],
) -> str:
    lines = ["# Session Usage", "", "```", "Graph total"]
    lines.append(f"  {render_usage_line(payload.get('total_usage') or {})}")
    total_cost = payload.get("estimated_cost") or {}
    if total_cost.get("value_usd") is not None:
        lines.append(
            f"  cost {format_cost(total_cost.get('value_usd'))} "
            f"({total_cost.get('confidence', 'estimated')})"
        )
    _append_model_usage(lines, payload.get("models"), indent="  ")
    runtime = payload.get("runtime") or {}
    if runtime:
        lines.append(
            f"  execution {format_duration(runtime.get('execution_seconds'))}  "
            f"wait {format_duration(runtime.get('wait_seconds'))}"
        )
    lines.append("```")

    show_all = bool(getattr(args, "all_turns", False))
    name_by_id, depth_by_id = _section_label_maps(session_sections)
    for section in session_sections:
        lines.extend(
            [
                "",
                f"## {_session_section_label(section, name_by_id=name_by_id, depth_by_id=depth_by_id)}",
                "",
                "```",
            ]
        )
        lines.append("Total")
        lines.append(f"  {render_usage_line(section.get('total_usage') or {})}")
        section_cost = section.get("estimated_cost") or {}
        if section_cost.get("value_usd") is not None:
            lines.append(
                f"  cost {format_cost(section_cost.get('value_usd'))} "
                f"({section_cost.get('confidence', 'estimated')})"
            )
        _append_model_usage(lines, section.get("models"), indent="  ")
        section_runtime = section.get("runtime") or {}
        if section_runtime:
            lines.append(
                f"  execution {format_duration(section_runtime.get('execution_seconds'))}  "
                f"wait {format_duration(section_runtime.get('wait_seconds'))}"
            )
        turns = section.get("turns") or []
        rendered_turns = [t for t in turns if show_all or _turn_has_usage(t)]
        skipped = len(turns) - len(rendered_turns)
        if rendered_turns:
            lines.extend(["", "Turns"])
        for turn in rendered_turns:
            lines.append(f"  turn {turn.get('turn_id') or '-'}")
            lines.append(f"    {render_usage_line(turn.get('usage') or {})}")
            turn_cost = turn.get("estimated_cost") or {}
            if turn_cost.get("value_usd") is not None:
                lines.append(
                    f"    cost {format_cost(turn_cost.get('value_usd'))} "
                    f"({turn_cost.get('confidence', 'estimated')})"
                )
        if skipped:
            lines.append(
                f"  (skipped {skipped} turn(s) with no recorded token usage; "
                f"use --all to show)"
            )
        lines.append("```")

    return "\n".join(lines).rstrip()


def _append_model_usage(lines: list[str], models: Any, *, indent: str) -> None:
    rows = [row for row in models or [] if isinstance(row, dict)]
    if not rows:
        return
    lines.append(f"{indent}Usage by model")
    for row in rows:
        label = row.get("model") or "unknown model"
        if row.get("provider"):
            label = f"{row['provider']}/{label}"
        detail = render_usage_line(row.get("usage") or {})
        estimate = row.get("estimated_cost") or {}
        if estimate.get("value_usd") is not None:
            detail += f"  cost {format_cost(estimate['value_usd'])}"
        lines.append(f"{indent}  {label}: {detail}")


def _section_label_maps(
    sections: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, int]]:
    """Precompute parent display-name and depth per session for section labels."""
    name_by_id = {
        str(section.get("session_id")): (
            section.get("agent_name")
            or section.get("title")
            or str(section.get("session_id"))[:8]
        )
        for section in sections
        if section.get("session_id")
    }

    def depth_of(section: dict[str, Any]) -> int:
        seen: set[str] = set()
        depth = 0
        current = section
        while True:
            parent_id = (
                str(current.get("parent") or current.get("parent_session_id") or "")
                or None
            )
            if not parent_id or parent_id in seen or parent_id not in name_by_id:
                break
            seen.add(parent_id)
            depth += 1
            parent_section = next(
                (s for s in sections if str(s.get("session_id")) == parent_id),
                None,
            )
            if parent_section is None:
                break
            current = parent_section
        return depth

    depth_by_id = {
        str(section.get("session_id")): depth_of(section)
        for section in sections
        if section.get("session_id")
    }
    return name_by_id, depth_by_id


def _session_section_label(
    section: dict[str, Any],
    *,
    name_by_id: dict[str, str] | None = None,
    depth_by_id: dict[str, int] | None = None,
) -> str:
    role = str(section.get("role") or section.get("relationship") or "session")
    session_id = str(section.get("session_id") or section.get("root_session_id") or "-")
    label = (
        section.get("title")
        or section.get("agent_name")
        or section.get("relationship")
        or role
    )
    text = f"{role}: {label} ({session_id[:8]})"
    parent_id = (
        str(section.get("parent") or section.get("parent_session_id") or "") or None
    )
    if parent_id and name_by_id:
        parent_label = name_by_id.get(parent_id)
        if parent_label:
            text += f" <- {parent_label}"
    if depth_by_id:
        depth = depth_by_id.get(str(section.get("session_id")))
        if depth:
            text += f" [depth {depth}]"
    return text


def _turn_has_usage(turn: dict[str, Any]) -> bool:
    usage = turn.get("usage") or {}
    if not usage:
        return False
    for key in (
        "prompt_tokens",
        "uncached_prompt_tokens",
        "cached_prompt_tokens",
        "cache_write_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "reported_total_tokens",
    ):
        if int(usage.get(key) or 0):
            return True
    return False


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    session_parser = subparsers.add_parser(
        "session",
        prog="ct session",
        usage="ct session <command> [flags]",
        help="Analyze one coding-agent thread.",
        formatter_class=GhFormatter,
    )
    session_sub = session_parser.add_subparsers(dest="action", required=True)

    session_tree = session_sub.add_parser(
        "tree",
        prog="ct session tree",
        help="Show ordinary human conversation forks and their agent-run counts.",
        formatter_class=GhFormatter,
    )
    add_session_source(session_tree)
    add_output_flags(session_tree)
    session_tree.set_defaults(
        _method="session.tree",
        _params=_session_stats_params,
        _default_output="markdown",
        _renderer=_render_conversation_tree_text,
    )

    session_overview = session_sub.add_parser(
        "overview",
        prog="ct session overview",
        help="Show a compact session hierarchy.",
        formatter_class=GhFormatter,
    )
    add_session_source(session_overview)
    add_turn_window_flags(session_overview, view_name="projection")
    add_output_flags(session_overview)
    session_overview.set_defaults(
        _method="session.overview",
        _params=_session_turn_window_params,
        _default_output="markdown",
        _renderer=_render_session_overview_text,
    )

    session_stats = session_sub.add_parser(
        "stats",
        prog="ct session stats",
        help="Show compact context/token usage composition.",
        formatter_class=GhFormatter,
    )
    add_session_source(session_stats)
    add_output_flags(session_stats)
    session_stats.set_defaults(
        _method="session.stats",
        _params=_session_stats_params,
        _default_output="markdown",
        _renderer=_render_session_stats_text,
    )

    session_usage = session_sub.add_parser(
        "usage",
        prog="ct session usage",
        help="Show turn-level token usage and request-summed cost.",
        formatter_class=GhFormatter,
    )
    add_session_source(session_usage)
    session_usage.add_argument(
        "--turn",
        dest="turn_id",
        metavar="TURN_ID",
        default=None,
        help="Limit usage analysis to one turn.",
    )
    session_usage.add_argument(
        "--all",
        dest="all_turns",
        action="store_true",
        default=False,
        help="Include turns with no recorded token usage (default: hide them).",
    )
    add_output_flags(session_usage)
    session_usage.set_defaults(
        _method="session.usage",
        _params=_session_usage_params,
        _default_output="markdown",
        _renderer=_render_session_usage_text,
    )

    session_request_usage = session_sub.add_parser(
        "request-usage",
        prog="ct session request-usage",
        help="Show exact provider-request usage and cost.",
        formatter_class=GhFormatter,
    )
    add_session_source(session_request_usage)
    session_request_usage.add_argument(
        "--turn",
        dest="turn_id",
        metavar="TURN_ID",
        default=None,
        help="Limit request usage to one turn.",
    )
    session_request_usage.add_argument(
        "--include-context",
        action="store_true",
        help="Include request context-window diagnostics.",
    )
    session_request_usage.add_argument(
        "--include-causality",
        action="store_true",
        help="Include tool-result-to-next-request causal links.",
    )
    add_json_output_flag(session_request_usage)
    session_request_usage.set_defaults(
        _method="session.request_usage",
        _params=_session_request_usage_params,
        _default_output="json",
    )

    session_events = session_sub.add_parser(
        "events",
        prog="ct session events",
        help="Query events by session scope or explicit event IDs.",
        epilog=EVENT_SCAN_EPILOG,
        formatter_class=GhFormatter,
    )
    add_session_source(session_events, required=False)
    add_json_output_flag(session_events)
    add_global_scope_flag(session_events)
    session_events.add_argument(
        "--event-id",
        dest="event_ids",
        action="append",
        metavar="EVENT_ID",
        default=None,
        help="Resolve an explicit event ID. Repeatable; SESSION_ID is optional.",
    )
    session_events.add_argument(
        "--turn",
        dest="turn_id",
        metavar="TURN_ID",
        default=None,
        help="Limit the event query to one turn.",
    )
    session_events.add_argument(
        "--type",
        dest="event_type",
        required=False,
        metavar="TYPE",
        help="Event type to match.",
    )
    session_events.add_argument(
        "--filter",
        dest="filters",
        action="append",
        metavar="KEY=VALUE",
        default=None,
        help="Filter on event payload fields. Repeatable.",
    )
    session_events.set_defaults(
        _method="session.events",
        _params=_session_events_params,
        _default_output="json",
    )

    session_items = session_sub.add_parser(
        "items",
        prog="ct session items",
        help="Query items within a session by explicit IDs or full session scope.",
        formatter_class=GhFormatter,
    )
    session_items.add_argument("session_id", metavar="SESSION_ID")
    session_items.add_argument("resource_ids", metavar="ITEM_ID", nargs="*")
    session_items.add_argument(
        "--turn",
        dest="turn_id",
        metavar="TURN_ID",
        default=None,
        help="Limit the item query to one turn.",
    )
    session_items.add_argument(
        "--include-content",
        action="store_true",
        default=False,
        help="Return full item content instead of truncation references.",
    )
    session_items.add_argument(
        "--type",
        dest="item_types",
        action="append",
        metavar="ITEM_TYPE",
        default=None,
        help="Limit results to an item type. Repeatable.",
    )
    add_json_output_flag(session_items)
    session_items.set_defaults(
        _method="session.items",
        _params=_session_items_params,
        _default_output="json",
    )

    from coding_trajectory_cli.commands.graph import register as register_graph

    register_graph(session_sub)
