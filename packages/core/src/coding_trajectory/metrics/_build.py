"""Raw-log-to-metrics assembly.

Builds SessionGraphMetrics / SessionMetrics / TurnMetrics from canonical
sessions by walking context-usage observations and normalizing vendor
token-count conventions. Extracted from metrics/analysis.py so the main
module reads as projection building over these assembled metrics.

Internal helpers re-imported by :mod:`coding_trajectory.metrics.analysis`.
"""

from __future__ import annotations

from typing import Any

from coding_trajectory import debug
from coding_trajectory.ingestion.models import (
    ContextUsageObservation,
    Session,
    SessionGraph,
    Turn,
)
from coding_trajectory.metrics.models import (
    MetricSource,
    SessionGraphMetrics,
    SessionMetrics,
    TokenUsage,
    TokenUsageObservation,
    TurnMetrics,
)
from coding_trajectory.metrics.pricing import _uses_net_input_convention


def _build_full_metrics(
    session_graph: SessionGraph,
) -> SessionGraphMetrics:
    """Return full token metrics projected onto the session_graph hierarchy."""
    session_metrics: list[SessionMetrics] = []
    total = TokenUsage()
    warnings: list[str] = []

    for session in session_graph.sessions:
        metrics = _build_session_metrics(session)
        session_metrics.append(metrics)
        total = total.plus(metrics.token_usage)
        if not _session_has_usage(metrics) and session.context_usage:
            _record_usage_warning(
                warnings,
                f"no token usage metrics found for session {session.session_id}",
                code="usage.no_token_metrics",
                session=session,
                severity="info",
            )
        for message in _usage_consistency_warnings(metrics):
            _record_usage_warning(
                warnings,
                message,
                code="usage.reported_total_inconsistent",
                session=session,
            )

    return SessionGraphMetrics(
        root_session_id=session_graph.root_session_id,
        token_usage=total,
        sessions=session_metrics,
        warnings=_unique(warnings),
    )


def _build_session_metrics(
    session: Session,
) -> SessionMetrics:
    turn_metrics: list[TurnMetrics] = []
    session_total = TokenUsage()

    for turn in session.turns:
        metrics = _build_turn_metrics(session, turn)
        turn_metrics.append(metrics)
        session_total = session_total.plus(metrics.token_usage)

    return SessionMetrics(
        session_id=session.session_id,
        vendor=session.vendor.value,
        status=session.status.value,
        token_usage=session_total,
        turns=turn_metrics,
    )


def _build_turn_metrics(
    session: Session,
    turn: Turn,
) -> TurnMetrics:
    context_observations = _context_usage_for_turn(session, turn)
    observations: list[TokenUsageObservation] = []
    for context_observation in context_observations:
        usage_observation = _usage_from_context_observation(
            context_observation,
            turn=turn,
            session=session,
        )
        if usage_observation is not None:
            observations.append(usage_observation)

    observations.sort(key=lambda item: item.timestamp)
    total = TokenUsage()
    for observation in observations:
        total = total.plus(observation.usage)

    return TurnMetrics(
        turn_id=turn.turn_id,
        sequence=turn.sequence,
        status=turn.status.value,
        started_at=turn.started_at,
        completed_at=turn.ended_at,
        token_usage=total,
        observations=observations,
    )


def _usage_from_context_observation(
    observation: ContextUsageObservation,
    *,
    turn: Turn,
    session: Session,
) -> TokenUsageObservation | None:
    provider = observation.provider or session.vendor.value
    token_usage = _token_usage_from_mapping(observation.usage, provider=provider)
    if token_usage is None or _is_zero_usage(token_usage):
        return None

    return TokenUsageObservation(
        scope_type="turn",
        scope_id=turn.turn_id,
        timestamp=observation.timestamp,
        usage=token_usage,
        provider=provider,
        model=observation.model,
        source=MetricSource(
            vendor=session.vendor.value,
            source_type="session.context_usage",
            event_id=observation.source_event_id,
        ),
    )


def _token_usage_from_mapping(
    value: dict[str, Any],
    *,
    provider: str | None = None,
) -> TokenUsage:
    input_tokens = _as_int(value.get("input_tokens") or value.get("inputTokens"))
    cached_input_tokens = _as_int(
        value.get("cached_input_tokens") or value.get("cachedInputTokens")
    )
    cache_creation_input_tokens = _as_int(
        value.get("cache_creation_input_tokens")
        or value.get("cacheCreationInputTokens")
    )
    output_tokens = _as_int(value.get("output_tokens") or value.get("outputTokens"))
    reasoning_output_tokens = _as_int(
        value.get("reasoning_output_tokens") or value.get("reasoningOutputTokens")
    )
    reported_total_tokens = _as_int(
        value.get("total_tokens") or value.get("totalTokens")
    )
    uncached_raw = value.get("uncached_input_tokens") or value.get(
        "uncachedInputTokens"
    )
    total_tokens, processed_tokens, total_confidence = _normalized_total_tokens(
        provider=provider,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        output_tokens=output_tokens,
        reasoning_output_tokens=reasoning_output_tokens,
        reported_total_tokens=reported_total_tokens,
    )
    return TokenUsage(
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        output_tokens=output_tokens,
        reasoning_output_tokens=reasoning_output_tokens,
        total_tokens=total_tokens,
        processed_tokens=processed_tokens,
        reported_total_tokens=(
            reported_total_tokens if reported_total_tokens > 0 else None
        ),
        uncached_input_tokens=(
            uncached_raw
            if isinstance(uncached_raw, int) and not isinstance(uncached_raw, bool)
            else None
        ),
        cost_usd=_as_float_or_none(value.get("cost_usd") or value.get("costUsd")),
        total_confidence=total_confidence,
    )


def _normalized_total_tokens(
    *,
    provider: str | None,
    input_tokens: int,
    cached_input_tokens: int,
    cache_creation_input_tokens: int,
    output_tokens: int,
    reasoning_output_tokens: int,
    reported_total_tokens: int,
) -> tuple[int, int, str]:
    fresh_io_total = input_tokens + output_tokens
    reasoning_inclusive_total = fresh_io_total + reasoning_output_tokens
    processed_without_reasoning_total = (
        input_tokens + cached_input_tokens + cache_creation_input_tokens + output_tokens
    )
    processed_total = processed_without_reasoning_total + reasoning_output_tokens
    if reported_total_tokens <= 0:
        derived_total = (
            processed_total
            if _uses_net_input_convention(provider, None)
            else reasoning_inclusive_total
        )
        return derived_total, derived_total, "reported_missing"
    if reported_total_tokens in {fresh_io_total, reasoning_inclusive_total}:
        return reported_total_tokens, reasoning_inclusive_total, "reported_consistent"
    if reported_total_tokens in {processed_without_reasoning_total, processed_total}:
        return reported_total_tokens, processed_total, "reported_consistent"
    return processed_total, processed_total, "reported_inconsistent"


def _context_usage_for_turn(
    session: Session,
    turn: Turn,
) -> list[ContextUsageObservation]:
    event_ids = set(turn.event_ids)
    return [
        observation
        for observation in session.context_usage
        if observation.source_event_id in event_ids
    ]


def _session_has_usage(metrics: SessionMetrics) -> bool:
    return not _is_zero_usage(metrics.token_usage)


def _is_zero_usage(usage: TokenUsage) -> bool:
    return (
        usage.input_tokens == 0
        and usage.cached_input_tokens == 0
        and usage.cache_creation_input_tokens == 0
        and usage.output_tokens == 0
        and usage.reasoning_output_tokens == 0
        and usage.total_tokens == 0
    )


def _usage_consistency_warnings(metrics: SessionMetrics) -> list[str]:
    inconsistent = sum(
        1
        for turn in metrics.turns
        for observation in turn.observations
        if observation.usage.total_confidence == "reported_inconsistent"
    )
    if inconsistent == 0:
        return []
    return [
        f"{inconsistent} token usage observations had inconsistent reported "
        "totals; total_tokens was derived from processed token buckets"
    ]


def _record_usage_warning(
    warnings: list[str],
    message: str,
    *,
    code: str,
    session: Session,
    severity: str = "warning",
) -> None:
    """Record a usage warning on both channels at once.

    The payload ``warnings`` list carries a human-readable string for inline
    rendering; ``debug.warn`` carries the structured (code/severity/context)
    twin so ``ct doctor`` can aggregate it. Routing both through this helper
    keeps the two representations in sync by construction.

    ``severity`` defaults to ``warning`` (a genuine anomaly). Expected data
    conditions - e.g. a session whose logs carry no token-usage records - pass
    ``info`` so ``ct doctor``'s warning aggregation surfaces only anomalies
    while the inline payload still notes the condition.
    """
    warnings.append(message)
    debug.warn(
        message,
        code=code,
        severity=severity,
        session_id=str(session.session_id),
        vendor=session.vendor.value if getattr(session.vendor, "value", None) else None,
    )


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _as_float_or_none(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None
