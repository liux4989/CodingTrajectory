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
    if raw_args and raw_args[0] == "forecast":
        return _run_forecast(raw_args[1:])

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


# ---------------------------------------------------------------------------
# forecast — agent temporality forecasts and calibration
# ---------------------------------------------------------------------------

_KIND_LABELS = {
    "historical_backcast": "backcast",
    "prospective": "prospective",
    "prospective_unbound": "unbound",
    "runtime_advisory": "advisory",
}


def _run_forecast(args: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="ct plugin code-time forecast",
        description=(
            "Agent temporality: duration forecasts versus measured actuals. "
            "Evidence is always labeled by forecast kind; historical backcasts "
            "are not prospective calibration."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_predict = sub.add_parser("predict", help="One forecast for a turn or task text.")
    p_predict.add_argument("--turn-id", default=None)
    p_predict.add_argument("--task-text", default=None)
    p_predict.add_argument("--project", default=None)
    p_predict.add_argument("--target-agent-vendor", default=None)
    p_predict.add_argument("--target-harness-name", default=None)
    p_predict.add_argument("--target-model", default=None)
    p_predict.add_argument("--target-effort", default=None)
    p_predict.add_argument("--estimator-model", default=None)
    p_predict.add_argument("--estimator-effort", default=None)
    p_predict.add_argument("--max-examples", type=int, default=8)

    p_bind = sub.add_parser("bind", help="Bind an unbound forecast to a turn once.")
    p_bind.add_argument("prediction_id")
    p_bind.add_argument("turn_id")

    p_get = sub.add_parser("get", help="Show one forecast record.")
    p_get.add_argument("prediction_id")

    p_list = sub.add_parser("list", help="List forecasts.")
    p_list.add_argument("--kind", default=None)
    p_list.add_argument("--project", default=None)
    p_list.add_argument("--target-harness-name", default=None)
    p_list.add_argument("--status", default=None)
    p_list.add_argument("--limit", type=int, default=50)

    p_cal = sub.add_parser("calibration", help="Cohort calibration statistics.")
    p_cal.add_argument("--kind", default=None)
    p_cal.add_argument("--project", default=None)
    p_cal.add_argument("--target-harness-name", default=None)
    p_cal.add_argument("--target-model", default=None)
    p_cal.add_argument("--estimator-model", default=None)

    p_backfill = sub.add_parser(
        "backfill", help="Run or resume a resumable historical-backcast job."
    )
    p_backfill.add_argument("--project", default=None)
    p_backfill.add_argument("--since-days", type=int, default=None)
    p_backfill.add_argument("--agent-vendor", default=None)
    p_backfill.add_argument("--max", dest="max_forecasts", type=int, default=25)
    p_backfill.add_argument("--max-examples", type=int, default=8)
    p_backfill.add_argument("--estimator-model", default=None)
    p_backfill.add_argument("--estimator-effort", default=None)
    p_backfill.add_argument("--job-id", default=None, help="Resume an existing job.")

    p_job = sub.add_parser("job", help="Show one backfill job status.")
    p_job.add_argument("job_id")

    parsed = parser.parse_args(args)
    client = default_plugin_client()

    if parsed.command == "predict":
        params: dict[str, Any] = {
            "max_examples": parsed.max_examples,
        }
        if parsed.turn_id:
            params["turn_id"] = parsed.turn_id
        if parsed.task_text:
            params["task_text"] = parsed.task_text
        for arg_key, param_key in (
            ("project", "project_name"),
            ("target_agent_vendor", "target_agent_vendor"),
            ("target_harness_name", "target_harness_name"),
            ("target_model", "target_model"),
            ("target_effort", "target_effort"),
            ("estimator_model", "estimator_model"),
            ("estimator_effort", "estimator_effort"),
        ):
            value = getattr(parsed, arg_key)
            if value:
                params[param_key] = value
        result = _forecast_api(client, "estimate.predict", params)
        return _render_predict(result)

    if parsed.command == "bind":
        result = _forecast_api(
            client,
            "estimate.bind",
            {"prediction_id": parsed.prediction_id, "turn_id": parsed.turn_id},
        )
        return _render_predict(result)

    if parsed.command == "get":
        result = _forecast_api(
            client, "estimate.get", {"prediction_id": parsed.prediction_id}
        )
        forecast = result.get("forecast")
        if not forecast:
            print("Forecast not found.")
            return 1
        print(json.dumps(forecast, indent=2, default=str))
        return 0

    if parsed.command == "list":
        params = {}
        for arg_key, param_key in (
            ("kind", "forecast_kind"),
            ("project", "project_name"),
            ("target_harness_name", "target_harness_name"),
            ("status", "status"),
        ):
            value = getattr(parsed, arg_key)
            if value:
                params[param_key] = value
        params["limit"] = parsed.limit
        result = _forecast_api(client, "estimate.list", params)
        print(_render_forecast_table(result.get("items") or []))
        return 0

    if parsed.command == "calibration":
        params = {}
        for arg_key, param_key in (
            ("kind", "forecast_kind"),
            ("project", "project_name"),
            ("target_harness_name", "target_harness_name"),
            ("target_model", "target_model"),
            ("estimator_model", "estimator_model"),
        ):
            value = getattr(parsed, arg_key)
            if value:
                params[param_key] = value
        result = _forecast_api(client, "estimate.calibration", params)
        print(_render_calibration(result))
        return 0

    if parsed.command == "backfill":
        params = {
            "max_forecasts": parsed.max_forecasts,
            "max_examples": parsed.max_examples,
        }
        for arg_key, param_key in (
            ("project", "project_name"),
            ("since_days", "since_days"),
            ("agent_vendor", "agent_vendor"),
            ("estimator_model", "estimator_model"),
            ("estimator_effort", "estimator_effort"),
            ("job_id", "job_id"),
        ):
            value = getattr(parsed, arg_key)
            if value is not None:
                params[param_key] = value
        result = _forecast_api(client, "estimate.backfill.start", params)
        print(_render_job(result.get("job") or {}))
        return 0

    if parsed.command == "job":
        result = _forecast_api(
            client, "estimate.backfill.status", {"job_id": parsed.job_id}
        )
        print(_render_job(result.get("job") or {}))
        return 0

    return 2


def _forecast_api(client, method: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        result = client.call(method, params)
    except PluginApiError as exc:
        raise SystemExit(str(exc)) from exc
    if not isinstance(result, dict):
        raise SystemExit(f"ct api call {method} returned a non-object result")
    return result


def _render_predict(result: dict[str, Any]) -> int:
    failure = result.get("failure")
    forecast = result.get("forecast")
    if failure and not forecast:
        print(
            f"Forecast failed ({failure.get('state')}): {failure.get('reason')}"
            + (f" — {failure.get('detail')}" if failure.get("detail") else "")
        )
        return 1
    if failure:
        print(
            f"Note ({failure.get('state')}): {failure.get('reason')}"
            + (f" — {failure.get('detail')}" if failure.get("detail") else "")
        )
    if not forecast:
        print("No forecast record.")
        return 1
    if result.get("reused_existing"):
        print("(existing forecast reused; idempotent)")
    print(_render_forecast_table([forecast]))
    return 0


def _render_forecast_table(items: list[dict[str, Any]]) -> str:
    lines = [
        "Forecasts (kind-labeled; backcasts are not prospective evidence)",
        "=" * 72,
    ]
    if not items:
        lines.append("No forecasts found.")
        return "\n".join(lines)
    header = (
        f"  {'ID':<10} {'Kind':<12} {'Project':<18} {'Target':<22} "
        f"{'p50':>7} {'p80':>7} {'Actual':>8} {'Ratio':>6}  Status"
    )
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for item in items:
        kind = _KIND_LABELS.get(item.get("forecast_kind"), item.get("forecast_kind"))
        target = item.get("target") or {}
        target_label = (
            "/".join(
                part
                for part in (target.get("harness_name"), target.get("model"))
                if part
            )
            or "-"
        )
        comparison = item.get("comparison") or {}
        actual_seconds = comparison.get("actual_execution_seconds")
        p50 = item.get("p50_minutes")
        actual_minutes = actual_seconds / 60.0 if actual_seconds else None
        ratio = (
            f"{p50 / actual_minutes:.2f}x"
            if p50 and actual_minutes and actual_minutes > 0
            else "-"
        )
        lines.append(
            f"  {str(item.get('prediction_id'))[:8]:<10} {kind:<12} "
            f"{_one_line(item.get('project_name') or '-', 18):<18} "
            f"{_one_line(target_label, 22):<22} "
            f"{_fmt_minutes(p50):>7} {_fmt_minutes(item.get('p80_minutes')):>7} "
            f"{_fmt_minutes(actual_minutes):>8} {ratio:>6}  {item.get('status')}"
        )
    return "\n".join(lines)


def _render_calibration(result: dict[str, Any]) -> str:
    policy = result.get("policy") or {}
    cohorts = result.get("cohorts") or []
    lines = [
        f"Calibration (policy {policy.get('version', '?')})",
        "=" * 72,
    ]
    if not cohorts:
        lines.append("No cohorts match the filters.")
        return "\n".join(lines)
    for cohort in cohorts:
        key = cohort.get("cohort") or {}
        kind = _KIND_LABELS.get(key.get("forecast_kind"), key.get("forecast_kind"))
        lines.append(
            f"\nCohort: kind={kind} estimator={key.get('estimator_provider')}"
            f"/{key.get('estimator_model') or 'default'}"
            f" prompt={key.get('prompt_version')}"
            f" retrieval={key.get('retrieval_policy_version')}"
        )
        lines.append(
            f"  eligible={cohort.get('eligible_count')} "
            f"primary={cohort.get('primary_count')} "
            f"exclusions={cohort.get('exclusions') or {}}"
        )
        stats = cohort.get("statistics") or {}
        lines.append(f"  usable samples: {stats.get('sample_count')}")
        ratio = stats.get("calibration_ratio")
        if isinstance(ratio, dict):
            if ratio.get("value") == "undefined":
                lines.append(f"  calibration ratio: undefined ({ratio.get('reason')})")
            else:
                lines.append(
                    f"  calibration ratio (geo mean p50/actual): {ratio.get('value')}"
                    f"  95% interval {ratio.get('interval_95')}"
                )
        lines.append(f"  median |log error|: {stats.get('median_absolute_log_error')}")
        lines.append(f"  within 1.5x of actual: {stats.get('within_1_5x_share')}")
        lines.append(f"  p80 coverage: {stats.get('p80_coverage')}")
        compression = stats.get("compression_exponent")
        if isinstance(compression, dict):
            if compression.get("value") == "undefined":
                lines.append(
                    f"  compression exponent: undefined ({compression.get('reason')})"
                )
            else:
                lines.append(f"  compression exponent: {compression.get('value')}")
        buckets = [b for b in (cohort.get("buckets") or []) if b.get("sample_count")]
        if buckets:
            lines.append("  duration buckets (diagnostic, not difficulty):")
            for bucket in buckets:
                lines.append(
                    f"    {bucket.get('bucket'):<10} n={bucket.get('sample_count')}"
                    f" ratio={bucket.get('calibration_ratio')}"
                    f" within1.5x={bucket.get('within_1_5x_share')}"
                )
    return "\n".join(lines)


def _render_job(job: dict[str, Any]) -> str:
    counts = job.get("counts") or {}
    lines = [
        f"Backfill job {job.get('job_id')}",
        "=" * 72,
        f"  status: {job.get('status')}  stop: {job.get('stop_reason') or '-'}",
        f"  eligible: {counts.get('eligible', 0)}  "
        f"succeeded: {counts.get('succeeded', 0)}  "
        f"skipped existing: {counts.get('skipped_existing', 0)}  "
        f"retryable failed: {counts.get('retryable_failed', 0)}  "
        f"permanent failed: {counts.get('permanent_failed', 0)}",
    ]
    excluded = counts.get("excluded") or {}
    if excluded:
        lines.append(
            "  excluded: "
            + ", ".join(
                f"{reason}={count}" for reason, count in sorted(excluded.items())
            )
        )
    return "\n".join(lines)


def _fmt_minutes(minutes: float | None) -> str:
    if minutes is None:
        return "-"
    if minutes < 60:
        return f"{minutes:.0f}m"
    return f"{minutes / 60:.1f}h"


if __name__ == "__main__":
    raise SystemExit(main())
