"""Pi context stats — derived from per-step normalized usage metrics."""

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


def build_pi_context_stats(session_graph: SessionGraph) -> dict[str, Any]:
    latest_metrics = latest_step_usage(session_graph)
    runtime = runtime_stats(session_graph)
    messages = message_stats(session_graph)

    if latest_metrics is None:
        return SessionContextStatsFlat(
            root_session_id=session_graph.root_session_id,
            vendor=Vendor.PI.value,
            runtime=runtime,
            messages=messages,
            warnings=["No pi assistant usage found; cannot compute context stats."],
        ).model_dump(mode="json")

    usage_dict = latest_metrics.get("usage") if isinstance(latest_metrics.get("usage"), dict) else {}
    usage = token_usage_from_mapping(usage_dict)

    model = latest_metrics.get("model") if isinstance(latest_metrics.get("model"), str) else None
    raw_provider = latest_metrics.get("provider") if isinstance(latest_metrics.get("provider"), str) else None
    provider = "openai" if raw_provider == "openai-codex" else raw_provider
    context_window = model_context_window(model, provider=provider) or 0

    used_tokens = _as_int(latest_metrics.get("cumulative_input_tokens"))
    denominator = context_window or used_tokens

    cached = _as_int(usage_dict.get("cached_input_tokens"))
    cache_creation = _as_int(usage_dict.get("cache_creation_input_tokens"))
    input_tokens = _as_int(usage_dict.get("input_tokens"))

    categories: list[ContextCategoryFlat] = []
    if cached > 0:
        categories.append(
            ContextCategoryFlat(
                key="cached_context",
                label="Cached prefix (system + tools + prior turns)",
                tokens=cached,
                percent=percent(cached, denominator),
                confidence="exact_usage",
                source="pi_usage_block",
            )
        )
    if cache_creation > 0:
        categories.append(
            ContextCategoryFlat(
                key="new_cached_prefix",
                label="Newly cached this turn",
                tokens=cache_creation,
                percent=percent(cache_creation, denominator),
                confidence="exact_usage",
                source="pi_usage_block",
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
                source="pi_usage_block",
            )
        )

    return SessionContextStatsFlat(
        root_session_id=session_graph.root_session_id,
        vendor=Vendor.PI.value,
        model=ContextModelStatsFlat(
            name=model,
            context_window_tokens=context_window or None,
        ),
        context_window=ContextWindowStatsFlat(
            used_tokens=used_tokens,
            used_percent=percent(used_tokens, context_window),
            source="pi_usage_block",
            categories=categories,
        ),
        runtime=runtime,
        messages=messages,
        usage=usage,
        quota=None,
        warnings=[
            "Pi context categories are derived from cache-bucket sizes; codex-style category attribution is not available for pi.",
        ],
    ).model_dump(mode="json")


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
