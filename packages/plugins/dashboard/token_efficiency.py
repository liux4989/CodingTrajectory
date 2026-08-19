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
from pydantic import BaseModel, Field

Grain = Literal["daily", "weekly"]

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
_SEARCH_RE = re.compile(
    r"(?:\brg\b|\bgrep\b|\bripgrep\b|search_text)"
)
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


class Distribution(BaseModel):
    count: int = 0
    avg: float = 0
    median: float = 0
    p90: float = 0
    p95: float = 0
    max: float = 0


class PeriodSummary(BaseModel):
    bucket: str
    label: str
    is_complete: bool = True
    started_at: datetime
    ended_at: datetime
    session_count: int = 0
    turn_count: int = 0
    total_prompt_tokens: int = 0
    session_prompt: Distribution = Field(default_factory=Distribution)
    turn_prompt: Distribution = Field(default_factory=Distribution)
    pattern_prompt_tokens: int = 0
    pattern_share: float = 0


class ComparisonDelta(BaseModel):
    total_prompt_tokens_pct: float | None = None
    session_median_pct: float | None = None
    session_p90_pct: float | None = None
    turn_median_pct: float | None = None
    turn_p90_pct: float | None = None


class PeriodComparison(BaseModel):
    grain: Grain
    current: PeriodSummary
    previous: PeriodSummary | None = None
    deltas: ComparisonDelta = Field(default_factory=ComparisonDelta)


class PatternIndicators(BaseModel):
    repeated_read: int = 0
    parallel_fanout: int = 0
    truncated_output: int = 0


class UnitDistributions(BaseModel):
    session: Distribution = Field(default_factory=Distribution)
    turn: Distribution = Field(default_factory=Distribution)


class Contributor(BaseModel):
    session_id: str
    turn_id: str | None = None
    title: str | None = None
    prompt_tokens: int = 0
    calls: int = 0
    repeated_calls: int = 0
    pattern: str | None = None


class PatternMetrics(BaseModel):
    incidence_count: int = 0
    incidence_rate: float = 0
    calls: int = 0
    total_prompt_tokens: int = 0
    token_share: float = 0
    zero_inclusive: UnitDistributions = Field(default_factory=UnitDistributions)
    conditional: UnitDistributions = Field(default_factory=UnitDistributions)
    indicators: PatternIndicators = Field(default_factory=PatternIndicators)


class PatternDelta(BaseModel):
    prompt_tokens_pct: float | None = None
    incidence_rate_points: float = 0
    calls_pct: float | None = None
    session_median_pct: float | None = None
    session_p90_pct: float | None = None
    turn_median_pct: float | None = None
    turn_p90_pct: float | None = None


class PatternRow(BaseModel):
    key: str
    label: str
    kind: Literal["exclusive", "indicator"]
    current: PatternMetrics
    previous: PatternMetrics | None = None
    deltas: PatternDelta = Field(default_factory=PatternDelta)
    contributors: list[Contributor] = Field(default_factory=list)


class HotspotRow(BaseModel):
    key: str
    resource: str
    status: Literal["persistent", "phase", "outlier_dominated", "emerging"]
    sessions: int = 0
    turns: int = 0
    calls: int = 0
    repeat_count: int = 0
    enclosing_prompt_tokens: int = 0
    largest_call_tokens: int = 0
    largest_call_share: float = 0
    broad_calls: int = 0
    targeted_calls: int = 0
    previous_enclosing_prompt_tokens: int = 0
    delta_pct: float | None = None
    session: Distribution = Field(default_factory=Distribution)
    turn: Distribution = Field(default_factory=Distribution)
    contributors: list[Contributor] = Field(default_factory=list)


class OutlierRow(BaseModel):
    session_id: str
    turn_id: str
    title: str | None = None
    completed_at: datetime | None = None
    prompt_tokens: int = 0
    session_share: float = 0
    max_context_tokens: int | None = None
    primary_pattern: str | None = None
    reason_codes: list[str] = Field(default_factory=list)


class Coverage(BaseModel):
    root_graphs: int = 0
    sessions: int = 0
    turns: int = 0
    tool_items: int = 0
    attributed_tool_items: int = 0
    undated_tool_items: int = 0
    truncated_input_summaries: int = 0


class IndexCoverage(BaseModel):
    root_graphs: int = 0


class ProjectOption(BaseModel):
    name: str
    path: str | None = None
    vendors: list[str] = Field(default_factory=list)


class ProjectIndexRow(BaseModel):
    project_name: str
    display_name: str
    root_graphs: int = 0
    prompt_tokens: int = 0
    graph_prompt: Distribution = Field(default_factory=Distribution)


class IndexProjection(BaseModel):
    schema_version: Literal[1] = 1
    generated_at: datetime
    filters: dict[str, Any]
    attribution: dict[str, Any]
    coverage: IndexCoverage
    warnings: list[str] = Field(default_factory=list)
    project_options: list[ProjectOption] = Field(default_factory=list)
    projects: list[ProjectIndexRow] = Field(default_factory=list)


class ProjectProjection(BaseModel):
    schema_version: Literal[1] = 1
    generated_at: datetime
    filters: dict[str, Any]
    attribution: dict[str, Any]
    coverage: Coverage
    warnings: list[str] = Field(default_factory=list)
    project: dict[str, Any]
    comparisons: dict[str, PeriodComparison | None]
    trends: dict[str, list[PeriodSummary]]
    patterns: dict[str, list[PatternRow]] = Field(default_factory=dict)
    hotspots: dict[str, list[HotspotRow]] = Field(default_factory=dict)
    outliers: dict[str, list[OutlierRow]] = Field(default_factory=dict)


def build_index_projection(
    *,
    client: ServiceApiClient,
    since_days: int = 30,
) -> dict[str, Any]:
    since_days = max(int(since_days), 1)
    projects_payload = client.call("project.list", {})
    sessions_payload = client.call(
        "project.sessions",
        {"since_days": since_days, "include": ["usage"]},
    )
    options = _project_options(projects_payload)
    option_by_key = {item.name.casefold(): item for item in options}
    options_by_compact: dict[str, list[ProjectOption]] = defaultdict(list)
    for item in options:
        options_by_compact[_compact_project_key(item.name)].append(item)
    grouped: dict[str, list[int]] = defaultdict(list)
    project_names: dict[str, str] = {}
    display_names: dict[str, str] = {}
    warnings: list[str] = []
    for item in sessions_payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        raw_name = str(item.get("project") or "unknown")
        raw_key = raw_name.casefold()
        option = option_by_key.get(raw_key)
        if option is None:
            compact_matches = options_by_compact.get(
                _compact_project_key(raw_name), []
            )
            option = compact_matches[0] if len(compact_matches) == 1 else None
        key = option.name.casefold() if option else raw_key
        project_names[key] = option.name if option else raw_name
        display_names[key] = (
            _project_display_name(option) if option else raw_name
        )
        grouped[key].append(_prompt_tokens(item.get("usage")))
        warnings.extend(str(value) for value in item.get("warnings") or [])
    rows = [
        ProjectIndexRow(
            project_name=project_names[key],
            display_name=display_names[key],
            root_graphs=len(values),
            prompt_tokens=sum(values),
            graph_prompt=_distribution(values),
        )
        for key, values in grouped.items()
    ]
    rows.sort(key=lambda row: row.prompt_tokens, reverse=True)
    return IndexProjection(
        generated_at=datetime.now(UTC),
        filters={"since_days": since_days},
        attribution=_attribution(),
        coverage=IndexCoverage(root_graphs=sum(row.root_graphs for row in rows)),
        warnings=list(dict.fromkeys(warnings)),
        project_options=options,
        projects=rows,
    ).model_dump(mode="json")


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
    undated_tools = sum(
        1 for row in tool_records if row.get("completed_at") is None
    )
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
    rows: dict[str, dict[str, dict[str, Any]]] = {
        method: {} for method in methods
    }
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
            root_id
            for _session_id, root_id in chunk
            if root_id not in session_ids
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
            (method, session_id)
            for method in methods
            for session_id in session_ids
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
            path=(str(item.get("path")) if isinstance(item, dict) and item.get("path") else None),
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
        option
        for option in options
        if _compact_project_key(option.name) == wanted
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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    turns: list[dict[str, Any]] = []
    sessions: dict[str, dict[str, Any]] = {}
    turn_by_id: dict[tuple[str, str], dict[str, Any]] = {}
    for root_id, payload in model_rows.items():
        title = _optional_text(payload.get("title"))
        payload_turns = [
            turn
            for turn in payload.get("turns") or []
            if isinstance(turn, dict)
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


def _tool_records(
    tool_rows: dict[str, dict[str, Any]],
    *,
    turn_by_id: dict[tuple[str, str], dict[str, Any]],
    project_path: str | None,
    project_name: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_resources: dict[str, set[str]] = defaultdict(set)
    for root_id, payload in tool_rows.items():
        for index, item in enumerate(payload.get("tool_items") or []):
            if not isinstance(item, dict):
                continue
            session_id = str(item.get("session_id") or root_id)
            turn_id = str(item.get("turn_id") or "")
            turn = turn_by_id.get((session_id, turn_id)) or {}
            summary = str(item.get("input_summary") or "")
            tool_name = str(item.get("tool_name") or "")
            resources = _extract_resources(
                summary,
                project_path=project_path,
                project_name=project_name,
            )
            category, parallel = _classify_tool(
                tool_name, summary, resource_count=len(resources)
            )
            repeated_resources = [
                resource
                for resource in resources
                if resource in seen_resources[root_id]
            ]
            seen_resources[root_id].update(resources)
            real_cost = item.get("allocated_real_token_cost") or {}
            records.append(
                {
                    "root_id": root_id,
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "title": turn.get("title"),
                    "completed_at": turn.get("completed_at"),
                    "item_id": str(item.get("item_id") or f"{root_id}:{index}"),
                    "tool_name": tool_name,
                    "input_summary": summary,
                    "category": category,
                    "parallel_fanout": parallel,
                    "truncated_output": bool(item.get("output_truncated")),
                    "resources": resources,
                    "repeated_resources": repeated_resources,
                    "repeated_read": bool(repeated_resources),
                    "prompt_tokens": _prompt_tokens(real_cost),
                }
            )
    return records


def _search_command_details(
    summary: str,
) -> tuple[Literal["broad", "targeted"] | None, set[str]]:
    scopes: list[Literal["broad", "targeted"]] = []
    patterns: set[str] = set()
    normalized = summary.replace('\\"', '"')
    for match in _SEARCH_COMMAND_RE.finditer(normalized):
        fragment = normalized[match.start("command") :]
        fragment = re.split(r"(?:&&|\|\||;|\n)", fragment, maxsplit=1)[0]
        fragment = re.split(
            r'(?<!\\)"\s*,\s*"\w+"\s*:',
            fragment,
            maxsplit=1,
        )[0]
        try:
            tokens = shlex.split(fragment)
        except ValueError:
            tokens = fragment.split()
        if not tokens:
            continue
        command = Path(tokens[0]).name
        if command not in {"rg", "grep", "ripgrep"}:
            continue
        paths: list[str] = []
        pattern_seen = False
        recursive = False
        index = 1
        while index < len(tokens):
            token = tokens[index].strip()
            if token in {"|", "&&", "||", ";"}:
                break
            if token in {"-R", "-r", "--recursive", "--files"}:
                recursive = True
                index += 1
                continue
            if token in _SEARCH_OPTIONS_WITH_VALUES:
                if index + 1 < len(tokens):
                    value = tokens[index + 1].strip()
                    if token in {"-e", "--regexp"}:
                        patterns.add(value)
                        pattern_seen = True
                    index += 2
                else:
                    index += 1
                continue
            if token.startswith(("--regexp=", "--file=", "--glob=", "--iglob=")):
                if token.startswith("--regexp="):
                    patterns.add(token.split("=", 1)[1])
                    pattern_seen = True
                index += 1
                continue
            if token.startswith("-"):
                index += 1
                continue
            cleaned = token.strip(" \t\r\n\"'`,)}]")
            if not cleaned:
                index += 1
                continue
            if not pattern_seen:
                patterns.add(cleaned)
                pattern_seen = True
            else:
                paths.append(cleaned)
            index += 1
        explicit_file_paths = [
            value
            for value in paths
            if _looks_like_file_operand(value)
        ]
        broad_scope = (
            recursive
            or not paths
            or len(paths) != 1
            or len(explicit_file_paths) != 1
        )
        scopes.append("broad" if broad_scope else "targeted")
    if not scopes:
        return None, patterns
    if len(scopes) > 1 or "broad" in scopes:
        return "broad", patterns
    return "targeted", patterns


def _looks_like_file_operand(value: str) -> bool:
    normalized = value.removeprefix("./")
    if value in {".", "..", "/"} or value.endswith("/"):
        return False
    suffix = Path(normalized).suffix.removeprefix(".").casefold()
    return bool(suffix and ("/" in normalized or suffix in _BARE_FILE_SUFFIXES))


def _classify_tool(
    tool_name: str, summary: str, *, resource_count: int
) -> tuple[str, bool]:
    normalized_tool = tool_name.casefold()
    tool_key = re.split(r"[.:/]", normalized_tool)[-1]
    text = f"{tool_name} {summary}".casefold()
    parallel = (
        "promise.all" in text
        or "allsettled" in text
        or "parallel" in text
        or text.count("tools.exec_command") > 1
        or text.count('"cmd"') > 1
        or text.count("cmd:") > 1
    )
    if (
        normalized_tool.startswith(("web.", "browser.", "chrome."))
        or tool_key in {"web_search", "web_fetch"}
    ):
        return "web", parallel
    if tool_key in _EDIT_TOOL_NAMES:
        return "editing", parallel
    if tool_key in _COORDINATION_TOOL_NAMES:
        return "coordination", parallel
    if _WEB_RE.search(text):
        return "web", parallel
    if _EDIT_RE.search(text):
        return "editing", parallel
    if _COORDINATION_RE.search(text):
        return "coordination", parallel
    search_scope, _patterns = _search_command_details(summary)
    search = bool(
        search_scope
        or
        _SEARCH_RE.search(text)
        or "rg --files" in text
        or re.search(r"\bfind\b.+(?:-type|-name|-exec)", text)
        or re.search(r"\bls\b", text)
    )
    read = bool(_READ_RE.search(text))
    search_read = search or read
    if search_read:
        search_read_signals = len(_SEARCH_RE.findall(text)) + len(
            _READ_RE.findall(text)
        )
        broad = (
            parallel
            or search_scope == "broad"
            or (
                search_scope is None
                and (
                    resource_count > 1
                    or (search and resource_count == 0)
                )
            )
            or (search_scope is None and search_read_signals > 1)
            or "rg --files" in text
            or bool(re.search(r"\bfind\b", text))
            or "recursive" in text
            or "multi-file" in text
        )
        return (
            "broad_search_read" if broad else "targeted_search_read",
            parallel,
        )
    if _VALIDATION_RE.search(text):
        return "validation", parallel
    return "other", parallel


def _extract_resources(
    summary: str,
    *,
    project_path: str | None,
    project_name: str,
) -> list[str]:
    resources: list[str] = []
    project_root = Path(project_path).expanduser() if project_path else None
    project_leaf = project_root.name if project_root else project_name
    _search_scope, search_patterns = _search_command_details(summary)
    for match in _FILE_RE.finditer(summary):
        raw = match.group(1).rstrip(".,;:)]}'\"")
        if raw in search_patterns:
            continue
        if (
            "/" not in raw
            and raw.rsplit(".", 1)[-1].casefold() not in _BARE_FILE_SUFFIXES
        ):
            continue
        if raw.startswith(
            (
                ".cargo/",
                "CARGO_HOME/",
                "HOME/",
                "RUSTUP_HOME/",
            )
        ):
            continue
        if any(
            part in raw
            for part in (
                "/.cache/",
                "/.cargo/",
                "/node_modules/",
                "/.git/",
                "/.codex/memories/",
                "/.rustup/",
                "/site-packages/",
                "/tmp/",
            )
        ):
            continue
        normalized: str | None = None
        candidate = Path(raw)
        if candidate.is_absolute():
            if project_root is not None:
                try:
                    normalized = candidate.relative_to(project_root).as_posix()
                except ValueError:
                    normalized = None
            if normalized is None:
                parts = candidate.parts
                for index, part in enumerate(parts):
                    if part.casefold() == project_leaf.casefold():
                        normalized = Path(*parts[index + 1 :]).as_posix()
                        break
        else:
            normalized = raw.removeprefix("./")
        if (
            normalized
            and not normalized.startswith("../")
            and normalized not in resources
        ):
            resources.append(normalized)
    return resources


def _latest_periods(
    grain: Grain, now_local: datetime
) -> tuple[tuple[datetime, datetime, str, str], tuple[datetime, datetime, str, str]]:
    today = datetime.combine(now_local.date(), time.min, tzinfo=now_local.tzinfo)
    if grain == "daily":
        current_start = today - timedelta(days=1)
        previous_start = current_start - timedelta(days=1)
        return (
            _period_tuple(current_start, current_start + timedelta(days=1), grain),
            _period_tuple(previous_start, current_start, grain),
        )
    this_week = today - timedelta(days=today.weekday())
    current_start = this_week - timedelta(days=7)
    previous_start = current_start - timedelta(days=7)
    return (
        _period_tuple(current_start, this_week, grain),
        _period_tuple(previous_start, current_start, grain),
    )


def _required_discovery_days(since_days: int, now_local: datetime) -> int:
    """Cover the requested trend and both latest completed weekly buckets."""
    _current, previous = _latest_periods("weekly", now_local)
    weekly_lookback = math.ceil(
        (now_local - previous[0]).total_seconds() / 86_400
    )
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
    turns = [
        row for row in turn_records if _in_period(row.get("completed_at"), period)
    ]
    turn_keys = {
        (str(row["session_id"]), str(row["turn_id"])) for row in turns
    }
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
            turn_p90_pct=_pct_change(
                current.turn_prompt.p90, previous.turn_prompt.p90
            ),
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
    return sorted(
        rows, key=lambda row: row.current.total_prompt_tokens, reverse=True
    )


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
    turns = [
        row for row in turn_records if _in_period(row.get("completed_at"), period)
    ]
    active_session_ids = {str(row["session_id"]) for row in turns}
    turn_keys = {
        (str(row["session_id"]), str(row["turn_id"])) for row in turns
    }
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
        by_turn.get((str(row["session_id"]), str(row["turn_id"])), 0)
        for row in turns
    ]
    total_prompt = sum(int(row.get("prompt_tokens") or 0) for row in turns)
    activity_pattern_sessions = {
        str(row.get("session_id") or "") for row in turn_tools
    }
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
    return sorted(
        hotspots, key=lambda row: row.enclosing_prompt_tokens, reverse=True
    )[:60]


def _outlier_rows(
    period: tuple[datetime, datetime, str, str],
    *,
    session_records: list[dict[str, Any]],
    turn_records: list[dict[str, Any]],
    tool_records: list[dict[str, Any]],
) -> list[OutlierRow]:
    turns = [
        row for row in turn_records if _in_period(row.get("completed_at"), period)
    ]
    session_prompt = {
        str(row["session_id"]): int(row.get("prompt_tokens") or 0)
        for row in session_records
    }
    p90 = _distribution(
        [int(row.get("prompt_tokens") or 0) for row in turns]
    ).p90
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


def _percentile(values: list[float], percentile: float) -> float:
    if len(values) == 1:
        return round(values[0], 8)
    position = (len(values) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return round(values[lower] * (1 - weight) + values[upper] * weight, 8)


def _pct_change(current: int | float, previous: int | float) -> float | None:
    if previous == 0:
        return None
    return round(((float(current) - float(previous)) / float(previous)) * 100, 2)


def _safe_div(numerator: int | float, denominator: int | float) -> float:
    if denominator <= 0:
        return 0
    return round(float(numerator) / float(denominator), 8)


def _prompt_tokens(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    return int(value.get("prompt_tokens") or value.get("input_tokens") or 0)


def _parse_datetime(value: Any, *, tz: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(tz)


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
        "pattern_classifier": "dashboard generic structural classifier v1",
        "hotspot_costs": (
            "completed-session cohort; enclosing and non-additive across resources"
        ),
        "period_timezone": "dashboard host local timezone",
        "discovery_scope": (
            "project.sessions since_days selects recently modified graphs; "
            "project periods are then filtered by completed turn timestamp"
        ),
    }
