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


def _period_tuple(
    start: datetime, end: datetime, grain: Grain
) -> tuple[datetime, datetime, str, str]:
    return start, end, start.date().isoformat(), grain


def _prompt_tokens(unit: dict[str, Any]) -> int:
    return int(unit.get("input_tokens") or 0) + int(unit.get("cache_read_tokens") or 0)


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
            value for value in paths if _looks_like_file_operand(value)
        ]
        broad_scope = (
            recursive or not paths or len(paths) != 1 or len(explicit_file_paths) != 1
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
    if normalized_tool.startswith(("web.", "browser.", "chrome.")) or tool_key in {
        "web_search",
        "web_fetch",
    }:
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
        or _SEARCH_RE.search(text)
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
                and (resource_count > 1 or (search and resource_count == 0))
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
