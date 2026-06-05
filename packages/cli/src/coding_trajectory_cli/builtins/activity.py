from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from heapq import nlargest
from pathlib import Path
from typing import Any

from coding_trajectory.analysis.activity_flow import build_overview_flows
from coding_trajectory.ingestion.common import format_datetime, normalize_project_key
from coding_trajectory.ingestion.models import Session, SessionGraph
from coding_trajectory.metrics import SessionMetrics, TurnMetrics, build_session_graph_full_metrics

from coding_trajectory_cli.plugins import CtPluginContext


@dataclass(frozen=True)
class _Window:
    key: str
    label: str
    start: datetime
    end: datetime
    timezone: str


class ActivityPlugin:
    name = "activity"

    def register(self, namespace_subparsers: argparse._SubParsersAction, ctx: CtPluginContext) -> None:
        def handler(args: argparse.Namespace) -> dict[str, Any]:
            window = _resolve_window(args.window)
            params: dict[str, Any] = {"since_days": _window_since_days(args.window), "modified_since": window.start}
            if args.project:
                params["project_name"] = args.project
            store, _discovery_note = ctx.resolve_document_store(
                params=params,
                global_scope=True,
                current_dir=Path.cwd(),
            )
            return _build_activity_payload(store.session_graphs.values(), args, window=window)

        activity = namespace_subparsers.add_parser("activity", help="Inspect recent activity across sessions.")
        sub = activity.add_subparsers(dest="activity_action", required=True)

        summary = sub.add_parser("summary", help="Show aggregate activity for a time window.")
        _add_common_flags(summary)
        ctx.bind_command(summary, handler=handler, renderer=_render_activity)

        sessions = sub.add_parser("sessions", help="List sessions matching the activity filters.")
        _add_common_flags(sessions)
        ctx.bind_command(sessions, handler=handler, renderer=_render_activity)

        usage = sub.add_parser("usage", help="Show usage breakdown across matching sessions.")
        _add_common_flags(usage)
        usage.add_argument(
            "--extra-billing",
            action="store_true",
            help="Mark cost estimates as outside-plan/API billing instead of plan-usage estimates.",
        )
        ctx.bind_command(usage, handler=handler, renderer=_render_activity)


def _add_common_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--window",
        choices=("5h", "today", "72h", "7d"),
        default="today",
        help="Time window to inspect. Defaults to today.",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="Filter to one project name. Omit to include all projects.",
    )
    parser.add_argument(
        "--account",
        default=None,
        help="Filter to one account key. Accepts either ACCOUNT or VENDOR:ACCOUNT.",
    )
    parser.add_argument(
        "--format",
        choices=("overview", "json"),
        default="overview",
        help="Select stdout format: overview for reading, json for exact data. --output always writes JSON.",
    )


def _build_activity_payload(
    session_graphs: Any,
    args: argparse.Namespace,
    *,
    window: _Window | None = None,
) -> dict[str, Any]:
    window = window or _resolve_window(args.window)
    action = str(args.activity_action)
    extra_billing = bool(getattr(args, "extra_billing", False))
    project_filter = normalize_project_key(args.project) if args.project else None
    account_filter = (args.account or "").strip() or None
    include_activity_usage = action in {"summary", "usage"}
    include_activity_preview = action == "sessions"

    matching_sessions: list[dict[str, Any]] = []
    summary_sessions: list[dict[str, Any]] = []
    project_rollup: dict[str, dict[str, Any]] = {}
    account_rollup: dict[str, dict[str, Any]] = {}
    total_usage = _new_usage_totals()
    total_activity_usage: dict[str, dict[str, float]] = {}

    for session_graph in session_graphs:
        if not isinstance(session_graph, SessionGraph):
            continue
        project_name = session_graph.project_identifier or "unknown"
        if project_filter and normalize_project_key(project_name) != project_filter:
            continue

        graph_matches: list[tuple[Session, dict[str, str | None]]] = []
        for session in session_graph.sessions:
            if not _session_matches_window(session, window):
                continue

            account = _session_account(session)
            if account_filter and account_filter not in {account["key"], account["id"]}:
                continue

            graph_matches.append((session, account))

        if not graph_matches:
            continue

        full_metrics = build_session_graph_full_metrics(session_graph, extra_billing=extra_billing)
        metrics_by_session = {
            session_metrics.session_id: session_metrics
            for session_metrics in full_metrics.sessions
        }

        for session, account in graph_matches:
            session_metrics = metrics_by_session.get(session.session_id)
            usage = _session_metrics_usage(session_metrics)
            activity_usage = (
                _aggregate_turn_activity_usage(session_metrics.turns)
                if include_activity_usage and session_metrics
                else []
            )
            turn_count = len(session.turns)
            activity_preview = _session_activity_preview(session) if include_activity_preview else []

            session_row = {
                "session_id": str(session.session_id),
                "root_session_id": str(session_graph.root_session_id),
                "project": project_name,
                "vendor": session.vendor.value,
                "agent_name": session.agent_name,
                "account": account,
                "started_at": format_datetime(session.started_at),
                "ended_at": format_datetime(session.ended_at),
                "status": session.status.value,
                "turn_count": turn_count,
                "usage": usage,
                "activity_usage": activity_usage,
                "activity_preview": activity_preview,
            }
            if action == "summary":
                summary_sessions.append(session_row)
            else:
                matching_sessions.append(session_row)

            _add_usage(total_usage, usage)
            _merge_activity_usage(total_activity_usage, activity_usage)

            project_entry = project_rollup.setdefault(
                project_name,
                {"project": project_name, "session_count": 0, "turn_count": 0, "usage": _new_usage_totals(), "accounts": set()},
            )
            project_entry["session_count"] += 1
            project_entry["turn_count"] += turn_count
            project_entry["accounts"].add(account["id"])
            _add_usage(project_entry["usage"], usage)

            account_entry = account_rollup.setdefault(
                account["id"],
                {
                    "account": account,
                    "session_count": 0,
                    "turn_count": 0,
                    "usage": _new_usage_totals(),
                    "projects": set(),
                },
            )
            account_entry["session_count"] += 1
            account_entry["turn_count"] += turn_count
            account_entry["projects"].add(project_name)
            _add_usage(account_entry["usage"], usage)

    if action == "summary":
        matching_sessions = nlargest(10, summary_sessions, key=lambda item: str(item.get("started_at") or ""))
    else:
        matching_sessions.sort(key=lambda item: item.get("started_at") or "", reverse=True)
    projects = [
        {
            "project": entry["project"],
            "session_count": entry["session_count"],
            "turn_count": entry["turn_count"],
            "account_count": len(entry["accounts"]),
            "usage": entry["usage"],
        }
        for entry in sorted(project_rollup.values(), key=lambda item: item["project"].lower())
    ]
    accounts = [
        {
            "account": entry["account"],
            "session_count": entry["session_count"],
            "turn_count": entry["turn_count"],
            "project_count": len(entry["projects"]),
            "usage": entry["usage"],
        }
        for entry in sorted(account_rollup.values(), key=lambda item: item["account"]["id"])
    ]

    payload = {
        "command": action,
        "window": {
            "key": window.key,
            "label": window.label,
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "timezone": window.timezone,
        },
        "filters": {
            "project": args.project,
            "account": account_filter,
        },
        "totals": {
            "session_count": len(matching_sessions),
            "turn_count": sum(int(session["turn_count"]) for session in matching_sessions),
            "project_count": len(projects),
            "account_count": len(accounts),
            "usage": total_usage,
            "activity_usage": _activity_usage_list(total_activity_usage),
        },
        "projects": projects,
        "accounts": accounts,
    }
    if action == "summary":
        payload["sessions"] = matching_sessions
        return payload
    if action == "sessions":
        payload["sessions"] = matching_sessions
        return payload
    payload["sessions"] = [
        {
            "session_id": session["session_id"],
            "project": session["project"],
            "vendor": session["vendor"],
            "account": session["account"],
            "started_at": session["started_at"],
            "ended_at": session["ended_at"],
            "turn_count": session["turn_count"],
            "usage": session["usage"],
            "activity_usage": session["activity_usage"],
        }
        for session in matching_sessions
    ]
    return payload


def _resolve_window(raw: str) -> _Window:
    now = datetime.now().astimezone()
    timezone_name = now.tzname() or str(now.tzinfo) or "local"
    if raw == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return _Window(key=raw, label="today", start=start, end=now, timezone=timezone_name)
    delta_by_key = {
        "5h": timedelta(hours=5),
        "72h": timedelta(hours=72),
        "7d": timedelta(days=7),
    }
    delta = delta_by_key[raw]
    return _Window(key=raw, label=f"last {raw}", start=now - delta, end=now, timezone=timezone_name)


def _window_since_days(raw: str) -> int:
    return {
        "5h": 1,
        "today": 1,
        "72h": 3,
        "7d": 7,
    }[raw]


def _session_matches_window(session: Session, window: _Window) -> bool:
    started_at = session.started_at.astimezone(window.start.tzinfo)
    ended_at = (session.ended_at or session.started_at).astimezone(window.start.tzinfo)
    return ended_at >= window.start and started_at <= window.end


def _session_account(session: Session) -> dict[str, str | None]:
    vendor = session.vendor.value
    if session.account and session.account.key:
        key = session.account.key
        return {
            "id": f"{vendor}:{key}",
            "key": key,
            "label": session.account.label or key,
            "vendor": session.account.vendor or vendor,
        }
    return {
        "id": f"{vendor}:unknown",
        "key": "unknown",
        "label": "unknown",
        "vendor": vendor,
    }


def _session_metrics_usage(session_metrics: SessionMetrics | None) -> dict[str, float]:
    if session_metrics is None:
        return _new_usage_totals()
    usage = session_metrics.token_usage.model_dump(mode="json")
    return {**usage, "cost_usd": float(session_metrics.cost_estimate.amount_usd)}


def _aggregate_turn_activity_usage(turns: list[TurnMetrics]) -> list[dict[str, Any]]:
    totals: dict[str, dict[str, float]] = {}
    for turn in turns:
        for step in turn.steps:
            category = _activity_breakdown_kind(step)
            usage = step.token_usage.model_dump(mode="json")
            bucket = totals.setdefault(category, _new_usage_totals())
            _add_usage(bucket, usage)
            bucket["cost_usd"] += float(step.cost_estimate.amount_usd)
    return _activity_usage_list(totals)


def _activity_breakdown_kind(step: Any) -> str:
    if step.kind == "mixed":
        return "mixed_steps"
    if step.tool_count > 0:
        return "tool_steps"
    if step.kind == "response":
        return "response_steps"
    return "other_steps"


def _activity_usage_list(totals: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
    return [
        {"category": category, "usage": usage}
        for category, usage in sorted(totals.items())
    ]


def _merge_activity_usage(target: dict[str, dict[str, float]], items: list[dict[str, Any]]) -> None:
    for item in items:
        category = str(item.get("category") or "-")
        usage = item.get("usage") or {}
        bucket = target.setdefault(category, _new_usage_totals())
        _add_usage(bucket, usage)


def _new_usage_totals() -> dict[str, float]:
    return {
        "input_tokens": 0.0,
        "cached_input_tokens": 0.0,
        "output_tokens": 0.0,
        "reasoning_output_tokens": 0.0,
        "total_tokens": 0.0,
        "cost_usd": 0.0,
    }


def _add_usage(target: dict[str, float], usage: dict[str, Any]) -> None:
    for key in target:
        try:
            target[key] += float(usage.get(key) or 0.0)
        except (TypeError, ValueError):
            continue


def _session_activity_preview(session: Session) -> list[str]:
    preview: list[str] = []
    for turn in session.turns[-3:]:
        for item in build_overview_flows(turn.steps)[:3]:
            label = _activity_label(item)
            if label:
                preview.append(label)
            if len(preview) >= 4:
                return preview
    return preview


def _activity_label(item: dict[str, Any]) -> str | None:
    tool = item.get("tool")
    if isinstance(tool, str) and tool:
        count = int(item.get("count") or 1)
        suffix = f" x{count}" if count > 1 else ""
        for key in ("cmd", "path", "query", "url"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return f"{tool}{suffix}: {value}"
        return f"{tool}{suffix}"
    text = item.get("text")
    if isinstance(text, str) and text:
        return text
    return None


def _render_activity(args: argparse.Namespace, payload: dict[str, Any]) -> str:
    if getattr(args, "format", "overview") == "json":
        return json.dumps(payload, indent=2, ensure_ascii=False)
    action = str(payload.get("command") or "")
    if action == "sessions":
        return _render_sessions(payload)
    if action == "usage":
        return _render_usage(payload)
    return _render_summary(payload)


def _render_summary(payload: dict[str, Any]) -> str:
    totals = payload.get("totals") or {}
    lines = [
        f"Activity Summary ({payload['window']['label']})",
        f"Sessions {totals.get('session_count') or 0}  Turns {totals.get('turn_count') or 0}  "
        f"Projects {totals.get('project_count') or 0}  Accounts {totals.get('account_count') or 0}",
        "",
        f"Usage  {_render_usage_line(totals.get('usage') or {})}",
    ]
    activity_usage = totals.get("activity_usage") or []
    if activity_usage:
        lines.extend(["", "Activity Usage"])
        for item in activity_usage:
            lines.append(f"  {str(item.get('category') or '-'):14} {_render_usage_line(item.get('usage') or {})}")
    projects = payload.get("projects") or []
    if projects:
        lines.extend(["", "Projects"])
        for project in projects[:8]:
            lines.append(
                f"  {project['project']:<24} sessions {project['session_count']:<3} turns {project['turn_count']:<4} "
                f"{_render_usage_line(project['usage'])}"
            )
    return "\n".join(lines).rstrip()


def _render_sessions(payload: dict[str, Any]) -> str:
    sessions = payload.get("sessions") or []
    lines = [f"Activity Sessions ({payload['window']['label']})", ""]
    if not sessions:
        lines.append("No matching sessions.")
        return "\n".join(lines).rstrip()
    for session in sessions:
        account = session.get("account") or {}
        lines.append(
            f"{session['project']}  {_short_id(session['session_id'])}  {session['vendor']}  "
            f"{account.get('label') or '-'}  turns {session['turn_count']}"
        )
        lines.append(
            f"  {session.get('started_at') or '-'} -> {session.get('ended_at') or '-'}  "
            f"{_render_usage_line(session.get('usage') or {})}"
        )
        for item in session.get("activity_preview") or []:
            lines.append(f"  - {item}")
    return "\n".join(lines).rstrip()


def _render_usage(payload: dict[str, Any]) -> str:
    totals = payload.get("totals") or {}
    lines = [
        f"Activity Usage ({payload['window']['label']})",
        "",
        f"Total  {_render_usage_line(totals.get('usage') or {})}",
    ]
    activity_usage = totals.get("activity_usage") or []
    if activity_usage:
        lines.extend(["", "By Category"])
        for item in activity_usage:
            lines.append(f"  {str(item.get('category') or '-'):14} {_render_usage_line(item.get('usage') or {})}")
    sessions = payload.get("sessions") or []
    if sessions:
        lines.extend(["", "Sessions"])
        for session in sessions:
            account = session.get("account") or {}
            lines.append(
                f"  {session['project']:<20} {_short_id(session['session_id'])}  "
                f"{account.get('label') or '-'}  {_render_usage_line(session.get('usage') or {})}"
            )
            for item in session.get("activity_usage") or []:
                lines.append(f"    {str(item.get('category') or '-'):12} {_render_usage_line(item.get('usage') or {})}")
    return "\n".join(lines).rstrip()


def _render_usage_line(usage: dict[str, Any]) -> str:
    return (
        f"input {_format_tokens(usage.get('input_tokens'))}  "
        f"cached {_format_tokens(usage.get('cached_input_tokens'))}  "
        f"output {_format_tokens(usage.get('output_tokens'))}  "
        f"reasoning {_format_tokens(usage.get('reasoning_output_tokens'))}  "
        f"total {_format_tokens(usage.get('total_tokens'))}  "
        f"cost {_format_cost(usage.get('cost_usd'))}"
    )


def _format_tokens(value: Any) -> str:
    try:
        tokens = int(value or 0)
    except (TypeError, ValueError):
        return "-"
    if abs(tokens) >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}m"
    if abs(tokens) >= 1_000:
        return f"{tokens / 1_000:.1f}k"
    return str(tokens)


def _format_cost(value: Any) -> str:
    try:
        cost = float(value or 0)
    except (TypeError, ValueError):
        return "$0.00"
    return f"${cost:.4f}" if cost and cost < 0.01 else f"${cost:.2f}"


def _short_id(value: Any) -> str:
    text = str(value or "")
    return text[:8] if text else "-"


plugin = ActivityPlugin()
