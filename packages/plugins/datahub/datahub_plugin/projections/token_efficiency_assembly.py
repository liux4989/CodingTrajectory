# ruff: noqa: F401, I001
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
from datahub_plugin.projections.token_efficiency_period_analysis import (
    _attribution,
    _comparison,
    _hotspot_rows,
    _optional_int,
    _optional_text,
    _outlier_rows,
    _pattern_rows,
    _period_summary,
    _prompt_tokens,
    _required_discovery_days,
    _trend_periods,
)
from datahub_plugin.projections.token_efficiency_tool_analysis import (
    _latest_periods,
    _tool_records,
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


def build_project_projection(
    *,
    client: ServiceApiClient,
    project_name: str,
    since_days: int = 30,
) -> dict[str, Any]:
    since_days = max(int(since_days), 1)
    now_local = datetime.now().astimezone()
    discovery_days = _required_discovery_days(since_days, now_local)
    projects_payload = client.call("project.list", {})
    option = _resolve_project_option(projects_payload, project_name)
    sessions_payload = client.call(
        "project.sessions",
        {"project_name": option.name, "since_days": discovery_days},
    )
    session_items = [
        item for item in sessions_payload.get("items") or [] if isinstance(item, dict)
    ]
    root_ids = list(
        dict.fromkeys(
            str(item.get("root_session_id") or item.get("id") or "")
            for item in session_items
            if item.get("root_session_id") or item.get("id")
        )
    )
    telemetry_targets: list[tuple[str, str]] = []
    seen_session_ids: set[str] = set()
    for item in session_items:
        root_id = str(item.get("root_session_id") or item.get("id") or "")
        for raw_session_id in item.get("session_ids") or [root_id]:
            session_id = str(raw_session_id or "")
            if not root_id or not session_id or session_id in seen_session_ids:
                continue
            seen_session_ids.add(session_id)
            telemetry_targets.append((session_id, root_id))
    telemetry_rows, telemetry_warnings = _batch_methods(
        client,
        telemetry_targets,
        ("session.model_usage", "session.tool_usage"),
    )
    model_rows = telemetry_rows["session.model_usage"]
    tool_rows = telemetry_rows["session.tool_usage"]
    warnings = list(telemetry_warnings)
    for item in session_items:
        warnings.extend(str(value) for value in item.get("warnings") or [])

    tz = now_local.tzinfo
    if tz is None:
        tz = UTC
    turn_records, session_records, turn_by_id = _usage_units(model_rows, tz=tz)
    tool_records = _tool_records(
        tool_rows,
        turn_by_id=turn_by_id,
        project_path=option.path,
        project_name=option.name,
    )
    periods = {
        "daily": _latest_periods("daily", now_local),
        "weekly": _latest_periods("weekly", now_local),
    }
    comparisons = {
        key: _comparison(
            key,
            values[0],
            values[1],
            session_records=session_records,
            turn_records=turn_records,
            tool_records=tool_records,
        )
        for key, values in periods.items()
    }
    trends = {
        key: [
            _period_summary(
                period,
                session_records=session_records,
                turn_records=turn_records,
                tool_records=tool_records,
            )
            for period in _trend_periods(key, since_days, now_local)
        ]
        for key in ("daily", "weekly")
    }
    patterns = {
        key: _pattern_rows(
            value[0],
            value[1],
            session_records=session_records,
            turn_records=turn_records,
            tool_records=tool_records,
        )
        for key, value in periods.items()
    }
    hotspots = {
        key: _hotspot_rows(
            value[0],
            value[1],
            session_records=session_records,
            turn_records=turn_records,
            tool_records=tool_records,
        )
        for key, value in periods.items()
    }
    outliers = {
        key: _outlier_rows(
            value[0],
            session_records=session_records,
            turn_records=turn_records,
            tool_records=tool_records,
        )
        for key, value in periods.items()
    }
    attributed = sum(
        1 for row in tool_records if int(row.get("prompt_tokens") or 0) > 0
    )
    truncated_summaries = sum(
        1 for row in tool_records if str(row.get("input_summary") or "").endswith("…")
    )
    undated_tools = sum(1 for row in tool_records if row.get("completed_at") is None)
    if truncated_summaries:
        warnings.append(
            f"{truncated_summaries} tool input summaries were truncated; "
            "resource hotspot coverage may be partial"
        )
    if undated_tools:
        warnings.append(
            f"{undated_tools} tool items could not be mapped to a dated turn "
            "and were excluded from turn-level period metrics"
        )
    projection = ProjectProjection(
        generated_at=datetime.now(UTC),
        filters={
            "since_days": since_days,
            "discovery_days": discovery_days,
            "project_name": option.name,
        },
        attribution=_attribution(),
        coverage=Coverage(
            root_graphs=len(root_ids),
            sessions=len(session_records),
            turns=len(turn_records),
            tool_items=len(tool_records),
            attributed_tool_items=attributed,
            undated_tool_items=undated_tools,
            truncated_input_summaries=truncated_summaries,
        ),
        warnings=list(dict.fromkeys(warnings)),
        project={
            "name": option.name,
            "display_name": _project_display_name(option),
            "path": option.path,
        },
        comparisons={
            "daily": comparisons["daily"],
            "weekly": comparisons["weekly"],
        },
        trends=trends,
        patterns=patterns,
        hotspots=hotspots,
        outliers=outliers,
    )
    return projection.model_dump(mode="json")


def _batch_methods(
    client: ServiceApiClient,
    telemetry_targets: list[tuple[str, str]],
    methods: tuple[str, ...],
) -> tuple[dict[str, dict[str, dict[str, Any]]], list[str]]:
    rows: dict[str, dict[str, dict[str, Any]]] = {method: {} for method in methods}
    warnings: list[str] = []

    def load_chunk(chunk: list[tuple[str, str]]) -> None:
        session_ids = [session_id for session_id, _root_id in chunk]
        requests = [
            {
                "id": f"{method}:{session_id}",
                "method": method,
                "params": {"session_id": session_id},
            }
            for method in methods
            for session_id in session_ids
        ]
        anchors = {
            root_id for _session_id, root_id in chunk if root_id not in session_ids
        }
        requests.extend(
            {
                "id": f"anchor:{root_id}",
                "method": "session.usage",
                "params": {"session_id": root_id},
            }
            for root_id in sorted(anchors)
        )
        seen: set[tuple[str, str]] = set()
        for request in requests:
            item = client.execute(request)
            if not isinstance(item, dict):
                continue
            request_id = str(item.get("id") or "")
            method, separator, root_id = request_id.partition(":")
            if method == "anchor":
                if not item.get("ok"):
                    error = item.get("error") or {}
                    warnings.append(
                        f"graph anchor failed for {root_id}: "
                        f"{error.get('message') or 'unknown error'}"
                    )
                continue
            key = (method, root_id)
            if not separator or method not in rows or root_id not in session_ids:
                warnings.append(f"telemetry returned unknown response id: {request_id}")
                continue
            seen.add(key)
            if not item.get("ok"):
                error = item.get("error") or {}
                warnings.append(
                    f"{method} failed for {root_id}: "
                    f"{error.get('message') or 'unknown error'}"
                )
                continue
            result = item.get("result")
            if isinstance(result, dict):
                rows[method][root_id] = result
                for warning in result.get("warnings") or []:
                    if isinstance(warning, dict):
                        detail = str(
                            warning.get("message")
                            or warning.get("code")
                            or json.dumps(warning, sort_keys=True)
                        )
                    else:
                        detail = str(warning)
                    warnings.append(f"{method} warning for {root_id}: {detail}")
        expected = {
            (method, session_id) for method in methods for session_id in session_ids
        }
        for method, root_id in expected - seen:
            warnings.append(f"{method} omitted response for {root_id}")

    for start in range(0, len(telemetry_targets), _BATCH_SIZE):
        load_chunk(telemetry_targets[start : start + _BATCH_SIZE])
    return rows, warnings


def _project_options(payload: dict[str, Any]) -> list[ProjectOption]:
    items = payload.get("items") or {}
    if not isinstance(items, dict):
        return []
    return [
        ProjectOption(
            name=str(name),
            path=(
                str(item.get("path"))
                if isinstance(item, dict) and item.get("path")
                else None
            ),
            vendors=(
                [str(value) for value in item.get("vendors") or []]
                if isinstance(item, dict)
                else []
            ),
        )
        for name, item in sorted(items.items())
    ]


def _resolve_project_option(
    payload: dict[str, Any], project_name: str
) -> ProjectOption:
    options = _project_options(payload)
    for option in options:
        if option.name.casefold() == project_name.casefold():
            return option
    wanted = _compact_project_key(project_name)
    matches = [
        option for option in options if _compact_project_key(option.name) == wanted
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"project name is ambiguous: {project_name}")
    return ProjectOption(name=project_name)


def _compact_project_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _project_display_name(option: ProjectOption) -> str:
    if option.path:
        leaf = Path(option.path).expanduser().name
        if leaf:
            return leaf
    return option.name


def _usage_units(
    model_rows: dict[str, dict[str, Any]],
    *,
    tz: Any,
) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]]
]:
    turns: list[dict[str, Any]] = []
    sessions: dict[str, dict[str, Any]] = {}
    turn_by_id: dict[tuple[str, str], dict[str, Any]] = {}
    for root_id, payload in model_rows.items():
        title = _optional_text(payload.get("title"))
        payload_turns = [
            turn for turn in payload.get("turns") or [] if isinstance(turn, dict)
        ]
        session_id = str(
            next(
                (
                    turn.get("session_id")
                    for turn in payload_turns
                    if turn.get("session_id")
                ),
                root_id,
            )
        )
        completed = _parse_datetime(payload.get("completed_at"), tz=tz)
        if completed is None:
            completed_values = [
                value
                for turn in payload_turns
                if (
                    value := _parse_datetime(
                        turn.get("completed_at") or turn.get("started_at"),
                        tz=tz,
                    )
                )
                is not None
            ]
            completed = max(completed_values, default=None)
        session_prompt = _prompt_tokens(payload.get("usage"))
        if session_prompt == 0:
            session_prompt = sum(
                _prompt_tokens(turn.get("usage")) for turn in payload_turns
            )
        sessions[session_id] = {
            "root_id": root_id,
            "session_id": session_id,
            "title": title,
            "completed_at": completed,
            "prompt_tokens": session_prompt,
        }
        for turn in payload_turns:
            session_id = str(turn.get("session_id") or root_id)
            turn_id = str(turn.get("turn_id") or "")
            if not turn_id:
                continue
            completed = _parse_datetime(
                turn.get("completed_at") or turn.get("started_at"), tz=tz
            )
            record = {
                "root_id": root_id,
                "session_id": session_id,
                "turn_id": turn_id,
                "sequence": int(turn.get("sequence") or 0),
                "title": title,
                "completed_at": completed,
                "prompt_tokens": _prompt_tokens(turn.get("usage")),
                "max_context_tokens": _optional_int(
                    (turn.get("context") or {}).get("max_used_tokens")
                ),
            }
            turns.append(record)
            turn_by_id[(session_id, turn_id)] = record
    return turns, list(sessions.values()), turn_by_id
