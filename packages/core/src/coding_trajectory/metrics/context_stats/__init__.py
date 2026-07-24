"""Provider-neutral session context stats (used by `ct session stats`)."""

from typing import Any
from uuid import UUID

from coding_trajectory import debug
from coding_trajectory.ingestion.models import ContextUsageObservation, SessionGraph
from coding_trajectory.metrics.context_stats._common import (
    compaction_stats,
    message_stats,
    percent,
    runtime_stats,
    token_usage_from_mapping,
)
from coding_trajectory.metrics.context_stats.composition import (
    AnchorOutcome,
    build_context_composition,
)
from coding_trajectory.metrics._build import _build_full_metrics
from coding_trajectory.metrics.pricing import get_model_context_window
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
    allocated_usage_by_context_source: dict[str, dict[str, int]] | None = None,
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
    full = _build_full_metrics(session_graph)
    runtime = runtime_stats(
        session_graph,
        processed_tokens=full.token_usage.processed_token_total(),
        model_active_seconds=full.model_active_seconds,
    )
    messages = message_stats(session_graph)
    compaction = compaction_stats(session_graph)
    observation = _latest_context_usage(session_graph)
    categories, anchor_outcome = build_context_composition(
        session_graph,
        allocated_usage_by_item=allocated_usage_by_item,
        allocated_usage_by_context_source=allocated_usage_by_context_source,
        pricing_model=observation.model if observation else None,
        pricing_provider=(observation.provider if observation else None)
        or vendor.value,
    )
    if observation is None:
        no_obs_message = f"No {vendor.value} context usage observation found; provider context usage is unavailable."
        warnings: list[str] = []
        _record_context_warning(
            warnings,
            no_obs_message,
            code="context.no_observation",
            vendor=vendor.value,
            severity="info",
        )
        return SessionContextStatsFlat(
            root_session_id=session_graph.root_session_id,
            vendor=vendor.value,
            context_window=ContextWindowStatsFlat(categories=categories),
            runtime=runtime,
            compaction=compaction,
            messages=messages,
            warnings=warnings,
        ).model_dump(mode="json")

    context_window = observation.context_window_tokens or get_model_context_window(
        observation.model, provider=observation.provider or vendor.value
    )
    context_window_inferred = (
        not observation.context_window_tokens and context_window is not None
    )
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
    warnings: list[str] = []
    if anchor_outcome != AnchorOutcome.ANCHORED:
        _record_context_warning(
            warnings,
            _anchor_outcome_warning(anchor_outcome),
            code="context.composition_anchor",
            vendor=vendor.value,
        )
    if provider_usage_buckets:
        _record_context_warning(
            warnings,
            "Provider usage buckets are reported separately from semantic context composition.",
            code="context.provider_buckets_separate",
            vendor=vendor.value,
        )
    if context_window_inferred:
        _record_context_warning(
            warnings,
            f"Context window of {context_window} tokens inferred from a static model "
            f"catalog; {vendor.value} logs do not report the model context window.",
            code="context.window_inferred",
            vendor=vendor.value,
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
        compaction=compaction,
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


_ANCHOR_OUTCOME_WARNINGS = {
    AnchorOutcome.OVERCOUNT: (
        "Context composition overcounts the provider-reported used_input_tokens "
        "(e.g. reasoning the API stripped); observed estimates were retained "
        "without scaling to the active context window."
    ),
    AnchorOutcome.NO_CONVERSATION: (
        "Context composition has no resident conversation to scale to the "
        "provider-reported used_input_tokens; starting-context estimate only."
    ),
    AnchorOutcome.NO_USAGE: (
        "No usable provider used_input_tokens to anchor the context composition; "
        "observed estimates only, not reconciled to the active context window."
    ),
}


def _anchor_outcome_warning(outcome: AnchorOutcome) -> str:
    return _ANCHOR_OUTCOME_WARNINGS.get(
        outcome,
        "Context composition did not reconcile to the provider-reported active context window.",
    )


def _record_context_warning(
    warnings: list[str],
    message: str,
    *,
    code: str,
    vendor: str,
    severity: str = "warning",
) -> None:
    """Record a context warning on both the payload list and ``debug.warn``.

    Mirrors ``_record_usage_warning`` in the metrics analysis layer: the
    payload ``warnings`` list drives inline rendering while ``debug.warn``
    carries the structured (code/severity/context) twin so ``ct doctor`` can
    aggregate it. Routing both through this helper keeps them in sync.

    ``severity`` defaults to ``warning`` (a genuine anomaly). Expected data
    conditions - e.g. a session whose logs carry no context-usage observation
    - pass ``info`` so ``ct doctor``'s warning aggregation surfaces only
    anomalies while the inline payload still notes the condition.
    """
    warnings.append(message)
    debug.warn(message, code=code, severity=severity, vendor=vendor)


__all__ = [
    "build_session_graph_context_stats",
]
