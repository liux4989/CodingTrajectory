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
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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


class CacheBreakRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_id: str
    # ttl_confirmed: idle gap exceeds the vendor's prompt-cache TTL max
    #   (OpenAI >=600s, Anthropic >=300s) — cache evicted by age.
    # ttl_likely: idle in the ambiguous band (OpenAI 300–600s); could be TTL
    #   but not certain.
    # effort_switch: an observed effort change aligns with an observed drop in
    #   cache-hit tokens across the turn boundary. It overrides TTL when both
    #   align with the same measured cache loss.
    # model_switch: the dominant (provider, model) changed across the turn
    #   boundary, so the prefix was re-processed under a new cache key.
    # unattributed: a measured cache-hit loss (boundary or intra-turn) with no
    #   aligned effort change, no model switch, and no TTL-sized idle gap.
    #   Surfaced instead of dropped so the miss is visible - the cause (e.g. a
    #   mid-turn cache invalidation, a cold start, a backend that doesn't couple
    #   cache to effort like glm-5.2, tool reorder/removal, nondeterministic
    #   enumeration, system-prompt churn, or a proxy dropping session affinity)
    #   is simply unknown.
    type: Literal[
        "ttl_confirmed", "ttl_likely", "effort_switch", "model_switch", "unattributed"
    ]
    idle_seconds: float
    re_read_tokens: int
    cached_after_tokens: int | None = None
    est_cost_usd: float | None = None
    # Populated only for a confirmed effort_switch — the resolved effort levels
    # from the aligned ``effort_changed`` observation. ``effort_from`` is ``None``
    # on Claude Code's first ``/effort`` switch (baseline unknown); always set
    # for Codex (per-turn effort, first turn establishes the baseline).
    effort_from: str | None = None
    effort_to: str | None = None
    # Populated only for a model_switch — the dominant model identities that
    # bracket the cache-key change. Either may be ``None`` on the first turn
    # after a reset where the prior context is unknown.
    model_from: str | None = None
    model_to: str | None = None


class CacheBreakSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = 0
    # Effort-independent static prefix that survives cache misses; the value
    # cached prefixes collapse toward on a break. ``None`` only when no turn
    # in the session reported a cached footprint (no cache accounting at all).
    floor_tokens: int | None = None
    total_re_read_tokens: int = 0
    estimated_waste_usd: float | None = None
    by_type: dict[str, int] = Field(default_factory=dict)
    events: list[CacheBreakRecord] = Field(default_factory=list)


class ContextSessionSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    role: str
    label: str
    relationship: str | None = None
    parent_session_id: str | None = None
    used_tokens: TokenEvidence | None = None
    used_percent: float | None = None
    token_cost: CostEvidence | None = None


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
    active_session_id: str
    vendor: str
    model: str | None = None
    context_window_tokens: TokenEvidence | None = None
    used_tokens: TokenEvidence | None = None
    used_percent: float | None = None
    token_cost: CostEvidence | None = None
    categories: list[ContextCategory]
    provider_usage_buckets: list[ContextCategory]
    session_sections: list[ContextSessionSection] = Field(default_factory=list)
    expensive_items: list[ExpensiveItem] = Field(default_factory=list)
    events: list[ContextEvent]
    compaction: CompactionSummary | None = None
    cache_breaks: CacheBreakSummary | None = None
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

    try:
        projection = build_projection(args.session_id, turn_id=args.turn_id)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
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
    stats, overview, usage, tool_usage = _load_projection_inputs(session_id, run)

    selected_stats = _selected_session_stats(stats, session_id)
    active_session_id = str(
        selected_stats.get("session_id") or selected_stats.get("id") or session_id
    )
    selected_usage = _selected_session_usage(usage, active_session_id)
    session_sections = _project_session_sections(stats, usage)

    vendor = str(
        selected_stats.get("vendor")
        or stats.get("vendor")
        or _overview_vendor(overview)
        or "unknown"
    )
    model = selected_stats.get("model") or {}
    model_name = _optional_text(model.get("name"))
    categories = _project_categories(selected_stats)
    provider_usage_buckets = _project_provider_usage_buckets(selected_stats)
    expensive_items = _project_expensive_items(tool_usage, session_id=active_session_id)
    events = [
        *_category_events(categories),
        *_trajectory_events(
            overview,
            selected_usage,
            tool_usage,
            turn_id=turn_id,
            session_id=active_session_id,
        ),
    ]
    warnings = [
        str(item)
        for item in selected_stats.get("warnings") or stats.get("warnings") or []
    ]
    if len(session_sections) > 1:
        warnings.append(
            "This session id resolves to a session graph; context-window values are scoped to the active session, not the graph aggregate."
        )
    warnings.extend(_projection_warnings(events))
    if turn_id and not any(event.turn_id == turn_id for event in events):
        raise SystemExit(f"turn not found in session overview: {turn_id}")

    compaction = _project_compaction(selected_stats)
    cache_breaks = _project_cache_breaks(
        selected_usage, vendor=vendor, compaction=compaction
    )

    context = selected_stats.get("context") or {}
    reported_context_window = model.get("context_window") or model.get(
        "context_window_tokens"
    )
    # Core resolves the context window in ``session.stats`` (reported value or
    # the merged static/live model catalog) — the dashboard reads it directly.
    context_window = reported_context_window
    used_tokens = _optional_int(context.get("used") or context.get("used_tokens"))
    used_percent = _optional_float(context.get("pct") or context.get("used_percent"))
    if used_percent is None and used_tokens is not None and context_window:
        used_percent = round((used_tokens / context_window) * 100, 1)
    # Session-total cost is the pricing SoT's estimate, emitted by
    # ``session.usage`` as ``cost`` + ``pricing`` (source/effective_date).
    reported_cost = _optional_float(selected_usage.get("cost"))
    pricing = selected_usage.get("pricing") or {}
    return ContextWindowProjection(
        session_id=str(stats.get("id") or session_id),
        active_session_id=active_session_id,
        vendor=vendor,
        model=model_name,
        context_window_tokens=_token_evidence(
            context_window,
            confidence="structural",
            source="ct session stats:model.context_window",
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
                confidence=str(pricing.get("confidence") or "estimated"),
                source=str(pricing.get("source") or "ct session usage:cost"),
                effective_date=pricing.get("effective_date"),
            )
            if reported_cost is not None
            else None
        ),
        categories=sorted(categories, key=_category_sort_key, reverse=True),
        provider_usage_buckets=sorted(
            provider_usage_buckets,
            key=_category_sort_key,
            reverse=True,
        ),
        session_sections=session_sections,
        expensive_items=expensive_items,
        events=events,
        compaction=compaction,
        cache_breaks=cache_breaks,
        warnings=_dedupe(warnings),
    )


def _load_projection_inputs(
    session_id: str,
    run: Callable[[list[str]], dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    # ``ct api batch`` returns internal service shapes, while the dedicated
    # commands apply CLI display compaction. Keep that compatibility boundary
    # local to this plugin instead of importing CLI-private helpers.
    requests = [
        {
            "id": name,
            "method": method,
            "params": {"session_id": session_id},
        }
        for name, method in (
            ("stats", "session.stats"),
            ("overview", "session.overview"),
            ("usage", "session.usage"),
            ("tool_usage", "session.tool_usage"),
        )
    ]
    payload = run(
        [
            "api",
            "batch",
            "--global-scope",
            "--requests",
            json.dumps(requests),
        ]
    )
    rows = {
        str(item.get("id")): item
        for item in payload.get("items") or []
        if isinstance(item, dict)
    }
    stats = _required_batch_result(rows, "stats")
    overview = _required_batch_result(rows, "overview")
    usage = _required_batch_result(rows, "usage")
    tool_usage = _required_batch_result(rows, "tool_usage")
    if not isinstance(stats.get("root_session_id"), str):
        raise RuntimeError("ct api request returned invalid result: stats")
    if not isinstance(overview.get("root_session_id"), str):
        raise RuntimeError("ct api request returned invalid result: overview")
    if not isinstance(usage.get("session_id"), str) or not isinstance(
        usage.get("total_usage"), dict
    ):
        raise RuntimeError("ct api request returned invalid result: usage")
    if not isinstance(tool_usage.get("root_session_id"), str):
        raise RuntimeError("ct api request returned invalid result: tool_usage")
    return (
        _compact_stats_api(stats),
        _compact_overview_api(overview),
        _compact_usage_api(usage),
        tool_usage,
    )


def _required_batch_result(
    rows: dict[str, dict[str, Any]], request_id: str
) -> dict[str, Any]:
    row = rows.get(request_id)
    if row is None:
        raise RuntimeError(f"ct api batch omitted response: {request_id}")
    if not row.get("ok"):
        error = row.get("error") or {}
        message = error.get("message") if isinstance(error, dict) else error
        raise RuntimeError(str(message or f"ct api request failed: {request_id}"))
    result = row.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"ct api request returned invalid result: {request_id}")
    return result


def _compact_stats_api(payload: dict[str, Any]) -> dict[str, Any]:
    model = payload.get("model") or {}
    context = payload.get("context_window") or {}
    runtime = payload.get("runtime") or {}
    messages = payload.get("messages") or {}
    compaction = payload.get("compaction") or {}
    compact = _drop_none(
        {
            "id": payload.get("root_session_id") or payload.get("session_id"),
            "session_id": payload.get("session_id"),
            "role": payload.get("role"),
            "relationship": payload.get("relationship"),
            "parent": payload.get("parent_session_id"),
            "agent_name": payload.get("agent_name"),
            "title": payload.get("title"),
            "vendor": payload.get("vendor"),
            "model": _drop_none(
                {
                    "name": model.get("name"),
                    "context_window": model.get("context_window_tokens"),
                }
            )
            or None,
            "context": _drop_none(
                {
                    "used": context.get("used_tokens"),
                    "pct": context.get("used_percent"),
                    "categories": [
                        _compact_context_category(item)
                        for item in context.get("categories") or []
                    ]
                    or None,
                }
            )
            or None,
            "provider_usage_buckets": [
                _compact_context_category(item)
                for item in payload.get("provider_usage_buckets") or []
            ]
            or None,
            "runtime": _compact_stats_runtime(runtime),
            "compaction": _compact_compaction(compaction),
            "messages": _drop_none(
                {
                    "user": messages.get("user"),
                    "assistant": messages.get("assistant"),
                    "developer": messages.get("developer"),
                    "tools": messages.get("tool_outputs"),
                    "reasoning": messages.get("reasoning_items"),
                    "compacted": messages.get("compacted_contexts"),
                }
            )
            or None,
            "usage": _compact_usage_tokens(payload.get("usage"), include_cost=False),
            "billed_token_usage": _compact_usage_tokens(
                payload.get("billed_token_usage"), include_cost=False
            ),
            "warnings": payload.get("warnings") or None,
        }
    )
    if payload.get("scope"):
        compact["scope"] = payload["scope"]
    compact["sessions"] = [
        _compact_stats_api(item)
        for item in payload.get("sessions") or []
        if isinstance(item, dict)
    ] or None
    return _drop_none(compact)


def _compact_stats_runtime(runtime: dict[str, Any]) -> dict[str, Any] | None:
    return (
        _drop_none(
            {
                "status": runtime.get("status"),
                "start": runtime.get("started_at"),
                "end": runtime.get("ended_at"),
                "execution_seconds": runtime.get("execution_seconds"),
                "model_active_seconds": runtime.get("model_active_seconds"),
                "processed_tokens_per_second": runtime.get(
                    "processed_tokens_per_second"
                ),
                "wait_seconds": runtime.get("wait_seconds"),
                "turns": runtime.get("turns"),
                "items": runtime.get("items"),
                "tools": runtime.get("tool_calls"),
                "failed_tools": runtime.get("failed_tool_calls") or None,
                "subagents": runtime.get("subagent_sessions"),
                "compactions": runtime.get("compactions"),
                "interrupted_turns": runtime.get("interrupted_turns") or None,
                "rollbacks": runtime.get("rollbacks") or None,
                "average_ttft_ms": runtime.get("average_time_to_first_token_ms"),
            }
        )
        or None
    )


def _compact_context_category(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return _drop_none(
        {
            "key": value.get("key"),
            "label": value.get("label"),
            "tokens": value.get("tokens"),
            "usage": value.get("allocated_usage"),
            "estimated_cost": value.get("estimated_cost"),
            "pct": value.get("percent"),
            "chars": value.get("observed_chars"),
            "items": value.get("items"),
            "confidence": value.get("confidence"),
            "source": value.get("source"),
            "children": [
                _compact_context_category(child)
                for child in value.get("children") or []
            ]
            or None,
        }
    )


def _compact_overview_api(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": payload.get("root_session_id"),
        "sessions": [
            _drop_none(
                {
                    "id": session.get("session_id"),
                    "relationship": _compact_relationship(session.get("relationship")),
                    "vendor": session.get("vendor"),
                    "status": session.get("status"),
                    "agent": session.get("agent_name"),
                    "cwd": session.get("cwd"),
                    "compactions": session.get("compactions"),
                    "turns": [
                        _drop_none(
                            {
                                "id": turn.get("turn_id"),
                                "status": turn.get("status"),
                                "request": _compact_request(turn.get("user_request")),
                                "activity": [
                                    _compact_activity(activity)
                                    for activity in turn.get("activity") or []
                                ]
                                or None,
                                "teammate_summary": turn.get("teammate_summary"),
                                "items": (
                                    (turn.get("refs") or {}).get("item_ids")
                                    if isinstance(turn.get("refs"), dict)
                                    else None
                                ),
                            }
                        )
                        for turn in session.get("turns") or []
                        if isinstance(turn, dict)
                    ],
                }
            )
            for session in payload.get("sessions") or []
            if isinstance(session, dict)
        ],
    }


def _compact_relationship(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if value.get("role") == "main":
        return _drop_none({"role": "main", "forks": value.get("forked_session_ids")})
    return (
        _drop_none(
            {
                "type": value.get("relationship"),
                "parent": value.get("parent_session_id"),
                "forks": value.get("forked_session_ids"),
            }
        )
        or None
    )


def _compact_request(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return (
        _drop_none(
            {
                "text": value.get("content")
                or value.get("summary")
                or value.get("text"),
                "source": value.get("source"),
                "type": value.get("type")
                if value.get("type") not in {None, "message"}
                else None,
            }
        )
        or None
    )


def _compact_activity(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if "compaction" in value:
        return _drop_none(
            {
                "compaction": value.get("compaction"),
                "mechanism": value.get("mechanism"),
                "summary": value.get("summary"),
                "trigger": value.get("trigger"),
                "pre": value.get("pre_tokens"),
                "post": value.get("post_tokens"),
                "dropped": value.get("dropped_tokens"),
            }
        )
    if "tool" in value:
        compact = {
            "tool": value.get("tool"),
            "count": value.get("count"),
            "status": value.get("status"),
        }
        for key in ("cmd", "path", "query", "url", "text"):
            if value.get(key) is not None:
                compact[key] = value[key]
        for key in ("paths", "queries", "urls", "targets"):
            if value.get(key) is not None:
                compact[key] = value[key]
        if value.get("item_ids") is not None:
            compact["item_ids"] = value["item_ids"]
        if compact.get("count") == 1:
            compact.pop("count", None)
        return _drop_none(compact)
    if "text" in value:
        return _drop_none(
            {"text": value.get("text"), "item_ids": value.get("item_ids")}
        )
    if "teammate_summary" in value:
        return {"teammate_summary": value.get("teammate_summary")}
    return value


def _compact_usage_api(payload: dict[str, Any]) -> dict[str, Any]:
    runtime = payload.get("runtime") or {}
    effort_changes = payload.get("effort_changes") or {}
    return _drop_none(
        {
            "id": payload.get("session_id"),
            "scope": payload.get("scope"),
            "extra_billing": payload.get("extra_billing"),
            "runtime": _drop_none(
                {
                    "status": runtime.get("status"),
                    "start": runtime.get("started_at"),
                    "end": runtime.get("ended_at"),
                    "execution_seconds": runtime.get("execution_seconds"),
                    "model_active_seconds": runtime.get("model_active_seconds"),
                    "processed_tokens_per_second": runtime.get(
                        "processed_tokens_per_second"
                    ),
                    "wait_seconds": runtime.get("wait_seconds"),
                }
            )
            or None,
            "usage": _compact_usage_tokens(payload.get("total_usage")),
            "cost": _evidence_value(payload.get("estimated_cost")),
            "pricing": _evidence_pricing(payload.get("estimated_cost")),
            "models": _compact_usage_models(payload.get("models")),
            "compaction": _compact_compaction(payload.get("compaction")),
            "effort_changes": _compact_effort_changes(effort_changes),
            "turns": [
                _compact_usage_turn(turn)
                for turn in payload.get("turns") or []
                if isinstance(turn, dict)
            ],
            "sessions": [
                _compact_usage_session(session)
                for session in payload.get("sessions") or []
                if isinstance(session, dict)
            ]
            or None,
            "warnings": payload.get("warnings") or None,
        }
    )


def _compact_usage_tokens(
    value: Any, *, include_cost: bool = True
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return (
        _drop_none(
            {
                "prompt": value.get("prompt_tokens"),
                "uncached_prompt": value.get("uncached_prompt_tokens"),
                "cached_prompt": value.get("cached_prompt_tokens"),
                "cache_write": value.get("cache_write_tokens"),
                "completion": value.get("completion_tokens"),
                "reasoning": value.get("reasoning_tokens"),
                "reported_total": value.get("reported_total_tokens"),
                "processed": value.get("processed_tokens"),
                "prompt_completion": value.get("prompt_completion_tokens"),
                "cost": value.get("cost_usd") if include_cost else None,
            }
        )
        or None
    )


def _compact_usage_turn(value: dict[str, Any]) -> dict[str, Any]:
    runtime = value.get("runtime") or {}
    return _drop_none(
        {
            "id": value.get("turn_id"),
            "session": value.get("session_id"),
            "runtime": _drop_none(
                {
                    "start": runtime.get("started_at"),
                    "end": runtime.get("ended_at"),
                    "execution_seconds": runtime.get("execution_seconds"),
                    "model_active_seconds": runtime.get("model_active_seconds"),
                    "processed_tokens_per_second": runtime.get(
                        "processed_tokens_per_second"
                    ),
                    "wait_before_seconds": runtime.get("wait_before_seconds"),
                }
            )
            or None,
            "usage": _compact_usage_tokens(value.get("usage")),
            "cost": _evidence_value(value.get("estimated_cost")),
            "pricing": _evidence_pricing(value.get("estimated_cost")),
            "cache_break_waste_usd": value.get("cache_break_waste_usd"),
            "cache_break_re_read_tokens": value.get("cache_break_re_read_tokens"),
            "cache_boundary_loss_tokens": value.get("cache_boundary_loss_tokens"),
            "cache_first_call_cached_tokens": value.get(
                "cache_first_call_cached_tokens"
            ),
            "cache_intra_turn_loss_tokens": value.get("cache_intra_turn_loss_tokens"),
            "cache_intra_turn_waste_usd": value.get("cache_intra_turn_waste_usd"),
        }
    )


def _compact_usage_session(value: dict[str, Any]) -> dict[str, Any]:
    runtime = value.get("runtime") or {}
    return _drop_none(
        {
            "id": value.get("session_id"),
            "role": value.get("role"),
            "relationship": value.get("relationship"),
            "parent": value.get("parent_session_id"),
            "agent_name": value.get("agent_name"),
            "title": value.get("title"),
            "runtime": _drop_none(
                {
                    "status": runtime.get("status"),
                    "start": runtime.get("started_at"),
                    "end": runtime.get("ended_at"),
                    "execution_seconds": runtime.get("execution_seconds"),
                    "model_active_seconds": runtime.get("model_active_seconds"),
                    "processed_tokens_per_second": runtime.get(
                        "processed_tokens_per_second"
                    ),
                    "wait_seconds": runtime.get("wait_seconds"),
                    "turns": runtime.get("turns"),
                    "items": runtime.get("items"),
                    "tools": runtime.get("tool_calls"),
                    "failed_tools": runtime.get("failed_tool_calls") or None,
                }
            )
            or None,
            "usage": _compact_usage_tokens(value.get("total_usage")),
            "cost": _evidence_value(value.get("estimated_cost")),
            "pricing": _evidence_pricing(value.get("estimated_cost")),
            "models": _compact_usage_models(value.get("models")),
            "effort_changes": _compact_effort_changes(
                value.get("effort_changes") or {}
            ),
            "turns": [
                _compact_usage_turn(turn)
                for turn in value.get("turns") or []
                if isinstance(turn, dict)
            ],
        }
    )


def _compact_usage_models(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    rows = [
        _drop_none(
            {
                "provider": model.get("provider"),
                "model": model.get("model"),
                "turns": model.get("turns"),
                "model_active_seconds": model.get("model_active_seconds"),
                "processed_tokens_per_second": model.get("processed_tokens_per_second"),
                "usage": _compact_usage_tokens(model.get("usage")),
                "cost": _evidence_value(model.get("estimated_cost")),
                "pricing": _evidence_pricing(model.get("estimated_cost")),
            }
        )
        for model in value
        if isinstance(model, dict)
    ]
    return rows or None


def _compact_effort_changes(value: dict[str, Any]) -> dict[str, Any]:
    return _drop_none(
        {
            "count": value.get("count") or 0,
            "events": [
                _drop_none(
                    {
                        "timestamp": event.get("timestamp"),
                        "from": event.get("effort_from"),
                        "to": event.get("effort_to"),
                    }
                )
                for event in value.get("events") or []
                if isinstance(event, dict)
            ]
            or None,
        }
    )


def _compact_compaction(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return (
        _drop_none(
            {
                "count": value.get("count"),
                "cumulative_dropped": value.get("cumulative_dropped_tokens"),
                "last": _compact_compaction_event(value.get("last")),
                "events": [
                    _compact_compaction_event(event)
                    for event in value.get("events") or []
                    if isinstance(event, dict)
                ]
                or None,
            }
        )
        or None
    )


def _compact_compaction_event(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return (
        _drop_none(
            {
                "mechanism": value.get("mechanism"),
                "timestamp": value.get("timestamp"),
                "trigger": value.get("trigger"),
                "pre": value.get("pre_tokens"),
                "post": value.get("post_tokens"),
                "dropped": value.get("dropped_tokens"),
            }
        )
        or None
    )


def _evidence_value(value: Any) -> float | None:
    if not isinstance(value, dict):
        return None
    raw = value.get("value_usd")
    return (
        float(raw)
        if isinstance(raw, int | float) and not isinstance(raw, bool)
        else None
    )


def _evidence_pricing(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return (
        _drop_none(
            {
                "confidence": value.get("confidence"),
                "source": value.get("source"),
                "effective_date": value.get("effective_date"),
            }
        )
        or None
    )


def _drop_none(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


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
    ]
    teaser = _cache_breaks_teaser(projection.cache_breaks)
    if teaser:
        lines.append(teaser)
    lines.extend(
        [
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
    )
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

    breaks_by_turn = {
        record.turn_id: record
        for record in (
            projection.cache_breaks.events if projection.cache_breaks else []
        )
    }
    current_group: tuple[str, str | None] | None = None
    for event in projection.events:
        group = (event.group, event.turn_id)
        is_turn_start = group != current_group
        current_group = group
        if is_turn_start:
            lines.extend(["", _group_label(event)])
        delta = _format_delta(event.tokens.value) if event.tokens else "       -"
        summary = _one_line(event.summary or event.label, 74)
        # Show the break flag once per turn, on its first (user_input) event.
        flag = (
            _cache_break_flag(breaks_by_turn.get(event.turn_id))
            if is_turn_start
            else None
        )
        line = f"  {event.category:<20} {delta:>8}  {summary}"
        if flag:
            line += f"  {flag}"
        lines.append(line)

    if projection.cache_breaks and projection.cache_breaks.events:
        cb = projection.cache_breaks
        lines.extend(["", "Cache breaks"])
        lines.append(
            f"   #  {'turn':<10}  {'type':<15}  {'idle':>7}  "
            f"{'re-read':>9}  {'est. cost':>11}  cause"
        )
        for index, record in enumerate(cb.events, start=1):
            if record.effort_to:
                effort = (
                    f"{record.effort_from}→{record.effort_to}"
                    if record.effort_from
                    else f"→{record.effort_to}"
                )
            elif record.model_to:
                effort = (
                    f"{record.model_from}→{record.model_to}"
                    if record.model_from
                    else f"→{record.model_to}"
                )
            else:
                effort = "-"
            lines.append(
                f"  {index:>2}  {record.turn_id[:10]:<10}  {record.type:<15}  "
                f"{_format_idle(record.idle_seconds):>7}  "
                f"{_format_tokens(record.re_read_tokens):>9}  "
                f"{_format_cost(record.est_cost_usd):>11}  {effort}"
            )
        type_summary = ", ".join(
            f"{cb.by_type.get(key, 0)} {key}"
            for key in (
                "effort_switch",
                "model_switch",
                "ttl_confirmed",
                "ttl_likely",
                "unattributed",
            )
            if cb.by_type.get(key)
        )
        lines.append(
            f"  total: {cb.count} breaks · "
            f"{_format_tokens(cb.total_re_read_tokens)} re-read tokens · "
            f"{_format_cost(cb.estimated_waste_usd)} wasted"
            + (f" — {type_summary}" if type_summary else "")
        )

    if projection.compaction and projection.compaction.events:
        lines.extend(["", "Compaction timeline"])
        for index, event in enumerate(projection.compaction.events, start=1):
            mechanism = event.mechanism or "-"
            trigger = event.trigger or "-"
            pre = _format_tokens(event.pre_tokens)
            post = _format_tokens(event.post_tokens)
            delta = (
                f"{pre} -> {post}"
                if event.pre_tokens is not None and event.post_tokens is not None
                else "-"
            )
            dropped = (
                _format_tokens(event.dropped_tokens)
                if event.dropped_tokens is not None
                else "-"
            )
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


def _selected_session_stats(stats: dict[str, Any], session_id: str) -> dict[str, Any]:
    sessions = [item for item in stats.get("sessions") or [] if isinstance(item, dict)]
    if not sessions:
        return stats
    for item in sessions:
        if session_id in {str(item.get("id")), str(item.get("session_id"))}:
            return item
    for item in sessions:
        if item.get("role") == "main":
            return item
    return sessions[0]


def _selected_session_usage(usage: dict[str, Any], session_id: str) -> dict[str, Any]:
    sessions = [item for item in usage.get("sessions") or [] if isinstance(item, dict)]
    if not sessions:
        return usage
    for item in sessions:
        if session_id in {str(item.get("id")), str(item.get("session_id"))}:
            return item
    for item in sessions:
        if item.get("role") == "main":
            return item
    return sessions[0]


def _project_session_sections(
    stats: dict[str, Any],
    usage: dict[str, Any],
) -> list[ContextSessionSection]:
    usage_by_id = {
        str(item.get("id") or item.get("session_id")): item
        for item in usage.get("sessions") or []
        if isinstance(item, dict)
    }
    sections: list[ContextSessionSection] = []
    for item in stats.get("sessions") or []:
        if not isinstance(item, dict):
            continue
        session_id = str(item.get("session_id") or item.get("id") or "")
        if not session_id:
            continue
        usage_item = (
            usage_by_id.get(session_id) or usage_by_id.get(str(item.get("id"))) or {}
        )
        context = item.get("context") or {}
        pricing = usage_item.get("pricing") or {}
        cost = _optional_float(usage_item.get("cost"))
        sections.append(
            ContextSessionSection(
                session_id=session_id,
                role=str(item.get("role") or item.get("relationship") or "session"),
                label=str(
                    item.get("title")
                    or item.get("agent_name")
                    or item.get("relationship")
                    or item.get("role")
                    or session_id[:8]
                ),
                relationship=_optional_text(item.get("relationship")),
                parent_session_id=_optional_text(item.get("parent")),
                used_tokens=_token_evidence(
                    _optional_int(context.get("used")),
                    confidence="exact_usage",
                    source="ct session stats:sessions[].context.used",
                ),
                used_percent=_optional_float(context.get("pct")),
                token_cost=(
                    CostEvidence(
                        value_usd=cost,
                        confidence=str(pricing.get("confidence") or "estimated"),
                        source=str(pricing.get("source") or "ct session usage:cost"),
                        effective_date=pricing.get("effective_date"),
                    )
                    if cost is not None
                    else None
                ),
            )
        )
    return sections


def _category_sort_key(category: ContextCategory) -> tuple[float, int]:
    return (
        category.estimated_cost.value_usd if category.estimated_cost else 0.0,
        category.tokens.value,
    )


def _project_compaction(stats: dict[str, Any]) -> CompactionSummary | None:
    """Lift the compaction timeline from ``ct session stats`` or ``session.usage`` JSON.

    ``stats`` carries ``compaction`` (count, cumulative dropped, last event,
    and the full ``events`` list) when the session has compacted; the field is
    absent or ``None`` otherwise.

    Form-agnostic: the CLI display path (``compact_stats_payload`` /
    ``compact_payload``) renames ``cumulative_dropped_tokens`` ->
    ``cumulative_dropped``, ``pre_tokens`` -> ``pre``, etc. The raw
    ``ct api call session.usage`` / ``session.stats`` api emits the long
    names. Read both so this works on either form - the cache-breaks
    aggregate calls this on raw-api ``session.usage`` data.
    """
    compaction = stats.get("compaction")
    if not isinstance(compaction, dict) or not compaction.get("count"):
        return None
    events = [
        CompactionEventRecord(
            timestamp=str(event.get("timestamp") or ""),
            mechanism=str(event.get("mechanism") or ""),
            trigger=_optional_text(event.get("trigger")),
            pre_tokens=_optional_int(event.get("pre") or event.get("pre_tokens")),
            post_tokens=_optional_int(event.get("post") or event.get("post_tokens")),
            dropped_tokens=_optional_int(
                event.get("dropped") or event.get("dropped_tokens")
            ),
        )
        for event in compaction.get("events") or []
        if isinstance(event, dict) and event.get("timestamp")
    ]
    return CompactionSummary(
        count=int(compaction.get("count") or 0),
        cumulative_dropped_tokens=_optional_int(
            compaction.get("cumulative_dropped")
            or compaction.get("cumulative_dropped_tokens")
        ),
        events=events,
    )


def _turn_model_key(turn: dict[str, Any]) -> str | None:
    """Stable ``provider/model`` identity for a turn, for cache-key-change
    detection. Reads the ``provider``/``model`` fields core emits per turn in
    ``session.usage``. Returns ``None`` when either is absent (stale/CLI form),
    in which case model-switch attribution is skipped for that turn."""
    provider = _optional_text(turn.get("provider"))
    model = _optional_text(turn.get("model"))
    if not provider or not model:
        return None
    return f"{provider}/{model}"


# Per-turn miss count at or below this is cache-breakpoint granularity noise.
# Applied only to ``unattributed`` (the no-cause bucket); the attributed types
# already require a confirming observation so ``re_read > 0`` suffices.
NOISE_FLOOR_TOKENS = 1024


def _project_cache_breaks(
    usage: dict[str, Any],
    *,
    vendor: str,
    compaction: CompactionSummary | None = None,
    require_effort_changes: bool = True,
) -> CacheBreakSummary | None:
    """Classify measured turn-boundary cache losses into supported causes.

    A row needs both an observed cache-hit reduction from the prior turn's
    final request to this turn's first request and either an aligned effort
    change or a TTL-sized idle gap. Post-compaction turns are skipped because
    their loss is a compaction signature, not an effort/TTL cache break.

    ``effort_changes`` must be present in ``usage`` (core always emits it, even
    as ``{"count": 0}`` when the session never changed effort) — its absence
    means the installed ct is stale/incomplete and lacks the
    effort_change-ingestion capability. Throw rather than silently falling back
    to the pure heuristic, so a stale install is loud instead of producing
    plausible-but-unconfirmed break labels.
    """
    if require_effort_changes and "effort_changes" not in usage:
        raise SystemExit(
            "ct does not surface effort_changes in session.usage — the install "
            "is stale or incomplete (Direction 1 effort-change ingestion is "
            "missing). Reinstall ct editable and retry:\n"
            "  uv tool install --force --editable packages/cli "
            "--with-editable packages/core --with-editable "
            "packages/plugins/code_time --with-editable packages/plugins/dashboard"
        )
    turns = [t for t in (usage.get("turns") or []) if isinstance(t, dict)]
    if not turns:
        return None

    skip_turns = _post_compaction_turn_ids(usage, compaction)
    effort_change_by_turn = _effort_changed_turns(usage)
    ttl_confirmed_s, ttl_likely_min_s = _vendor_ttl_thresholds(vendor)
    records: list[CacheBreakRecord] = []
    prev_model_key: str | None = None
    for turn in turns:
        turn_id = str(turn.get("id") or turn.get("turn_id") or "")
        if turn_id in skip_turns:
            continue
        runtime = turn.get("runtime") or {}
        idle = _optional_float(runtime.get("wait_before_seconds"))
        re_read = _optional_int(turn.get("cache_boundary_loss_tokens")) or 0
        model_key = _turn_model_key(turn)
        if idle is None or re_read <= 0:
            prev_model_key = model_key or prev_model_key
            continue
        cached_after = _optional_int(turn.get("cache_first_call_cached_tokens"))
        # Cause attribution, strongest first. A confirmed effort change
        # overrides everything (an observed fact, not a heuristic). A model
        # switch overrides TTL: a new cache key re-bills the whole prefix
        # regardless of idle. TTL applies when nothing else explains the loss.
        # Anything left is ``unattributed`` — the bad-behavior bucket — filtered
        # by a noise floor so breakpoint-granularity churn does not drown the signal.
        confirmed = effort_change_by_turn.get(turn_id)
        if confirmed is not None:
            break_type = "effort_switch"
            effort_from, effort_to = confirmed
            model_from = None
            model_to = None
        elif (
            prev_model_key is not None
            and model_key is not None
            and model_key != prev_model_key
        ):
            break_type = "model_switch"
            effort_from = None
            effort_to = None
            model_from = prev_model_key
            model_to = model_key
        else:
            break_type = (
                "ttl_confirmed"
                if idle >= ttl_confirmed_s
                else "ttl_likely"
                if idle >= ttl_likely_min_s
                else None
            )
            if break_type is None:
                # A measured boundary loss with no aligned effort change, no model
                # switch, and no TTL-sized idle - surface it as ``unattributed``
                # rather than dropping the miss. Covers glm-5.2 (idle ~0 by
                # construction, cache not coupled to effort) and the bad-behavior
                # class (tool reorder/removal, nondeterministic enumeration,
                # system-prompt churn, proxy session-affinity loss). Filter
                # breakpoint-granularity noise so the signal stays meaningful.
                if re_read <= NOISE_FLOOR_TOKENS:
                    prev_model_key = model_key or prev_model_key
                    continue

                break_type = "unattributed"
            effort_from = None
            effort_to = None
            model_from = None
            model_to = None
        prev_model_key = model_key or prev_model_key
        # Core prices the observable boundary cache-hit loss; the dashboard
        # only assigns a supported cause to that measured gap.
        waste = _optional_float(turn.get("cache_break_waste_usd"))
        records.append(
            CacheBreakRecord(
                turn_id=turn_id,
                type=break_type,
                idle_seconds=idle,
                re_read_tokens=re_read,
                cached_after_tokens=cached_after,
                est_cost_usd=waste,
                effort_from=effort_from,
                effort_to=effort_to,
                model_from=model_from,
                model_to=model_to,
            )
        )
    # Intra-turn collapses: a cache-hit drop between two provider calls *inside*
    # the same turn (below the turn-boundary detector's resolution). Emit one
    # ``unattributed`` record per turn whose largest intra-turn drop is > 0. This
    # is independent of the inter-turn gate above (``idle``/``re_read``), so a
    # turn with no boundary loss but a mid-turn invalidation still surfaces.
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        turn_id = str(turn.get("id") or turn.get("turn_id") or "")
        if not turn_id or turn_id in skip_turns:
            continue
        intra_loss = _optional_int(turn.get("cache_intra_turn_loss_tokens")) or 0
        if intra_loss <= 0:
            continue
        records.append(
            CacheBreakRecord(
                turn_id=turn_id,
                type="unattributed",
                # Intra-call gap; not TTL-driven. ``wait_before_seconds`` only
                # covers the inter-turn think-time, so report the within-turn
                # collapse with a zero idle marker.
                idle_seconds=0.0,
                re_read_tokens=intra_loss,
                cached_after_tokens=None,
                est_cost_usd=_optional_float(turn.get("cache_intra_turn_waste_usd")),
                effort_from=None,
                effort_to=None,
            )
        )
    if not records:
        return None
    by_type: dict[str, int] = {}
    total_re_read = 0
    total_waste = 0.0
    has_waste = False
    for record in records:
        by_type[record.type] = by_type.get(record.type, 0) + 1
        total_re_read += record.re_read_tokens
        if record.est_cost_usd is not None:
            total_waste += record.est_cost_usd
            has_waste = True
    return CacheBreakSummary(
        count=len(records),
        floor_tokens=None,
        total_re_read_tokens=total_re_read,
        estimated_waste_usd=round(total_waste, 4) if has_waste else None,
        by_type=by_type,
        events=records,
    )


def _vendor_ttl_thresholds(vendor: str) -> tuple[float, float]:
    """Return ``(confirmed_seconds, likely_min_seconds)`` for prompt-cache TTL.

    OpenAI's automatic prompt cache TTL is 5–10 min, so >=600s is confirmed
    and the 300–600s band is ambiguous (``ttl_likely``). Anthropic's explicit
    ``cache_control`` TTL is 5 min, so >=300s is confirmed. Other vendors fall
    back to the conservative 300s threshold.
    """
    normalized = (vendor or "").strip().lower()
    if normalized in {"anthropic", "claude", "claude-code", "claude_code"}:
        return (300.0, 300.0)
    if normalized in {"openai", "codex", "codex_cli", "openai-codex"}:
        return (600.0, 300.0)
    return (300.0, 300.0)


def _parse_iso_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _post_compaction_turn_ids(
    usage: dict[str, Any],
    compaction: CompactionSummary | None,
) -> set[str]:
    """Turn ids whose start immediately follows a compaction event.

    Such turns re-read the freshly compacted context — a compaction signature,
    not an effort/TTL cache break — so the classifier skips them.
    """
    if compaction is None or not compaction.events:
        return set()
    compaction_ts = [
        ts
        for ts in (_parse_iso_timestamp(event.timestamp) for event in compaction.events)
        if ts is not None
    ]
    if not compaction_ts:
        return set()
    starts: list[tuple[datetime, str]] = []
    for turn in usage.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        runtime = turn.get("runtime") or {}
        start = _parse_iso_timestamp(runtime.get("start") or runtime.get("started_at"))
        if start is not None and (turn.get("id") or turn.get("turn_id")):
            starts.append((start, str(turn.get("id") or turn.get("turn_id"))))
    if not starts:
        return set()
    starts.sort()
    skip: set[str] = set()
    for comp_ts in compaction_ts:
        for start, turn_id in starts:
            if start >= comp_ts:
                skip.add(turn_id)
                break
    return skip


def _effort_changed_turns(
    usage: dict[str, Any],
) -> dict[str, tuple[str | None, str | None]]:
    """Map each ``effort_changed`` observation to the turn that first uses the
    new effort.

    Prefer a usage turn whose runtime contains the observation timestamp. This
    matters for Codex, where ``turn_context`` can be written a few
    milliseconds after the turn's lifecycle start even though it belongs to
    that same turn. If no active turn contains the observation, fall back to
    the first later turn that actually re-processed tokens
    (``uncached_prompt > 0``).

    Skipping zero-usage turns matters for Claude Code: ``/effort`` lands on its
    own no-model-call turn(s), and the effort-caused re-read surfaces on the
    next turn with usage (the cache rebuild under the new key). For Codex the
    observation is on the turn_context of the collapse turn itself, which has a
    re-read, so it maps to the same turn. Returns ``turn_id ->
    (effort_from, effort_to)``; ``effort_from`` is ``None`` on Claude Code's
    first ``/effort`` switch (baseline unknown). Earlier change wins
    (``setdefault``).
    """
    block = usage.get("effort_changes") or {}
    events = [e for e in (block.get("events") or []) if isinstance(e, dict)]
    if not events:
        return {}
    change_ts: list[tuple[datetime, str | None, str | None]] = []
    for event in events:
        ts = _parse_iso_timestamp(event.get("timestamp"))
        if ts is None:
            continue
        # Form-agnostic: the CLI reshapes to ``from``/``to``; the raw api emits
        # ``effort_from``/``effort_to``. Read both so this works on either form.
        change_ts.append(
            (
                ts,
                event.get("from") or event.get("effort_from"),
                event.get("to") or event.get("effort_to"),
            )
        )
    if not change_ts:
        return {}
    # Candidate turns: those with a real re-read (uncached_prompt > 0), sorted
    # by start. A turn without a re-read cannot be an effort-caused break.
    candidates: list[tuple[datetime, datetime | None, str]] = []
    for turn in usage.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        runtime = turn.get("runtime") or {}
        start = _parse_iso_timestamp(runtime.get("start") or runtime.get("started_at"))
        end = _parse_iso_timestamp(runtime.get("end") or runtime.get("ended_at"))
        re_read = _usage_token(turn.get("usage"), "uncached_prompt") or 0
        if (
            start is not None
            and re_read > 0
            and (turn.get("id") or turn.get("turn_id"))
        ):
            candidates.append((start, end, str(turn.get("id") or turn.get("turn_id"))))
    if not candidates:
        return {}
    candidates.sort()
    mapping: dict[str, tuple[str | None, str | None]] = {}
    for ts, effort_from, effort_to in change_ts:
        # An initial ``/effort`` before the session's first provider request
        # configures that request; it has no already-warm, in-session prefix
        # to invalidate and must not be reported as a cache break.
        if not any(start < ts for start, _end, _turn_id in candidates):
            continue
        containing_turn = next(
            (
                turn_id
                for start, end, turn_id in candidates
                if start <= ts and (end is None or ts <= end)
            ),
            None,
        )
        if containing_turn is not None:
            mapping.setdefault(containing_turn, (effort_from, effort_to))
            continue
        for start, _end, turn_id in candidates:
            if start >= ts:
                mapping.setdefault(turn_id, (effort_from, effort_to))
                break
    return mapping


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
        confidence = _confidence(
            category.get("confidence"), fallback="estimated_tokens"
        )
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
                estimated_cost=_cost_evidence_from_estimate(
                    category.get("estimated_cost")
                ),
            )
        )
    return projected


def _project_provider_usage_buckets(
    stats: dict[str, Any],
) -> list[ContextCategory]:
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
                    confidence=_confidence(
                        category.get("confidence"), fallback="exact_usage"
                    ),
                    source=str(
                        category.get("source")
                        or "ct session stats:provider_usage_buckets"
                    ),
                ),
                percent=_optional_float(category.get("pct")),
                estimated_cost=_cost_evidence_from_estimate(
                    category.get("estimated_cost")
                ),
            )
        )
    return projected


def _project_expensive_items(
    tool_usage: dict[str, Any],
    *,
    session_id: str | None = None,
) -> list[ExpensiveItem]:
    items: list[ExpensiveItem] = []
    for index, item in enumerate(tool_usage.get("tool_items") or []):
        if not isinstance(item, dict):
            continue
        if session_id and str(item.get("session_id") or "") != session_id:
            continue
        real_cost = item.get("allocated_real_token_cost")
        if not isinstance(real_cost, dict):
            continue
        usage = _usage_dict(real_cost)
        estimate = _cost_evidence_from_estimate(item.get("estimated_cost"))
        if estimate is None:
            continue
        events = _tool_item_events(item, index=index)
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
    turn_id: str | None,
    session_id: str | None = None,
) -> list[ContextEvent]:
    turn_meta = {
        str(item.get("id")): item
        for item in usage.get("turns") or []
        if isinstance(item, dict) and item.get("id")
    }
    tool_events_by_turn = _tool_events_by_turn(tool_usage, session_id=session_id)
    events: list[ContextEvent] = []
    for session in overview.get("sessions") or []:
        if not isinstance(session, dict):
            continue
        current_session_id = str(session.get("id") or overview.get("id") or "")
        if session_id and current_session_id != session_id:
            continue
        for turn in session.get("turns") or []:
            if not isinstance(turn, dict):
                continue
            current_turn_id = str(turn.get("id") or "")
            raw_tool_events = list(tool_events_by_turn.get(current_turn_id) or [])
            turn_item = turn_meta.get(current_turn_id) or {}
            turn_runtime = turn_item.get("runtime") or {}
            turn_usage = turn_item.get("usage") or {}
            idle_seconds = _optional_float(turn_runtime.get("wait_before_seconds"))
            re_read_tokens = _usage_token(turn_usage, "uncached_prompt")
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
                            "session_id": current_session_id,
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
                    session_id=current_session_id,
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
    session_id: str | None = None,
) -> dict[str, list[list[ContextEvent]]]:
    by_turn: dict[str, list[list[ContextEvent]]] = {}
    for index, item in enumerate(tool_usage.get("tool_items") or []):
        if not isinstance(item, dict):
            continue
        if session_id and str(item.get("session_id") or "") != session_id:
            continue
        turn_id = _optional_text(item.get("turn_id"))
        if turn_id is None:
            continue
        by_turn.setdefault(turn_id, []).append(_tool_item_events(item, index=index))
    return by_turn


def _tool_item_events(
    item: dict[str, Any],
    *,
    index: int,
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
    estimated_cost = _cost_evidence_from_estimate(item.get("estimated_cost"))
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


def _cost_evidence_from_estimate(estimate: Any) -> CostEvidence | None:
    """Project a core-emitted ``estimated_cost`` dict to the dashboard's
    ``CostEvidence``. ``None`` when the model was unknown to the pricing
    catalog (no rule matched) — cost is omitted rather than reported as 0.
    """
    if not isinstance(estimate, dict) or estimate.get("value_usd") is None:
        return None
    return CostEvidence(
        value_usd=estimate.get("value_usd"),
        confidence=estimate.get("confidence") or "estimated",
        source=str(estimate.get("source") or "ct pricing"),
        effective_date=estimate.get("effective_date"),
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


def _usage_token(usage: dict[str, Any] | None, short_key: str) -> int | None:
    """Read a usage token by short key (``cached_prompt``) with long-form
    (``cached_prompt_tokens``) fallback. ``ct session usage`` emits the short
    form; ``allocated_real_token_cost`` from tool usage emits the long form.
    """
    if not isinstance(usage, dict):
        return None
    raw = usage.get(short_key)
    if raw is None:
        raw = usage.get(f"{short_key}_tokens")
    return _optional_int(raw)


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


def _format_idle(seconds: float) -> str:
    if seconds >= 60:
        return f"{seconds / 60:.1f}m"
    return f"{int(seconds)}s"


def _cache_break_flag(record: CacheBreakRecord | None) -> str | None:
    if record is None:
        return None
    icon = {
        "ttl_confirmed": "⏳",
        "ttl_likely": "⏳",
        "effort_switch": "⚡",
        "model_switch": "🔄",
        "unattributed": "❓",
    }[record.type]
    label = {
        "ttl_confirmed": "TTL break",
        "ttl_likely": "TTL break?",
        "effort_switch": "effort-switch",
        "model_switch": "model-switch",
        "unattributed": "cache miss",
    }[record.type]
    base = (
        f"{icon} {label}: {_format_idle(record.idle_seconds)} idle → "
        f"{_format_tokens(record.re_read_tokens)} re-read"
    )
    if record.effort_to:
        change = (
            f"{record.effort_from}→{record.effort_to}"
            if record.effort_from
            else f"→{record.effort_to}"
        )
        return f"{base} ({change} confirmed)"
    if record.model_to:
        change = (
            f"{record.model_from}→{record.model_to}"
            if record.model_from
            else f"→{record.model_to}"
        )
        return f"{base} ({change})"
    return base


def _cache_breaks_teaser(summary: CacheBreakSummary | None) -> str | None:
    if summary is None or summary.count == 0:
        return None
    type_summary = ", ".join(
        f"{summary.by_type.get(key, 0)} {key}"
        for key in (
            "effort_switch",
            "model_switch",
            "ttl_confirmed",
            "ttl_likely",
            "unattributed",
        )
        if summary.by_type.get(key)
    )
    confirmed = sum(1 for event in summary.events if event.effort_to)
    line = (
        f"Cache breaks: {summary.count} "
        f"({_format_tokens(summary.total_re_read_tokens)} re-read, "
        f"{_format_cost(summary.estimated_waste_usd)} wasted)"
    )
    if type_summary:
        line = f"{line} — {type_summary}"
    if confirmed:
        line = f"{line} ({confirmed} confirmed)"
    return line


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
