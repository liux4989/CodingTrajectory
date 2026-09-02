"""Build derived execution metrics from canonical session_graphs."""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Any, Iterable
from uuid import UUID

import numpy as np

from coding_trajectory.analysis.content_size import (
    scoped_content_size_cache,
)
from coding_trajectory.analysis.session_stats import (
    session_graph_title,
    session_role,
    session_title,
)
from coding_trajectory.ingestion.indexes import (
    SessionGraphIndex,
    build_session_graph_index,
    ordered_sessions,
)
from coding_trajectory.ingestion.models import (
    ContextUsageObservation,
    Session,
    SessionGraph,
    Turn,
    is_tool_shaped_item,
)
from coding_trajectory.metrics._build import (
    _build_full_metrics,
    _is_zero_usage,
    _usage_from_context_observation,
)
from coding_trajectory.metrics.accounting import usage_accounting_payload
from coding_trajectory.metrics.context_stats._common import (
    compaction_stats,
    effort_change_stats,
    runtime_stats,
)
from coding_trajectory.metrics.models import (
    AllocatedRealTokenCost,
    AttributionPolicy,
    DominantModelFlat,
    ItemRealTokenCostFlat,
    ModelUsageContextFlat,
    ModelUsageModelFlat,
    ModelUsageTurnFlat,
    RequestUsageFlat,
    SessionGraphMetrics,
    SessionGraphModelUsageFlat,
    SessionGraphRequestUsageFlat,
    SessionMetrics,
    SessionUsageCompactFlat,
    ToolItemFlat,
    TokenUsage,
    TokenUsageObservation,
    SessionGraphToolUsageFlat,
    TurnMetrics,
    TurnRuntimeFlat,
    TurnUsageCompactFlat,
)
from coding_trajectory.metrics.pricing import (
    CostEvidenceFlat,
    PriceRule,
    cache_break_waste_usd,
    cost_evidence_from_usage,
    get_model_context_window,
)
from coding_trajectory.metrics.throughput import processed_tokens_per_second
from coding_trajectory.token_counter import get_current_counter, session_scoped
from coding_trajectory.metrics.analysis_item_costs import (
    _CostAccum,
    _add_allocated_real_token_cost,
    _allocated_cost_from_usage_observation,
    _allocated_cost_usage_dict,
    _allocated_cost_usage_dict_from_array,
    _build_item_real_token_cost_projection_for_session,
    _build_tool_items_for_turn,
    _session_usage_observations,
    _stats_cost_entries_for_session,
    _sum_usage_dicts,
    _token_cost_allocations,
)


def build_session_graph_full_metrics(
    session_graph: SessionGraph,
) -> SessionGraphMetrics:
    """Return full metrics for callers that need multiple derived views."""
    return _build_full_metrics(session_graph)


def build_session_graph_context_stats(
    session_graph: SessionGraph,
    *,
    allocated_usage_by_item: dict[UUID, dict[str, int]] | None = None,
    allocated_usage_by_context_source: dict[str, dict[str, int]] | None = None,
    include_composition: bool = True,
    precomputed_metrics: SessionGraphMetrics | SessionMetrics | None = None,
) -> dict[str, Any]:
    """Return provider-specific context-window stats by dispatching to a vendor handler."""
    from coding_trajectory.metrics.context_stats import (
        build_session_graph_context_stats as dispatch,
    )

    return dispatch(
        session_graph,
        allocated_usage_by_item=allocated_usage_by_item,
        allocated_usage_by_context_source=allocated_usage_by_context_source,
        include_composition=include_composition,
        precomputed_metrics=precomputed_metrics,
    )


def build_session_graph_usage(
    session_graph: SessionGraph,
    *,
    turn_id: str | None = None,
    include_graph_turns: bool = True,
    precomputed_metrics: SessionGraphMetrics | None = None,
    precomputed_index: SessionGraphIndex | None = None,
) -> dict[str, Any]:
    """Return compact turn-level token usage accounting.

    Graph projections may omit the graph-level turn list (the same turns remain
    available in the per-session sections). Single-session projections retain
    their top-level turns regardless.
    """
    full = precomputed_metrics or _build_full_metrics(session_graph)
    index = precomputed_index or build_session_graph_index(session_graph)
    multi_session = len(full.sessions) > 1
    session_metrics_by_id = {session.session_id: session for session in full.sessions}
    turns: list[TurnUsageCompactFlat] = []
    session_sections: list[dict[str, Any]] = []
    selected_graph_turns: list[TurnMetrics] = []

    for source_session in ordered_sessions(index):
        session = session_metrics_by_id.get(source_session.session_id)
        if session is None:
            continue
        session_turns: list[TurnUsageCompactFlat] = []
        for turn_index, turn in enumerate(session.turns):
            if turn_id is not None and str(turn.turn_id) != turn_id:
                continue
            selected_graph_turns.append(turn)
            previous_turn = session.turns[turn_index - 1] if turn_index > 0 else None
            compact_turn = _compact_turn_usage(
                turn,
                previous_turn=previous_turn,
                session_id=session.session_id if multi_session else None,
                execution_seconds=_turn_execution_seconds(turn),
                wait_before_seconds=_turn_wait_before_seconds(previous_turn, turn),
            )
            if not multi_session or include_graph_turns:
                turns.append(compact_turn)
            session_turns.append(compact_turn)

        if turn_id is not None and not session_turns:
            continue

        selected_session_turns = [
            turn
            for turn in session.turns
            if turn_id is None or str(turn.turn_id) == turn_id
        ]
        session_usage = _sum_turn_usage(selected_session_turns)
        single = _single_session_graph(session_graph, source_session)
        model_breakdown = _model_usage_breakdown(selected_session_turns)
        estimated_cost = _aggregate_model_cost(model_breakdown)
        section = {
            "session_id": str(session.session_id),
            "role": session_role(
                source_session, session_graph=session_graph, index=index
            ),
            "relationship": index.incoming_edge_type.get(source_session.session_id),
            "parent_session_id": (
                str(index.parent[source_session.session_id])
                if index.parent.get(source_session.session_id)
                else None
            ),
            "agent_name": source_session.agent_name,
            "title": session_title(source_session),
            "runtime": runtime_stats(
                single,
                session_metrics=(session,),
            ).model_dump(mode="json"),
            "compaction": _optional_model_dump(compaction_stats(single)),
            "effort_changes": effort_change_stats(single).model_dump(mode="json"),
            "turns": [turn.model_dump(mode="json") for turn in session_turns],
            "total_usage": _token_usage_payload(session_usage),
            "models": [row.model_dump(mode="json") for row in model_breakdown],
            "estimated_cost": (
                estimated_cost.model_dump(mode="json")
                if session_usage.total_tokens and estimated_cost is not None
                else None
            ),
        }
        session_sections.append(section)

    selected_usage = _sum_turn_usage(selected_graph_turns)
    graph_model_breakdown = _model_usage_breakdown(selected_graph_turns)
    payload = SessionUsageCompactFlat(
        session_id=full.root_session_id,
        runtime=runtime_stats(
            session_graph,
            session_metrics=full.sessions,
        ),
        turns=turns,
        total_usage=selected_usage,
        estimated_cost=_aggregate_model_cost(graph_model_breakdown),
        compaction=compaction_stats(session_graph),
        effort_changes=effort_change_stats(session_graph),
        warnings=full.warnings,
    ).model_dump(mode="json")
    payload["scope"] = "session_graph" if multi_session else "session"
    if turn_id is not None:
        payload["selected_turn_id"] = turn_id
    if multi_session and not include_graph_turns:
        payload.pop("turns", None)
    payload["models"] = [row.model_dump(mode="json") for row in graph_model_breakdown]
    if multi_session:
        payload["sessions"] = session_sections
    else:
        payload.pop("sessions", None)
    return payload


def build_session_graph_model_usage(
    session_graph: SessionGraph,
    *,
    turn_id: str | None = None,
    precomputed_metrics: SessionGraphMetrics | None = None,
) -> dict[str, Any]:
    """Return provider/model usage facts at session and turn granularity."""
    full = precomputed_metrics or _build_full_metrics(session_graph)
    sessions_by_id = {session.session_id: session for session in session_graph.sessions}
    root_session = sessions_by_id.get(session_graph.root_session_id)
    turns: list[ModelUsageTurnFlat] = []

    for session_metrics in full.sessions:
        source_session = sessions_by_id.get(session_metrics.session_id)
        for turn in session_metrics.turns:
            if turn_id is not None and str(turn.turn_id) != turn_id:
                continue
            groups = _model_groups_for_turn(turn)
            primary = _dominant_group(groups)
            turns.append(
                ModelUsageTurnFlat(
                    turn_id=turn.turn_id,
                    session_id=session_metrics.session_id,
                    vendor=session_metrics.vendor,
                    sequence=turn.sequence,
                    started_at=turn.started_at,
                    completed_at=turn.completed_at,
                    model_active_seconds=(
                        turn.model_active_seconds if len(groups) == 1 else None
                    ),
                    processed_tokens_per_second=(
                        processed_tokens_per_second(
                            turn.token_usage.processed_token_total(),
                            turn.model_active_seconds,
                        )
                        if len(groups) == 1
                        else None
                    ),
                    provider=primary.provider if primary else None,
                    model=primary.model if primary else None,
                    usage=turn.token_usage,
                    models=groups,
                    context=_context_for_turn(source_session, turn.turn_id)
                    if source_session
                    else None,
                    estimated_cost=_aggregate_model_cost(groups),
                )
            )

    models = _model_usage_breakdown(
        turn
        for session in full.sessions
        for turn in session.turns
        if turn_id is None or str(turn.turn_id) == turn_id
    )
    dominant = _dominant_group(models)
    selected_usage = _sum_turn_usage(
        turn
        for session in full.sessions
        for turn in session.turns
        if turn_id is None or str(turn.turn_id) == turn_id
    )

    return SessionGraphModelUsageFlat(
        root_session_id=session_graph.root_session_id,
        vendor=root_session.vendor.value if root_session else None,
        project=session_graph.project_identifier,
        title=session_graph_title(session_graph),
        started_at=min(
            (session.started_at for session in session_graph.sessions),
            default=None,
        ),
        completed_at=max(
            (
                session.ended_at
                for session in session_graph.sessions
                if session.ended_at
            ),
            default=None,
        ),
        usage=selected_usage,
        model_active_seconds=full.model_active_seconds,
        processed_tokens_per_second=full.processed_tokens_per_second,
        context=_context_for_session_graph(session_graph),
        models=models,
        dominant_model=DominantModelFlat(
            provider=dominant.provider,
            model=dominant.model,
            basis="total_tokens",
        )
        if dominant
        else None,
        turns=turns,
        warnings=full.warnings,
    ).model_dump(mode="json")


def build_session_graph_request_usage(
    session_graph: SessionGraph,
    *,
    turn_id: str | None = None,
    include_causality: bool = True,
    include_context_diagnostics: bool = True,
) -> dict[str, Any]:
    """Return the exact provider-request usage ledger.

    Causal tool links and request-context diagnostics are independently
    projectable payload details. Usage, pricing, ordering, and request identity
    are unaffected. Defaults preserve the legacy response shape.
    """
    full = _build_full_metrics(session_graph)
    requests: list[RequestUsageFlat] = []

    for session in session_graph.sessions:
        context_by_event_id = (
            {
                item.source_event_id: item
                for item in session.context_usage
                if item.source_event_id is not None
            }
            if include_context_diagnostics
            else {}
        )
        for turn in sorted(session.turns, key=lambda item: item.sequence):
            if turn_id is not None and str(turn.turn_id) != turn_id:
                continue
            observations = _turn_usage_observations(session, turn)
            tool_items = (
                sorted(
                    (item for item in turn.items if is_tool_shaped_item(item)),
                    key=lambda item: (item.started_at, item.sequence),
                )
                if include_causality
                else []
            )
            previous_timestamp = turn.started_at
            previous_context_used: int | None = None
            for sequence, observation in enumerate(observations, start=1):
                context_observation = (
                    context_by_event_id.get(observation.source.event_id)
                    if include_context_diagnostics
                    else None
                )
                context_used = (
                    context_observation.used_input_tokens
                    if context_observation is not None
                    else None
                )
                context_window = (
                    (
                        context_observation.context_window_tokens
                        if context_observation is not None
                        else None
                    )
                    or get_model_context_window(
                        observation.model,
                        provider=observation.provider,
                    )
                    if include_context_diagnostics
                    else None
                )
                invoked = (
                    [
                        item
                        for item in tool_items
                        if previous_timestamp < item.started_at <= observation.timestamp
                    ]
                    if include_causality
                    else []
                )
                consumed = (
                    [
                        item
                        for item in tool_items
                        if item.completed_at is not None
                        and previous_timestamp
                        < item.completed_at
                        <= observation.timestamp
                    ]
                    if include_causality
                    else []
                )
                requests.append(
                    RequestUsageFlat(
                        usage_event_id=observation.source.event_id,
                        session_id=session.session_id,
                        turn_id=turn.turn_id,
                        sequence=sequence,
                        timestamp=observation.timestamp,
                        provider=observation.provider,
                        model=observation.model,
                        usage=observation.usage,
                        context_used_tokens=context_used,
                        context_window_tokens=context_window,
                        context_growth_tokens=(
                            context_used - previous_context_used
                            if context_used is not None
                            and previous_context_used is not None
                            else None
                        ),
                        estimated_cost=cost_evidence_from_usage(
                            observation.usage.model_dump(mode="json"),
                            model=observation.model,
                            provider=observation.provider,
                        ),
                        invokes_tool_item_ids=[item.item_id for item in invoked],
                        invokes_tool_call_ids=[
                            item.tool_call_id
                            for item in invoked
                            if getattr(item, "tool_call_id", None)
                        ],
                        consumes_tool_item_ids=[item.item_id for item in consumed],
                        consumes_tool_call_ids=[
                            item.tool_call_id
                            for item in consumed
                            if getattr(item, "tool_call_id", None)
                        ],
                    )
                )
                previous_timestamp = observation.timestamp
                if context_used is not None:
                    previous_context_used = context_used

    usage = TokenUsage()
    for request in requests:
        usage = usage.plus(request.usage)
    return SessionGraphRequestUsageFlat(
        root_session_id=full.root_session_id,
        request_count=len(requests),
        usage=usage,
        estimated_cost=_aggregate_cost_evidence(
            [request.estimated_cost for request in requests],
            source="request-attributed aggregate",
        ),
        requests=requests,
        warnings=full.warnings,
    ).model_dump(mode="json")


def build_session_graph_runtime(session_graph: SessionGraph) -> dict[str, Any]:
    """Return canonical runtime summary fields for one session graph."""
    full = _build_full_metrics(session_graph)
    return runtime_stats(
        session_graph,
        session_metrics=full.sessions,
    ).model_dump(mode="json")


def _compact_turn_usage(
    turn: TurnMetrics,
    *,
    previous_turn: TurnMetrics | None,
    session_id: UUID | None,
    execution_seconds: int | None,
    wait_before_seconds: int | None,
) -> TurnUsageCompactFlat:
    provider, model = _turn_pricing_context(turn)
    cost = _aggregate_model_cost(_model_usage_breakdown([turn]))
    first_call = _first_call_usage(turn)
    previous_last_call = _last_call_usage(previous_turn)
    re_read_tokens = first_call.uncached_input_tokens if first_call else None
    cache_boundary_loss_tokens = (
        max(previous_last_call.cached_input_tokens - first_call.cached_input_tokens, 0)
        if first_call is not None and previous_last_call is not None
        else None
    )
    waste = cache_break_waste_usd(
        cache_boundary_loss_tokens or 0, model=model, provider=provider
    )
    intra_turn_loss_tokens = _intra_turn_cache_loss(turn)
    intra_turn_waste = cache_break_waste_usd(
        intra_turn_loss_tokens or 0, model=model, provider=provider
    )
    return TurnUsageCompactFlat(
        turn_id=turn.turn_id,
        session_id=session_id,
        runtime=_turn_runtime(
            turn,
            execution_seconds=execution_seconds,
            wait_before_seconds=wait_before_seconds,
        ),
        usage=turn.token_usage,
        estimated_cost=cost,
        cache_break_waste_usd=waste,
        cache_break_re_read_tokens=re_read_tokens,
        cache_boundary_loss_tokens=cache_boundary_loss_tokens,
        cache_first_call_cached_tokens=(
            first_call.cached_input_tokens if first_call is not None else None
        ),
        cache_intra_turn_loss_tokens=intra_turn_loss_tokens,
        cache_intra_turn_waste_usd=intra_turn_waste,
        provider=provider,
        model=model,
    )


def _intra_turn_cache_loss(turn: TurnMetrics) -> int | None:
    """Largest single cache-hit drop between consecutive provider calls within a
    turn - ``max(prev.cached_input_tokens - cur.cached_input_tokens, 0)`` over
    adjacent observations ordered by timestamp. Catches mid-turn collapses (a
    cache invalidation between two assistant calls in the same turn) that the
    inter-turn boundary loss can't see. Returns ``None`` when the turn has fewer
    than two observations (nothing to compare).
    """
    observations = sorted(turn.observations, key=lambda item: item.timestamp)
    if len(observations) < 2:
        return None
    max_loss = 0
    for prev, cur in zip(observations, observations[1:]):
        loss = max(prev.usage.cached_input_tokens - cur.usage.cached_input_tokens, 0)
        if loss > max_loss:
            max_loss = loss
    return max_loss or None


def _first_call_usage(turn: TurnMetrics) -> TokenUsage | None:
    """Usage on the turn's first provider call."""
    observation = min(turn.observations, key=lambda item: item.timestamp, default=None)
    return observation.usage if observation else None


def _last_call_usage(turn: TurnMetrics | None) -> TokenUsage | None:
    """Usage on a prior turn's final provider call."""
    if turn is None:
        return None
    observation = max(turn.observations, key=lambda item: item.timestamp, default=None)
    return observation.usage if observation else None


def _turn_pricing_context(turn: TurnMetrics) -> tuple[str | None, str | None]:
    """Dominant ``(provider, model)`` for a turn — the pricing context."""
    dominant = _dominant_group(_model_groups_for_turn(turn))
    if dominant is None:
        return None, None
    return dominant.provider, dominant.model


def _single_session_graph(
    source_graph: SessionGraph,
    session: Session,
) -> SessionGraph:
    return SessionGraph(
        root_session_id=session.session_id,
        project_identifier=source_graph.project_identifier,
        sessions=[session],
    )


def _token_usage_payload(usage: TokenUsage) -> dict[str, Any]:
    return usage_accounting_payload(
        {
            "input_tokens": usage.input_tokens,
            "uncached_input_tokens": usage.uncached_input_tokens,
            "cached_input_tokens": usage.cached_input_tokens,
            "cache_creation_input_tokens": usage.cache_creation_input_tokens,
            "output_tokens": usage.output_tokens,
            "reasoning_output_tokens": usage.reasoning_output_tokens,
            "processed_tokens": usage.processed_token_total(),
            "reported_total_tokens": usage.reported_total_tokens,
            "total_tokens": usage.total_tokens,
        }
    )


def _optional_model_dump(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return value.model_dump(mode="json")


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
        model_active_seconds=turn.model_active_seconds,
        processed_tokens_per_second=processed_tokens_per_second(
            turn.token_usage.processed_token_total(),
            turn.model_active_seconds,
        ),
        wait_before_seconds=wait_before_seconds,
    )


def _model_groups_for_turn(turn: TurnMetrics) -> list[ModelUsageModelFlat]:
    grouped: dict[tuple[str | None, str | None], TokenUsage] = {}
    observations_by_model: dict[
        tuple[str | None, str | None], list[TokenUsageObservation]
    ] = {}
    for observation in turn.observations:
        key = (observation.provider, observation.model)
        grouped[key] = grouped.get(key, TokenUsage()).plus(observation.usage)
        observations_by_model.setdefault(key, []).append(observation)
    if not grouped and not _is_zero_usage(turn.token_usage):
        grouped[(None, None)] = turn.token_usage
    single_model = len(grouped) == 1
    return [
        ModelUsageModelFlat(
            provider=provider,
            model=model,
            requests=len(observations_by_model.get((provider, model), [])),
            turns=1,
            usage=usage,
            model_active_seconds=(turn.model_active_seconds if single_model else None),
            processed_tokens_per_second=(
                processed_tokens_per_second(
                    usage.processed_token_total(), turn.model_active_seconds
                )
                if single_model
                else None
            ),
            estimated_cost=(
                _aggregate_observation_cost(observations_by_model[(provider, model)])
                if observations_by_model.get((provider, model))
                else cost_evidence_from_usage(
                    usage.model_dump(mode="json"),
                    model=model,
                    provider=provider,
                )
            ),
        )
        for (provider, model), usage in sorted(
            grouped.items(),
            key=lambda item: item[1].total_tokens,
            reverse=True,
        )
    ]


def _model_usage_breakdown(
    turns: Iterable[TurnMetrics],
) -> list[ModelUsageModelFlat]:
    turn_list = list(turns)
    grouped: dict[tuple[str | None, str | None], TokenUsage] = {}
    model_requests: dict[tuple[str | None, str | None], int] = {}
    model_turns: dict[tuple[str | None, str | None], set[UUID]] = {}
    active_seconds: dict[tuple[str | None, str | None], float] = {}
    active_seconds_complete: dict[tuple[str | None, str | None], bool] = {}
    model_costs: dict[tuple[str | None, str | None], list[CostEvidenceFlat | None]] = {}
    for turn in turn_list:
        groups = _model_groups_for_turn(turn)
        for group in groups:
            key = (group.provider, group.model)
            grouped[key] = grouped.get(key, TokenUsage()).plus(group.usage)
            model_requests[key] = model_requests.get(key, 0) + group.requests
            model_turns.setdefault(key, set()).add(turn.turn_id)
            model_costs.setdefault(key, []).append(group.estimated_cost)
            if group.model_active_seconds is None:
                active_seconds_complete[key] = False
            elif active_seconds_complete.get(key, True):
                active_seconds[key] = active_seconds.get(key, 0.0) + (
                    group.model_active_seconds
                )
    return [
        ModelUsageModelFlat(
            provider=provider,
            model=model,
            requests=model_requests.get((provider, model), 0),
            turns=len(model_turns.get((provider, model), set())),
            usage=usage,
            model_active_seconds=(
                round(active_seconds[(provider, model)], 3)
                if active_seconds_complete.get((provider, model), True)
                and (provider, model) in active_seconds
                else None
            ),
            processed_tokens_per_second=processed_tokens_per_second(
                usage.processed_token_total(),
                (
                    active_seconds[(provider, model)]
                    if active_seconds_complete.get((provider, model), True)
                    else None
                ),
            ),
            estimated_cost=_aggregate_cost_evidence(
                model_costs.get((provider, model), []),
                source="request-attributed model aggregate",
            ),
        )
        for (provider, model), usage in sorted(
            grouped.items(),
            key=lambda item: item[1].total_tokens,
            reverse=True,
        )
    ]


def _aggregate_model_cost(
    models: Iterable[ModelUsageModelFlat],
) -> CostEvidenceFlat | None:
    rows = list(models)
    return _aggregate_cost_evidence(
        [row.estimated_cost for row in rows],
        source="request-attributed aggregate",
    )


def _aggregate_observation_cost(
    observations: Iterable[TokenUsageObservation],
) -> CostEvidenceFlat | None:
    return _aggregate_cost_evidence(
        [
            cost_evidence_from_usage(
                observation.usage.model_dump(mode="json"),
                model=observation.model,
                provider=observation.provider,
            )
            for observation in observations
        ],
        source="request-attributed aggregate",
    )


def _aggregate_cost_evidence(
    costs: Iterable[CostEvidenceFlat | None],
    *,
    source: str,
) -> CostEvidenceFlat | None:
    rows = list(costs)
    if not rows or any(item is None for item in rows):
        return None
    estimates = [item for item in rows if item is not None]
    effective_dates = {item.effective_date for item in estimates}
    return CostEvidenceFlat(
        value_usd=round(sum(item.value_usd for item in estimates), 8),
        confidence=(
            "reported"
            if all(item.confidence == "reported" for item in estimates)
            else "estimated"
        ),
        source=source,
        effective_date=(effective_dates.pop() if len(effective_dates) == 1 else None),
    )


def _sum_turn_usage(turns: Iterable[TurnMetrics]) -> TokenUsage:
    usage = TokenUsage()
    for turn in turns:
        usage = usage.plus(turn.token_usage)
    return usage


def _dominant_group(
    groups: Iterable[ModelUsageModelFlat],
) -> ModelUsageModelFlat | None:
    return max(groups, key=lambda item: item.usage.total_tokens, default=None)


def _context_for_turn(session: Session, turn_id: UUID) -> ModelUsageContextFlat | None:
    event_ids = {
        event_id
        for turn in session.turns
        if turn.turn_id == turn_id
        for event_id in turn.event_ids
    }
    observations = [
        observation
        for observation in session.context_usage
        if observation.source_event_id in event_ids
    ]
    return _context_from_observations(observations)


def _context_for_session_graph(
    session_graph: SessionGraph,
) -> ModelUsageContextFlat | None:
    observations = [
        observation
        for session in session_graph.sessions
        for observation in session.context_usage
    ]
    return _context_from_observations(observations)


def _context_from_observations(
    observations: Iterable[ContextUsageObservation],
) -> ModelUsageContextFlat | None:
    ordered = sorted(observations, key=lambda item: item.timestamp)
    if not ordered:
        return None
    final = ordered[-1]
    max_used = max((item.used_input_tokens for item in ordered), default=0)
    context_window = (
        final.context_window_tokens
        or next(
            (
                item.context_window_tokens
                for item in reversed(ordered)
                if item.context_window_tokens
            ),
            None,
        )
        or get_model_context_window(final.model, provider=final.provider)
    )
    return ModelUsageContextFlat(
        final_used_tokens=final.used_input_tokens or None,
        max_used_tokens=max_used or None,
        context_window_tokens=context_window,
        final_used_percent=_percent(final.used_input_tokens, context_window),
        max_used_percent=_percent(max_used, context_window),
        source=final.source,
        confidence="exact_usage",
    )


def _percent(numerator: int | None, denominator: int | None) -> float | None:
    if not numerator or not denominator:
        return None
    return round((numerator / denominator) * 100, 1)


def _turn_wait_before_seconds(
    previous_turn: TurnMetrics | None, turn: TurnMetrics
) -> int | None:
    if (
        previous_turn is None
        or previous_turn.completed_at is None
        or turn.started_at is None
    ):
        return None
    return max(round((turn.started_at - previous_turn.completed_at).total_seconds()), 0)


def _turn_execution_seconds(turn: TurnMetrics) -> int | None:
    if turn.started_at is None or turn.completed_at is None:
        return None
    return max(round((turn.completed_at - turn.started_at).total_seconds()), 0)


@session_scoped
def build_session_graph_tool_usage(
    session_graph: SessionGraph,
    *,
    turn_id: str | None = None,
    include_item_real_token_costs: bool = True,
    include_advanced_causality: bool = True,
    precomputed_metrics: SessionGraphMetrics | None = None,
) -> dict[str, Any]:
    """Return tool usage plus turn-stable cache-aware item attribution.

    The all-item cost ledger and advanced causal diagnostics are independent
    payload details. Aggregate and per-tool allocation/pricing remain intact
    when either detail is omitted. Defaults retain both detail groups.
    """
    with scoped_content_size_cache():
        return _build_session_graph_tool_usage(
            session_graph,
            turn_id=turn_id,
            include_item_real_token_costs=include_item_real_token_costs,
            include_advanced_causality=include_advanced_causality,
            precomputed_metrics=precomputed_metrics,
        )


def _build_session_graph_tool_usage(
    session_graph: SessionGraph,
    *,
    turn_id: str | None = None,
    include_item_real_token_costs: bool = True,
    include_advanced_causality: bool = True,
    precomputed_metrics: SessionGraphMetrics | None = None,
) -> dict[str, Any]:
    full = precomputed_metrics or _build_full_metrics(session_graph)

    tool_items: list[ToolItemFlat] = []
    item_real_token_costs: list[ItemRealTokenCostFlat] = []
    selected_allocated_costs: list[AllocatedRealTokenCost | None] = []
    pricing_rule_cache: dict[tuple[str | None, str], PriceRule | None] = {}
    for session in session_graph.sessions:
        selected_turns = [
            turn
            for turn in session.turns
            if turn_id is None or str(turn.turn_id) == turn_id
        ]
        selected_turn_ids = {turn.turn_id for turn in selected_turns}
        selected_tool_item_ids = {
            item.item_id
            for turn in selected_turns
            for item in turn.items
            if is_tool_shaped_item(item)
        }
        cost_projection = _build_item_real_token_cost_projection_for_session(
            session,
            selected_turn_ids=selected_turn_ids,
            selected_item_ids=selected_tool_item_ids,
            include_items=include_item_real_token_costs,
            pricing_rule_cache=pricing_rule_cache,
        )
        item_real_token_costs.extend(cost_projection.items)
        selected_allocated_costs.append(cost_projection.allocated_real_token_cost)
        for turn in selected_turns:
            turn_observations = (
                _turn_usage_observations(session, turn)
                if include_advanced_causality
                else []
            )
            tool_items.extend(
                _build_tool_items_for_turn(
                    turn,
                    session_id=session.session_id,
                    turn_observations=turn_observations,
                    allocated_real_token_cost_by_item=(
                        cost_projection.allocated_real_token_cost_by_item
                    ),
                    estimated_cost_by_item=cost_projection.estimated_cost_by_item,
                    include_advanced_causality=include_advanced_causality,
                )
            )

    payload = SessionGraphToolUsageFlat(
        root_session_id=full.root_session_id,
        tool_item_count=len(tool_items),
        tool_output_chars=sum(item.output_chars for item in tool_items),
        tool_output_original_tokens=sum(
            item.output_original_tokens or 0 for item in tool_items
        ),
        allocated_real_token_cost=_sum_allocated_real_token_costs(
            selected_allocated_costs
        ),
        item_real_token_costs=(
            item_real_token_costs if include_item_real_token_costs else []
        ),
        tool_items=tool_items,
        attribution_policy=AttributionPolicy(scope="turn_items"),
        warnings=full.warnings,
    ).model_dump(mode="json")
    if not include_item_real_token_costs:
        payload.pop("item_real_token_costs", None)
    return payload


@dataclass(slots=True)
class _StatsTokenUsageBreakdown:
    graph_usage: dict[str, Any]
    usage_by_session: dict[UUID, dict[str, Any]]
    counter_name: str


@session_scoped
def _build_session_graph_stats_usage_breakdown(
    session_graph: SessionGraph,
) -> _StatsTokenUsageBreakdown:
    """Build graph and reusable per-session stats attribution in one pass."""
    allocated_usage_by_item: dict[UUID, dict[str, int]] = {}
    allocated_usage_by_context_source: dict[str, dict[str, int]] = {}
    billed_token_usage: _CostAccum | None = None
    usage_by_session: dict[UUID, dict[str, Any]] = {}

    for session in session_graph.sessions:
        session_usage_by_item: dict[UUID, dict[str, int]] = {}
        session_usage_by_context_source: dict[str, dict[str, int]] = {}
        session_billed_token_usage: _CostAccum | None = None
        entries = _stats_cost_entries_for_session(session)
        if not entries:
            usage_by_session[session.session_id] = {
                "allocated_usage_by_item": session_usage_by_item,
                "allocated_usage_by_context_source": session_usage_by_context_source,
                "billed_token_usage": {},
            }
            continue
        entries.sort(key=lambda e: e.started_at)
        n_entries = len(entries)
        entry_times = [e.started_at for e in entries]
        all_weights = np.array([e.visible_tokens for e in entries], dtype=np.int64)
        is_output_eligible = np.array(
            [e.context_key is None and e.output_eligible for e in entries],
            dtype=bool,
        )
        turn_to_int: dict[UUID | None, int] = {}
        for e in entries:
            if e.turn_id not in turn_to_int:
                turn_to_int[e.turn_id] = len(turn_to_int)
        turn_id_idx = np.array(
            [turn_to_int[e.turn_id] for e in entries], dtype=np.int64
        )
        context_indices_by_key: dict[str, list[int]] = {}
        for index, entry in enumerate(entries):
            if entry.context_key is not None:
                context_indices_by_key.setdefault(entry.context_key, []).append(index)
        allocated_by_index = np.zeros((n_entries, 7), dtype=np.int64)

        for observation, turn_id in _session_usage_observations(session):
            cutoff = bisect.bisect_right(entry_times, observation.timestamp)
            if cutoff == 0:
                continue
            session_billed_token_usage = _add_allocated_real_token_cost(
                session_billed_token_usage,
                _allocated_cost_from_usage_observation(observation),
            )
            weights = all_weights[:cutoff]
            eligible = is_output_eligible[:cutoff]
            if turn_id is None:
                output_mask = eligible
            else:
                output_mask = eligible & (
                    turn_id_idx[:cutoff] == turn_to_int.get(turn_id, -1)
                )
            output_weights = np.where(output_mask, weights, np.int64(0))
            allocations = _token_cost_allocations(
                observation.usage,
                observation,
                weights,
                output_weights,
                as_arrays=True,
            )
            if allocations is None:
                continue
            (
                input_a,
                uncached_input_a,
                cached_a,
                cache_creation_a,
                output_a,
                reasoning_a,
                total_a,
                _method,
            ) = allocations
            allocated_by_index[:cutoff, 0] += input_a
            allocated_by_index[:cutoff, 1] += uncached_input_a
            allocated_by_index[:cutoff, 2] += cached_a
            allocated_by_index[:cutoff, 3] += cache_creation_a
            allocated_by_index[:cutoff, 4] += output_a
            allocated_by_index[:cutoff, 5] += reasoning_a
            allocated_by_index[:cutoff, 6] += total_a

        for index, entry in enumerate(entries):
            if entry.context_key is not None:
                continue
            usage = _allocated_cost_usage_dict_from_array(allocated_by_index[index])
            if usage:
                session_usage_by_item[entry.item_id] = usage

        for key, indices in context_indices_by_key.items():
            usage = _allocated_cost_usage_dict_from_array(
                allocated_by_index[indices].sum(axis=0)
            )
            if not usage:
                continue
            existing = session_usage_by_context_source.get(key)
            if existing is None:
                session_usage_by_context_source[key] = usage
            else:
                session_usage_by_context_source[key] = _sum_usage_dicts(
                    (existing, usage)
                )

        session_billed_usage = _allocated_cost_usage_dict(session_billed_token_usage)
        assert (
            _sum_usage_dicts(
                (
                    _sum_usage_dicts(session_usage_by_item.values()),
                    _sum_usage_dicts(session_usage_by_context_source.values()),
                )
            )
            == session_billed_usage
        ), "per-session stats attribution must reconcile to billed token usage"
        usage_by_session[session.session_id] = {
            "allocated_usage_by_item": session_usage_by_item,
            "allocated_usage_by_context_source": session_usage_by_context_source,
            "billed_token_usage": session_billed_usage,
        }
        allocated_usage_by_item.update(session_usage_by_item)
        for key, usage in session_usage_by_context_source.items():
            existing = allocated_usage_by_context_source.get(key)
            allocated_usage_by_context_source[key] = (
                usage if existing is None else _sum_usage_dicts((existing, usage))
            )
        if session_billed_token_usage is not None:
            billed_token_usage = _add_allocated_real_token_cost(
                billed_token_usage, session_billed_token_usage
            )

    assert _sum_usage_dicts(
        (
            _sum_usage_dicts(allocated_usage_by_item.values()),
            _sum_usage_dicts(allocated_usage_by_context_source.values()),
        )
    ) == _allocated_cost_usage_dict(billed_token_usage), (
        "session stats attribution must reconcile to billed token usage"
    )
    return _StatsTokenUsageBreakdown(
        graph_usage={
            "allocated_usage_by_item": allocated_usage_by_item,
            "allocated_usage_by_context_source": allocated_usage_by_context_source,
            "billed_token_usage": _allocated_cost_usage_dict(billed_token_usage),
        },
        usage_by_session=usage_by_session,
        counter_name=get_current_counter().name,
    )


def build_session_graph_stats_token_usage(
    session_graph: SessionGraph,
) -> dict[str, Any]:
    """Return stats-table token attribution across starting context and items."""
    return _build_session_graph_stats_usage_breakdown(session_graph).graph_usage


def build_session_graph_billed_token_usage(
    session_graph: SessionGraph,
    *,
    precomputed_metrics: SessionGraphMetrics | None = None,
) -> dict[str, int]:
    """Return canonical billed usage without item/context allocation.

    Core economics readiness needs the provider-request accounting total and an
    exact reconciliation boundary, but it does not need to allocate every
    observation over visible transcript items.  Evidence projections still use
    :func:`build_session_graph_stats_token_usage` for that expensive detail.
    """

    full = precomputed_metrics or _build_full_metrics(session_graph)
    billed: _CostAccum | None = None
    for session in full.sessions:
        for turn in session.turns:
            for observation in turn.observations:
                billed = _add_allocated_real_token_cost(
                    billed,
                    _allocated_cost_from_usage_observation(observation),
                )
    return _allocated_cost_usage_dict(billed)


def _sum_allocated_real_token_costs(
    costs: Iterable[AllocatedRealTokenCost | None],
) -> AllocatedRealTokenCost | None:
    present_costs = [cost for cost in costs if cost is not None]
    if not present_costs:
        return None
    return AllocatedRealTokenCost(
        input_tokens=sum(cost.input_tokens for cost in present_costs),
        uncached_input_tokens=sum(cost.uncached_input_tokens for cost in present_costs),
        cached_input_tokens=sum(cost.cached_input_tokens for cost in present_costs),
        cache_creation_input_tokens=sum(
            cost.cache_creation_input_tokens for cost in present_costs
        ),
        output_tokens=sum(cost.output_tokens for cost in present_costs),
        reasoning_output_tokens=sum(
            cost.reasoning_output_tokens for cost in present_costs
        ),
        total_tokens=sum(cost.total_tokens for cost in present_costs),
    )


def _turn_usage_observations(
    session: Session,
    turn: Turn,
) -> list[TokenUsageObservation]:
    event_ids = set(turn.event_ids)
    token_observations: list[TokenUsageObservation] = []
    for observation in session.context_usage:
        if observation.source_event_id not in event_ids:
            continue
        token_observation = _usage_from_context_observation(
            observation,
            scope_id=turn.turn_id,
            session=session,
        )
        if token_observation is not None:
            token_observations.append(token_observation)
    token_observations.sort(key=lambda item: item.timestamp)
    return token_observations
