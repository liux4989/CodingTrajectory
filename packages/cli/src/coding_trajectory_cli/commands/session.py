"""Session command registration and renderers."""

from __future__ import annotations

import argparse
from typing import Any

from coding_trajectory_cli._shared import (
    GhFormatter,
    add_base_output_flags,
    add_metrics_flags,
    add_output_flags,
    add_params_flag,
    add_session_source,
    add_turn_window_flags,
    display_value,
    format_duration,
    format_percent,
    format_tokens,
    one_line,
    params_from_json,
    render_usage_line,
    render_usage_line_compact,
    short_id,
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

COMPACT_THRESHOLD = 90


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
    if args.extra_billing is not None:
        params["extra_billing"] = args.extra_billing
    elif "extra_billing" not in params:
        params["extra_billing"] = False
    if args.session_id:
        params["session_id"] = args.session_id
    return params


def _session_usage_params(args: argparse.Namespace) -> dict[str, Any]:
    params = params_from_json(args)
    if args.extra_billing is not None:
        params["extra_billing"] = args.extra_billing
    elif "extra_billing" not in params:
        params["extra_billing"] = False
    if args.session_id:
        params["session_id"] = args.session_id
    if args.turn_id:
        params["turn_id"] = args.turn_id
    return params


def _overview_request_label(request: Any, *, width: int) -> str:
    if not isinstance(request, dict):
        return "-"
    content = request.get("content") or request.get("summary") or request.get("text")
    return one_line(content, limit=max(width - 30, 32))


def _overview_activity_label(activity: dict[str, Any], *, width: int) -> str:
    if "tool" in activity:
        tool = str(activity.get("tool") or "tool")
        count = activity.get("count")
        suffix = f" x{count}" if count and count != 1 else ""
        for key in ("cmd", "path", "query", "url"):
            if activity.get(key):
                return f"{tool}{suffix}: {one_line(activity[key], limit=max(width - 36, 24))}"
        for key in ("paths", "queries", "urls", "targets"):
            values = activity.get(key)
            if isinstance(values, list) and values:
                joined = ", ".join(one_line(item, limit=max(width // 4, 16)) for item in values[:3])
                more = f" +{len(values) - 3}" if len(values) > 3 else ""
                return f"{tool}{suffix}: {joined}{more}"
        return f"{tool}{suffix}"
    if "teammate_summary" in activity:
        return "teammate summary"
    if "text" in activity:
        return f"assistant: {one_line(activity.get('text'), limit=max(width - 32, 24))}"
    return one_line(activity, limit=max(width - 28, 24))


def _render_session_overview_text(payload: dict[str, Any], width: int) -> str:
    sessions = payload.get("sessions") or []
    turn_count = sum(len(session.get("turns") or []) for session in sessions)
    lines = [
        f"# Session `{short_id(payload.get('root_session_id'))}`",
        "",
        f"{len(sessions)} session{'s' if len(sessions) != 1 else ''}, {turn_count} visible turn{'s' if turn_count != 1 else ''}",
        "",
    ]

    for session in sessions:
        relationship = session.get("relationship") or {}
        role = relationship.get("role") or relationship.get("relationship") or "session"
        header = f"- session `{short_id(session.get('session_id'))}`"
        header += f"  {role}, {session.get('vendor') or '-'}, {display_value(session.get('status')) or '-'}"
        if session.get("agent_name"):
            header += f", {session['agent_name']}"
        lines.append(header)
        if session.get("cwd"):
            lines.append(f"   cwd: {session['cwd']}")

        turns = session.get("turns") or []
        for turn in turns:
            lines.append(
                f"  - turn {short_id(turn.get('turn_id'))}  "
                f"{display_value(turn.get('status')) or '-'}  {_overview_request_label(turn.get('user_request'), width=width)}"
            )

            activities = turn.get("activity") or []
            if turn.get("teammate_summary"):
                activities = [{"teammate_summary": turn.get("teammate_summary")}]
            for activity in activities:
                if isinstance(activity, dict):
                    lines.append(f"    - {_overview_activity_label(activity, width=width)}")

    return "\n".join(lines).rstrip()


def _render_context_category(lines: list[str], category: dict[str, Any], *, indent: int = 0, category_width: int) -> None:
    label = str(category.get("label") or category.get("key") or "-")
    display_width = max(category_width - indent, 16)
    label = one_line(label, limit=display_width)
    lines.append(
        f"{' ' * indent}{label:<{display_width}} {format_tokens(category.get('tokens')):>7} "
        f"{format_percent(category.get('percent')):>8}"
    )
    for child in category.get("children") or []:
        if isinstance(child, dict):
            _render_context_category(lines, child, indent=indent + 2, category_width=category_width)


def _render_session_stats_text(payload: dict[str, Any], width: int) -> str:
    model = payload.get("model") or {}
    context_window = payload.get("context_window") or {}
    runtime = payload.get("runtime") or {}
    messages = payload.get("messages") or {}
    usage = payload.get("usage") or {}

    category_width = max(width - 20, 24)
    model_name = model.get("name") or "-"
    context_tokens = model.get("context_window_tokens")
    lines = [
        "# Session Stats",
        "",
        f"Model: {model_name} ({format_tokens(context_tokens)} context)",
        "",
        f"{'Category':<{category_width}} {'Tokens':>7} {'Context':>8}",
    ]

    for category in context_window.get("categories") or []:
        if isinstance(category, dict):
            _render_context_category(lines, category, category_width=category_width)

    used_tokens = context_window.get("used_tokens") or usage.get("input_tokens")
    used_percent = context_window.get("used_percent")
    tool_calls_total = runtime.get("tool_calls") or 0
    failed_tool_calls = runtime.get("failed_tool_calls") or 0
    runtime_line = (
        f"Runtime: {format_duration(runtime.get('duration_seconds'))}, "
        f"{runtime.get('turns') or 0} turns, "
        f"{runtime.get('model_steps') or 0} model steps, "
        f"{tool_calls_total} tool calls"
    )
    if failed_tool_calls:
        runtime_line += f" ({failed_tool_calls} failed)"
    runtime_line += f", {runtime.get('subagent_sessions') or 0} subagent sessions"
    lines.extend(
        [
            "",
            f"Used: {format_tokens(used_tokens)} tokens {format_percent(used_percent)} of context",
            runtime_line,
        ]
    )
    if runtime.get("compactions"):
        lines[-1] += f", {runtime['compactions']} compactions"
    if tool_calls_total:
        success_rate = round(((tool_calls_total - failed_tool_calls) / tool_calls_total) * 100, 1)
        lines.append(f"Tool Success Rate: {success_rate}%")
    if messages:
        lines.append(
            "Messages: "
            f"{messages.get('user') or 0} user, "
            f"{messages.get('assistant') or 0} assistant, "
            f"{messages.get('tool_outputs') or 0} tool outputs, "
            f"{messages.get('reasoning_items') or 0} reasoning items"
        )
    quota = payload.get("quota") or {}
    if quota:
        quota_bits = [f"plan {quota.get('plan_type')}"] if quota.get("plan_type") else []
        if quota.get("primary_used_percent") is not None:
            quota_bits.append(f"primary {quota['primary_used_percent']:.1f}%")
        if quota.get("secondary_used_percent") is not None:
            quota_bits.append(f"secondary {quota['secondary_used_percent']:.1f}%")
        if quota_bits:
            lines.append("Quota: " + ", ".join(quota_bits))
    warnings = payload.get("warnings") or []
    if warnings:
        lines.append("")
        lines.extend(f"Warning: {one_line(warning, limit=max(width - 10, 40))}" for warning in warnings)
    return "\n".join(lines).rstrip()


def _render_session_usage_text(payload: dict[str, Any], width: int) -> str:
    compact = width < COMPACT_THRESHOLD
    usage_fn = render_usage_line_compact if compact else render_usage_line
    lines = ["# Session Usage", "", "Total"]
    lines.append(f"  {usage_fn(payload.get('total_usage') or {})}")

    turns = payload.get("turns") or []
    if turns:
        lines.extend(["", "Turns"])
    for turn in turns:
        lines.append(f"  turn {short_id(turn.get('turn_id'))}")
        lines.append(f"    {usage_fn(turn.get('usage') or {})}")
        for activity in turn.get("activity_usage") or []:
            category = str(activity.get("category") or "-")
            lines.append(f"    {category:<14} {usage_fn(activity.get('usage') or {})}")

    warnings = payload.get("warnings") or []
    if warnings:
        lines.append("")
        lines.extend(f"Warning: {one_line(warning, limit=max(width - 10, 40))}" for warning in warnings)
    return "\n".join(lines).rstrip()


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    session_parser = subparsers.add_parser(
        "session",
        help="Analyze a session and its connected session tree.",
        formatter_class=GhFormatter,
    )
    session_sub = session_parser.add_subparsers(dest="action", required=True)

    session_overview = session_sub.add_parser(
        "overview",
        help="Show a compact session hierarchy.",
        formatter_class=GhFormatter,
    )
    add_session_source(session_overview)
    add_turn_window_flags(session_overview, view_name="projection")
    add_output_flags(session_overview)
    add_params_flag(session_overview)
    session_overview.set_defaults(
        _method="session.overview",
        _params=_session_turn_window_params,
        _default_output="markdown",
        _renderer=_render_session_overview_text,
    )

    session_stats = session_sub.add_parser(
        "stats",
        help="Show compact context/token usage composition.",
        formatter_class=GhFormatter,
    )
    add_session_source(session_stats)
    add_output_flags(session_stats)
    add_params_flag(session_stats)
    add_metrics_flags(session_stats)
    session_stats.set_defaults(
        _method="session.stats",
        _params=_session_stats_params,
        _default_output="markdown",
        _renderer=_render_session_stats_text,
    )

    session_usage = session_sub.add_parser(
        "usage",
        help="Show turn-level token and cost accounting.",
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
    add_metrics_flags(session_usage)
    session_usage.set_defaults(
        _method="session.usage",
        _params=_session_usage_params,
        _default_output="markdown",
        _renderer=_render_session_usage_text,
    )

    session_step_detail = session_sub.add_parser(
        "step-detail",
        help="Show full detail for one or more steps.",
        formatter_class=GhFormatter,
    )
    session_step_detail.add_argument("resource_ids", metavar="STEP_ID", nargs="*")
    add_base_output_flags(session_step_detail)
    add_params_flag(session_step_detail)
    session_step_detail.set_defaults(
        _method="step.details",
        _params=lambda args: {
            **params_from_json(args),
            **({"step_ids": args.resource_ids} if args.resource_ids else {}),
        },
        _default_output="json",
    )

    session_event_detail = session_sub.add_parser(
        "event-detail",
        help="Expand the full content of a single event (resolves $truncated refs).",
        formatter_class=GhFormatter,
    )
    session_event_detail.add_argument("resource_id", metavar="EVENT_ID", nargs="?")
    add_base_output_flags(session_event_detail)
    add_params_flag(session_event_detail)
    session_event_detail.set_defaults(
        _method="event.detail",
        _params=lambda args: {
            **params_from_json(args),
            **({"event_id": args.resource_id} if args.resource_id else {}),
        },
        _default_output="json",
    )

    session_event_scan = session_sub.add_parser(
        "event-scan",
        help="Query events matching --type and optional --filter expressions.",
        epilog=EVENT_SCAN_EPILOG,
        formatter_class=GhFormatter,
    )
    add_session_source(session_event_scan)
    add_output_flags(session_event_scan)
    add_params_flag(session_event_scan)
    session_event_scan.add_argument(
        "--type",
        dest="event_type",
        required=False,
        metavar="TYPE",
        help="Event type to match (e.g. tool.call.succeeded, llm.response).",
    )
    session_event_scan.add_argument(
        "--filter",
        dest="filters",
        action="append",
        metavar="KEY=VALUE",
        default=None,
        help=(
            "Filter on event payload fields. Repeatable. "
            "VALUE=* means field must exist; VALUE=! means field must be absent."
        ),
    )
    session_event_scan.set_defaults(
        _method="event.scan",
        _params=lambda args: {
            **params_from_json(args),
            **({"type": args.event_type} if args.event_type else {}),
            **({"filters": args.filters} if args.filters is not None else {}),
            **({"session_id": args.session_id} if args.session_id else {}),
        },
        _default_output="json",
    )

