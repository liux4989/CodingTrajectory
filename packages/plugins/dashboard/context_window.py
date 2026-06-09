from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CategoryKey = Literal[
    "system",
    "project_instructions",
    "memory",
    "skills",
    "mcp",
    "rules",
    "you",
    "files",
    "output",
    "agent",
    "assistant",
    "hooks",
    "unattributed",
]
Confidence = Literal["exact_usage", "exact_text", "estimated_tokens", "structural", "unknown"]


class TokenEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: int = Field(ge=0)
    confidence: Confidence
    source: str


class ContextCategory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: CategoryKey
    source_key: str
    label: str
    tokens: TokenEvidence
    percent: float | None = None


class ContextEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    group: Literal["before_first_prompt", "turn", "post_turn"]
    turn_id: str | None = None
    category: CategoryKey
    label: str
    summary: str | None = None
    tokens: TokenEvidence | None = None
    source: str
    confidence: Confidence
    detail_ref: dict[str, str] = Field(default_factory=dict)
    terminal_visible: bool = True


class ContextWindowProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    session_id: str
    vendor: str
    model: str | None = None
    context_window_tokens: TokenEvidence | None = None
    used_tokens: TokenEvidence | None = None
    used_percent: float | None = None
    categories: list[ContextCategory]
    events: list[ContextEvent]
    warnings: list[str]


def main(
    argv: list[str] | None = None,
    *,
    prog: str = "ct plugin dashboard session context-window",
) -> int:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Inspect context composition and trajectory events for one session.",
    )
    parser.add_argument("session_id")
    parser.add_argument("--turn", dest="turn_id", default=None, help="Limit the event timeline to one turn.")
    parser.add_argument(
        "--output",
        choices=("markdown", "json"),
        default="markdown",
        help="Output format. Defaults to markdown.",
    )
    args = parser.parse_args(argv)

    projection = build_projection(args.session_id, turn_id=args.turn_id)
    if args.output == "json":
        print(projection.model_dump_json(indent=2))
    else:
        print(render_markdown(projection))
    return 0


def build_projection(session_id: str, *, turn_id: str | None = None) -> ContextWindowProjection:
    stats = _ct_json(["session", "stats", "--global-scope", "--output", "json", session_id])
    overview = _ct_json(["session", "overview", "--global-scope", "--output", "json", session_id])
    usage = _ct_json(["session", "usage", "--global-scope", "--output", "json", session_id])

    vendor = str(stats.get("vendor") or _overview_vendor(overview) or "unknown")
    categories = _project_categories(stats, vendor)
    events = [
        *_category_events(categories),
        *_trajectory_events(
            overview,
            usage,
            turn_id=turn_id,
            category_tokens=_category_tokens_by_source(categories),
        ),
    ]
    warnings = [str(item) for item in stats.get("warnings") or []]
    warnings.extend(_projection_warnings(vendor, events))
    if turn_id and not any(event.turn_id == turn_id for event in events):
        raise SystemExit(f"turn not found in session overview: {turn_id}")

    model = stats.get("model") or {}
    context = stats.get("context") or {}
    return ContextWindowProjection(
        session_id=str(stats.get("id") or session_id),
        vendor=vendor,
        model=_optional_text(model.get("name")),
        context_window_tokens=_token_evidence(
            model.get("context_window"),
            confidence="structural",
            source="ct session stats:model.context_window",
        ),
        used_tokens=_token_evidence(
            context.get("used"),
            confidence="exact_usage",
            source="ct session stats:context.used",
        ),
        used_percent=_optional_float(context.get("pct")),
        categories=categories,
        events=events,
        warnings=_dedupe(warnings),
    )


def render_markdown(projection: ContextWindowProjection) -> str:
    context_label = _format_tokens(
        projection.context_window_tokens.value if projection.context_window_tokens else None
    )
    used_label = _format_tokens(projection.used_tokens.value if projection.used_tokens else None)
    percent_label = (
        f" ({projection.used_percent:.1f}%)" if projection.used_percent is not None else ""
    )
    lines = [
        "# Context Window",
        "",
        f"Provider: {projection.vendor}",
        f"Model: {projection.model or '-'} ({context_label} context)",
        f"Used: {used_label}{percent_label}, {len(projection.events)} events",
        "",
        "Composition",
    ]
    for category in sorted(
        projection.categories,
        key=lambda item: item.tokens.value,
        reverse=True,
    ):
        lines.append(
            f"  {category.category:<20} {_format_delta(category.tokens.value):>8}  "
            f"{_one_line(category.label, 62)} [{category.tokens.confidence}]"
        )

    current_group: tuple[str, str | None] | None = None
    for event in projection.events:
        group = (event.group, event.turn_id)
        if group != current_group:
            lines.extend(["", _group_label(event)])
            current_group = group
        delta = _format_delta(event.tokens.value) if event.tokens else "       -"
        summary = _one_line(event.summary or event.label, 74)
        lines.append(f"  {event.category:<20} {delta:>8}  {summary}")

    if projection.warnings:
        lines.extend(["", "Warnings"])
        lines.extend(f"  - {_one_line(warning, 110)}" for warning in projection.warnings)
    return "\n".join(lines)


def _project_categories(stats: dict[str, Any], vendor: str) -> list[ContextCategory]:
    context = stats.get("context") or {}
    leaves = list(_category_leaves(context.get("categories") or []))
    projected: list[ContextCategory] = []
    for index, category in enumerate(leaves):
        source_key = str(category.get("key") or f"category_{index}")
        key = _category_key(source_key, vendor)
        tokens = category.get("tokens")
        if not isinstance(tokens, int) or isinstance(tokens, bool):
            continue
        confidence: Confidence = "estimated_tokens" if vendor == "codex_cli" else "exact_usage"
        projected.append(
            ContextCategory(
                id=f"category:{source_key}:{index}",
                category=key,
                source_key=source_key,
                label=str(category.get("label") or source_key),
                tokens=TokenEvidence(
                    value=tokens,
                    confidence=confidence,
                    source=f"ct session stats:context.categories.{source_key}",
                ),
                percent=_optional_float(category.get("pct")),
            )
        )
    return projected


def _category_leaves(categories: Iterable[Any]) -> Iterable[dict[str, Any]]:
    for category in categories:
        if not isinstance(category, dict):
            continue
        children = category.get("children") or []
        if children:
            yield from _category_leaves(children)
        else:
            yield category


def _category_key(source_key: str, vendor: str) -> CategoryKey:
    mapping: dict[str, CategoryKey] = {
        "base_system": "system",
        "developer_instructions": "system",
        "agents_md": "project_instructions",
        "skills": "skills",
        "mcp": "mcp",
        "memory": "memory",
        "prompt_user_initial_request": "you",
        "prompt_user_follow_up_requests": "you",
        "agent_final_answer": "agent",
        "agent_progress_update": "agent",
        "agent_assistant_message": "agent",
        "context_readfile": "files",
        "context_searchtext": "files",
        "context_listfiles": "files",
    }
    if source_key in mapping:
        return mapping[source_key]
    if source_key.startswith(
        (
            "tool_editfile",
            "tool_writefile",
            "tool_todolist",
            "tool_subagenttask",
            "tool_sessionhandoff",
            "tool_runcommand_code_fix",
        )
    ):
        return "agent"
    if source_key.startswith(("context_", "tool_", "verification", "repository_", "command_")):
        return "output"
    if vendor in {"claude_code", "pi"} and source_key in {
        "cached_context",
        "new_cached_prefix",
        "messages",
    }:
        return "unattributed"
    return "unattributed"


def _category_tokens_by_source(categories: list[ContextCategory]) -> dict[str, int]:
    return {category.source_key: category.tokens.value for category in categories}


def _category_events(categories: list[ContextCategory]) -> list[ContextEvent]:
    starting_context_keys = {
        "base_system",
        "developer_instructions",
        "agents_md",
        "skills",
        "mcp",
        "memory",
    }
    return [
        ContextEvent(
            id=f"event:{category.id}",
            group="before_first_prompt",
            category=category.category,
            label=category.label,
            summary=f"Aggregate context category from {category.source_key}",
            tokens=category.tokens,
            source=category.tokens.source,
            confidence=category.tokens.confidence,
            detail_ref={"stats_category": category.source_key},
            terminal_visible=True,
        )
        for category in categories
        if category.source_key in starting_context_keys
    ]


def _allocate_tool_event_tokens(
    events: list[ContextEvent],
    source_weights_by_event_id: dict[str, Counter[str]],
    category_tokens: dict[str, int],
) -> None:
    event_indexes = {event.id: index for index, event in enumerate(events)}
    events_by_source: defaultdict[str, dict[str, int]] = defaultdict(dict)
    for event_id, source_weights in source_weights_by_event_id.items():
        if event_id not in event_indexes:
            continue
        for source_key, weight in source_weights.items():
            if weight > 0:
                events_by_source[source_key][event_id] = weight

    allocated_by_event: defaultdict[str, int] = defaultdict(int)
    sources_by_event: defaultdict[str, list[str]] = defaultdict(list)
    for source_key, event_weights in events_by_source.items():
        budget = category_tokens.get(source_key, 0)
        if budget <= 0:
            continue
        source_allocations = _weighted_integer_allocation(budget, event_weights)
        for event_id, tokens in source_allocations.items():
            if tokens <= 0:
                continue
            allocated_by_event[event_id] += tokens
            sources_by_event[event_id].append(source_key)

    for event_id, tokens in allocated_by_event.items():
        event = events[event_indexes[event_id]]
        if event.tokens is not None or tokens <= 0:
            continue
        sources = ", ".join(sorted(sources_by_event[event_id]))
        event.tokens = TokenEvidence(
            value=tokens,
            confidence="estimated_tokens",
            source=f"allocated from ct session stats categories: {sources}",
        )
        event.confidence = "estimated_tokens"


def _weighted_integer_allocation(total: int, weights: dict[str, int]) -> dict[str, int]:
    positive = {key: weight for key, weight in weights.items() if weight > 0}
    weight_total = sum(positive.values())
    if total <= 0 or weight_total <= 0:
        return {key: 0 for key in weights}

    allocations = {
        key: int((weight * total) // weight_total)
        for key, weight in positive.items()
    }
    remainder = total - sum(allocations.values())
    fractional_order = sorted(
        positive,
        key=lambda key: ((positive[key] * total) / weight_total) % 1,
        reverse=True,
    )
    for key in fractional_order[:remainder]:
        allocations[key] += 1
    return allocations


def _trajectory_events(
    overview: dict[str, Any],
    usage: dict[str, Any],
    *,
    turn_id: str | None,
    category_tokens: dict[str, int],
) -> list[ContextEvent]:
    usage_by_turn = {
        str(item.get("id")): item.get("usage") or {}
        for item in usage.get("turns") or []
        if isinstance(item, dict) and item.get("id")
    }
    events: list[ContextEvent] = []
    tool_source_weights: dict[str, Counter[str]] = {}
    for session in overview.get("sessions") or []:
        if not isinstance(session, dict):
            continue
        session_id = str(session.get("id") or overview.get("id") or "")
        for turn in session.get("turns") or []:
            if not isinstance(turn, dict):
                continue
            current_turn_id = str(turn.get("id") or "")
            request = turn.get("request") or {}
            request_text = _optional_text(request.get("text"))
            if request_text:
                events.append(
                    ContextEvent(
                        id=f"turn:{current_turn_id}:user",
                        group="turn",
                        turn_id=current_turn_id,
                        category="you",
                        label="User prompt",
                        summary=request_text,
                        tokens=TokenEvidence(
                            value=_estimate_tokens(request_text),
                            confidence="estimated_tokens",
                            source="ct session overview:request.text length estimate",
                        ),
                        source="ct session overview:request.text",
                        confidence="exact_text",
                        detail_ref={
                            "session_id": session_id,
                            "turn_id": current_turn_id,
                        },
                    )
                )
            for index, activity in enumerate(turn.get("activity") or []):
                if not isinstance(activity, dict):
                    continue
                event, source_weights = _activity_event(
                    activity,
                    session_id=session_id,
                    turn_id=current_turn_id,
                    index=index,
                    turn_usage=usage_by_turn.get(current_turn_id),
                )
                events.append(event)
                if source_weights:
                    tool_source_weights[event.id] = source_weights
    _allocate_tool_event_tokens(events, tool_source_weights, category_tokens)
    if turn_id:
        events = [
            event
            for event in events
            if event.group == "before_first_prompt" or event.turn_id == turn_id
        ]
    return events


def _activity_event(
    activity: dict[str, Any],
    *,
    session_id: str,
    turn_id: str,
    index: int,
    turn_usage: dict[str, Any] | None,
) -> tuple[ContextEvent, Counter[str]]:
    if activity.get("text"):
        text = str(activity["text"])
        return (
            ContextEvent(
                id=f"turn:{turn_id}:activity:{index}",
                group="turn",
                turn_id=turn_id,
                category="agent",
                label="Assistant message",
                summary=text,
                tokens=TokenEvidence(
                    value=_estimate_tokens(text),
                    confidence="estimated_tokens",
                    source="ct session overview:activity.text length estimate",
                ),
                source="ct session overview:activity.text",
                confidence="exact_text",
                detail_ref={"session_id": session_id, "turn_id": turn_id},
            ),
            Counter(),
        )

    tool = str(activity.get("tool") or "Tool activity")
    summary = _activity_summary(activity)
    detail_ref = {"session_id": session_id, "turn_id": turn_id}
    if turn_usage:
        detail_ref["turn_usage_total"] = str(turn_usage.get("total") or 0)
    source_weights = _activity_source_weights(activity)
    if source_weights:
        detail_ref["stats_categories"] = ", ".join(
            f"{key}:{weight}" for key, weight in sorted(source_weights.items())
        )
    return (
        ContextEvent(
            id=f"turn:{turn_id}:activity:{index}",
            group="turn",
            turn_id=turn_id,
            category=_tool_category(tool),
            label=tool,
            summary=summary,
            tokens=None,
            source="ct session overview:activity summary",
            confidence="structural",
            detail_ref=detail_ref,
        ),
        source_weights,
    )


def _tool_category(tool: str) -> CategoryKey:
    normalized = tool.lower()
    if any(term in normalized for term in ("read", "search", "list", "find", "glob")):
        return "files"
    if any(term in normalized for term in ("edit", "write", "todo", "subagent", "handoff")):
        return "agent"
    if "hook" in normalized:
        return "hooks"
    return "output"


def _activity_source_weights(activity: dict[str, Any]) -> Counter[str]:
    tool = str(activity.get("tool") or "")
    normalized = tool.lower()
    count = activity.get("count")
    fallback_weight = count if isinstance(count, int) and count > 0 else 1

    if normalized == "readfile":
        return Counter({"context_readfile": fallback_weight})
    if normalized == "searchtext":
        return Counter({"context_searchtext": fallback_weight})
    if normalized == "listfiles":
        return Counter({"context_listfiles": fallback_weight})
    if normalized == "js":
        return Counter({"tool_js": fallback_weight})
    if normalized != "runcommand":
        return Counter()

    targets = activity.get("targets")
    if not isinstance(targets, list) or not targets:
        return Counter({"tool_runcommand_other_command": fallback_weight})
    return Counter(_run_command_source_key(str(target)) for target in targets)


def _run_command_source_key(command: str) -> str:
    tokens = _command_tokens(command)
    if not tokens:
        return "tool_runcommand_other_command"
    head = _command_head(tokens)
    if head == "stdin":
        return "tool_runcommand_other_command"
    token_set = set(tokens)
    if head == "ct":
        return "tool_runcommand_cli_report"
    if head in {"git", "gh", "hg", "svn"} or tokens[0] in {"git", "gh", "hg", "svn"}:
        return "tool_runcommand_repo"
    if token_set & _TEST_TOKENS or token_set & _BUILD_TOKENS:
        return "tool_runcommand_build"
    if token_set & _PACKAGE_MANAGERS and token_set & _DEPENDENCY_TOKENS:
        return "tool_runcommand_dependency"
    return f"tool_{_slug(f'RunCommand:other:{head}')}"


def _command_tokens(command: str) -> list[str]:
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        parts = command.split()
    return [os.path.basename(part.lower()) for part in parts if part]


def _command_head(tokens: list[str]) -> str:
    index = 0
    while index < len(tokens) and "=" in tokens[index] and not tokens[index].startswith("-"):
        index += 1
    if index < len(tokens) and tokens[index] in _COMMAND_RUNNERS:
        index += 1
        while index < len(tokens) and tokens[index] in _RUNNER_SUBWORDS:
            index += 1
    if index + 2 < len(tokens) and tokens[index] in {"python", "python3"} and tokens[index + 1] == "-m":
        return tokens[index + 2]
    return tokens[index] if index < len(tokens) else "command"


def _slug(value: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in value.lower()).strip("_") or "command"


_TEST_TOKENS: frozenset[str] = frozenset(
    {"pytest", "jest", "vitest", "mocha", "rspec", "phpunit", "unittest", "tox", "ctest", "test"}
)
_BUILD_TOKENS: frozenset[str] = frozenset({
    "tsc", "mypy", "ruff", "eslint", "flake8", "pylint", "black", "isort", "prettier",
    "make", "cmake", "webpack", "rollup", "vite", "esbuild", "clippy",
    "build", "compile", "lint", "typecheck", "check", "vet",
})
_PACKAGE_MANAGERS: frozenset[str] = frozenset({
    "npm", "pnpm", "yarn", "bun", "pip", "pip3", "uv", "poetry", "pipenv",
    "cargo", "gem", "bundle", "brew", "conda", "apt", "apt-get",
})
_DEPENDENCY_TOKENS: frozenset[str] = frozenset(
    {"install", "add", "ci", "sync", "get", "lock", "update", "upgrade", "remove"}
)
_COMMAND_RUNNERS: frozenset[str] = frozenset(
    {"uv", "poetry", "pdm", "pipenv", "rye", "hatch", "npx", "bunx", "pnpm", "yarn", "bun"}
)
_RUNNER_SUBWORDS: frozenset[str] = frozenset({"run", "exec", "dlx", "tool"})


def _activity_summary(activity: dict[str, Any]) -> str:
    tool = str(activity.get("tool") or "Tool activity")
    count = activity.get("count")
    suffix = f" x{count}" if isinstance(count, int) and count > 1 else ""
    for key in ("cmd", "path", "query", "url"):
        if activity.get(key):
            return f"{tool}{suffix}: {activity[key]}"
    for key in ("paths", "queries", "urls", "targets"):
        values = activity.get(key)
        if isinstance(values, list) and values:
            return f"{tool}{suffix}: {', '.join(str(item) for item in values[:3])}"
    return f"{tool}{suffix}"


def _projection_warnings(vendor: str, events: list[ContextEvent]) -> list[str]:
    warnings = [
        "Timeline user and assistant token deltas estimate only the visible overview text; "
        "tool activity token deltas are allocated from aggregate stats categories because "
        "overview does not expose per-row result text.",
        "Turn usage is cumulative model accounting and is retained as a detail reference, "
        "not presented as context added by one timeline event.",
    ]
    if vendor in {"claude_code", "pi"}:
        warnings.append(
            f"{vendor} cache buckets cannot be split into system, project instructions, files, "
            "output, and assistant history; they are shown as unattributed instead."
        )
    if not any(event.tokens for event in events):
        warnings.append("No event-level token evidence is available for this session.")
    return warnings


def _overview_vendor(overview: dict[str, Any]) -> str | None:
    for session in overview.get("sessions") or []:
        if isinstance(session, dict) and session.get("vendor"):
            return str(session["vendor"])
    return None


def _ct_json(args: list[str]) -> dict[str, Any]:
    ct = os.environ.get("CT_COMMAND") or shutil.which("ct")
    if not ct:
        raise SystemExit("ct executable not found; set CT_COMMAND to the ct command path")
    command = [*shlex.split(ct), *args]
    try:
        completed = subprocess.run(command, check=False, text=True, capture_output=True, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(f"ct command timed out: {' '.join(command)}") from exc
    if completed.returncode != 0:
        sys.stderr.write(completed.stderr or completed.stdout)
        raise SystemExit(completed.returncode)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ct command returned invalid JSON: {' '.join(command)}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"ct command returned a non-object payload: {' '.join(command)}")
    return payload


def _token_evidence(
    value: Any,
    *,
    confidence: Confidence,
    source: str,
) -> TokenEvidence | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return TokenEvidence(value=value, confidence=confidence, source=source)


def _estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


def _optional_text(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _format_tokens(value: int | None) -> str:
    if value is None:
        return "-"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def _format_delta(value: int) -> str:
    return f"+{_format_tokens(value)}"


def _group_label(event: ContextEvent) -> str:
    if event.group == "before_first_prompt":
        return "Before first prompt"
    if event.group == "post_turn":
        return "After final turn"
    return f"Turn {event.turn_id or '-'}"


def _one_line(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


if __name__ == "__main__":
    raise SystemExit(main())
