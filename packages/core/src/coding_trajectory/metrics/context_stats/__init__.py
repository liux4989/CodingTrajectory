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
    QuotaStatsFlat,
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
        quota=_quota_stats(observation),
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


def _quota_stats(observation: ContextUsageObservation) -> QuotaStatsFlat | None:
    quota = observation.quota
    if not isinstance(quota, dict):
        return None
    primary = quota.get("primary") if isinstance(quota.get("primary"), dict) else {}
    secondary = (
        quota.get("secondary") if isinstance(quota.get("secondary"), dict) else {}
    )
    credits = quota.get("credits") if isinstance(quota.get("credits"), dict) else {}
    individual = (
        quota.get("individual_limit")
        if isinstance(quota.get("individual_limit"), dict)
        else {}
    )
    return QuotaStatsFlat(
        limit_id=_as_str(quota.get("limit_id")),
        limit_name=_as_str(quota.get("limit_name")),
        plan_type=_as_str(quota.get("plan_type")),
        primary_used_percent=_as_float(primary.get("used_percent")),
        primary_window_minutes=_as_int(primary.get("window_minutes")),
        primary_resets_at=_as_int(primary.get("resets_at")),
        secondary_used_percent=_as_float(secondary.get("used_percent")),
        secondary_window_minutes=_as_int(secondary.get("window_minutes")),
        secondary_resets_at=_as_int(secondary.get("resets_at")),
        credits_has_credits=_as_bool(credits.get("has_credits")),
        credits_unlimited=_as_bool(credits.get("unlimited")),
        credits_balance=_as_str(credits.get("balance")),
        individual_limit=_as_str(individual.get("limit")),
        individual_used=_as_str(individual.get("used")),
        individual_remaining_percent=_as_int(individual.get("remaining_percent")),
        rate_limit_reached_type=_as_str(quota.get("rate_limit_reached_type")),
    )


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _as_float(value: Any) -> float | None:
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return float(value)
    return None


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


__all__ = [
    "build_session_graph_context_stats",
]
