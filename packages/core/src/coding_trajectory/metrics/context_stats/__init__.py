"""Provider-neutral session context stats (used by `ct session stats`)."""

from typing import Any
from uuid import UUID

from coding_trajectory import debug
from coding_trajectory.ingestion.models import ContextUsageObservation, SessionGraph
from coding_trajectory.metrics.context_stats._common import (
    message_stats,
    percent,
    runtime_stats,
    token_usage_from_mapping,
)
from coding_trajectory.metrics.context_stats.composition import (
    build_context_composition,
)
from coding_trajectory.metrics.models import (
    ContextCategoryFlat,
    ContextModelStatsFlat,
    ContextWindowStatsFlat,
    SessionContextStatsFlat,
)


def build_session_graph_context_stats(
    session_graph: SessionGraph,
    *,
    allocated_usage_by_item: dict[UUID, dict[str, int]] | None = None,
) -> dict[str, Any]:
    vendors = {session.vendor for session in session_graph.sessions if session.vendor}
    if not vendors:
        raise ValueError("session_graph has no vendor sessions")
    if len(vendors) > 1:
        names = ", ".join(sorted(vendor.value for vendor in vendors))
        raise NotImplementedError(
            f"session stats does not yet support multi-vendor session graphs; got: {names}"
        )

    vendor = next(iter(vendors))
    runtime = runtime_stats(session_graph)
    messages = message_stats(session_graph)
    categories = build_context_composition(
        session_graph,
        allocated_usage_by_item=allocated_usage_by_item,
    )
    observation = _latest_context_usage(session_graph)
    if observation is None:
        no_obs_message = f"No {vendor.value} context usage observation found; provider context usage is unavailable."
        debug.warn(
            no_obs_message,
            code="context.no_observation",
            severity="warning",
        )
        return SessionContextStatsFlat(
            root_session_id=session_graph.root_session_id,
            vendor=vendor.value,
            context_window=ContextWindowStatsFlat(categories=categories),
            runtime=runtime,
            messages=messages,
            warnings=[no_obs_message],
        ).model_dump(mode="json")

    context_window = observation.context_window_tokens or 0
    provider_usage_buckets = [
        ContextCategoryFlat(
            key=category.key,
            label=category.label,
            tokens=category.tokens,
            percent=percent(category.tokens, observation.used_input_tokens),
            confidence=category.confidence,
            source=category.source,
        )
        for category in observation.categories
    ]
    warnings = [
        (
            "Context composition measures observed canonical content and is not scaled to the "
            "provider-reported active context window."
        )
    ]
    if provider_usage_buckets:
        warnings.append(
            "Provider usage buckets are reported separately from semantic context composition."
        )

    return SessionContextStatsFlat(
        root_session_id=session_graph.root_session_id,
        vendor=vendor.value,
        model=ContextModelStatsFlat(
            name=observation.model,
            context_window_tokens=context_window or None,
        ),
        context_window=ContextWindowStatsFlat(
            used_tokens=observation.used_input_tokens,
            used_percent=percent(observation.used_input_tokens, context_window),
            source=observation.source,
            categories=categories,
        ),
        provider_usage_buckets=provider_usage_buckets,
        runtime=runtime,
        messages=messages,
        usage=token_usage_from_mapping(observation.usage),
        warnings=warnings,
    ).model_dump(mode="json")


def _latest_context_usage(
    session_graph: SessionGraph,
) -> ContextUsageObservation | None:
    observations = [
        observation
        for session in session_graph.sessions
        for observation in session.context_usage
    ]
    return max(observations, key=lambda item: item.timestamp) if observations else None


__all__ = [
    "build_session_graph_context_stats",
]
