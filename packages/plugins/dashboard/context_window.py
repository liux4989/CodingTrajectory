from __future__ import annotations

import argparse
import importlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

import token_pricing


def _load_core_module(name: str) -> Any:
    here = os.path.dirname(os.path.abspath(__file__))
    cursor = here
    core_src = None
    for _ in range(8):
        candidate = os.path.join(cursor, "core", "src")
        if os.path.isdir(candidate):
            core_src = candidate
            break
        parent = os.path.dirname(cursor)
        if parent == cursor:
            break
        cursor = parent
    if core_src is None:
        raise ImportError(
            "cannot locate core package; searched ancestors of " + here
        )
    if core_src not in sys.path:
        sys.path.insert(0, core_src)
    return importlib.import_module(name)


_content_size = _load_core_module("coding_trajectory.analysis.content_size")

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


class CostEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value_usd: float = Field(ge=0)
    confidence: Literal["reported", "estimated"]
    source: str
    effective_date: str | None = None


class ContextWindowProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    session_id: str
    vendor: str
    model: str | None = None
    context_window_tokens: TokenEvidence | None = None
    used_tokens: TokenEvidence | None = None
    used_percent: float | None = None
    token_cost: CostEvidence | None = None
    categories: list[ContextCategory]
    provider_usage_buckets: list[ContextCategory]
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
    categories = _project_categories(stats)
    provider_usage_buckets = _project_provider_usage_buckets(stats)
    events = [
        *_category_events(categories),
        *_trajectory_events(
            overview,
            usage,
            turn_id=turn_id,
        ),
    ]
    warnings = [str(item) for item in stats.get("warnings") or []]
    warnings.extend(_projection_warnings(events))
    if turn_id and not any(event.turn_id == turn_id for event in events):
        raise SystemExit(f"turn not found in session overview: {turn_id}")

    model = stats.get("model") or {}
    context = stats.get("context") or {}
    model_name = _optional_text(model.get("name"))
    reported_context_window = model.get("context_window") or model.get("context_window_tokens")
    catalog_context_window = token_pricing.get_model_context_window(
        model_name,
        provider=vendor,
    )
    context_window = reported_context_window or catalog_context_window
    used_tokens = _optional_int(context.get("used") or context.get("used_tokens"))
    used_percent = _optional_float(context.get("pct") or context.get("used_percent"))
    if used_percent is None and used_tokens is not None and context_window:
        used_percent = round((used_tokens / context_window) * 100, 1)
    usage_summary = usage.get("total_usage") or usage.get("usage") or {}
    cost_not_reported = any(
        "cost not reported in session log" in str(warning)
        for warning in usage.get("warnings") or []
    )
    reported_cost = _optional_float(usage_summary.get("cost"))
    estimated_cost = None
    if reported_cost is None or cost_not_reported:
        estimated_cost = token_pricing.estimate_cost(
            usage_summary,
            model=model_name,
            provider=vendor,
        )
    return ContextWindowProjection(
        session_id=str(stats.get("id") or session_id),
        vendor=vendor,
        model=model_name,
        context_window_tokens=_token_evidence(
            context_window,
            confidence="structural",
            source=(
                "ct session stats:model.context_window"
                if reported_context_window
                else token_pricing.MODELS_DEV_SOURCE
            ),
        ),
        used_tokens=_token_evidence(
            used_tokens,
            confidence="exact_usage",
            source="ct session stats:context.used",
        ),
        used_percent=used_percent,
        token_cost=(
            CostEvidence(
                value_usd=reported_cost,
                confidence="reported",
                source="session log",
            )
            if reported_cost is not None and not cost_not_reported
            else
            CostEvidence(
                value_usd=estimated_cost.amount_usd,
                confidence="estimated",
                source=estimated_cost.pricing_source,
                effective_date=estimated_cost.pricing_effective_date,
            )
            if estimated_cost
            else None
        ),
        categories=categories,
        provider_usage_buckets=provider_usage_buckets,
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
        (
            f"Token cost: ${projection.token_cost.value_usd:.4f} "
            f"({projection.token_cost.confidence}, {projection.token_cost.source}"
            f"{', ' + projection.token_cost.effective_date if projection.token_cost.effective_date else ''})"
            if projection.token_cost
            else "Token cost: unavailable"
        ),
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
    if projection.provider_usage_buckets:
        lines.extend(["", "Provider usage buckets"])
        for category in projection.provider_usage_buckets:
            lines.append(
                f"  {category.source_key:<20} {_format_delta(category.tokens.value):>8}  "
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


def _project_categories(stats: dict[str, Any]) -> list[ContextCategory]:
    context = stats.get("context") or {}
    leaves = list(_category_leaves(context.get("categories") or []))
    projected: list[ContextCategory] = []
    for index, category in enumerate(leaves):
        source_key = str(category.get("key") or f"category_{index}")
        key = _category_key(source_key)
        tokens = category.get("tokens")
        if not isinstance(tokens, int) or isinstance(tokens, bool):
            continue
        confidence = _confidence(category.get("confidence"), fallback="estimated_tokens")
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


def _project_provider_usage_buckets(stats: dict[str, Any]) -> list[ContextCategory]:
    projected: list[ContextCategory] = []
    for index, category in enumerate(stats.get("provider_usage_buckets") or []):
        if not isinstance(category, dict):
            continue
        tokens = category.get("tokens")
        if not isinstance(tokens, int) or isinstance(tokens, bool):
            continue
        source_key = str(category.get("key") or f"provider_bucket_{index}")
        projected.append(
            ContextCategory(
                id=f"provider:{source_key}:{index}",
                category="unattributed",
                source_key=source_key,
                label=str(category.get("label") or source_key),
                tokens=TokenEvidence(
                    value=tokens,
                    confidence=_confidence(category.get("confidence"), fallback="exact_usage"),
                    source=str(category.get("source") or "ct session stats:provider_usage_buckets"),
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


def _category_key(source_key: str) -> CategoryKey:
    mapping: dict[str, CategoryKey] = {
        "base_system": "system",
        "developer_instructions": "system",
        "agents_md": "project_instructions",
        "skills": "skills",
        "mcp": "mcp",
        "memory": "memory",
        "user_initial_request": "you",
        "user_follow_up_requests": "you",
        "final_answer": "agent",
        "progress_update": "agent",
        "assistant_message": "agent",
        "reasoning": "agent",
        "context_readfile": "files",
        "context_searchtext": "files",
        "context_listfiles": "files",
        "context_webfetch": "files",
        "context_websearch": "files",
        "output": "output",
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
            "editfile",
            "writefile",
            "todolist",
            "subagenttask",
            "sessionhandoff",
        )
    ):
        return "agent"
    if source_key.startswith(("context_", "tool_", "verification", "repository_", "command_")):
        return "output"
    return "unattributed"


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

def _trajectory_events(
    overview: dict[str, Any],
    usage: dict[str, Any],
    *,
    turn_id: str | None,
) -> list[ContextEvent]:
    usage_by_turn = {
        str(item.get("id")): item.get("usage") or {}
        for item in usage.get("turns") or []
        if isinstance(item, dict) and item.get("id")
    }
    events: list[ContextEvent] = []
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
                            value=_content_size.visible_text_size(request_text).tokens,
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
                event = _activity_event(
                    activity,
                    session_id=session_id,
                    turn_id=current_turn_id,
                    index=index,
                    turn_usage=usage_by_turn.get(current_turn_id),
                )
                events.append(event)
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
) -> ContextEvent:
    if activity.get("text"):
        text = str(activity["text"])
        return ContextEvent(
            id=f"turn:{turn_id}:activity:{index}",
            group="turn",
            turn_id=turn_id,
            category="agent",
            label="Assistant message",
            summary=text,
            tokens=TokenEvidence(
                value=_content_size.visible_text_size(text).tokens,
                confidence="estimated_tokens",
                source="ct session overview:activity.text length estimate",
            ),
            source="ct session overview:activity.text",
            confidence="exact_text",
            detail_ref={"session_id": session_id, "turn_id": turn_id},
        )

    tool = str(activity.get("tool") or "Tool activity")
    summary = _activity_summary(activity)
    detail_ref = {"session_id": session_id, "turn_id": turn_id}
    if turn_usage:
        detail_ref["turn_usage_total"] = str(turn_usage.get("total") or 0)
    return ContextEvent(
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


def _projection_warnings(events: list[ContextEvent]) -> list[str]:
    warnings = [
        "Timeline user and assistant token deltas estimate only the visible overview text; "
        "tool activity remains structural because overview does not expose per-item result text.",
        "Turn usage is cumulative model accounting and is retained as a detail reference, "
        "not presented as context added by one timeline event.",
    ]
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


def _confidence(value: Any, *, fallback: Confidence) -> Confidence:
    if value in {"exact_usage", "exact_text", "estimated_tokens", "structural", "unknown"}:
        return value
    return fallback


def _optional_text(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
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
