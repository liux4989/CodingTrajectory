"""Codex CLI context stats — uses preserved prompt_block + token_count events."""

from __future__ import annotations

from typing import Any

from coding_trajectory.ingestion.models import Event, EventType, SessionGraph, Vendor
from coding_trajectory.metrics.context_stats._common import (
    latest_step_usage,
    message_stats,
    model_context_window,
    percent,
    runtime_stats,
    token_usage_from_mapping,
)
from coding_trajectory.metrics.models import (
    ContextCategoryFlat,
    ContextModelStatsFlat,
    ContextWindowStatsFlat,
    QuotaStatsFlat,
    SessionContextStatsFlat,
)


def build_codex_context_stats(session_graph: SessionGraph) -> dict[str, Any]:
    latest_usage_event = _latest_codex_token_count_event(session_graph)
    latest_metrics = (
        latest_usage_event.payload.get("metrics") if latest_usage_event is not None else None
    )
    latest_metrics = latest_metrics if isinstance(latest_metrics, dict) else {}
    latest_usage = token_usage_from_mapping(
        latest_metrics.get("last_token_usage")
        if isinstance(latest_metrics.get("last_token_usage"), dict)
        else {}
    )
    context_window = _as_int(latest_metrics.get("model_context_window"))
    if context_window == 0:
        context_window = _latest_task_started_context_window(session_graph) or 0

    model = _codex_model(session_graph) or _fallback_model_from_step(session_graph)
    if context_window == 0:
        context_window = model_context_window(model, provider="openai") or 0

    categories = _codex_context_categories(session_graph, latest_usage.input_tokens, context_window)
    quota = _quota_stats_from_latest_event(latest_usage_event)
    runtime = runtime_stats(session_graph)
    messages = message_stats(session_graph)

    warnings = [
        "Category token counts are estimated from Codex JSONL prompt text, not vendor category attribution.",
    ]
    if not categories:
        warnings.append("No Codex prompt blocks were found for context category breakdown.")

    return SessionContextStatsFlat(
        root_session_id=session_graph.root_session_id,
        vendor=Vendor.CODEX_CLI.value,
        model=ContextModelStatsFlat(
            name=model,
            context_window_tokens=context_window or None,
        ),
        context_window=ContextWindowStatsFlat(
            used_tokens=latest_usage.input_tokens,
            used_percent=percent(latest_usage.input_tokens, context_window),
            source="latest_token_count",
            categories=categories,
        ),
        runtime=runtime,
        messages=messages,
        usage=latest_usage,
        quota=quota,
        warnings=warnings,
    ).model_dump(mode="json")


def _latest_codex_token_count_event(session_graph: SessionGraph) -> Event | None:
    events = [
        event
        for session in session_graph.sessions
        for event in session.events
        if event.vendor_source == Vendor.CODEX_CLI
        and event.type == EventType.VENDOR_RAW
        and event.payload.get("raw_type") == "token_count"
    ]
    return max(events, key=lambda item: item.timestamp) if events else None


def _latest_task_started_context_window(session_graph: SessionGraph) -> int | None:
    values = [
        _as_int(event.payload.get("model_context_window"))
        for session in session_graph.sessions
        for event in session.events
        if event.vendor_source == Vendor.CODEX_CLI
        and event.type == EventType.VENDOR_RAW
        and event.payload.get("raw_type") == "task_started"
    ]
    values = [value for value in values if value > 0]
    return values[-1] if values else None


def _codex_model(session_graph: SessionGraph) -> str | None:
    for session in session_graph.sessions:
        for event in sorted(session.events, key=lambda item: item.timestamp):
            if event.vendor_source != Vendor.CODEX_CLI:
                continue
            if event.payload.get("raw_type") != "token_count":
                continue
            metrics = event.payload.get("metrics")
            if not isinstance(metrics, dict):
                continue
            model = _as_str(metrics.get("model"))
            if model:
                return model
    return None


def _fallback_model_from_step(session_graph: SessionGraph) -> str | None:
    for session in session_graph.sessions:
        for turn in session.turns:
            for step in turn.steps:
                data = (step.vendor_data or {}).get("metrics")
                if isinstance(data, dict):
                    model = _as_str(data.get("model"))
                    if model:
                        return model
    return None


def _codex_context_categories(
    session_graph: SessionGraph,
    used_tokens: int,
    context_window: int,
) -> list[ContextCategoryFlat]:
    buckets: dict[str, dict[str, Any]] = {}
    for session in session_graph.sessions:
        for event in session.events:
            if event.vendor_source != Vendor.CODEX_CLI:
                continue
            if event.type != EventType.VENDOR_RAW or event.payload.get("raw_type") != "prompt_block":
                continue
            text = event.payload.get("text")
            if not isinstance(text, str) or not text:
                continue
            key, label = _codex_prompt_category(event.payload)
            bucket = buckets.setdefault(key, {"label": label, "tokens": 0})
            tokens = _estimate_text_tokens(text)
            bucket["tokens"] += tokens

    denominator = context_window or used_tokens
    categories = [
        ContextCategoryFlat(
            key=key,
            label=value["label"],
            tokens=int(value["tokens"]),
            percent=percent(int(value["tokens"]), denominator),
            confidence="estimated_tokens",
        )
        for key, value in sorted(buckets.items(), key=lambda item: _category_sort_key(item[0]))
        if int(value["tokens"]) > 0
    ]
    prompt_tokens = sum(category.tokens for category in categories)
    message_tokens = max(used_tokens - prompt_tokens, 0)
    if message_tokens:
        categories.append(
            ContextCategoryFlat(
                key="messages",
                label="Conversation",
                tokens=message_tokens,
                percent=percent(message_tokens, denominator),
                confidence="estimated_tokens",
                source="latest_input_minus_prompt_blocks",
            )
        )
    return categories


def _codex_prompt_category(payload: dict[str, Any]) -> tuple[str, str]:
    block = _as_str(payload.get("prompt_block")) or ""
    text = payload.get("text")
    haystack = f"{block}\n{text if isinstance(text, str) else ''}".lower()
    if block == "base_instructions":
        return "system_instructions", "System instructions"
    if "agents.md" in haystack:
        return "project_instructions", "Project instructions"
    if "skills_instructions" in block or "### available skills" in haystack:
        return "tools_integrations", "Tools and integrations"
    if "plugins_instructions" in block or "### available plugins" in haystack:
        return "tools_integrations", "Tools and integrations"
    if "memory_summary" in haystack or "memory layout" in haystack or "## memory" in haystack:
        return "memory", "Memory"
    if "mcp" in haystack or "tools are grouped" in haystack:
        return "tools_integrations", "Tools and integrations"
    return "developer_instructions", "Developer instructions"


def _category_sort_key(key: str) -> int:
    order = {
        "system_instructions": 0,
        "developer_instructions": 1,
        "project_instructions": 2,
        "tools_integrations": 3,
        "memory": 4,
        "messages": 5,
    }
    return order.get(key, 99)


def _estimate_text_tokens(text: str) -> int:
    return max(round(len(text) / 4), 1) if text else 0


def _quota_stats_from_latest_event(event: Event | None) -> QuotaStatsFlat | None:
    if event is None:
        return None
    quota = event.payload.get("quota")
    if not isinstance(quota, dict):
        return None
    primary = quota.get("primary") if isinstance(quota.get("primary"), dict) else {}
    secondary = quota.get("secondary") if isinstance(quota.get("secondary"), dict) else {}
    return QuotaStatsFlat(
        plan_type=_as_str(quota.get("plan_type")),
        primary_used_percent=_as_float(primary.get("used_percent")),
        secondary_used_percent=_as_float(secondary.get("used_percent")),
        resets_at=_as_int(primary.get("resets_at")) or None,
    )


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _as_float(value: Any) -> float | None:
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return float(value)
    return None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
