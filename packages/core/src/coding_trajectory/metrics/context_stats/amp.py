"""Amp context stats — derived from per-step normalized usage metrics."""

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


def build_amp_context_stats(session_graph: SessionGraph) -> dict[str, Any]:
    latest_metrics = latest_step_usage(session_graph)
    runtime = runtime_stats(session_graph)
    messages = message_stats(session_graph)

    if latest_metrics is None:
        return SessionContextStatsFlat(
            root_session_id=session_graph.root_session_id,
            vendor=Vendor.AMP.value,
            runtime=runtime,
            messages=messages,
            warnings=["No amp assistant usage found; cannot compute context stats."],
        ).model_dump(mode="json")

    usage_dict = latest_metrics.get("usage") if isinstance(latest_metrics.get("usage"), dict) else {}
    usage = token_usage_from_mapping(usage_dict)
    model = _as_str(latest_metrics.get("model"))

    context_window = _as_int(usage_dict.get("max_input_tokens"))
    if context_window == 0:
        context_window = model_context_window(model) or 0

    input_tokens = _as_int(usage_dict.get("input_tokens"))
    cached_input_tokens = _as_int(usage_dict.get("cached_input_tokens"))
    cache_creation_tokens = _as_int(usage_dict.get("cache_creation_input_tokens"))

    used_tokens = _as_int(latest_metrics.get("cumulative_input_tokens"))
    if used_tokens == 0:
        used_tokens = input_tokens + cached_input_tokens + cache_creation_tokens

    denominator = context_window or used_tokens
    categories: list[ContextCategoryFlat] = []
    if cached_input_tokens > 0:
        categories.append(
            ContextCategoryFlat(
                key="cached_context",
                label="Cached prefix (system + tools + prior turns)",
                tokens=cached_input_tokens,
                percent=percent(cached_input_tokens, denominator),
                confidence="exact_usage",
                source="amp_usage_block.cached_input_tokens",
            )
        )
    if cache_creation_tokens > 0:
        categories.append(
            ContextCategoryFlat(
                key="new_cached_prefix",
                label="Newly cached this turn",
                tokens=cache_creation_tokens,
                percent=percent(cache_creation_tokens, denominator),
                confidence="exact_usage",
                source="amp_usage_block.cache_creation_input_tokens",
            )
        )
    if input_tokens > 0:
        categories.append(
            ContextCategoryFlat(
                key="messages",
                label="Messages (uncached input)",
                tokens=input_tokens,
                percent=percent(input_tokens, denominator),
                confidence="exact_usage",
                source="amp_usage_block.input_tokens",
            )
        )

    warnings = [
        "Amp context categories are derived from cache-bucket sizes; "
        "codex-style category attribution is not available for amp.",
    ]

    return SessionContextStatsFlat(
        root_session_id=session_graph.root_session_id,
        vendor=Vendor.AMP.value,
        model=ContextModelStatsFlat(
            name=model,
            context_window_tokens=context_window or None,
        ),
        context_window=ContextWindowStatsFlat(
            used_tokens=used_tokens,
            used_percent=percent(used_tokens, context_window),
            source="amp_usage_block",
            categories=categories,
        ),
        runtime=runtime,
        messages=messages,
        usage=usage,
        quota=None,
        warnings=warnings,
    ).model_dump(mode="json")


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
