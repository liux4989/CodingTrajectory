# ruff: noqa: F401
from __future__ import annotations

import hashlib
import json
import math
import re
import shlex
from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal

from coding_trajectory.runtime import ServiceApiClient

from datahub_plugin.projections.stat_utils import parse_datetime as _parse_datetime
from datahub_plugin.projections.stat_utils import percentile as _percentile
from datahub_plugin.projections.stat_utils import safe_div as _safe_div
from datahub_plugin.projections.token_efficiency_models import (
    ComparisonDelta,
    Contributor,
    Coverage,
    Distribution,
    Grain,
    HotspotRow,
    OutlierRow,
    PatternDelta,
    PatternIndicators,
    PatternMetrics,
    PatternRow,
    PeriodComparison,
    PeriodSummary,
    ProjectOption,
    ProjectProjection,
    UnitDistributions,
)

# A batch shares one ServiceRuntime and its resolved store.  Keep this bounded
# so one CLI response cannot grow without limit, while avoiding repeated index
# loads/saves for the small legacy chunks.  Batches remain sequential: parallel
# CLI processes would race on the shared index cache.
_BATCH_SIZE = 128
_PATTERN_LABELS = {
    "broad_search_read": "Broad / batched search and read",
    "targeted_search_read": "Targeted search and read",
    "editing": "Editing and writing",
    "validation": "Validation",
    "web": "Web access",
    "coordination": "Agent coordination",
    "other": "Other tools",
    "repeated_read": "Repeated resource access",
    "parallel_fanout": "Parallel fan-out",
    "truncated_output": "Truncated tool output",
}
_EXCLUSIVE_PATTERNS = tuple(list(_PATTERN_LABELS)[:7])
_INDICATOR_PATTERNS = ("repeated_read", "parallel_fanout", "truncated_output")
_FILE_RE = re.compile(
    r"(?<![\w./-])("
    r"(?:(?:/|\.{1,2}/)?(?:[\w.@+-]+/)+)?"
    r"[\w.@+-]+\.[A-Za-z][A-Za-z0-9]{0,11}"
    r")(?![\w.-])"
)
_BARE_FILE_SUFFIXES = {
    "astro",
    "bash",
    "c",
    "cc",
    "cfg",
    "cjs",
    "conf",
    "cpp",
    "cs",
    "css",
    "csv",
    "env",
    "fish",
    "go",
    "gql",
    "graphql",
    "h",
    "hpp",
    "htm",
    "html",
    "ini",
    "java",
    "js",
    "json",
    "jsonl",
    "jsx",
    "kt",
    "kts",
    "less",
    "lock",
    "md",
    "mdx",
    "mjs",
    "php",
    "properties",
    "proto",
    "ps1",
    "py",
    "pyi",
    "rb",
    "rs",
    "rst",
    "sass",
    "scala",
    "scss",
    "sh",
    "sql",
    "svelte",
    "swift",
    "toml",
    "ts",
    "tsv",
    "tsx",
    "txt",
    "vue",
    "wasm",
    "xml",
    "yaml",
    "yml",
    "zsh",
}
_SEARCH_COMMAND_RE = re.compile(
    r"(?:^|&&|\|\||;|\n|[\"']cmd[\"']\s*:\s*[\"'])\s*"
    r"(?P<command>(?:[\w./-]+/)?(?:rg|grep|ripgrep))\b"
)
_SEARCH_OPTIONS_WITH_VALUES = {
    "-A",
    "-B",
    "-C",
    "-e",
    "-f",
    "-g",
    "-m",
    "-t",
    "--after-context",
    "--before-context",
    "--context",
    "--encoding",
    "--exclude",
    "--exclude-dir",
    "--file",
    "--glob",
    "--iglob",
    "--include",
    "--max-count",
    "--regexp",
    "--type",
    "--type-add",
}
_SEARCH_RE = re.compile(r"(?:\brg\b|\bgrep\b|\bripgrep\b|search_text)")
_READ_RE = re.compile(
    r"(?:\bsed\s+-n\b|\bcat\b|\bhead\b|\btail\b|read_file|view_file|open_file)"
)
_VALIDATION_RE = re.compile(
    r"(?:\bpytest\b|\bruff\b|\bmypy\b|\bpy_compile\b|\bcompileall\b|"
    r"\bcargo\s+(?:test|check|clippy)\b|\b(?:bun|npm|pnpm|yarn)\s+(?:run\s+)?"
    r"(?:test|build|lint|check)\b|\btsc\b|validate|quality-gate|diff\s+--check)"
)
_EDIT_RE = re.compile(
    r"(?:apply_patch|write_file|edit_file|create_file|perl\s+-[pi]|"
    r"\bsed\s+-i\b|\bmv\b|\bcp\b)"
)
_WEB_RE = re.compile(
    r"(?:web_search|web_fetch|search_query|image_query|browser\.|"
    r"\b(?:curl|wget)\b[^\n]{0,160}https?://)"
)
_COORDINATION_RE = re.compile(
    r"(?:spawn_agent|followup_task|send_message|wait_agent|update_plan|"
    r"request_user_input|create_thread|send_message_to_thread)"
)
_EDIT_TOOL_NAMES = {
    "apply_patch",
    "create_file",
    "edit_file",
    "write_file",
}
_COORDINATION_TOOL_NAMES = {
    "create_thread",
    "followup_task",
    "request_user_input",
    "send_message",
    "send_message_to_thread",
    "spawn_agent",
    "update_plan",
    "wait_agent",
}


from datahub_plugin.projections.token_efficiency_tool_analysis import _latest_periods


def _required_discovery_days(since_days: int, now_local: datetime) -> int:
    """Cover the requested trend and both latest completed weekly buckets."""
    _current, previous = _latest_periods("weekly", now_local)
    weekly_lookback = math.ceil((now_local - previous[0]).total_seconds() / 86_400)
    return max(since_days, weekly_lookback + 1)


def _trend_periods(
    grain: Grain, since_days: int, now_local: datetime
) -> list[tuple[datetime, datetime, str, str]]:
    current, _previous = _latest_periods(grain, now_local)
    count = since_days if grain == "daily" else max(math.ceil(since_days / 7), 2)
    step = timedelta(days=1 if grain == "daily" else 7)
    rows = [
        _period_tuple(
            current[0] - step * offset,
            current[1] - step * offset,
            grain,
        )
        for offset in range(count)
    ]
    return list(reversed(rows))


def _period_tuple(
    start: datetime, end: datetime, grain: Grain
) -> tuple[datetime, datetime, str, str]:
    if grain == "daily":
        key = start.date().isoformat()
        label = start.strftime("%b %-d")
    else:
        iso = start.date().isocalendar()
        key = f"{iso.year}-W{iso.week:02d}"
        label = f"{start.strftime('%b %-d')} – {(end - timedelta(days=1)).strftime('%b %-d')}"
    return start, end, key, label


def _in_period(
    value: datetime | None,
    period: tuple[datetime, datetime, str, str],
) -> bool:
    return value is not None and period[0] <= value < period[1]


def _period_summary(
    period: tuple[datetime, datetime, str, str],
    *,
    session_records: list[dict[str, Any]],
    turn_records: list[dict[str, Any]],
    tool_records: list[dict[str, Any]],
) -> PeriodSummary:
    sessions = [
        row for row in session_records if _in_period(row.get("completed_at"), period)
    ]
    turns = [row for row in turn_records if _in_period(row.get("completed_at"), period)]
    turn_keys = {(str(row["session_id"]), str(row["turn_id"])) for row in turns}
    tools = [
        row
        for row in tool_records
        if (
            str(row.get("session_id") or ""),
            str(row.get("turn_id") or ""),
        )
        in turn_keys
    ]
    pattern_tokens = sum(
        int(row.get("prompt_tokens") or 0)
        for row in tools
        if row.get("category") in {"broad_search_read", "targeted_search_read"}
    )
    total_prompt = sum(int(row.get("prompt_tokens") or 0) for row in turns)
    return PeriodSummary(
        bucket=period[2],
        label=period[3],
        started_at=period[0],
        ended_at=period[1],
        session_count=len(sessions),
        turn_count=len(turns),
        total_prompt_tokens=total_prompt,
        session_prompt=_distribution(
            [int(row.get("prompt_tokens") or 0) for row in sessions]
        ),
        turn_prompt=_distribution(
            [int(row.get("prompt_tokens") or 0) for row in turns]
        ),
        pattern_prompt_tokens=pattern_tokens,
        pattern_share=_safe_div(pattern_tokens, total_prompt),
    )


def _comparison(
    grain: Grain,
    current_period: tuple[datetime, datetime, str, str],
    previous_period: tuple[datetime, datetime, str, str],
    *,
    session_records: list[dict[str, Any]],
    turn_records: list[dict[str, Any]],
    tool_records: list[dict[str, Any]],
) -> PeriodComparison:
    current = _period_summary(
        current_period,
        session_records=session_records,
        turn_records=turn_records,
        tool_records=tool_records,
    )
    previous = _period_summary(
        previous_period,
        session_records=session_records,
        turn_records=turn_records,
        tool_records=tool_records,
    )
    return PeriodComparison(
        grain=grain,
        current=current,
        previous=previous,
        deltas=ComparisonDelta(
            total_prompt_tokens_pct=_pct_change(
                current.total_prompt_tokens, previous.total_prompt_tokens
            ),
            session_median_pct=_pct_change(
                current.session_prompt.median, previous.session_prompt.median
            ),
            session_p90_pct=_pct_change(
                current.session_prompt.p90, previous.session_prompt.p90
            ),
            turn_median_pct=_pct_change(
                current.turn_prompt.median, previous.turn_prompt.median
            ),
            turn_p90_pct=_pct_change(current.turn_prompt.p90, previous.turn_prompt.p90),
        ),
    )


def _pattern_rows(
    current_period: tuple[datetime, datetime, str, str],
    previous_period: tuple[datetime, datetime, str, str],
    *,
    session_records: list[dict[str, Any]],
    turn_records: list[dict[str, Any]],
    tool_records: list[dict[str, Any]],
) -> list[PatternRow]:
    rows: list[PatternRow] = []
    for key in (*_EXCLUSIVE_PATTERNS, *_INDICATOR_PATTERNS):
        current, contributors = _pattern_metrics(
            key,
            current_period,
            session_records=session_records,
            turn_records=turn_records,
            tool_records=tool_records,
        )
        previous, _ = _pattern_metrics(
            key,
            previous_period,
            session_records=session_records,
            turn_records=turn_records,
            tool_records=tool_records,
        )
        if (
            current.calls == 0
            and previous.calls == 0
            and current.conditional.session.count == 0
            and previous.conditional.session.count == 0
        ):
            continue
        rows.append(
            PatternRow(
                key=key,
                label=_PATTERN_LABELS[key],
                kind=("exclusive" if key in _EXCLUSIVE_PATTERNS else "indicator"),
                current=current,
                previous=previous,
                deltas=PatternDelta(
                    prompt_tokens_pct=_pct_change(
                        current.total_prompt_tokens, previous.total_prompt_tokens
                    ),
                    incidence_rate_points=round(
                        current.incidence_rate - previous.incidence_rate, 4
                    ),
                    calls_pct=_pct_change(current.calls, previous.calls),
                    session_median_pct=_pct_change(
                        current.zero_inclusive.session.median,
                        previous.zero_inclusive.session.median,
                    ),
                    session_p90_pct=_pct_change(
                        current.zero_inclusive.session.p90,
                        previous.zero_inclusive.session.p90,
                    ),
                    turn_median_pct=_pct_change(
                        current.zero_inclusive.turn.median,
                        previous.zero_inclusive.turn.median,
                    ),
                    turn_p90_pct=_pct_change(
                        current.zero_inclusive.turn.p90,
                        previous.zero_inclusive.turn.p90,
                    ),
                ),
                contributors=contributors[:30],
            )
        )
    return sorted(rows, key=lambda row: row.current.total_prompt_tokens, reverse=True)


def _pattern_metrics(
    key: str,
    period: tuple[datetime, datetime, str, str],
    *,
    session_records: list[dict[str, Any]],
    turn_records: list[dict[str, Any]],
    tool_records: list[dict[str, Any]],
) -> tuple[PatternMetrics, list[Contributor]]:
    sessions = [
        row for row in session_records if _in_period(row.get("completed_at"), period)
    ]
    session_ids = {str(row["session_id"]) for row in sessions}
    turns = [row for row in turn_records if _in_period(row.get("completed_at"), period)]
    active_session_ids = {str(row["session_id"]) for row in turns}
    turn_keys = {(str(row["session_id"]), str(row["turn_id"])) for row in turns}
    session_tools = [
        row
        for row in tool_records
        if str(row.get("session_id") or "") in session_ids
        and _matches_pattern(row, key)
    ]
    turn_tools = [
        row
        for row in tool_records
        if (
            str(row.get("session_id") or ""),
            str(row.get("turn_id") or ""),
        )
        in turn_keys
        and _matches_pattern(row, key)
    ]
    by_session: dict[str, int] = defaultdict(int)
    by_turn: dict[tuple[str, str], int] = defaultdict(int)
    contributor_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for row in session_tools:
        tokens = int(row.get("prompt_tokens") or 0)
        session_id = str(row.get("session_id") or "")
        by_session[session_id] += tokens
    for row in turn_tools:
        tokens = int(row.get("prompt_tokens") or 0)
        session_id = str(row.get("session_id") or "")
        turn_id = str(row.get("turn_id") or "")
        by_turn[(session_id, turn_id)] += tokens
        target = contributor_rows.setdefault(
            (session_id, turn_id),
            {
                "session_id": session_id,
                "turn_id": turn_id,
                "title": row.get("title"),
                "prompt_tokens": 0,
                "calls": 0,
                "repeated_calls": 0,
                "pattern": row.get("category"),
            },
        )
        target["prompt_tokens"] += tokens
        target["calls"] += 1
        target["repeated_calls"] += int(bool(row.get("repeated_read")))
    session_values = [by_session.get(str(row["session_id"]), 0) for row in sessions]
    turn_values = [
        by_turn.get((str(row["session_id"]), str(row["turn_id"])), 0) for row in turns
    ]
    total_prompt = sum(int(row.get("prompt_tokens") or 0) for row in turns)
    activity_pattern_sessions = {str(row.get("session_id") or "") for row in turn_tools}
    activity_pattern_tokens = sum(by_turn.values())
    metrics = PatternMetrics(
        incidence_count=len(activity_pattern_sessions),
        incidence_rate=_safe_div(
            len(activity_pattern_sessions), len(active_session_ids)
        ),
        calls=len(turn_tools),
        total_prompt_tokens=activity_pattern_tokens,
        token_share=_safe_div(activity_pattern_tokens, total_prompt),
        zero_inclusive=UnitDistributions(
            session=_distribution(session_values),
            turn=_distribution(turn_values),
        ),
        conditional=UnitDistributions(
            session=_distribution([value for value in session_values if value > 0]),
            turn=_distribution([value for value in turn_values if value > 0]),
        ),
        indicators=PatternIndicators(
            repeated_read=sum(
                int(bool(row.get("repeated_read"))) for row in turn_tools
            ),
            parallel_fanout=sum(
                int(bool(row.get("parallel_fanout"))) for row in turn_tools
            ),
            truncated_output=sum(
                int(bool(row.get("truncated_output"))) for row in turn_tools
            ),
        ),
    )
    contributors = [
        Contributor(**row)
        for row in sorted(
            contributor_rows.values(),
            key=lambda value: int(value["prompt_tokens"]),
            reverse=True,
        )
    ]
    return metrics, contributors


def _matches_pattern(row: dict[str, Any], key: str) -> bool:
    if key in _EXCLUSIVE_PATTERNS:
        return row.get("category") == key
    return bool(row.get(key))


def _hotspot_rows(
    current_period: tuple[datetime, datetime, str, str],
    previous_period: tuple[datetime, datetime, str, str],
    *,
    session_records: list[dict[str, Any]],
    turn_records: list[dict[str, Any]],
    tool_records: list[dict[str, Any]],
) -> list[HotspotRow]:
    del turn_records
    current_session_ids = {
        str(row["session_id"])
        for row in session_records
        if _in_period(row.get("completed_at"), current_period)
    }
    previous_session_ids = {
        str(row["session_id"])
        for row in session_records
        if _in_period(row.get("completed_at"), previous_period)
    }
    current = [
        row
        for row in tool_records
        if str(row.get("session_id") or "") in current_session_ids
    ]
    previous = [
        row
        for row in tool_records
        if str(row.get("session_id") or "") in previous_session_ids
    ]
    previous_totals: dict[str, int] = defaultdict(int)
    previous_calls: dict[str, int] = defaultdict(int)
    for row in previous:
        for resource in row.get("resources") or []:
            previous_totals[resource] += int(row.get("prompt_tokens") or 0)
            previous_calls[resource] += 1
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in current:
        for resource in row.get("resources") or []:
            grouped[resource].append(row)
    hotspots: list[HotspotRow] = []
    for resource, calls in grouped.items():
        total = sum(int(row.get("prompt_tokens") or 0) for row in calls)
        if len(calls) < 2 and total < 250_000:
            continue
        session_totals: dict[str, int] = defaultdict(int)
        turn_totals: dict[tuple[str, str], int] = defaultdict(int)
        contributors: dict[tuple[str, str], dict[str, Any]] = {}
        call_values: list[int] = []
        repeat_count = 0
        broad_calls = 0
        targeted_calls = 0
        for row in calls:
            tokens = int(row.get("prompt_tokens") or 0)
            session_id = str(row.get("session_id") or "")
            turn_id = str(row.get("turn_id") or "")
            repeated = resource in (row.get("repeated_resources") or [])
            repeat_count += int(repeated)
            broad_calls += int(row.get("category") == "broad_search_read")
            targeted_calls += int(row.get("category") == "targeted_search_read")
            session_totals[session_id] += tokens
            turn_totals[(session_id, turn_id)] += tokens
            call_values.append(tokens)
            target = contributors.setdefault(
                (session_id, turn_id),
                {
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "title": row.get("title"),
                    "prompt_tokens": 0,
                    "calls": 0,
                    "repeated_calls": 0,
                    "pattern": row.get("category"),
                },
            )
            target["prompt_tokens"] += tokens
            target["calls"] += 1
            target["repeated_calls"] += int(repeated)
        largest = max(call_values, default=0)
        previous_total = previous_totals.get(resource, 0)
        largest_share = _safe_div(largest, total)
        if largest_share >= 0.8:
            status = "outlier_dominated"
        elif previous_calls.get(resource, 0) and len(session_totals) >= 2:
            status = "persistent"
        elif len(session_totals) >= 2:
            status = "phase"
        else:
            status = "emerging"
        hotspots.append(
            HotspotRow(
                key=hashlib.sha256(resource.encode()).hexdigest()[:16],
                resource=resource,
                status=status,
                sessions=len(session_totals),
                turns=len(turn_totals),
                calls=len(calls),
                repeat_count=repeat_count,
                enclosing_prompt_tokens=total,
                largest_call_tokens=largest,
                largest_call_share=largest_share,
                broad_calls=broad_calls,
                targeted_calls=targeted_calls,
                previous_enclosing_prompt_tokens=previous_total,
                delta_pct=_pct_change(total, previous_total),
                session=_distribution(list(session_totals.values())),
                turn=_distribution(list(turn_totals.values())),
                contributors=[
                    Contributor(**row)
                    for row in sorted(
                        contributors.values(),
                        key=lambda value: int(value["prompt_tokens"]),
                        reverse=True,
                    )[:30]
                ],
            )
        )
    return sorted(hotspots, key=lambda row: row.enclosing_prompt_tokens, reverse=True)[
        :60
    ]


def _outlier_rows(
    period: tuple[datetime, datetime, str, str],
    *,
    session_records: list[dict[str, Any]],
    turn_records: list[dict[str, Any]],
    tool_records: list[dict[str, Any]],
) -> list[OutlierRow]:
    turns = [row for row in turn_records if _in_period(row.get("completed_at"), period)]
    session_prompt = {
        str(row["session_id"]): int(row.get("prompt_tokens") or 0)
        for row in session_records
    }
    p90 = _distribution([int(row.get("prompt_tokens") or 0) for row in turns]).p90
    pattern_totals: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    for tool in tool_records:
        key = (str(tool.get("session_id") or ""), str(tool.get("turn_id") or ""))
        tokens = int(tool.get("prompt_tokens") or 0)
        category = str(tool.get("category") or "other")
        pattern_totals[key][category] += tokens
    primary_patterns = {
        key: max(values.items(), key=lambda item: item[1])[0]
        for key, values in pattern_totals.items()
        if values
    }
    rows: list[OutlierRow] = []
    for turn in turns:
        session_id = str(turn["session_id"])
        turn_id = str(turn["turn_id"])
        prompt = int(turn.get("prompt_tokens") or 0)
        share = _safe_div(prompt, session_prompt.get(session_id, 0))
        max_context = _optional_int(turn.get("max_context_tokens")) or 0
        reasons: list[str] = []
        if prompt >= p90 and prompt > 0:
            reasons.append("prompt_p90")
        if share >= 0.2:
            reasons.append("session_share_over_20pct")
        if max_context >= 200_000:
            reasons.append("context_over_200k")
        elif max_context >= 100_000:
            reasons.append("context_over_100k")
        if not reasons:
            continue
        rows.append(
            OutlierRow(
                session_id=session_id,
                turn_id=turn_id,
                title=turn.get("title"),
                completed_at=turn.get("completed_at"),
                prompt_tokens=prompt,
                session_share=share,
                max_context_tokens=max_context or None,
                primary_pattern=primary_patterns.get((session_id, turn_id)),
                reason_codes=reasons,
            )
        )
    return sorted(rows, key=lambda row: row.prompt_tokens, reverse=True)[:100]


def _distribution(values: list[int | float]) -> Distribution:
    clean = sorted(float(value) for value in values if value >= 0)
    if not clean:
        return Distribution()
    return Distribution(
        count=len(clean),
        avg=round(sum(clean) / len(clean), 8),
        median=_percentile(clean, 0.5),
        p90=_percentile(clean, 0.9),
        p95=_percentile(clean, 0.95),
        max=round(clean[-1], 8),
    )


def _pct_change(current: float, previous: float) -> float | None:
    if previous == 0:
        return None
    return round(((float(current) - float(previous)) / float(previous)) * 100, 2)


def _prompt_tokens(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    return int(value.get("prompt_tokens") or value.get("input_tokens") or 0)


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return int(value)
    return None


def _optional_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _attribution() -> dict[str, Any]:
    return {
        "billing_authority": (
            "session.model_usage aggregate usage for sessions and completed-turn "
            "usage for turns"
        ),
        "tool_cost_authority": (
            "session.tool_usage tool_items[].allocated_real_token_cost"
        ),
        "session_unit": "individual vendor session reconstructed from turn session_id",
        "turn_unit": "turn_id within an individual vendor session",
        "period_assignment": (
            "full sessions are assigned by session completion timestamp; "
            "turns are assigned independently by turn completion timestamp"
        ),
        "pattern_classifier": "datahub generic structural classifier v1",
        "hotspot_costs": (
            "completed-session cohort; enclosing and non-additive across resources"
        ),
        "period_timezone": "datahub host local timezone",
        "discovery_scope": (
            "project.sessions since_days selects recently modified graphs; "
            "project periods are then filtered by completed turn timestamp"
        ),
    }
