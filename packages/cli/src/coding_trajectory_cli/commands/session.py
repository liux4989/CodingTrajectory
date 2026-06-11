"""Session command registration and renderers."""

from __future__ import annotations

import argparse
from typing import Any

from coding_trajectory_cli._shared import (
    GhFormatter,
    add_output_flags,
    add_params_flag,
    add_schema_flag,
    add_session_source,
    add_turn_window_flags,
    display_value,
    format_duration,
    format_percent,
    format_tokens,
    one_line,
    params_from_json,
    render_usage_line,
)

EVENT_SCAN_EPILOG = """\
EVENT TYPES
  user.prompt.submitted    A user prompt submission
  tool.call.requested      A tool invocation request
  tool.call.succeeded      A tool call that succeeded
  tool.call.failed         A tool call that failed
  llm.response             An LLM response
  vendor.raw               A vendor-specific raw event

FILTER SYNTAX
  key=value     Exact match on a payload field
  key=*         Field must exist
  key=!         Field must be absent/null
  Dot-paths supported: result.error=*
"""

CONTEXT_CATEGORY_WIDTH = 56


def _session_turn_window_params(args: argparse.Namespace) -> dict[str, Any]:
    params = params_from_json(args)
    if args.session_id:
        params["session_id"] = args.session_id
    if args.num_turns is not None:
        params["num_turns"] = args.num_turns
    if args.drop_turns is not None:
        params["drop_turns"] = args.drop_turns
    return params


def _session_stats_params(args: argparse.Namespace) -> dict[str, Any]:
    params = params_from_json(args)
    if args.session_id:
        params["session_id"] = args.session_id
    return params


def _session_usage_params(args: argparse.Namespace) -> dict[str, Any]:
    params = params_from_json(args)
    if args.session_id:
        params["session_id"] = args.session_id
    if args.turn_id:
        params["turn_id"] = args.turn_id
    return params


def _overview_request_label(request: Any) -> str:
    if not isinstance(request, dict):
        return "-"
    content = request.get("content") or request.get("summary") or request.get("text")
    return one_line(content, limit=88)


def _overview_activity_label(activity: dict[str, Any]) -> str:
    if "tool" in activity:
        tool = str(activity.get("tool") or "tool")
        count = activity.get("count")
        suffix = f" x{count}" if count and count != 1 else ""
        for key in ("cmd", "path", "query", "url"):
            if activity.get(key):
                return f"{tool}{suffix}: {one_line(activity[key], limit=72)}"
        for key in ("paths", "queries", "urls", "targets"):
            values = activity.get(key)
            if isinstance(values, list) and values:
                joined = ", ".join(one_line(item, limit=32) for item in values[:3])
                more = f" +{len(values) - 3}" if len(values) > 3 else ""
                return f"{tool}{suffix}: {joined}{more}"
        return f"{tool}{suffix}"
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

    for session in sessions:
        relationship = session.get("relationship") or {}
        role = relationship.get("role") or relationship.get("relationship") or "session"
        header = f"- session `{session.get('session_id') or '-'}`"
        header += f"  {role}, {session.get('vendor') or '-'}, {display_value(session.get('status')) or '-'}"
        if session.get("agent_name"):
            header += f", {session['agent_name']}"
        lines.append(header)
        if session.get("cwd"):
            lines.append(f"   cwd: {session['cwd']}")

        turns = session.get("turns") or []
        for turn in turns:
            lines.append(
                f"  - turn {turn.get('turn_id') or '-'}  "
                f"{display_value(turn.get('status')) or '-'}  {_overview_request_label(turn.get('user_request'))}"
            )

            activities = turn.get("activity") or []
            if turn.get("teammate_summary"):
                activities = [{"teammate_summary": turn.get("teammate_summary")}]
            for activity in activities:
                if isinstance(activity, dict):
                    lines.append(f"    - {_overview_activity_label(activity)}")

    return "\n".join(lines).rstrip()


def _render_context_category(
    lines: list[str], category: dict[str, Any], *, indent: int = 0
) -> None:
    label = str(category.get("label") or category.get("key") or "-")
    display_width = max(CONTEXT_CATEGORY_WIDTH - indent, 16)
    label = one_line(label, limit=display_width)
    lines.append(
        f"{' ' * indent}{label:<{display_width}} {format_tokens(category.get('tokens')):>7} "
        f"{format_percent(category.get('percent')):>8}"
    )
    for child in category.get("children") or []:
        if isinstance(child, dict):
            _render_context_category(lines, child, indent=indent + 2)


def _render_session_stats_text(payload: dict[str, Any]) -> str:
    model = payload.get("model") or {}
    context_window = payload.get("context_window") or {}
    runtime = payload.get("runtime") or {}
    messages = payload.get("messages") or {}
    usage = payload.get("usage") or {}

    model_name = model.get("name") or "-"
    context_tokens = model.get("context_window_tokens")
    lines = [
        "# Session Stats",
        "",
        f"Model: {model_name} ({format_tokens(context_tokens)} context)",
        "",
        "```",
        f"{'Observed composition':<{CONTEXT_CATEGORY_WIDTH}} {'Tokens':>7} {'Share':>8}",
    ]

    for category in context_window.get("categories") or []:
        if isinstance(category, dict):
            _render_context_category(lines, category)

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

    used_tokens = context_window.get("used_tokens") or usage.get("input_tokens")
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
        f"- Used: {format_tokens(used_tokens)} tokens {format_percent(used_percent)} of context"
    )
    lines.append(f"- {runtime_line}")
    if runtime.get("compactions"):
        lines[-1] += f", {runtime['compactions']} compactions"
    if runtime.get("interrupted_turns"):
        lines[-1] += f", {runtime['interrupted_turns']} interrupted"
    if runtime.get("rollbacks"):
        lines[-1] += f", {runtime['rollbacks']} rolled back"
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
        lines.append(
            f"- Messages: "
            f"{messages.get('user') or 0} user, "
            f"{messages.get('assistant') or 0} assistant, "
            f"{messages.get('tool_outputs') or 0} tool outputs, "
            f"{messages.get('reasoning_items') or 0} reasoning items"
        )
    quota = payload.get("quota") or {}
    if quota:
        quota_bits = (
            [f"plan {quota.get('plan_type')}"] if quota.get("plan_type") else []
        )
        if quota.get("limit_name") or quota.get("limit_id"):
            quota_bits.append(
                f"limit {quota.get('limit_name') or quota.get('limit_id')}"
            )
        if quota.get("primary_used_percent") is not None:
            quota_bits.append(f"primary {quota['primary_used_percent']:.1f}%")
        if quota.get("secondary_used_percent") is not None:
            quota_bits.append(f"secondary {quota['secondary_used_percent']:.1f}%")
        if quota.get("credits_balance") is not None:
            quota_bits.append(f"credits {quota['credits_balance']}")
        elif quota.get("credits_unlimited"):
            quota_bits.append("credits unlimited")
        if quota.get("rate_limit_reached_type"):
            quota_bits.append(f"reached {quota['rate_limit_reached_type']}")
        if quota_bits:
            lines.append("- Quota: " + ", ".join(quota_bits))
    return "\n".join(lines).rstrip()


def _render_session_usage_text(payload: dict[str, Any]) -> str:
    lines = ["# Session Usage", "", "```", "Total"]
    lines.append(f"  {render_usage_line(payload.get('total_usage') or {})}")
    runtime = payload.get("runtime") or {}
    if runtime:
        lines.append(
            f"  execution {format_duration(runtime.get('execution_seconds'))}  "
            f"wait {format_duration(runtime.get('wait_seconds'))}"
        )

    turns = payload.get("turns") or []
    if turns:
        lines.extend(["", "Turns"])
    for turn in turns:
        lines.append(f"  turn {turn.get('turn_id') or '-'}")
        lines.append(f"    {render_usage_line(turn.get('usage') or {})}")
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

    lines.append("```")
    return "\n".join(lines).rstrip()


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    session_parser = subparsers.add_parser(
        "session",
        prog="ct session",
        usage="ct session <command> [flags]",
        help="Analyze a session and its connected session tree.",
        formatter_class=GhFormatter,
    )
    session_sub = session_parser.add_subparsers(dest="action", required=True)

    session_overview = session_sub.add_parser(
        "overview",
        prog="ct session overview",
        help="Show a compact session hierarchy.",
        formatter_class=GhFormatter,
    )
    add_session_source(session_overview)
    add_turn_window_flags(session_overview, view_name="projection")
    add_output_flags(session_overview)
    add_params_flag(session_overview)
    add_schema_flag(session_overview)
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
    add_params_flag(session_stats)
    add_schema_flag(session_stats)
    session_stats.set_defaults(
        _method="session.stats",
        _params=_session_stats_params,
        _default_output="markdown",
        _renderer=_render_session_stats_text,
    )

    session_usage = session_sub.add_parser(
        "usage",
        prog="ct session usage",
        help="Show turn-level token usage and log-reported cost.",
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
    add_output_flags(session_usage)
    add_params_flag(session_usage)
    add_schema_flag(session_usage)
    session_usage.set_defaults(
        _method="session.usage",
        _params=_session_usage_params,
        _default_output="markdown",
        _renderer=_render_session_usage_text,
    )

    session_data = session_sub.add_parser(
        "data",
        prog="ct session data",
        help="Bulk read reusable session facts for many sessions.",
        formatter_class=GhFormatter,
    )
    add_output_flags(session_data)
    add_params_flag(session_data)
    add_schema_flag(session_data)
    session_data.set_defaults(
        _method="session.data",
        _params=lambda args: params_from_json(args),
        _default_output="json",
    )

    session_events = session_sub.add_parser(
        "events",
        prog="ct session events",
        help="Query events by session scope or explicit event IDs.",
        epilog=EVENT_SCAN_EPILOG,
        formatter_class=GhFormatter,
    )
    add_session_source(session_events)
    add_output_flags(session_events)
    add_params_flag(session_events)
    add_schema_flag(session_events)
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
        _params=lambda args: {
            **params_from_json(args),
            **({"session_id": args.session_id} if args.session_id else {}),
            **({"type": args.event_type} if args.event_type else {}),
            **({"filters": args.filters} if args.filters is not None else {}),
        },
        _default_output="json",
    )

    session_items = session_sub.add_parser(
        "items",
        prog="ct session items",
        help="Query items by explicit IDs or session scope.",
        formatter_class=GhFormatter,
    )
    session_items.add_argument("resource_ids", metavar="ITEM_ID", nargs="*")
    add_output_flags(session_items)
    add_params_flag(session_items)
    add_schema_flag(session_items)
    session_items.set_defaults(
        _method="session.items",
        _params=lambda args: {
            **params_from_json(args),
            **({"item_ids": args.resource_ids} if args.resource_ids else {}),
        },
        _default_output="json",
    )
