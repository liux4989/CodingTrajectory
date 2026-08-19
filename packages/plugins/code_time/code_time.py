from __future__ import annotations

import argparse
import json
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from coding_trajectory.runtime import (
    PluginApiClient,
    PluginApiError,
    default_plugin_client,
)

WINDOW_SINCE_DAYS = {
    "today": 1,
    "72h": 3,
    "7d": 7,
    "30d": 30,
}


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args and raw_args[0] in {"web", "--web"}:
        return _run_web(raw_args[1:])

    parser = argparse.ArgumentParser(
        prog="ct plugin code-time",
        description="Today's coding work overview: time, sessions, and cost.",
    )
    parser.add_argument(
        "--window",
        choices=tuple(WINDOW_SINCE_DAYS),
        default="today",
    )
    parser.add_argument("--project", default=None, help="Filter to one project.")
    parser.add_argument("--agent-vendor", default=None)
    parser.add_argument(
        "--output",
        choices=("text", "json"),
        default="text",
    )
    args = parser.parse_args(raw_args)

    report = build_report(
        window=args.window,
        project_filter=args.project,
        agent_vendor=args.agent_vendor,
    )

    if args.output == "json":
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render_report(report))
    return 0


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------


def build_report(
    *,
    window: str,
    project_filter: str | None,
    agent_vendor: str | None,
) -> dict[str, Any]:
    cache_key = (window, project_filter, agent_vendor)
    cached = _report_cache_get(cache_key)
    if cached is not None:
        return cached

    since_days = WINDOW_SINCE_DAYS[window]
    client = default_plugin_client()

    project_names = (
        [project_filter]
        if project_filter
        else sorted((_api_result(client, "project.list", {}).get("items") or {}))
    )

    project_sessions: dict[str, list[dict[str, Any]]] = {}
    all_session_ids: list[str] = []
    session_meta: dict[str, dict[str, Any]] = {}

    def _fetch_sessions_for_project(name: str) -> tuple[str, list[dict[str, Any]]]:
        params: dict[str, Any] = {"since_days": since_days, "project_name": name}
        if agent_vendor:
            params["agent_vendor"] = agent_vendor
        payload = _api_result_safe(client, "project.sessions", params)
        return name, (payload or {}).get("items") or []

    with ThreadPoolExecutor(max_workers=min(8, len(project_names) or 1)) as pool:
        futures = {
            pool.submit(_fetch_sessions_for_project, name): name
            for name in project_names
        }
        for future in as_completed(futures):
            name, sessions = future.result()
            if not sessions:
                continue
            project_sessions[name] = sessions
            for session in sessions:
                root_id = session.get("id") or session.get("root_session_id")
                if root_id and root_id not in session_meta:
                    all_session_ids.append(root_id)
                    session_meta[root_id] = session

    session_data_map = _fetch_session_data_bulk(client, all_session_ids)

    project_slices: list[dict[str, Any]] = []
    for project_name, sessions in sorted(project_sessions.items()):
        session_slices: list[dict[str, Any]] = []
        for session in sessions:
            root_id = session.get("id") or session.get("root_session_id")
            data = session_data_map.get(root_id)
            if not data:
                continue
            meta = session_meta.get(root_id, {})
            vendors = meta.get("vendors") or []
            vendor = vendors[0] if vendors else data.get("vendor", "unknown")
            session_slices.append(
                {
                    "root_session_id": root_id,
                    "title": meta.get("title"),
                    "vendor": vendor,
                    "execution_seconds": data.get("execution_seconds", 0),
                    "wait_seconds": data.get("wait_seconds", 0),
                    "turns": data.get("turns", 0),
                    "tool_calls": data.get("tool_calls", 0),
                    "tokens": data.get("tokens", _empty_tokens()),
                    "cost_usd": data.get("cost_usd"),
                }
            )

        if session_slices:
            project_slices.append(_aggregate_project(project_name, session_slices))

    report = {
        "window": window,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "totals": _aggregate_totals(project_slices),
        "projects": project_slices,
    }

    _report_cache_set(cache_key, report)
    return report


def _empty_tokens() -> dict[str, int]:
    return {
        "prompt_tokens": 0,
        "cached_prompt_tokens": 0,
        "cache_write_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "processed_tokens": 0,
    }


def _fetch_session_data_bulk(
    client: PluginApiClient,
    session_ids: list[str],
) -> dict[str, dict[str, Any]]:
    if not session_ids:
        return {}

    unique_ids = list(dict.fromkeys(session_ids))
    chunk_size = 50
    result_map: dict[str, dict[str, Any]] = {}

    for offset in range(0, len(unique_ids), chunk_size):
        chunk = unique_ids[offset : offset + chunk_size]
        requests = [
            {
                "id": root_id,
                "method": "graph.usage",
                "params": {"session_id": root_id},
            }
            for root_id in chunk
        ]
        for request in requests:
            item = client.execute(request)
            if not item.get("ok"):
                continue
            root_id = item.get("id")
            if not root_id:
                continue
            result = item.get("result") or {}
            runtime = result.get("runtime") or {}
            usage = result.get("total_usage") or {}
            estimated_cost = result.get("estimated_cost")
            cost_usd = (
                estimated_cost.get("value_usd")
                if isinstance(estimated_cost, dict)
                else None
            )
            result_map[root_id] = {
                "root_session_id": root_id,
                "vendor": "unknown",
                "execution_seconds": runtime.get("execution_seconds") or 0,
                "wait_seconds": runtime.get("wait_seconds") or 0,
                "turns": runtime.get("turns") or 0,
                "tool_calls": runtime.get("tool_calls") or 0,
                "tokens": _extract_tokens(usage),
                "cost_usd": cost_usd,
            }

    return result_map


_REPORT_CACHE: dict[tuple, tuple[float, dict[str, Any]]] = {}
_REPORT_CACHE_LOCK = threading.Lock()
_REPORT_CACHE_TTL = 30


def _report_cache_get(key: tuple) -> dict[str, Any] | None:
    with _REPORT_CACHE_LOCK:
        entry = _REPORT_CACHE.get(key)
        if entry and time.monotonic() - entry[0] < _REPORT_CACHE_TTL:
            return entry[1]
    return None


def _report_cache_set(key: tuple, report: dict[str, Any]) -> None:
    with _REPORT_CACHE_LOCK:
        _REPORT_CACHE[key] = (time.monotonic(), report)


def _extract_tokens(usage: dict[str, Any]) -> dict[str, int]:
    return {
        "prompt_tokens": usage.get("prompt_tokens") or 0,
        "cached_prompt_tokens": usage.get("cached_prompt_tokens") or 0,
        "cache_write_tokens": usage.get("cache_write_tokens") or 0,
        "completion_tokens": usage.get("completion_tokens") or 0,
        "reasoning_tokens": usage.get("reasoning_tokens") or 0,
        "processed_tokens": usage.get("processed_tokens") or 0,
    }


def _aggregate_project(
    project_name: str,
    session_slices: list[dict[str, Any]],
) -> dict[str, Any]:
    execution_seconds = sum(s["execution_seconds"] for s in session_slices)
    wait_seconds = sum(s["wait_seconds"] for s in session_slices)
    turns = sum(s["turns"] for s in session_slices)
    tool_calls = sum(s["tool_calls"] for s in session_slices)
    tokens = _sum_tokens(s["tokens"] for s in session_slices)
    cost_usd = sum((s["cost_usd"] or 0) for s in session_slices) or None

    return {
        "project_name": project_name,
        "session_count": len(session_slices),
        "execution_seconds": execution_seconds,
        "wait_seconds": wait_seconds,
        "turns": turns,
        "tool_calls": tool_calls,
        "tokens": tokens,
        "cost_usd": cost_usd,
        "sessions": session_slices,
    }


def _aggregate_totals(project_slices: list[dict[str, Any]]) -> dict[str, Any]:
    session_count = sum(p["session_count"] for p in project_slices)
    execution_seconds = sum(p["execution_seconds"] for p in project_slices)
    wait_seconds = sum(p["wait_seconds"] for p in project_slices)
    turns = sum(p["turns"] for p in project_slices)
    tool_calls = sum(p["tool_calls"] for p in project_slices)
    tokens = _sum_tokens(p["tokens"] for p in project_slices)
    cost_usd = sum((p["cost_usd"] or 0) for p in project_slices) or None

    return {
        "session_count": session_count,
        "project_count": len(project_slices),
        "execution_seconds": execution_seconds,
        "wait_seconds": wait_seconds,
        "turns": turns,
        "tool_calls": tool_calls,
        "tokens": tokens,
        "cost_usd": cost_usd,
    }


def _sum_tokens(iterable) -> dict[str, int]:
    result = {
        "prompt_tokens": 0,
        "cached_prompt_tokens": 0,
        "cache_write_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "processed_tokens": 0,
    }
    for tokens in iterable:
        for key in result:
            result[key] += tokens.get(key) or 0
    return result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_report(report: dict[str, Any]) -> str:
    totals = report["totals"]
    projects = report["projects"]
    window = report["window"]

    lines = [
        f"Code Time ({window})",
        "=" * 60,
        "",
        _totals_line(totals),
        "",
    ]

    if not projects:
        lines.append("No sessions found.")
        return "\n".join(lines)

    col_project = max(len(p["project_name"]) for p in projects)
    col_project = max(col_project, 7)
    col_project = min(col_project, 32)

    header = (
        f"  {'Project':<{col_project}}  {'Sessions':>8}  {'Coding':>10}  "
        f"{'Tokens':>10}  {'Cost':>8}"
    )
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))

    for project in projects:
        name = _one_line(project["project_name"], col_project)
        sessions_n = project["session_count"]
        coding = _format_duration(project["execution_seconds"])
        tokens = _format_tokens(project["tokens"]["processed_tokens"])
        cost = _format_cost(project["cost_usd"])
        lines.append(
            f"  {name:<{col_project}}  {sessions_n:>8}  {coding:>10}  "
            f"{tokens:>10}  {cost:>8}"
        )

    lines.append("")
    lines.append("  " + "-" * (len(header) - 2))
    t_coding = _format_duration(totals["execution_seconds"])
    t_tokens = _format_tokens(totals["tokens"]["processed_tokens"])
    t_cost = _format_cost(totals["cost_usd"])
    lines.append(
        f"  {'Total':<{col_project}}  {totals['session_count']:>8}  {t_coding:>10}  "
        f"{t_tokens:>10}  {t_cost:>8}"
    )

    lines.append("")
    for project in projects:
        lines.append(f"  {project['project_name']}")
        for session in project["sessions"]:
            sid = str(session["root_session_id"])[:8]
            title = _one_line(session.get("title") or "-", 60)
            vendor = session.get("vendor") or "-"
            coding = _format_duration(session["execution_seconds"])
            cost = _format_cost(session["cost_usd"])
            lines.append(f"    {sid}  {vendor:<16} {coding:>8}  {cost:>8}  {title}")
        lines.append("")

    return "\n".join(lines).rstrip()


def _totals_line(totals: dict[str, Any]) -> str:
    parts = [
        f"Sessions: {totals['session_count']}",
        f"Projects: {totals['project_count']}",
        f"Coding time: {_format_duration(totals['execution_seconds'])}",
        f"Wait time: {_format_duration(totals['wait_seconds'])}",
        f"Turns: {totals['turns']}",
        f"Tool calls: {totals['tool_calls']}",
        f"Tokens: {_format_tokens(totals['tokens']['processed_tokens'])}",
    ]
    if totals.get("cost_usd") is not None:
        parts.append(f"Cost: {_format_cost(totals['cost_usd'])}")
    return "  ".join(parts)


def _format_duration(seconds: int) -> str:
    if not seconds:
        return "-"
    hours, remainder = divmod(int(seconds), 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{int(seconds)}s"


def _format_tokens(tokens: int) -> str:
    if not tokens:
        return "-"
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.0f}k"
    return str(tokens)


def _format_cost(cost_usd: float | None) -> str:
    if cost_usd is None:
        return "-"
    if cost_usd < 0.01:
        return f"${cost_usd:.4f}"
    return f"${cost_usd:.2f}"


def _one_line(value: str, limit: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ---------------------------------------------------------------------------
# ct service helpers
# ---------------------------------------------------------------------------


def _api_result(
    client: PluginApiClient, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    try:
        result = client.call(method, params)
    except PluginApiError as exc:
        raise SystemExit(str(exc)) from exc
    if not isinstance(result, dict):
        raise SystemExit(f"ct api call {method} returned a non-object result")
    return result


def _api_result_safe(
    client: PluginApiClient, method: str, params: dict[str, Any]
) -> dict[str, Any] | None:
    try:
        result = client.call(method, params)
    except PluginApiError:
        return None
    return result if isinstance(result, dict) else None


def _run_web(args: list[str]) -> int:
    try:
        from . import code_time_web
    except ImportError:
        try:
            import code_time_web
        except ImportError:
            print(
                "error: code_time_web module not found; ensure the web module is available.",
                file=sys.stderr,
            )
            return 2
    return code_time_web.main(args)


if __name__ == "__main__":
    raise SystemExit(main())
