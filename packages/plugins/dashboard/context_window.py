from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

try:
    from . import token_pricing
except ImportError:
    import token_pricing


@dataclass(frozen=True)
class _VisibleTextSize:
    tokens: int


def _visible_text_size(text: str) -> _VisibleTextSize:
    return _VisibleTextSize(tokens=max(1, (len(text) + 3) // 4) if text else 0)


CategoryKey = Literal[
    "starting_context",
    "user_input",
    "files",
    "output",
    "agent",
    "unattributed",
]
Confidence = Literal[
    "exact_usage", "exact_text", "estimated_tokens", "structural", "unknown"
]


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
    estimated_cost: CostEvidence | None = None


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
    estimated_cost: CostEvidence | None = None
    # Wall-clock gap (``runtime.wait_before_seconds``) preceding this turn; the
    # prompt-cache TTL break is read off it together with ``re_read_tokens``.
    idle_seconds: float | None = None
    re_read_tokens: int | None = None


class ExpensiveItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    turn_id: str
    category: CategoryKey
    label: str
    summary: str
    allocated_usage: dict[str, int]
    estimated_cost: CostEvidence


class CompactionEventRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: str
    # Provider-native mechanism (``eviction_boundary`` for Claude Code,
    # ``context_compacted`` for Codex); controls which delta fields render.
    mechanism: str
    trigger: str | None = None
    pre_tokens: int | None = None
    post_tokens: int | None = None
    dropped_tokens: int | None = None


class CompactionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = 0
    cumulative_dropped_tokens: int | None = None
    events: list[CompactionEventRecord] = Field(default_factory=list)


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
    expensive_items: list[ExpensiveItem] = Field(default_factory=list)
    events: list[ContextEvent]
    compaction: CompactionSummary | None = None
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
    parser.add_argument(
        "--turn",
        dest="turn_id",
        default=None,
        help="Limit the event timeline to one turn.",
    )
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


def build_projection(
    session_id: str,
    *,
    turn_id: str | None = None,
    ct_json: Callable[[list[str]], dict[str, Any]] | None = None,
) -> ContextWindowProjection:
    run = ct_json or _ct_json
    stats = run(["session", "stats", "--global-scope", "--output", "json", session_id])
    overview = run(
        ["session", "overview", "--global-scope", "--output", "json", session_id]
    )
    usage = run(["session", "usage", "--global-scope", "--output", "json", session_id])
    tool_usage = _ct_api_result(
        "session.tool_usage",
        {"session_id": session_id},
        global_scope=True,
        ct_json=run,
    )

    vendor = str(stats.get("vendor") or _overview_vendor(overview) or "unknown")
    model = stats.get("model") or {}
    model_name = _optional_text(model.get("name"))
    categories = _project_categories(stats, model=model_name, provider=vendor)
    provider_usage_buckets = _project_provider_usage_buckets(
        stats,
        model=model_name,
        provider=vendor,
    )
    expensive_items = _project_expensive_items(
        tool_usage,
        model=model_name,
        provider=vendor,
    )
    events = [
        *_category_events(categories),
        *_trajectory_events(
            overview,
            usage,
            tool_usage,
            model=model_name,
            provider=vendor,
            turn_id=turn_id,
        ),
    ]
    warnings = [str(item) for item in stats.get("warnings") or []]
    warnings.extend(_projection_warnings(events))
    if turn_id and not any(event.turn_id == turn_id for event in events):
        raise SystemExit(f"turn not found in session overview: {turn_id}")

    compaction = _project_compaction(stats)

    context = stats.get("context") or {}
    reported_context_window = model.get("context_window") or model.get(
        "context_window_tokens"
    )
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
            else CostEvidence(
                value_usd=estimated_cost.amount_usd,
                confidence="estimated",
                source=estimated_cost.pricing_source,
                effective_date=estimated_cost.pricing_effective_date,
            )
            if estimated_cost
            else None
        ),
        categories=sorted(categories, key=_category_sort_key, reverse=True),
        provider_usage_buckets=sorted(
            provider_usage_buckets,
            key=_category_sort_key,
            reverse=True,
        ),
        expensive_items=expensive_items,
        events=events,
        compaction=compaction,
        warnings=_dedupe(warnings),
    )


def render_markdown(projection: ContextWindowProjection) -> str:
    context_label = _format_tokens(
        projection.context_window_tokens.value
        if projection.context_window_tokens
        else None
    )
    used_label = _format_tokens(
        projection.used_tokens.value if projection.used_tokens else None
    )
    percent_label = (
        f" ({projection.used_percent:.1f}%)"
        if projection.used_percent is not None
        else ""
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
        key=_category_sort_key,
        reverse=True,
    ):
        cost_label = (
            _format_cost(category.estimated_cost.value_usd)
            if category.estimated_cost
            else "-"
        )
        lines.append(
            f"  {category.category:<20} {_format_delta(category.tokens.value):>8}  "
            f"{cost_label:>9}  {_one_line(category.label, 52)} [{category.tokens.confidence}]"
        )
    if projection.provider_usage_buckets:
        lines.extend(["", "Provider usage buckets"])
        for category in projection.provider_usage_buckets:
            lines.append(
                f"  {category.source_key:<20} {_format_delta(category.tokens.value):>8}  "
                f"{_one_line(category.label, 62)} [{category.tokens.confidence}]"
            )

    if projection.expensive_items:
        lines.extend(["", "Most Expensive Items"])
        for item in projection.expensive_items[:12]:
            usage = item.allocated_usage
            lines.append(
                f"  {_format_cost(item.estimated_cost.value_usd):>9}  "
                f"{_format_tokens(usage.get('uncached_prompt_tokens'))}/"
                f"{_format_tokens(usage.get('cached_prompt_tokens'))}/"
                f"{_format_tokens(usage.get('completion_tokens'))}/"
                f"{_format_tokens(usage.get('reasoning_tokens'))}  "
                f"{item.category:<10} {_one_line(item.label + ': ' + item.summary, 72)}"
            )

    current_group: tuple[str, str | None] | None = None
    for event in projection.events:
        group = (event.group, event.turn_id)
        if group != current_group:
            lines.extend(["", _group_label(event)])
            current_group = group
        delta = _format_delta(event.tokens.value) if event.tokens else "       -"
        summary = _one_line(event.summary or event.label, 74)
        flag = _ttl_break_flag(event)
        line = f"  {event.category:<20} {delta:>8}  {summary}"
        if flag:
            line += f"  {flag}"
        lines.append(line)

    if projection.compaction and projection.compaction.events:
        lines.extend(["", "Compaction timeline"])
        for index, event in enumerate(projection.compaction.events, start=1):
            mechanism = event.mechanism or "-"
            trigger = event.trigger or "-"
            pre = _format_tokens(event.pre_tokens)
            post = _format_tokens(event.post_tokens)
            delta = f"{pre} -> {post}" if event.pre_tokens is not None and event.post_tokens is not None else "-"
            dropped = _format_tokens(event.dropped_tokens) if event.dropped_tokens is not None else "-"
            timestamp = _one_line(event.timestamp, 19)
            lines.append(
                f"  {index:>2}  {timestamp:<19} {mechanism:<18} {trigger:<10} {delta:>15} {dropped:>10}"
            )

    if projection.warnings:
        lines.extend(["", "Warnings"])
        lines.extend(
            f"  - {_one_line(warning, 110)}" for warning in projection.warnings
        )
    return "\n".join(lines)


def _category_sort_key(category: ContextCategory) -> tuple[float, int]:
    return (
        category.estimated_cost.value_usd if category.estimated_cost else 0.0,
        category.tokens.value,
    )


def _project_compaction(stats: dict[str, Any]) -> CompactionSummary | None:
    """Lift the compaction timeline from ``ct session stats`` JSON.

    ``stats`` carries ``compaction`` (count, cumulative dropped, last event,
    and the full ``events`` list) when the session has compacted; the field is
    absent or ``None`` otherwise.
    """
    compaction = stats.get("compaction")
    if not isinstance(compaction, dict) or not compaction.get("count"):
        return None
    events = [
        CompactionEventRecord(
            timestamp=str(event.get("timestamp") or ""),
            mechanism=str(event.get("mechanism") or ""),
            trigger=_optional_text(event.get("trigger")),
            pre_tokens=_optional_int(event.get("pre")),
            post_tokens=_optional_int(event.get("post")),
            dropped_tokens=_optional_int(event.get("dropped")),
        )
        for event in compaction.get("events") or []
        if isinstance(event, dict) and event.get("timestamp")
    ]
    return CompactionSummary(
        count=int(compaction.get("count") or 0),
        cumulative_dropped_tokens=_optional_int(
            compaction.get("cumulative_dropped")
        ),
        events=events,
    )


def _project_categories(
    stats: dict[str, Any],
    *,
    model: str | None,
    provider: str,
) -> list[ContextCategory]:
    context = stats.get("context") or {}
    leaves = list(_category_leaves(context.get("categories") or []))
    projected: list[ContextCategory] = []
    for index, category in enumerate(leaves):
        source_key = str(category.get("key") or f"category_{index}")
        key = _category_key(source_key)
        tokens = category.get("tokens")
        if not isinstance(tokens, int) or isinstance(tokens, bool):
            continue
        confidence = _confidence(
            category.get("confidence"), fallback="estimated_tokens"
        )
        allocated_usage = _category_usage(category)
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
                estimated_cost=_cost_evidence_from_usage(
                    allocated_usage,
                    model=model,
                    provider=provider,
                ),
            )
        )
    return projected


def _project_provider_usage_buckets(
    stats: dict[str, Any],
    *,
    model: str | None,
    provider: str,
) -> list[ContextCategory]:
    projected: list[ContextCategory] = []
    for index, category in enumerate(stats.get("provider_usage_buckets") or []):
        if not isinstance(category, dict):
            continue
        tokens = category.get("tokens")
        if not isinstance(tokens, int) or isinstance(tokens, bool):
            continue
        source_key = str(category.get("key") or f"provider_bucket_{index}")
        allocated_usage = _category_usage(category)
        projected.append(
            ContextCategory(
                id=f"provider:{source_key}:{index}",
                category="unattributed",
                source_key=source_key,
                label=str(category.get("label") or source_key),
                tokens=TokenEvidence(
                    value=tokens,
                    confidence=_confidence(
                        category.get("confidence"), fallback="exact_usage"
                    ),
                    source=str(
                        category.get("source")
                        or "ct session stats:provider_usage_buckets"
                    ),
                ),
                percent=_optional_float(category.get("pct")),
                estimated_cost=_cost_evidence_from_usage(
                    allocated_usage,
                    model=model,
                    provider=provider,
                ),
            )
        )
    return projected


def _project_expensive_items(
    tool_usage: dict[str, Any],
    *,
    model: str | None,
    provider: str,
) -> list[ExpensiveItem]:
    items: list[ExpensiveItem] = []
    for index, item in enumerate(tool_usage.get("tool_items") or []):
        if not isinstance(item, dict):
            continue
        real_cost = item.get("allocated_real_token_cost")
        if not isinstance(real_cost, dict):
            continue
        usage = _usage_dict(real_cost)
        estimate = _cost_evidence_from_usage(usage, model=model, provider=provider)
        if estimate is None:
            continue
        events = _tool_item_events(item, index=index, model=model, provider=provider)
        event = events[0] if events else None
        category = (
            event.category
            if event
            else _tool_category(str(item.get("tool_name") or ""))
        )
        label = event.label if event else str(item.get("tool_name") or "Tool")
        summary = event.summary if event else str(item.get("input_summary") or "")
        items.append(
            ExpensiveItem(
                item_id=str(item.get("item_id") or f"tool_item_{index}"),
                turn_id=str(item.get("turn_id") or ""),
                category=category,
                label=label,
                summary=summary,
                allocated_usage=usage,
                estimated_cost=estimate,
            )
        )
    return sorted(
        items,
        key=lambda item: (
            item.estimated_cost.value_usd,
            item.allocated_usage.get("uncached_prompt_tokens", 0),
            item.allocated_usage.get("completion_tokens", 0),
        ),
        reverse=True,
    )


def _category_leaves(categories: Iterable[Any]) -> Iterable[dict[str, Any]]:
    for category in categories:
        if not isinstance(category, dict):
            continue
        children = category.get("children") or []
        if children:
            yield from _category_leaves(children)
        else:
            yield category


def _category_usage(category: dict[str, Any]) -> dict[str, int]:
    for key in ("allocated_usage", "usage", "real_tokens"):
        usage = _usage_dict(category.get(key))
        if usage.get("processed_tokens", 0) > 0 or any(
            usage.get(token_key, 0)
            for token_key in (
                "uncached_prompt_tokens",
                "cached_prompt_tokens",
                "cache_write_tokens",
                "completion_tokens",
                "reasoning_tokens",
            )
        ):
            return usage
    return {}


_STARTING_CONTEXT_KEYS = {
    "base_system",
    "developer_instructions",
    "agents_md",
    "skills",
    "mcp",
    "memory",
}
_USER_INPUT_KEYS = {"user_initial_request", "user_follow_up_requests"}
_AGENT_FILES_KEYS = {
    "context_readfile",
}
_AGENT_AGENT_KEYS = {
    "assistant_messages",
    "final_answer",
    "progress_update",
    "assistant_message",
    "reasoning",
    "editfile",
    "writefile",
    "todolist",
    "subagenttask",
    "sessionhandoff",
}


def _category_key(source_key: str) -> CategoryKey:
    if source_key in _STARTING_CONTEXT_KEYS:
        return "starting_context"
    if source_key in _USER_INPUT_KEYS:
        return "user_input"
    if source_key in _AGENT_FILES_KEYS:
        return "files"
    if source_key == "output" or source_key.startswith("output_"):
        return "output"
    if source_key in _AGENT_AGENT_KEYS or source_key.startswith(
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
    return "unattributed"


def _category_events(categories: list[ContextCategory]) -> list[ContextEvent]:
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
        if category.source_key in _STARTING_CONTEXT_KEYS
    ]


def _trajectory_events(
    overview: dict[str, Any],
    usage: dict[str, Any],
    tool_usage: dict[str, Any],
    *,
    model: str | None,
    provider: str,
    turn_id: str | None,
) -> list[ContextEvent]:
    turn_meta = {
        str(item.get("id")): item
        for item in usage.get("turns") or []
        if isinstance(item, dict) and item.get("id")
    }
    tool_events_by_turn = _tool_events_by_turn(
        tool_usage,
        model=model,
        provider=provider,
    )
    events: list[ContextEvent] = []
    for session in overview.get("sessions") or []:
        if not isinstance(session, dict):
            continue
        session_id = str(session.get("id") or overview.get("id") or "")
        for turn in session.get("turns") or []:
            if not isinstance(turn, dict):
                continue
            current_turn_id = str(turn.get("id") or "")
            raw_tool_events = list(tool_events_by_turn.get(current_turn_id) or [])
            turn_item = turn_meta.get(current_turn_id) or {}
            turn_runtime = turn_item.get("runtime") or {}
            turn_usage = turn_item.get("usage") or {}
            idle_seconds = _optional_float(turn_runtime.get("wait_before_seconds"))
            re_read_tokens = _optional_int(turn_usage.get("uncached_prompt_tokens"))
            turn_start = len(events)
            request = turn.get("request") or {}
            request_text = _optional_text(request.get("text"))
            if request_text:
                events.append(
                    ContextEvent(
                        id=f"turn:{current_turn_id}:user",
                        group="turn",
                        turn_id=current_turn_id,
                        category="user_input",
                        label="User prompt",
                        summary=request_text,
                        tokens=TokenEvidence(
                            value=_visible_text_size(request_text).tokens,
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
                if not activity.get("text") and raw_tool_events:
                    count = activity.get("count")
                    take = count if isinstance(count, int) and count > 0 else 1
                    for _ in range(take):
                        if not raw_tool_events:
                            break
                        events.extend(raw_tool_events.pop(0))
                    continue
                event = _activity_event(
                    activity,
                    session_id=session_id,
                    turn_id=current_turn_id,
                    index=index,
                    turn_usage=turn_usage,
                )
                events.append(event)
            while raw_tool_events:
                events.extend(raw_tool_events.pop(0))
            for event in events[turn_start:]:
                event.idle_seconds = idle_seconds
                event.re_read_tokens = re_read_tokens
    if turn_id:
        events = [
            event
            for event in events
            if event.group == "before_first_prompt" or event.turn_id == turn_id
        ]
    return events


def _tool_events_by_turn(
    tool_usage: dict[str, Any],
    *,
    model: str | None,
    provider: str,
) -> dict[str, list[list[ContextEvent]]]:
    by_turn: dict[str, list[list[ContextEvent]]] = {}
    for index, item in enumerate(tool_usage.get("tool_items") or []):
        if not isinstance(item, dict):
            continue
        turn_id = _optional_text(item.get("turn_id"))
        if turn_id is None:
            continue
        by_turn.setdefault(turn_id, []).append(
            _tool_item_events(item, index=index, model=model, provider=provider)
        )
    return by_turn


def _tool_item_events(
    item: dict[str, Any],
    *,
    index: int,
    model: str | None,
    provider: str,
) -> list[ContextEvent]:
    item_id = str(item.get("item_id") or f"tool_item_{index}")
    tool = str(item.get("tool_name") or "Tool")
    attribution = (
        item.get("token_attribution")
        if isinstance(item.get("token_attribution"), dict)
        else {}
    )
    real_cost = (
        item.get("allocated_real_token_cost")
        if isinstance(item.get("allocated_real_token_cost"), dict)
        else {}
    )
    input_tokens = _optional_int(attribution.get("tool_input_tokens")) or 0
    output_tokens = _optional_int(attribution.get("tool_output_tokens")) or 0
    total_tokens = input_tokens + output_tokens
    real_total_tokens = _optional_int(real_cost.get("processed_tokens"))
    output_chars = _optional_int(item.get("output_chars")) or 0
    output_original_tokens = _optional_int(item.get("output_original_tokens"))
    input_summary = _optional_text(item.get("input_summary")) or f"{tool} input"
    detail_ref = {
        "item_id": item_id,
        "session_id": str(item.get("session_id") or ""),
        "turn_id": str(item.get("turn_id") or ""),
        "tool_name": tool,
        "tool_bucket": _tool_bucket_key(input_summary, tool),
        "tool_input_tokens": str(input_tokens),
        "tool_output_tokens": str(output_tokens),
    }
    for source_key, detail_key in (
        ("prompt_tokens", "allocated_prompt_tokens"),
        ("uncached_prompt_tokens", "allocated_uncached_prompt_tokens"),
        ("cached_prompt_tokens", "allocated_cached_prompt_tokens"),
        ("cache_write_tokens", "allocated_cache_write_tokens"),
        ("completion_tokens", "allocated_completion_tokens"),
        ("reasoning_tokens", "allocated_reasoning_tokens"),
        ("processed_tokens", "allocated_processed_tokens"),
    ):
        value = _optional_int(real_cost.get(source_key))
        if value is not None:
            detail_ref[detail_key] = str(value)
    if real_cost.get("allocation_method"):
        detail_ref["allocated_token_method"] = str(real_cost["allocation_method"])
    estimated_cost = _cost_evidence_from_usage(
        _usage_dict(real_cost),
        model=model,
        provider=provider,
    )
    if estimated_cost:
        detail_ref["estimated_cost_usd"] = str(estimated_cost.value_usd)
    status = _optional_text(item.get("status"))
    if status:
        detail_ref["status"] = status

    label = _tool_event_label(tool, input_summary)
    summary_bits = [input_summary, f"{output_chars} output chars"]
    if real_total_tokens is not None:
        summary_bits.append(f"{real_total_tokens} allocated real tokens")
    if output_original_tokens is not None:
        summary_bits.append(f"{output_original_tokens} observed output tokens")
    if item.get("output_truncated"):
        summary_bits.append("output truncated")

    output_confidence = _tool_output_confidence(attribution.get("content_confidence"))
    combined_confidence = _combined_tool_confidence(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        output_confidence=output_confidence,
    )
    return [
        ContextEvent(
            id=f"tool:{item_id}",
            group="turn",
            turn_id=detail_ref["turn_id"],
            category=_tool_category(tool),
            label=label,
            summary=", ".join(summary_bits),
            tokens=TokenEvidence(
                value=total_tokens,
                confidence=combined_confidence,
                source="ct session.tool_usage:tool_input_tokens + tool_output_tokens",
            ),
            source="ct session.tool_usage:tool_items",
            confidence=combined_confidence,
            detail_ref=detail_ref,
            terminal_visible=True,
            estimated_cost=estimated_cost,
        ),
    ]


def _combined_tool_confidence(
    *,
    input_tokens: int,
    output_tokens: int,
    output_confidence: Confidence,
) -> Confidence:
    if output_tokens == 0:
        return "estimated_tokens" if input_tokens else "structural"
    if input_tokens == 0:
        return output_confidence
    return "estimated_tokens"


def _tool_output_confidence(value: Any) -> Confidence:
    if value == "observed_tool_output_token_count":
        return "exact_usage"
    if value == "no_visible_content":
        return "structural"
    return "estimated_tokens"


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
                value=_visible_text_size(text).tokens,
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
    if any(
        term in normalized
        for term in (
            "todo",
            "subagent",
            "handoff",
            "update_plan",
            "edit",
            "write",
            "apply_patch",
        )
    ):
        return "agent"
    if normalized in {"read", "view"} or any(
        term in normalized for term in ("read_file", "readfile", "read_many_files")
    ):
        return "files"
    return "output"


def _tool_bucket_key(input_summary: str, tool: str) -> str:
    lower = input_summary.lower()
    normalized_tool = tool.lower()
    if "apply_patch" in normalized_tool:
        return "edits"
    if normalized_tool == "reasoning":
        return "reasoning_items"
    if not _is_shell_tool(tool):
        return "other_tool"
    if "curl -fssl" in lower and "espn.com/soccer/" in lower and "| rg" in lower:
        return "raw_html_scrape"
    if lower.startswith("rg ") or lower.startswith("rg -n") or " rg -n " in lower:
        if (
            re.search(r"\s\.(?:$|\s)", lower)
            or "src aws packages readme" in lower
            or "docs" in lower
            or "/memories/" in lower
            or "world cup readiness|readiness" in lower
            or "source-evidence|research|aws smoke" in lower
            or "limit|limit|default_event_limit" in lower
        ):
            return "broad_search"
        return "targeted_search"
    if lower.startswith("sed ") or lower.startswith("nl ") or lower.startswith("cat "):
        return "file_read_shell"
    if any(term in lower for term in ["git status", "git diff", "git log"]):
        return "git_inspection"
    if any(
        term in lower
        for term in [
            "aws batch",
            " aws iam ",
            " aws sts ",
            "wrangler d1",
            "tt research",
            "curl -fss https://trailtrading-research-api",
        ]
    ):
        return "cloud_state_check"
    if any(
        term in lower
        for term in ["py_compile", "bun run check", "diff --check", "ruby -e"]
    ):
        return "validation"
    if any(term in lower for term in ["git add", "git commit"]):
        return "git_write"
    return "other_exec"


def _tool_event_label(tool: str, input_summary: str) -> str:
    normalized = tool.lower()
    if "apply_patch" in normalized:
        target = _patch_target(input_summary)
        return f"Edit {target}" if target else "Edit files"
    if any(term in normalized for term in ("edit", "write")):
        target = _path_title(input_summary)
        action = "Write" if "write" in normalized else "Edit"
        return f"{action} {target}" if target else f"{action} files"
    if any(term in normalized for term in ("todo", "update_plan")):
        return "Update plan"
    if any(term in normalized for term in ("subagent", "handoff")):
        return _compact_title(tool.replace("_", " ").title())
    if _is_shell_tool(tool):
        return _shell_event_label(input_summary)
    if normalized in {"read", "view"} or any(
        term in normalized for term in ("read_file", "readfile", "read_many_files")
    ):
        target = _path_title(input_summary)
        return f"Read {target}" if target else "Read files"
    if any(term in normalized for term in ("search", "grep")):
        query = _search_query_title(input_summary)
        return f"grep {_quote_title(query)}" if query else "Search output"
    if any(term in normalized for term in ("list", "glob")):
        return "File listing output"
    return _compact_title(tool.replace("_", " ").strip().title() or "Tool")


def _is_shell_tool(tool: str) -> bool:
    return tool in {
        "bash",
        "Bash",
        "exec_command",
        "run_shell_command",
        "shell",
        "write_stdin",
    }


def _shell_event_label(command: str) -> str:
    primary = _primary_shell_stage(command)
    tokens = _safe_split(primary)
    head = _command_head(tokens)
    if head in {"rg", "grep", "ag", "ack", "rga"}:
        if any(token in {"--files", "-l", "--files-with-matches"} for token in tokens):
            return "File listing output"
        query = _grep_query(tokens, head)
        return f"grep {_quote_title(query)}" if query else "Search output"
    if head in {"ls", "find", "fd", "tree", "eza", "exa"}:
        return "File listing output"
    if head in {"cat", "bat", "head", "tail", "less", "more", "nl", "sed"}:
        target = _shell_path_arg(tokens, head)
        return f"Read {_path_title(target)}" if target else "Read command output"
    if head in {"apply_patch", "applypatch"}:
        target = _patch_target(command)
        return f"Edit {target}" if target else "Edit files"
    short = _compact_command(primary)
    return f"{short} output" if short else "Command output"


def _primary_shell_stage(command: str) -> str:
    for separator in ("&&", "||", ";", "\n"):
        if separator in command:
            parts = [part.strip() for part in command.split(separator) if part.strip()]
            informative = next(
                (
                    part
                    for part in parts
                    if _command_head(_safe_split(part))
                    in {"rg", "grep", "sed", "cat", "ls", "find", "fd"}
                ),
                None,
            )
            return informative or parts[0]
    return command.strip()


def _safe_split(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _command_head(tokens: list[str]) -> str:
    if not tokens:
        return ""
    index = 0
    while (
        index < len(tokens)
        and "=" in tokens[index]
        and not tokens[index].startswith("-")
    ):
        index += 1
    if index < len(tokens) and tokens[index] in {
        "uv",
        "poetry",
        "pdm",
        "pipenv",
        "npx",
        "bunx",
        "pnpm",
        "yarn",
        "bun",
        "deno",
    }:
        index += 1
        while index < len(tokens) and tokens[index] in {
            "run",
            "exec",
            "dlx",
            "tool",
            "task",
        }:
            index += 1
    if (
        index + 2 < len(tokens)
        and tokens[index] in {"python", "python3"}
        and tokens[index + 1] == "-m"
    ):
        return os.path.basename(tokens[index + 2].lower())
    return os.path.basename(tokens[index].lower()) if index < len(tokens) else ""


def _grep_query(tokens: list[str], head: str) -> str | None:
    saw_head = False
    skip_next = False
    flag_value_options = {
        "-A",
        "-B",
        "-C",
        "-e",
        "-f",
        "-g",
        "--glob",
        "-m",
        "--max-count",
        "-t",
        "--type",
        "--type-not",
        "-T",
        "-r",
        "--replace",
        "--include",
        "--exclude",
        "--exclude-dir",
    }
    for token in tokens:
        if not saw_head:
            if os.path.basename(token) == head:
                saw_head = True
            continue
        if skip_next:
            skip_next = False
            continue
        if token in flag_value_options:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        return token
    return None


def _shell_path_arg(tokens: list[str], head: str) -> str | None:
    saw_head = False
    skip_next = False
    for token in tokens:
        if not saw_head:
            if os.path.basename(token) == head:
                saw_head = True
            continue
        if skip_next:
            skip_next = False
            continue
        if token in {"-n", "-e"}:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        return token
    return None


def _patch_target(text: str) -> str | None:
    for marker in ("*** Update File: ", "*** Add File: ", "*** Delete File: "):
        if marker in text:
            tail = text.split(marker, 1)[1]
            return _path_title(tail.splitlines()[0].strip())
    return _path_title(text) if "/" in text else None


def _path_title(path: str | None) -> str | None:
    if not path:
        return None
    cleaned = path.strip().strip("'\"")
    if not cleaned:
        return None
    return os.path.basename(cleaned.rstrip("/")) or cleaned


def _search_query_title(text: str) -> str | None:
    if ":" in text:
        text = text.split(":", 1)[-1]
    stripped = text.strip().strip("'\"")
    return stripped or None


def _quote_title(value: str) -> str:
    escaped = value.replace('"', '\\"')
    return f'"{_compact_title(escaped, limit=48)}"'


def _compact_command(command: str, *, limit: int = 48) -> str:
    return _compact_title(" ".join(command.split()), limit=limit)


def _compact_title(value: str, *, limit: int = 72) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


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
    has_tool_token_events = any(
        event.id.startswith("tool:") and event.tokens is not None for event in events
    )
    warnings = [
        "Turn usage is cumulative model accounting and is retained as a detail reference, "
        "not presented as context added by one timeline event.",
    ]
    if has_tool_token_events:
        warnings.append(
            "Tool items combine input and output token evidence from session.tool_usage; USD cost remains a "
            "plugin-side estimate over allocated item usage."
        )
    else:
        warnings.append(
            "Timeline user and assistant token deltas estimate only the visible overview text; "
            "tool activity remains structural because overview does not expose per-item result text."
        )
    if not any(event.tokens for event in events):
        warnings.append("No event-level token evidence is available for this session.")
    return warnings


def _usage_dict(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        "prompt_tokens": _optional_int(value.get("prompt_tokens")) or 0,
        "uncached_prompt_tokens": _optional_int(value.get("uncached_prompt_tokens"))
        or 0,
        "cached_prompt_tokens": _optional_int(value.get("cached_prompt_tokens")) or 0,
        "cache_write_tokens": _optional_int(value.get("cache_write_tokens")) or 0,
        "completion_tokens": _optional_int(value.get("completion_tokens")) or 0,
        "reasoning_tokens": _optional_int(value.get("reasoning_tokens")) or 0,
        "processed_tokens": _optional_int(value.get("processed_tokens")) or 0,
        "prompt_completion_tokens": _optional_int(value.get("prompt_completion_tokens"))
        or 0,
    }


def _cost_evidence_from_usage(
    usage: dict[str, int],
    *,
    model: str | None,
    provider: str,
) -> CostEvidence | None:
    if not any(
        usage.get(key, 0)
        for key in (
            "prompt_tokens",
            "uncached_prompt_tokens",
            "cached_prompt_tokens",
            "cache_write_tokens",
            "completion_tokens",
            "reasoning_tokens",
        )
    ):
        return None
    estimate = token_pricing.estimate_cost(
        usage,
        model=model,
        provider=provider,
    )
    if estimate is None:
        return None
    return CostEvidence(
        value_usd=estimate.amount_usd,
        confidence="estimated",
        source=estimate.pricing_source,
        effective_date=estimate.pricing_effective_date,
    )


def _overview_vendor(overview: dict[str, Any]) -> str | None:
    for session in overview.get("sessions") or []:
        if isinstance(session, dict) and session.get("vendor"):
            return str(session["vendor"])
    return None


def _ct_json(args: list[str]) -> dict[str, Any]:
    ct = os.environ.get("CT_COMMAND") or shutil.which("ct")
    if not ct:
        raise SystemExit(
            "ct executable not found; set CT_COMMAND to the ct command path"
        )
    command = [*shlex.split(ct), *args]
    try:
        completed = subprocess.run(
            command, check=False, text=True, capture_output=True, timeout=60
        )
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(f"ct command timed out: {' '.join(command)}") from exc
    if completed.returncode != 0:
        sys.stderr.write(completed.stderr or completed.stdout)
        raise SystemExit(completed.returncode)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"ct command returned invalid JSON: {' '.join(command)}"
        ) from exc
    if not isinstance(payload, dict):
        raise SystemExit(
            f"ct command returned a non-object payload: {' '.join(command)}"
        )
    return payload


def _ct_api_result(
    method: str,
    params: dict[str, Any],
    *,
    global_scope: bool = False,
    ct_json: Callable[[list[str]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    args = [
        "api",
        "call",
        method,
        "--params",
        json.dumps(params),
    ]
    if global_scope:
        args.insert(3, "--global-scope")
    payload = (ct_json or _ct_json)(args)
    if not payload.get("ok"):
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        raise SystemExit(str(error.get("message") or f"ct api call failed: {method}"))
    result = payload.get("result")
    if not isinstance(result, dict):
        raise SystemExit(f"ct api call returned a non-object result: {method}")
    return result


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
    if value in {
        "exact_usage",
        "exact_text",
        "estimated_tokens",
        "structural",
        "unknown",
    }:
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


def _format_cost(value: float | None) -> str:
    if value is None:
        return "-"
    if value < 0.01:
        return f"${value:.4f}"
    return f"${value:.2f}"


def _format_delta(value: int) -> str:
    return f"+{_format_tokens(value)}"


# Anthropic prompt-cache default TTL is 5 minutes; an idle gap beyond it
# expires cached prefixes, forcing the prompt to be re-processed next turn.
_TTL_BREAK_SECONDS = 300


def _ttl_break_flag(event: ContextEvent) -> str | None:
    idle = event.idle_seconds
    re_read = event.re_read_tokens
    if idle is None or idle <= _TTL_BREAK_SECONDS:
        return None
    if re_read is None or re_read <= 0:
        return None
    minutes = int(idle // 60)
    duration = f"{minutes} min" if minutes >= 1 else f"{int(idle)} s"
    return f"TTL break: {duration} idle → {_format_tokens(re_read)} re-read"


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
