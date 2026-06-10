"""Build derived execution metrics from canonical session_graphs."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from uuid import UUID

from coding_trajectory.ingestion.models import (
    ContextUsageObservation,
    Item,
    Session,
    SessionGraph,
    Turn,
    is_tool_shaped_item,
)
from coding_trajectory.metrics.models import (
    AttributionPolicy,
    CostEstimate,
    InvokeResponseTokens,
    MetricSource,
    QuotaSnapshot,
    QuotaWindow,
    ReadAfterResult,
    SessionMetrics,
    SessionMetricsFlat,
    SessionUsageCompactFlat,
    ToolItemFlat,
    ToolTokenAttribution,
    TokenUsage,
    TokenUsageObservation,
    SessionGraphMetrics,
    SessionGraphMetricsFlat,
    SessionGraphToolUsageFlat,
    TurnRuntimeFlat,
    TurnUsageCompactFlat,
    TurnMetrics,
    TurnMetricsFlat,
)
from coding_trajectory.metrics.context_stats._common import runtime_stats


def build_session_graph_metrics(
    session_graph: SessionGraph,
    *,
    extra_billing: bool = False,
) -> dict[str, Any]:
    """Return a flat usage summary: sessions -> turns."""
    full = _build_full_metrics(session_graph, extra_billing=extra_billing)
    sessions_flat: list[SessionMetricsFlat] = []

    for session in full.sessions:
        turns_flat: list[TurnMetricsFlat] = []
        for turn in session.turns:
            model = _turn_model(turn)
            turns_flat.append(
                TurnMetricsFlat(
                    turn_id=turn.turn_id,
                    sequence=turn.sequence,
                    status=turn.status,
                    started_at=turn.started_at,
                    completed_at=turn.completed_at,
                    model=model,
                    token_usage=turn.token_usage,
                    cost=_reported_cost_amount(turn.cost_estimate),
                    extra_billing=turn.cost_estimate.extra_billing,
                )
            )
        sessions_flat.append(
            SessionMetricsFlat(
                session_id=session.session_id,
                vendor=session.vendor,
                status=session.status,
                token_usage=session.token_usage,
                cost=_reported_cost_amount(session.cost_estimate),
                extra_billing=session.cost_estimate.extra_billing,
                turns=turns_flat,
            )
        )

    return SessionGraphMetricsFlat(
        root_session_id=full.root_session_id,
        token_usage=full.token_usage,
        cost=_reported_cost_amount(full.cost_estimate),
        extra_billing=full.cost_estimate.extra_billing,
        sessions=sessions_flat,
        warnings=full.warnings,
    ).model_dump(mode="json")


def build_session_graph_full_metrics(
    session_graph: SessionGraph,
    *,
    extra_billing: bool = False,
) -> SessionGraphMetrics:
    """Return full metrics for callers that need multiple derived views."""
    return _build_full_metrics(session_graph, extra_billing=extra_billing)


def build_session_graph_context_stats(session_graph: SessionGraph) -> dict[str, Any]:
    """Return provider-specific context-window stats by dispatching to a vendor handler."""
    from coding_trajectory.metrics.context_stats import build_session_graph_context_stats as dispatch

    return dispatch(session_graph)


def build_session_graph_usage(
    session_graph: SessionGraph,
    *,
    extra_billing: bool = False,
    turn_id: str | None = None,
) -> dict[str, Any]:
    """Return compact turn-level token usage and log-reported cost accounting."""
    full = _build_full_metrics(session_graph, extra_billing=extra_billing)
    multi_session = len(full.sessions) > 1
    turns: list[TurnUsageCompactFlat] = []

    for session in full.sessions:
        for index, turn in enumerate(session.turns):
            if turn_id is not None and str(turn.turn_id) != turn_id:
                continue
            previous_turn = session.turns[index - 1] if index > 0 else None
            turns.append(
                _compact_turn_usage(
                    turn,
                    session_id=session.session_id if multi_session else None,
                    execution_seconds=_turn_execution_seconds(turn),
                    wait_before_seconds=_turn_wait_before_seconds(previous_turn, turn),
                )
            )

    return SessionUsageCompactFlat(
        session_id=full.root_session_id,
        extra_billing=full.cost_estimate.extra_billing,
        runtime=runtime_stats(session_graph),
        turns=turns,
        total_usage=full.token_usage,
        cost_usd=_reported_cost_amount(full.cost_estimate),
        warnings=full.warnings,
    ).model_dump(mode="json")


def _compact_turn_usage(
    turn: TurnMetrics,
    *,
    session_id: UUID | None,
    execution_seconds: int | None,
    wait_before_seconds: int | None,
) -> TurnUsageCompactFlat:
    return TurnUsageCompactFlat(
        turn_id=turn.turn_id,
        session_id=session_id,
        runtime=_turn_runtime(
            turn,
            execution_seconds=execution_seconds,
            wait_before_seconds=wait_before_seconds,
        ),
        usage=turn.token_usage,
        cost_usd=_reported_cost_amount(turn.cost_estimate),
    )


def _turn_runtime(
    turn: TurnMetrics,
    *,
    execution_seconds: int | None,
    wait_before_seconds: int | None,
) -> TurnRuntimeFlat:
    return TurnRuntimeFlat(
        started_at=turn.started_at,
        ended_at=turn.completed_at,
        execution_seconds=execution_seconds,
        wait_before_seconds=wait_before_seconds,
    )


def _turn_wait_before_seconds(previous_turn: TurnMetrics | None, turn: TurnMetrics) -> int | None:
    if previous_turn is None or previous_turn.completed_at is None or turn.started_at is None:
        return None
    return max(round((turn.started_at - previous_turn.completed_at).total_seconds()), 0)


def _turn_execution_seconds(turn: TurnMetrics) -> int | None:
    if turn.started_at is None or turn.completed_at is None:
        return None
    return max(round((turn.completed_at - turn.started_at).total_seconds()), 0)


def _reported_cost_amount(cost: CostEstimate) -> float | None:
    return cost.amount_usd if cost.complete else None


def build_session_graph_tool_usage(
    session_graph: SessionGraph,
    *,
    extra_billing: bool = False,
) -> dict[str, Any]:
    """Return per-tool-item output size signals with visible-content token attribution."""
    full = _build_full_metrics(session_graph, extra_billing=extra_billing)

    tool_items: list[ToolItemFlat] = []
    for session in session_graph.sessions:
        for turn in session.turns:
            turn_observations = _turn_usage_observations(session, turn)
            tool_items.extend(
                _build_tool_items_for_turn(
                    turn,
                    session_id=session.session_id,
                    turn_observations=turn_observations,
                )
            )

    return SessionGraphToolUsageFlat(
        root_session_id=full.root_session_id,
        tool_item_count=len(tool_items),
        tool_call_count=len(tool_items),
        tool_output_chars=sum(item.output_chars for item in tool_items),
        tool_output_original_tokens=sum(item.output_original_tokens or 0 for item in tool_items),
        tool_items=tool_items,
        attribution_policy=AttributionPolicy(),
        warnings=full.warnings,
    ).model_dump(mode="json")


def _turn_usage_observations(
    session: Session,
    turn: Turn,
) -> list[TokenUsageObservation]:
    event_ids = set(turn.event_ids)
    observations = [
        obs
        for obs in session.context_usage
        if obs.source_event_id in event_ids
    ]
    token_observations: list[TokenUsageObservation] = []
    for observation in observations:
        usage = _token_usage_from_mapping(observation.usage)
        if _is_zero_usage(usage):
            continue
        token_observations.append(
            TokenUsageObservation(
                scope_type="turn",
                scope_id=turn.turn_id,
                timestamp=observation.timestamp,
                usage=usage,
                provider=observation.provider or session.vendor.value,
                model=observation.model,
                source=MetricSource(
                    vendor=session.vendor.value,
                    source_type="session.context_usage",
                    event_id=observation.source_event_id,
                ),
            )
        )
    token_observations.sort(key=lambda item: item.timestamp)
    return token_observations


def _build_tool_items_for_turn(
    turn: Turn,
    *,
    session_id: UUID,
    turn_observations: list[TokenUsageObservation],
) -> list[ToolItemFlat]:
    tool_entries = [item for item in turn.items if is_tool_shaped_item(item)]
    if not tool_entries:
        return []

    tool_entries_sorted = sorted(tool_entries, key=lambda item: item.started_at)
    groups: list[tuple[list[Any], TokenUsageObservation | None]] = []
    current: list[Any] = []
    current_signature: tuple[Any, Any] | None = None

    for item in tool_entries_sorted:
        signature = _item_observation_signature(item, turn_observations)
        if current_signature is None:
            current_signature = signature
        if signature != current_signature:
            groups.append((current, _group_observation(current, turn_observations)))
            current = []
            current_signature = signature
        current.append(item)
    if current:
        groups.append((current, _group_observation(current, turn_observations)))

    items: list[ToolItemFlat] = []
    for group_items, invoke_obs in groups:
        count = len(group_items)
        for item in group_items:
            base = _tool_item_flat(
                item,
                session_id=session_id,
                turn_id=turn.turn_id,
            )
            base.token_attribution = _build_token_attribution(item)
            base.invoke_response_tokens = _build_invoke_response_tokens(
                invoke_obs, count=count
            )
            base.read_after_result = _build_read_after_result(
                item,
                invoke_observation=invoke_obs,
                turn_observations=turn_observations,
            )
            items.append(base)
    return items


def _item_observation_signature(
    item: Any,
    turn_observations: list[TokenUsageObservation],
) -> tuple[Any, Any]:
    anchor_start = item.started_at
    previous = _previous_observation_before(turn_observations, anchor_start)
    next_obs = _next_observation_after(turn_observations, anchor_start)
    return (
        previous.source.event_id if previous is not None else None,
        next_obs.source.event_id if next_obs is not None else None,
    )


def _previous_observation_before(
    turn_observations: list[TokenUsageObservation],
    anchor: datetime,
) -> TokenUsageObservation | None:
    for observation in reversed(turn_observations):
        if observation.timestamp < anchor:
            return observation
    return None


def _group_observation(
    group_items: list[Any],
    turn_observations: list[TokenUsageObservation],
) -> TokenUsageObservation | None:
    anchor = max(item.started_at for item in group_items)
    return _next_observation_after(turn_observations, anchor)


def _next_observation_after(
    turn_observations: list[TokenUsageObservation],
    anchor: datetime,
) -> TokenUsageObservation | None:
    for observation in turn_observations:
        if observation.timestamp > anchor:
            return observation
    return None


def _build_token_attribution(item: Any) -> ToolTokenAttribution:
    output_text = _tool_output_text(item)
    output_original = _tool_original_token_count(output_text)
    if output_original is not None:
        output_tokens = output_original
        confidence = "observed_tool_output_token_count"
    elif output_text:
        output_tokens = _estimate_tokens(output_text)
        confidence = "visible_content_estimate"
    else:
        output_tokens = 0
        confidence = "no_visible_content"

    input_text = _tool_input_text(item)
    input_tokens = _estimate_tokens(input_text) if input_text else 0

    return ToolTokenAttribution(
        tool_input_tokens=input_tokens,
        tool_output_tokens=output_tokens,
        content_confidence=confidence,
    )


def _build_invoke_response_tokens(
    observation: TokenUsageObservation | None,
    *,
    count: int,
) -> InvokeResponseTokens | None:
    if observation is None:
        return None
    output = int(observation.usage.output_tokens)
    reasoning = int(observation.usage.reasoning_output_tokens)
    if count <= 0 or (output == 0 and reasoning == 0):
        return None
    if count == 1:
        return InvokeResponseTokens(
            output_tokens=output,
            reasoning_output_tokens=reasoning,
            attribution="single_tool_response",
        )
    shared_output = output // count
    shared_reasoning = reasoning // count
    return InvokeResponseTokens(
        output_tokens=shared_output,
        reasoning_output_tokens=shared_reasoning,
        attribution="shared_model_response",
    )


def _build_read_after_result(
    item: Any,
    *,
    invoke_observation: TokenUsageObservation | None,
    turn_observations: list[TokenUsageObservation],
) -> ReadAfterResult:
    anchor = item.completed_at or item.started_at
    if invoke_observation is not None and invoke_observation.timestamp > anchor:
        anchor = invoke_observation.timestamp
    later = [obs for obs in turn_observations if obs.timestamp > anchor]
    if later:
        return ReadAfterResult(
            included_in_turn_usage=True,
            attribution="causal_next_model_request",
        )
    return ReadAfterResult(
        included_in_turn_usage=False,
        attribution="turn_completed_without_reuse",
    )


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _tool_output_text(item: Any) -> str:
    output = getattr(item, "output", None)
    return "" if output is None else str(output)


def _tool_input_text(item: Any) -> str:
    if item.kind == "command_execution":
        value = getattr(item, "command", None)
    else:
        value = getattr(item, "input", None)
    if value is None:
        return ""
    if isinstance(value, dict):
        import json

        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _tool_item_flat(item: Item, *, session_id: UUID, turn_id: UUID) -> ToolItemFlat:
    output = _tool_output_text(item)
    return ToolItemFlat(
        item_id=item.item_id,
        session_id=session_id,
        turn_id=turn_id,
        tool_name=getattr(item, "tool_name", None),
        status=getattr(item, "status", None),
        input_summary=_tool_input_summary(getattr(item, "input", None) if item.kind != "command_execution" else getattr(item, "command", None)),
        output_chars=len(output),
        output_original_tokens=_tool_original_token_count(output),
        output_truncated=_tool_output_is_truncated(output),
    )


def _tool_input_summary(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("cmd", "command", "path", "pattern", "query"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return _compact_text(candidate)
    return _compact_text(str(value))


def _compact_text(value: str, *, limit: int = 240) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _tool_original_token_count(output: str) -> int | None:
    match = re.search(r"Original token count: (\d+)", output)
    if match is None:
        return None
    return int(match.group(1))


def _tool_output_is_truncated(output: str) -> bool:
    return "chars → event.detail" in output or "tokens truncated" in output


def _turn_model(turn: TurnMetrics) -> str | None:
    for obs in turn.observations:
        if obs.model:
            return obs.model
    return None


def _build_full_metrics(
    session_graph: SessionGraph,
    *,
    extra_billing: bool = False,
) -> SessionGraphMetrics:
    """Return full token/quota metrics projected onto the session_graph hierarchy."""
    session_metrics: list[SessionMetrics] = []
    total = TokenUsage()
    cost_total = CostEstimate(extra_billing=extra_billing)
    warnings: list[str] = []

    for session in session_graph.sessions:
        metrics = _build_session_metrics(
            session,
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
    )


def _build_session_metrics(
    session: Session,
    *,
    extra_billing: bool,
) -> SessionMetrics:
    turn_metrics: list[TurnMetrics] = []
    session_total = TokenUsage()
    cost_total = CostEstimate(extra_billing=extra_billing)
    latest_quota: QuotaSnapshot | None = None

    for turn in session.turns:
        metrics = _build_turn_metrics(
            session,
            turn,
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
    extra_billing: bool,
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
    vendor_cost = _vendor_reported_cost(
        turn,
        context_observations=context_observations,
        observations=observations,
        extra_billing=extra_billing,
    )
    cost_total = vendor_cost or (
        _missing_reported_cost(observations, extra_billing=extra_billing)
        if observations
        else CostEstimate(extra_billing=extra_billing)
    )
    for observation in observations:
        total = total.plus(observation.usage)

    quota_snapshots: list[QuotaSnapshot] = []
    for observation in context_observations:
        quota = _quota_snapshot_from_context_usage(observation)
        if quota is not None:
            quota_snapshots.append(quota)
    quota_snapshots.sort(key=lambda item: item.timestamp)

    return TurnMetrics(
        turn_id=turn.turn_id,
        sequence=turn.sequence,
        status=turn.status.value,
        started_at=turn.started_at,
        completed_at=turn.ended_at,
        token_usage=total,
        cost_estimate=_finalize_cost(cost_total),
        observations=observations,
        quota_snapshots=quota_snapshots,
    )


def _vendor_reported_cost(
    turn: Turn,
    *,
    context_observations: list[ContextUsageObservation],
    observations: list[TokenUsageObservation],
    extra_billing: bool,
) -> CostEstimate | None:
    amount = next(
        (
            value
            for observation in context_observations
            if (value := _as_float(observation.usage.get("cost_usd"))) is not None
        ),
        None,
    )
    if amount is None:
        return None

    model = next((observation.model for observation in observations if observation.model), None)
    return CostEstimate(
        amount_usd=amount,
        extra_billing=extra_billing,
        pricing_source="vendor_reported",
        pricing_effective_date=turn.started_at.date().isoformat(),
        model=model,
        complete=True,
    )


def _missing_reported_cost(
    observations: list[TokenUsageObservation],
    *,
    extra_billing: bool,
) -> CostEstimate:
    model = next((observation.model for observation in observations if observation.model), None)
    return CostEstimate(
        extra_billing=extra_billing,
        model=model,
        complete=False,
        missing_reasons=["cost not reported in session log"],
    )


def _usage_from_context_observation(
    observation: ContextUsageObservation,
    *,
    turn: Turn,
    session: Session,
) -> TokenUsageObservation | None:
    token_usage = _token_usage_from_mapping(observation.usage)
    if token_usage is None or _is_zero_usage(token_usage):
        return None
    provider = observation.provider or session.vendor.value

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


def _quota_snapshot_from_context_usage(
    observation: ContextUsageObservation,
) -> QuotaSnapshot | None:
    rate_limits = observation.quota
    if not isinstance(rate_limits, dict) or observation.source_event_id is None:
        return None

    return QuotaSnapshot(
        timestamp=observation.timestamp,
        source_event_id=observation.source_event_id,
        limit_id=_as_str(rate_limits.get("limit_id")),
        limit_name=_as_str(rate_limits.get("limit_name")),
        plan_type=_as_str(rate_limits.get("plan_type")),
        primary=_quota_window(rate_limits.get("primary")),
        secondary=_quota_window(rate_limits.get("secondary")),
        credits=rate_limits.get("credits") if isinstance(rate_limits.get("credits"), dict) else None,
        individual_limit=(
            rate_limits.get("individual_limit")
            if isinstance(rate_limits.get("individual_limit"), dict)
            else None
        ),
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
    return TokenUsage(
        input_tokens=_as_int(value.get("input_tokens") or value.get("inputTokens")),
        cached_input_tokens=_as_int(value.get("cached_input_tokens") or value.get("cachedInputTokens")),
        cache_creation_input_tokens=_as_int(
            value.get("cache_creation_input_tokens") or value.get("cacheCreationInputTokens")
        ),
        output_tokens=_as_int(value.get("output_tokens") or value.get("outputTokens")),
        reasoning_output_tokens=_as_int(
            value.get("reasoning_output_tokens") or value.get("reasoningOutputTokens")
        ),
        total_tokens=_as_int(value.get("total_tokens") or value.get("totalTokens")),
    )


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
    return all(value == 0 for value in usage.model_dump().values())


def _finalize_cost(cost: CostEstimate) -> CostEstimate:
    return cost.model_copy(
        update={
            "amount_usd": round(cost.amount_usd, 8),
            "missing_reasons": _unique(cost.missing_reasons),
            "breakdown": cost.breakdown.model_copy(
                update={
                    "input_usd": round(cost.breakdown.input_usd, 8),
                    "cached_input_usd": round(cost.breakdown.cached_input_usd, 8),
                    "cache_creation_input_usd": round(cost.breakdown.cache_creation_input_usd, 8),
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
