"""Claude Code context stats — derived from per-step usage cache buckets."""

from __future__ import annotations

from typing import Any

from coding_trajectory.ingestion.models import SessionGraph, Vendor
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
    SessionContextStatsFlat,
)


def build_claude_code_context_stats(session_graph: SessionGraph) -> dict[str, Any]:
    latest_metrics = latest_step_usage(session_graph)
    runtime = runtime_stats(session_graph)
    messages = message_stats(session_graph)

    if latest_metrics is None:
        return SessionContextStatsFlat(
            root_session_id=session_graph.root_session_id,
            vendor=Vendor.CLAUDE_CODE.value,
            runtime=runtime,
            messages=messages,
            warnings=["No claude_code assistant usage found; cannot compute context stats."],
        ).model_dump(mode="json")

    usage_raw = latest_metrics.get("usage") if isinstance(latest_metrics.get("usage"), dict) else {}
    usage = token_usage_from_mapping(usage_raw)
    model = _as_str(latest_metrics.get("model"))
    context_window = model_context_window(model, provider="anthropic") or 0
    used_tokens = _as_int(latest_metrics.get("cumulative_input_tokens"))

    denominator = context_window or used_tokens
    cached = _as_int(usage_raw.get("cached_input_tokens"))
    new_cache = _as_int(usage_raw.get("cache_creation_input_tokens"))
    new_input = _as_int(usage_raw.get("input_tokens"))

    categories: list[ContextCategoryFlat] = []
    if cached > 0:
        categories.append(
            ContextCategoryFlat(
                key="cached_context",
                label="Cached prefix (system + tools + prior turns)",
                tokens=cached,
                percent=percent(cached, denominator),
                confidence="exact_usage",
                source="cache_read_input_tokens",
            )
        )
    if new_cache > 0:
        categories.append(
            ContextCategoryFlat(
                key="new_cached_prefix",
                label="Newly cached this turn",
                tokens=new_cache,
                percent=percent(new_cache, denominator),
                confidence="exact_usage",
                source="cache_creation_input_tokens",
            )
        )
    if new_input > 0:
        categories.append(
            ContextCategoryFlat(
                key="messages",
                label="Messages (uncached input)",
                tokens=new_input,
                percent=percent(new_input, denominator),
                confidence="exact_usage",
                source="input_tokens",
            )
        )

    return SessionContextStatsFlat(
        root_session_id=session_graph.root_session_id,
        vendor=Vendor.CLAUDE_CODE.value,
        model=ContextModelStatsFlat(
            name=model,
            context_window_tokens=context_window or None,
        ),
        context_window=ContextWindowStatsFlat(
            used_tokens=used_tokens,
            used_percent=percent(used_tokens, context_window),
            source="claude_usage_block",
            categories=categories,
        ),
        runtime=runtime,
        messages=messages,
        usage=usage,
        quota=None,
        warnings=[
            "Claude Code context categories are derived from cache-bucket sizes; "
            "codex-style category attribution is not available for claude_code.",
        ],
    ).model_dump(mode="json")


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
