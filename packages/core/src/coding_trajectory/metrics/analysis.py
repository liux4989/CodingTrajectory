"""Build derived execution metrics from canonical session_graphs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from coding_trajectory.ingestion.indexes import (
    SessionGraphIndex,
    build_session_graph_index,
    events_for_step,
    events_for_turn,
)
from coding_trajectory.ingestion.models import Event, EventType, Session, Step, SessionGraph, Turn, Vendor
from coding_trajectory.metrics.models import (
    CostEstimate,
    MetricSource,
    QuotaSnapshot,
    QuotaWindow,
    SessionMetrics,
    StepMetrics,
    TokenUsage,
    TokenUsageObservation,
    SessionGraphMetrics,
    TurnMetrics,
)
from coding_trajectory.metrics.pricing import estimate_observation_cost


@dataclass
class _CodexUsageState:
    previous_totals: TokenUsage | None = None
    remaining_inherited_totals: TokenUsage | None = None
    seen_totals: set[tuple[int, int, int, int, int, int, int, int, int]] | None = None


def build_session_graph_metrics(
    session_graph: SessionGraph,
    *,
    extra_billing: bool = False,
) -> dict[str, Any]:
    """Return token/quota metrics projected onto the session_graph hierarchy."""
    index = build_session_graph_index(session_graph)
    sessions_by_id = {session.session_id: session for session in session_graph.sessions}
    session_metrics: list[SessionMetrics] = []
    total = TokenUsage()
    cost_total = CostEstimate(extra_billing=extra_billing)
    warnings: list[str] = []

    for session in session_graph.sessions:
        metrics = _build_session_metrics(
            session,
            index=index,
            sessions_by_id=sessions_by_id,
            extra_billing=extra_billing,
        )
        session_metrics.append(metrics)
        total = total.plus(metrics.token_usage)
        cost_total = cost_total.plus(metrics.cost_estimate)
        if not _session_has_usage(metrics):
            warnings.append(f"no token usage metrics found for session {session.session_id}")
        if not metrics.cost_estimate.complete:
            warnings.extend(metrics.cost_estimate.missing_reasons)

    return SessionGraphMetrics(
        root_session_id=session_graph.root_session_id,
        token_usage=total,
        cost_estimate=_finalize_cost(cost_total),
        sessions=session_metrics,
        warnings=_unique(warnings),
    ).model_dump(mode="json")


def _build_session_metrics(
    session: Session,
    *,
    index: SessionGraphIndex,
    sessions_by_id: dict[UUID, Session],
    extra_billing: bool,
) -> SessionMetrics:
    codex_state = _CodexUsageState(
        remaining_inherited_totals=_inherited_codex_totals(session, sessions_by_id),
        seen_totals=set(),
    )
    turn_metrics: list[TurnMetrics] = []
    session_total = TokenUsage()
    cost_total = CostEstimate(extra_billing=extra_billing)
    latest_quota: QuotaSnapshot | None = None

    for turn in session.turns:
        metrics = _build_turn_metrics(
            session,
            turn,
            index=index,
            codex_state=codex_state,
            extra_billing=extra_billing,
        )
        turn_metrics.append(metrics)
        session_total = session_total.plus(metrics.token_usage)
        cost_total = cost_total.plus(metrics.cost_estimate)
        if metrics.quota_snapshots:
            latest_quota = metrics.quota_snapshots[-1]

    return SessionMetrics(
        session_id=session.session_id,
        vendor=session.vendor.value,
        status=session.status.value,
        token_usage=session_total,
        cost_estimate=_finalize_cost(cost_total),
        turns=turn_metrics,
        quota_snapshot=latest_quota,
    )


def _build_turn_metrics(
    session: Session,
    turn: Turn,
    *,
    index: SessionGraphIndex,
    codex_state: _CodexUsageState,
    extra_billing: bool,
) -> TurnMetrics:
    step_metrics: list[StepMetrics] = []
    turn_total = TokenUsage()
    cost_total = CostEstimate(extra_billing=extra_billing)
    quota_snapshots: list[QuotaSnapshot] = []

    for step in turn.steps:
        metrics = _build_step_metrics(
            session,
            step,
            index=index,
            codex_state=codex_state,
            extra_billing=extra_billing,
        )
        step_metrics.append(metrics)
        turn_total = turn_total.plus(metrics.token_usage)
        cost_total = cost_total.plus(metrics.cost_estimate)

    for event in events_for_turn(index, turn):
        quota = _quota_snapshot_from_event(event)
        if quota is not None:
            quota_snapshots.append(quota)

    quota_snapshots.sort(key=lambda item: item.timestamp)
    return TurnMetrics(
        turn_id=turn.turn_id,
        sequence=turn.sequence,
        status=turn.status.value,
        started_at=turn.started_at,
        completed_at=turn.ended_at,
        token_usage=turn_total,
        cost_estimate=_finalize_cost(cost_total),
        steps=step_metrics,
        quota_snapshots=quota_snapshots,
    )


def _build_step_metrics(
    session: Session,
    step: Step,
    *,
    index: SessionGraphIndex,
    codex_state: _CodexUsageState,
    extra_billing: bool,
) -> StepMetrics:
    observations: list[TokenUsageObservation] = []

    events = events_for_step(index, step)

    if not _step_has_usage_event(step, events):
        vendor_observation = _usage_from_step_vendor_data(step)
        if vendor_observation is not None:
            observations.append(vendor_observation)

    for event in events:
        event_observation = _usage_from_event(
            event,
            step=step,
            session=session,
            codex_state=codex_state,
        )
        if event_observation is not None:
            observations.append(event_observation)

    observations.sort(key=lambda item: item.timestamp)
    total = TokenUsage()
    cost_total = CostEstimate(extra_billing=extra_billing)
    for observation in observations:
        total = total.plus(observation.usage)
        cost_total = cost_total.plus(
            estimate_observation_cost(observation, extra_billing=extra_billing)
        )

    return StepMetrics(
        step_id=step.step_id,
        sequence=step.sequence,
        token_usage=total,
        cost_estimate=_finalize_cost(cost_total),
        observations=observations,
    )


def _usage_from_step_vendor_data(step: Step) -> TokenUsageObservation | None:
    data = step.vendor_data or {}
    provider = step.vendor.value
    normalized = data.get("metrics")
    if not isinstance(normalized, dict):
        return None

    usage = normalized.get("usage")
    if not isinstance(usage, dict):
        return None

    model = _as_str(normalized.get("model")) or _as_str(usage.get("model"))
    token_usage = _token_usage_from_mapping(usage)

    if _is_zero_usage(token_usage):
        return None

    return TokenUsageObservation(
        scope_type="step",
        scope_id=step.step_id,
        timestamp=step.timestamp,
        usage=token_usage,
        provider=provider,
        model=model,
        source=MetricSource(vendor=provider, source_type="step.vendor_data"),
    )


def _usage_from_event(
    event: Event,
    *,
    step: Step,
    session: Session,
    codex_state: _CodexUsageState,
) -> TokenUsageObservation | None:
    if event.type != EventType.VENDOR_RAW:
        return None
    if event.vendor_source != Vendor.CODEX_CLI:
        return None
    if event.payload.get("raw_type") != "token_count":
        return None

    metrics = event.payload.get("metrics")
    if not isinstance(metrics, dict):
        return None

    token_usage = _codex_delta_usage(metrics, codex_state)
    if token_usage is None or _is_zero_usage(token_usage):
        return None
    model = _as_str(metrics.get("model"))

    return TokenUsageObservation(
        scope_type="step",
        scope_id=step.step_id,
        timestamp=event.timestamp,
        usage=token_usage,
        provider=session.vendor.value,
        model=model,
        source=MetricSource(
            vendor=event.vendor_source.value,
            source_type="event.payload",
            event_id=event.event_id,
        ),
    )


def _codex_delta_usage(info: dict[str, Any], state: _CodexUsageState) -> TokenUsage | None:
    total_raw = info.get("total_token_usage")
    if isinstance(total_raw, dict):
        raw_totals = _token_usage_from_mapping(total_raw)
        total_key = _usage_key(raw_totals)
        if state.seen_totals is not None and total_key in state.seen_totals:
            return None
        if state.seen_totals is not None:
            state.seen_totals.add(total_key)

        current_totals = _subtract_usage(raw_totals, state.remaining_inherited_totals)
        previous_totals = state.previous_totals or TokenUsage()
        delta = _subtract_usage(current_totals, previous_totals)
        state.previous_totals = current_totals
        state.remaining_inherited_totals = None
        return delta

    last_raw = info.get("last_token_usage")
    if not isinstance(last_raw, dict):
        return None

    raw_delta = _token_usage_from_mapping(last_raw)
    delta = _subtract_usage(raw_delta, state.remaining_inherited_totals)
    state.remaining_inherited_totals = _subtract_usage(state.remaining_inherited_totals, raw_delta)
    previous_totals = state.previous_totals or TokenUsage()
    state.previous_totals = previous_totals.plus(delta)
    return delta


def _inherited_codex_totals(
    session: Session,
    sessions_by_id: dict[UUID, Session],
) -> TokenUsage | None:
    if session.vendor != Vendor.CODEX_CLI or session.parent_session_id is None:
        return None
    parent = sessions_by_id.get(session.parent_session_id)
    if parent is None or parent.vendor != Vendor.CODEX_CLI:
        return None

    state = _CodexUsageState(seen_totals=set())
    latest = TokenUsage()
    for event in sorted(parent.events, key=lambda item: item.timestamp):
        if event.timestamp > session.started_at:
            break
        if event.type != EventType.VENDOR_RAW:
            continue
        if event.vendor_source != Vendor.CODEX_CLI:
            continue
        if event.payload.get("raw_type") != "token_count":
            continue
        metrics = event.payload.get("metrics")
        if not isinstance(metrics, dict):
            continue
        delta = _codex_delta_usage(metrics, state)
        if delta is not None:
            latest = latest.plus(delta)

    return None if _is_zero_usage(latest) else latest


def _quota_snapshot_from_event(event: Event) -> QuotaSnapshot | None:
    if event.type != EventType.VENDOR_RAW:
        return None
    if event.vendor_source != Vendor.CODEX_CLI:
        return None
    if event.payload.get("raw_type") != "token_count":
        return None

    rate_limits = event.payload.get("quota")
    if not isinstance(rate_limits, dict):
        return None

    return QuotaSnapshot(
        timestamp=event.timestamp,
        source_event_id=event.event_id,
        limit_id=_as_str(rate_limits.get("limit_id")),
        plan_type=_as_str(rate_limits.get("plan_type")),
        primary=_quota_window(rate_limits.get("primary")),
        secondary=_quota_window(rate_limits.get("secondary")),
        rate_limit_reached_type=_as_str(rate_limits.get("rate_limit_reached_type")),
    )


def _quota_window(value: Any) -> QuotaWindow | None:
    if not isinstance(value, dict):
        return None
    return QuotaWindow(
        used_percent=_as_float(value.get("used_percent")),
        window_minutes=_as_int(value.get("window_minutes")),
        resets_at=_as_int(value.get("resets_at")),
    )


def _token_usage_from_mapping(value: dict[str, Any]) -> TokenUsage:
    cache_creation = value.get("cache_creation")
    cache_creation_5m = 0
    cache_creation_1h = 0
    if isinstance(cache_creation, dict):
        cache_creation_5m = _as_int(cache_creation.get("ephemeral_5m_input_tokens"))
        cache_creation_1h = _as_int(cache_creation.get("ephemeral_1h_input_tokens"))

    return TokenUsage(
        input_tokens=_as_int(value.get("input_tokens") or value.get("inputTokens")),
        cached_input_tokens=_as_int(value.get("cached_input_tokens") or value.get("cachedInputTokens")),
        cache_creation_input_tokens=_as_int(
            value.get("cache_creation_input_tokens") or value.get("cacheCreationInputTokens")
        ),
        cache_creation_5m_input_tokens=cache_creation_5m,
        cache_creation_1h_input_tokens=cache_creation_1h,
        cache_read_input_tokens=_as_int(value.get("cache_read_input_tokens") or value.get("cacheReadInputTokens")),
        output_tokens=_as_int(value.get("output_tokens") or value.get("outputTokens")),
        reasoning_output_tokens=_as_int(
            value.get("reasoning_output_tokens") or value.get("reasoningOutputTokens")
        ),
        total_tokens=_as_int(value.get("total_tokens") or value.get("totalTokens")),
    )


def _usage_key(usage: TokenUsage) -> tuple[int, int, int, int, int, int, int, int, int]:
    return (
        usage.input_tokens,
        usage.cached_input_tokens,
        usage.cache_creation_input_tokens,
        usage.cache_creation_5m_input_tokens,
        usage.cache_creation_1h_input_tokens,
        usage.cache_read_input_tokens,
        usage.output_tokens,
        usage.reasoning_output_tokens,
        usage.total_tokens,
    )


def _subtract_usage(left: TokenUsage | None, right: TokenUsage | None) -> TokenUsage:
    left = left or TokenUsage()
    right = right or TokenUsage()
    return TokenUsage(
        input_tokens=max(left.input_tokens - right.input_tokens, 0),
        cached_input_tokens=max(left.cached_input_tokens - right.cached_input_tokens, 0),
        cache_creation_input_tokens=max(left.cache_creation_input_tokens - right.cache_creation_input_tokens, 0),
        cache_creation_5m_input_tokens=max(left.cache_creation_5m_input_tokens - right.cache_creation_5m_input_tokens, 0),
        cache_creation_1h_input_tokens=max(left.cache_creation_1h_input_tokens - right.cache_creation_1h_input_tokens, 0),
        cache_read_input_tokens=max(left.cache_read_input_tokens - right.cache_read_input_tokens, 0),
        output_tokens=max(left.output_tokens - right.output_tokens, 0),
        reasoning_output_tokens=max(left.reasoning_output_tokens - right.reasoning_output_tokens, 0),
        total_tokens=max(left.total_tokens - right.total_tokens, 0),
    )


def _session_has_usage(metrics: SessionMetrics) -> bool:
    return not _is_zero_usage(metrics.token_usage)


def _is_zero_usage(usage: TokenUsage) -> bool:
    return all(value == 0 for value in usage.model_dump().values())


def _step_has_usage_event(step: Step, events: list[Event]) -> bool:
    if step.vendor != Vendor.CODEX_CLI:
        return False
    return any(
        event.type == EventType.VENDOR_RAW
        and event.vendor_source == Vendor.CODEX_CLI
        and event.payload.get("raw_type") == "token_count"
        for event in events
    )


def _finalize_cost(cost: CostEstimate) -> CostEstimate:
    return cost.model_copy(
        update={
            "amount_usd": round(cost.amount_usd, 8),
            "missing_reasons": _unique(cost.missing_reasons),
            "breakdown": cost.breakdown.model_copy(
                update={
                    "input_usd": round(cost.breakdown.input_usd, 8),
                    "cached_input_usd": round(cost.breakdown.cached_input_usd, 8),
                    "cache_creation_usd": round(cost.breakdown.cache_creation_usd, 8),
                    "cache_creation_5m_usd": round(cost.breakdown.cache_creation_5m_usd, 8),
                    "cache_creation_1h_usd": round(cost.breakdown.cache_creation_1h_usd, 8),
                    "cache_read_usd": round(cost.breakdown.cache_read_usd, 8),
                    "output_usd": round(cost.breakdown.output_usd, 8),
                    "reasoning_output_usd": round(cost.breakdown.reasoning_output_usd, 8),
                }
            ),
        }
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


def _as_float(value: Any) -> float | None:
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return float(value)
    return None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
