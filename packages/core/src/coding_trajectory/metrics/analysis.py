"""Build derived execution metrics from canonical session_graphs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable
from uuid import UUID, uuid5

from coding_trajectory.analysis.content_size import (
    item_input_size,
    item_output_size,
    item_output_text,
    output_is_truncated,
    reported_token_count,
    visible_text_size,
)
from coding_trajectory import debug
from coding_trajectory.ingestion.models import (
    ContextUsageObservation,
    Event,
    EventType,
    Item,
    Session,
    SessionGraph,
    Turn,
    is_tool_shaped_item,
)
from coding_trajectory.metrics.models import (
    AllocatedRealTokenCost,
    AttributionPolicy,
    DominantModelFlat,
    InvokeResponseTokens,
    ItemRealTokenCostFlat,
    MetricSource,
    ModelUsageContextFlat,
    ModelUsageModelFlat,
    ModelUsageTurnFlat,
    ReadAfterResult,
    SessionGraphModelUsageFlat,
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
) -> dict[str, Any]:
    """Return a flat usage summary: sessions -> turns."""
    full = _build_full_metrics(session_graph)
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
                )
            )
        sessions_flat.append(
            SessionMetricsFlat(
                session_id=session.session_id,
                vendor=session.vendor,
                status=session.status,
                token_usage=session.token_usage,
                turns=turns_flat,
            )
        )

    return SessionGraphMetricsFlat(
        root_session_id=full.root_session_id,
        token_usage=full.token_usage,
        sessions=sessions_flat,
        warnings=full.warnings,
    ).model_dump(mode="json")


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
) -> dict[str, Any]:
    """Return provider-specific context-window stats by dispatching to a vendor handler."""
    from coding_trajectory.metrics.context_stats import (
        build_session_graph_context_stats as dispatch,
    )

    return dispatch(
        session_graph,
        allocated_usage_by_item=allocated_usage_by_item,
        allocated_usage_by_context_source=allocated_usage_by_context_source,
    )


def build_session_graph_usage(
    session_graph: SessionGraph,
    *,
    turn_id: str | None = None,
) -> dict[str, Any]:
    """Return compact turn-level token usage accounting."""
    full = _build_full_metrics(session_graph)
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
        runtime=runtime_stats(session_graph),
        turns=turns,
        total_usage=full.token_usage,
        warnings=full.warnings,
    ).model_dump(mode="json")


def build_session_graph_model_usage(session_graph: SessionGraph) -> dict[str, Any]:
    """Return provider/model usage facts at session and turn granularity."""
    full = _build_full_metrics(session_graph)
    sessions_by_id = {session.session_id: session for session in session_graph.sessions}
    root_session = sessions_by_id.get(session_graph.root_session_id)
    turns: list[ModelUsageTurnFlat] = []
    model_usage: dict[tuple[str | None, str | None], TokenUsage] = {}
    model_turns: dict[tuple[str | None, str | None], set[UUID]] = {}

    for session_metrics in full.sessions:
        source_session = sessions_by_id.get(session_metrics.session_id)
        for turn in session_metrics.turns:
            groups = _model_groups_for_turn(turn)
            primary = _dominant_group(groups)
            for group in groups:
                key = (group.provider, group.model)
                model_usage[key] = model_usage.get(key, TokenUsage()).plus(group.usage)
                model_turns.setdefault(key, set()).add(turn.turn_id)
            turns.append(
                ModelUsageTurnFlat(
                    turn_id=turn.turn_id,
                    session_id=session_metrics.session_id,
                    vendor=session_metrics.vendor,
                    sequence=turn.sequence,
                    started_at=turn.started_at,
                    completed_at=turn.completed_at,
                    provider=primary.provider if primary else None,
                    model=primary.model if primary else None,
                    usage=turn.token_usage,
                    models=groups,
                    context=_context_for_turn(source_session, turn.turn_id)
                    if source_session
                    else None,
                )
            )

    models = [
        ModelUsageModelFlat(
            provider=provider,
            model=model,
            turns=len(model_turns.get((provider, model), set())),
            usage=usage,
        )
        for (provider, model), usage in sorted(
            model_usage.items(),
            key=lambda item: item[1].total_tokens,
            reverse=True,
        )
    ]
    dominant = _dominant_group(models)

    return SessionGraphModelUsageFlat(
        root_session_id=session_graph.root_session_id,
        vendor=root_session.vendor.value if root_session else None,
        project=session_graph.project_identifier,
        title=_session_graph_title(session_graph),
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
        usage=full.token_usage,
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


def build_session_graph_runtime(session_graph: SessionGraph) -> dict[str, Any]:
    """Return canonical runtime summary fields for one session graph."""
    return runtime_stats(session_graph).model_dump(mode="json")


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


def _model_groups_for_turn(turn: TurnMetrics) -> list[ModelUsageModelFlat]:
    grouped: dict[tuple[str | None, str | None], TokenUsage] = {}
    for observation in turn.observations:
        key = (observation.provider, observation.model)
        grouped[key] = grouped.get(key, TokenUsage()).plus(observation.usage)
    if not grouped and not _is_zero_usage(turn.token_usage):
        grouped[(None, None)] = turn.token_usage
    return [
        ModelUsageModelFlat(provider=provider, model=model, turns=1, usage=usage)
        for (provider, model), usage in sorted(
            grouped.items(),
            key=lambda item: item[1].total_tokens,
            reverse=True,
        )
    ]


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
    context_window = final.context_window_tokens or next(
        (
            item.context_window_tokens
            for item in reversed(ordered)
            if item.context_window_tokens
        ),
        None,
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


def _session_graph_title(session_graph: SessionGraph) -> str | None:
    by_id = {session.session_id: session for session in session_graph.sessions}
    root = by_id.get(session_graph.root_session_id)
    if root is not None:
        title = _session_title(root)
        if title:
            return title
    for session in session_graph.sessions:
        title = _session_title(session)
        if title:
            return title
    return None


def _session_title(session: Session) -> str | None:
    extensions = session.extensions
    if extensions and extensions.codex and extensions.codex.title:
        return extensions.codex.title
    if extensions and extensions.claude_code and extensions.claude_code.title:
        return extensions.claude_code.title
    if extensions and extensions.pi and extensions.pi.title:
        return extensions.pi.title
    return None


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


def build_session_graph_tool_usage(
    session_graph: SessionGraph,
) -> dict[str, Any]:
    """Return tool usage plus cache-aware cost attribution over chronological items."""
    full = _build_full_metrics(session_graph)

    tool_items: list[ToolItemFlat] = []
    item_real_token_costs: list[ItemRealTokenCostFlat] = []
    for session in session_graph.sessions:
        session_item_real_token_costs = _build_item_real_token_costs_for_session(
            session
        )
        item_real_token_costs.extend(session_item_real_token_costs)
        session_costs_by_item_id = {
            item.item_id: item.allocated_real_token_cost
            for item in session_item_real_token_costs
            if item.allocated_real_token_cost is not None
        }
        for turn in session.turns:
            turn_observations = _turn_usage_observations(session, turn)
            tool_items.extend(
                _build_tool_items_for_turn(
                    turn,
                    session_id=session.session_id,
                    turn_observations=turn_observations,
                    allocated_real_token_cost_by_item=session_costs_by_item_id,
                )
            )

    return SessionGraphToolUsageFlat(
        root_session_id=full.root_session_id,
        tool_item_count=len(tool_items),
        tool_call_count=len(tool_items),
        tool_output_chars=sum(item.output_chars for item in tool_items),
        tool_output_original_tokens=sum(
            item.output_original_tokens or 0 for item in tool_items
        ),
        allocated_real_token_cost=_sum_item_real_token_costs(item_real_token_costs),
        item_real_token_costs=item_real_token_costs,
        tool_items=tool_items,
        attribution_policy=AttributionPolicy(scope="all_items"),
        warnings=full.warnings,
    ).model_dump(mode="json")


def build_session_graph_stats_token_usage(
    session_graph: SessionGraph,
) -> dict[str, Any]:
    """Return stats-table token attribution across starting context and items."""
    allocated_by_item: dict[UUID, AllocatedRealTokenCost] = {}
    allocated_by_context_key: dict[str, AllocatedRealTokenCost] = {}
    billed_token_usage: AllocatedRealTokenCost | None = None

    for session in session_graph.sessions:
        entries = _stats_cost_entries_for_session(session)
        if not entries:
            continue
        context_entry_ids = {
            entry.item_id: entry.context_key
            for entry in entries
            if entry.context_key is not None
        }
        for observation, turn_id in _session_usage_observations(session):
            present_entries = [
                entry for entry in entries if entry.started_at <= observation.timestamp
            ]
            if not present_entries:
                continue
            response_entries = [
                entry
                for entry in present_entries
                if entry.context_key is None
                and entry.output_eligible
                and (turn_id is None or entry.turn_id == turn_id)
            ]
            costs = _allocate_real_token_costs_for_entries(
                present_entries,
                observation,
                output_entries=response_entries,
            )
            billed_token_usage = _add_allocated_real_token_cost(
                billed_token_usage,
                _allocated_cost_from_usage_observation(observation),
            )
            for entry_id, cost in costs.items():
                context_key = context_entry_ids.get(entry_id)
                if context_key is not None:
                    allocated_by_context_key[context_key] = (
                        _add_allocated_real_token_cost(
                            allocated_by_context_key.get(context_key),
                            cost,
                        )
                    )
                    continue
                allocated_by_item[entry_id] = _add_allocated_real_token_cost(
                    allocated_by_item.get(entry_id),
                    cost,
                )

    allocated_usage_by_item = {
        item_id: usage
        for item_id, cost in allocated_by_item.items()
        if (usage := _allocated_cost_usage_dict(cost))
    }
    allocated_usage_by_context_source = {
        key: usage
        for key, cost in allocated_by_context_key.items()
        if (usage := _allocated_cost_usage_dict(cost))
    }
    assert _sum_usage_dicts(
        (
            _sum_usage_dicts(allocated_usage_by_item.values()),
            _sum_usage_dicts(allocated_usage_by_context_source.values()),
        )
    ) == _allocated_cost_usage_dict(billed_token_usage), (
        "session stats attribution must reconcile to billed token usage"
    )
    return {
        "allocated_usage_by_item": allocated_usage_by_item,
        "allocated_usage_by_context_source": allocated_usage_by_context_source,
        "billed_token_usage": _allocated_cost_usage_dict(billed_token_usage),
    }


def _sum_item_real_token_costs(
    items: list[ItemRealTokenCostFlat],
) -> AllocatedRealTokenCost | None:
    costs = [
        item.allocated_real_token_cost
        for item in items
        if item.allocated_real_token_cost is not None
    ]
    if not costs:
        return None
    return AllocatedRealTokenCost(
        input_tokens=sum(cost.input_tokens for cost in costs),
        uncached_input_tokens=sum(cost.uncached_input_tokens for cost in costs),
        cached_input_tokens=sum(cost.cached_input_tokens for cost in costs),
        cache_creation_input_tokens=sum(
            cost.cache_creation_input_tokens for cost in costs
        ),
        output_tokens=sum(cost.output_tokens for cost in costs),
        reasoning_output_tokens=sum(cost.reasoning_output_tokens for cost in costs),
        total_tokens=sum(cost.total_tokens for cost in costs),
    )


def _turn_usage_observations(
    session: Session,
    turn: Turn,
) -> list[TokenUsageObservation]:
    event_ids = set(turn.event_ids)
    observations = [
        obs for obs in session.context_usage if obs.source_event_id in event_ids
    ]
    token_observations: list[TokenUsageObservation] = []
    for observation in observations:
        usage = _token_usage_from_mapping(
            observation.usage,
            provider=observation.provider or session.vendor.value,
        )
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


@dataclass(frozen=True)
class _ItemCostEntry:
    item_id: UUID
    session_id: UUID
    turn_id: UUID | None
    sequence: int
    started_at: datetime
    kind: str
    visible_tokens: int
    output_eligible: bool = False
    context_key: str | None = None


def _stats_cost_entries_for_session(session: Session) -> list[_ItemCostEntry]:
    entries: list[_ItemCostEntry] = []
    for source_index, source in enumerate(session.context_sources):
        size = visible_text_size(source.text)
        if size.tokens <= 0:
            continue
        entries.append(
            _ItemCostEntry(
                item_id=uuid5(
                    session.session_id,
                    "context_source:"
                    f"{source_index}:{source.key}:{source.timestamp.isoformat()}",
                ),
                session_id=session.session_id,
                turn_id=None,
                sequence=-2,
                started_at=source.timestamp,
                kind="context_source",
                visible_tokens=size.tokens,
                context_key=source.key,
            )
        )
    entries.extend(
        entry
        for turn in sorted(session.turns, key=lambda item: item.sequence)
        for entry in _item_cost_entries_for_turn(session, turn)
    )
    return entries


def _build_item_real_token_costs_for_session(
    session: Session,
) -> list[ItemRealTokenCostFlat]:
    """Billed attribution: sum every API call's per-call usage across present items.

    Iterates *every* usage observation (not just the last per turn) and allocates
    that call's per-call token delta across the items present at the call, weighted
    by visible tokens. Costs accumulate without capping, so per-item sums reconcile
    exactly to the provider-reported cumulative usage.
    """
    entries = [
        entry
        for turn in sorted(session.turns, key=lambda item: item.sequence)
        for entry in _item_cost_entries_for_turn(session, turn)
    ]
    if not entries:
        return []

    allocated_costs: dict[UUID, AllocatedRealTokenCost] = {}
    for observation, turn_id in _session_usage_observations(session):
        present_entries = [
            entry for entry in entries if entry.started_at <= observation.timestamp
        ]
        if not present_entries:
            continue
        response_entries = [
            entry
            for entry in present_entries
            if entry.output_eligible and (turn_id is None or entry.turn_id == turn_id)
        ]
        for item_id, cost in _allocate_real_token_costs_for_entries(
            present_entries,
            observation,
            output_entries=response_entries,
        ).items():
            allocated_costs[item_id] = _add_allocated_real_token_cost(
                allocated_costs.get(item_id),
                cost,
            )

    items = [
        ItemRealTokenCostFlat(
            item_id=entry.item_id,
            session_id=entry.session_id,
            turn_id=entry.turn_id,
            sequence=entry.sequence,
            kind=entry.kind,
            visible_tokens=entry.visible_tokens,
            allocated_real_token_cost=allocated_costs.get(entry.item_id),
        )
        for entry in entries
    ]
    _assert_item_real_token_costs_reconcile(session, entries, items)
    return items


def _assert_item_real_token_costs_reconcile(
    session: Session,
    entries: list[_ItemCostEntry],
    items: list[ItemRealTokenCostFlat],
) -> None:
    expected = _sum_allocated_usage_for_present_observations(session, entries)
    actual = _allocated_cost_usage_dict(_sum_item_real_token_costs(items))
    assert actual == expected, (
        "item real token cost allocation must reconcile to observed session usage"
    )


def _sum_allocated_usage_for_present_observations(
    session: Session,
    entries: list[_ItemCostEntry],
) -> dict[str, int]:
    total: dict[str, int] = {}
    for observation, _turn_id in _session_usage_observations(session):
        if not any(entry.started_at <= observation.timestamp for entry in entries):
            continue
        total = _sum_usage_dicts(
            (
                total,
                _resident_expected_usage(observation),
            )
        )
    return total


def _resident_expected_usage(observation: TokenUsageObservation) -> dict[str, int]:
    usage = observation.usage
    uncached_input_tokens = _effective_input_token_total(usage, observation)
    total_tokens = (
        uncached_input_tokens
        + usage.cached_input_tokens
        + usage.cache_creation_input_tokens
        + usage.output_tokens
        + usage.reasoning_output_tokens
    )
    usage_dict = {
        "input_tokens": usage.input_tokens,
        "uncached_input_tokens": uncached_input_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_output_tokens": usage.reasoning_output_tokens,
        "total_tokens": total_tokens,
    }
    return {key: value for key, value in usage_dict.items() if value > 0}


def _allocated_cost_from_usage_observation(
    observation: TokenUsageObservation,
) -> AllocatedRealTokenCost:
    usage = observation.usage
    uncached_input_tokens = _effective_input_token_total(usage, observation)
    return AllocatedRealTokenCost(
        input_tokens=usage.input_tokens,
        uncached_input_tokens=uncached_input_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
        output_tokens=usage.output_tokens,
        reasoning_output_tokens=usage.reasoning_output_tokens,
        total_tokens=(
            uncached_input_tokens
            + usage.cached_input_tokens
            + usage.cache_creation_input_tokens
            + usage.output_tokens
            + usage.reasoning_output_tokens
        ),
    )


def _allocated_cost_usage_dict(
    cost: AllocatedRealTokenCost | None,
) -> dict[str, int]:
    if cost is None:
        return {}
    usage_dict = {
        "input_tokens": cost.input_tokens,
        "uncached_input_tokens": cost.uncached_input_tokens,
        "cached_input_tokens": cost.cached_input_tokens,
        "cache_creation_input_tokens": cost.cache_creation_input_tokens,
        "output_tokens": cost.output_tokens,
        "reasoning_output_tokens": cost.reasoning_output_tokens,
        "total_tokens": cost.total_tokens,
    }
    return {key: value for key, value in usage_dict.items() if value > 0}


def _sum_usage_dicts(items: Iterable[dict[str, int]]) -> dict[str, int]:
    total: dict[str, int] = {}
    for item in items:
        for key, value in item.items():
            total[key] = total.get(key, 0) + max(value, 0)
    return {key: value for key, value in total.items() if value > 0}


def _session_usage_observations(
    session: Session,
) -> list[tuple[TokenUsageObservation, UUID | None]]:
    """All non-zero usage observations for a session, paired with their turn id."""
    event_turn: dict[UUID, UUID] = {}
    for turn in session.turns:
        for event_id in turn.event_ids:
            event_turn[event_id] = turn.turn_id

    observations: list[tuple[TokenUsageObservation, UUID | None]] = []
    for observation in session.context_usage:
        provider = observation.provider or session.vendor.value
        usage = _token_usage_from_mapping(observation.usage, provider=provider)
        if _is_zero_usage(usage):
            continue
        observations.append(
            (
                TokenUsageObservation(
                    scope_type="turn",
                    scope_id=session.session_id,
                    timestamp=observation.timestamp,
                    usage=usage,
                    provider=provider,
                    model=observation.model,
                    source=MetricSource(
                        vendor=session.vendor.value,
                        source_type="session.context_usage",
                        event_id=observation.source_event_id,
                    ),
                ),
                event_turn.get(observation.source_event_id),
            )
        )
    observations.sort(key=lambda item: item[0].timestamp)
    return observations


def _add_allocated_real_token_cost(
    left: AllocatedRealTokenCost | None,
    right: AllocatedRealTokenCost,
) -> AllocatedRealTokenCost:
    if left is None:
        return right
    return AllocatedRealTokenCost(
        input_tokens=left.input_tokens + right.input_tokens,
        uncached_input_tokens=(
            left.uncached_input_tokens + right.uncached_input_tokens
        ),
        cached_input_tokens=left.cached_input_tokens + right.cached_input_tokens,
        cache_creation_input_tokens=(
            left.cache_creation_input_tokens + right.cache_creation_input_tokens
        ),
        output_tokens=left.output_tokens + right.output_tokens,
        reasoning_output_tokens=(
            left.reasoning_output_tokens + right.reasoning_output_tokens
        ),
        total_tokens=left.total_tokens + right.total_tokens,
        allocation_method=left.allocation_method,
    )


def _item_cost_entries_for_turn(session: Session, turn: Turn) -> list[_ItemCostEntry]:
    entries: list[_ItemCostEntry] = []
    user_event = _event_by_id(session.events, turn.user_request_event_id)
    if user_event is not None and user_event.type == EventType.USER_PROMPT_SUBMITTED:
        text = user_event.payload.get("text")
        if isinstance(text, str) and text:
            entries.append(
                _ItemCostEntry(
                    item_id=user_event.event_id,
                    session_id=turn.session_id,
                    turn_id=turn.turn_id,
                    sequence=-1,
                    started_at=user_event.timestamp,
                    kind="user_prompt",
                    visible_tokens=visible_text_size(text).tokens,
                )
            )

    for item in sorted(turn.items, key=lambda item: (item.started_at, item.sequence)):
        entries.append(
            _ItemCostEntry(
                item_id=item.item_id,
                session_id=item.session_id,
                turn_id=item.turn_id,
                sequence=item.sequence,
                started_at=item.started_at,
                kind=item.kind,
                visible_tokens=_item_visible_token_weight(item),
                output_eligible=(
                    item.kind in {"agent_message", "reasoning"}
                    or is_tool_shaped_item(item)
                ),
            )
        )
    return entries


def _event_by_id(events: list[Event], event_id: UUID | None) -> Event | None:
    if event_id is None:
        return None
    for event in events:
        if event.event_id == event_id:
            return event
    return None


def _build_tool_items_for_turn(
    turn: Turn,
    *,
    session_id: UUID,
    turn_observations: list[TokenUsageObservation],
    allocated_real_token_cost_by_item: dict[UUID, AllocatedRealTokenCost] | None = None,
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

    observation_item_counts: dict[UUID, int] = {}
    for group_items, invoke_obs in groups:
        observation_id = invoke_obs.source.event_id if invoke_obs is not None else None
        if observation_id is not None:
            observation_item_counts[observation_id] = observation_item_counts.get(
                observation_id, 0
            ) + len(group_items)

    items: list[ToolItemFlat] = []
    for group_items, invoke_obs in groups:
        observation_id = invoke_obs.source.event_id if invoke_obs is not None else None
        count = observation_item_counts.get(observation_id, len(group_items))
        for item in group_items:
            base = _tool_item_flat(
                item,
                session_id=session_id,
                turn_id=turn.turn_id,
            )
            base.token_attribution = _build_token_attribution(item)
            base.allocated_real_token_cost = (
                allocated_real_token_cost_by_item or {}
            ).get(item.item_id)
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
    output_size = item_output_size(item)
    input_size = item_input_size(item)
    confidence = (
        "observed_tool_output_token_count"
        if output_size.confidence == "observed_token_count"
        else output_size.confidence
    )

    return ToolTokenAttribution(
        tool_input_tokens=input_size.tokens,
        tool_output_tokens=output_size.tokens,
        content_confidence=confidence,
    )


def _allocate_real_token_costs_for_entries(
    entries: list[_ItemCostEntry],
    observation: TokenUsageObservation | None,
    *,
    output_entries: list[_ItemCostEntry] | None = None,
) -> dict[UUID, AllocatedRealTokenCost]:
    if observation is None or not entries:
        return {}
    usage = observation.usage
    if _is_zero_usage(usage):
        return {}

    all_weights = [entry.visible_tokens for entry in entries]
    if sum(all_weights) > 0:
        method = "usage_observation_weighted_by_visible_item_tokens"
    else:
        method = "usage_observation_even_split"
        all_weights = [1 for _entry in entries]

    output_entry_ids = {entry.item_id for entry in (output_entries or [])}
    output_weights = [
        entry.visible_tokens if entry.item_id in output_entry_ids else 0
        for entry in entries
    ]
    if sum(output_weights) <= 0:
        output_weights = all_weights

    effective_input_total = _effective_input_token_total(usage, observation)
    input_allocations = _allocate_int(usage.input_tokens, all_weights)
    cached_allocations = _allocate_int(usage.cached_input_tokens, all_weights)
    cache_creation_allocations = _allocate_int(
        usage.cache_creation_input_tokens,
        all_weights,
    )
    uncached_input_allocations = _allocate_int(effective_input_total, all_weights)
    output_allocations = _allocate_int(usage.output_tokens, output_weights)
    reasoning_allocations = _allocate_int(usage.reasoning_output_tokens, output_weights)

    result: dict[UUID, AllocatedRealTokenCost] = {}
    for index, entry in enumerate(entries):
        result[entry.item_id] = AllocatedRealTokenCost(
            input_tokens=input_allocations[index],
            uncached_input_tokens=uncached_input_allocations[index],
            cached_input_tokens=cached_allocations[index],
            cache_creation_input_tokens=cache_creation_allocations[index],
            output_tokens=output_allocations[index],
            reasoning_output_tokens=reasoning_allocations[index],
            total_tokens=(
                uncached_input_allocations[index]
                + cached_allocations[index]
                + cache_creation_allocations[index]
                + output_allocations[index]
                + reasoning_allocations[index]
            ),
            allocation_method=method,
        )
    return result


def _effective_input_token_total(
    usage: TokenUsage,
    observation: TokenUsageObservation,
) -> int:
    if _uses_net_input_convention(observation.provider, observation.model):
        return usage.input_tokens
    return max(
        usage.input_tokens
        - usage.cached_input_tokens
        - usage.cache_creation_input_tokens,
        0,
    )


def _uses_net_input_convention(provider: str | None, model: str | None) -> bool:
    if provider:
        return provider.strip().lower() in {"anthropic", "claude", "claude-code"}
    return "claude" in (model or "").lower()


def _item_visible_token_weight(item: Any) -> int:
    if item.kind in {"agent_message", "reasoning"}:
        text = getattr(item, "text", None) or ""
        return max(visible_text_size(text).tokens, 0)
    attribution = _build_token_attribution(item)
    return max(
        int(attribution.tool_input_tokens) + int(attribution.tool_output_tokens),
        0,
    )


def _allocate_int(total: int, weights: list[int]) -> list[int]:
    if total <= 0 or not weights:
        return [0 for _weight in weights]

    weight_total = sum(weights)
    if weight_total <= 0:
        weights = [1 for _weight in weights]
        weight_total = sum(weights)

    raw = [(total * weight) / weight_total for weight in weights]
    floors = [int(value) for value in raw]
    remainder = total - sum(floors)
    order = sorted(
        range(len(weights)),
        key=lambda index: (raw[index] - floors[index], weights[index]),
        reverse=True,
    )
    for index in order[:remainder]:
        floors[index] += 1
    return floors


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


def _tool_item_flat(item: Item, *, session_id: UUID, turn_id: UUID) -> ToolItemFlat:
    output = item_output_text(item)
    return ToolItemFlat(
        item_id=item.item_id,
        session_id=session_id,
        turn_id=turn_id,
        tool_name=getattr(item, "tool_name", None),
        status=getattr(item, "status", None),
        input_summary=_tool_input_summary(
            getattr(item, "input", None)
            if item.kind != "command_execution"
            else getattr(item, "command", None)
        ),
        output_chars=len(output),
        output_original_tokens=reported_token_count(output),
        output_truncated=output_is_truncated(output),
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


def _turn_model(turn: TurnMetrics) -> str | None:
    for obs in turn.observations:
        if obs.model:
            return obs.model
    return None


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
            message = f"no token usage metrics found for session {session.session_id}"
            warnings.append(message)
            debug.warn(
                message,
                code="usage.no_token_metrics",
                severity="warning",
                session_id=str(session.session_id),
                vendor=session.vendor.value
                if getattr(session.vendor, "value", None)
                else None,
            )
        for message in _usage_consistency_warnings(metrics):
            warnings.append(message)
            debug.warn(
                message,
                code="usage.reported_total_inconsistent",
                severity="warning",
                session_id=str(session.session_id),
                vendor=session.vendor.value
                if getattr(session.vendor, "value", None)
                else None,
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
    total_tokens, total_confidence = _normalized_total_tokens(
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
        reported_total_tokens=(
            reported_total_tokens if reported_total_tokens > 0 else None
        ),
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
) -> tuple[int, str]:
    inclusive_input_total = input_tokens + output_tokens + reasoning_output_tokens
    additive_total = (
        input_tokens
        + cached_input_tokens
        + cache_creation_input_tokens
        + output_tokens
        + reasoning_output_tokens
    )
    derived_total = (
        additive_total
        if _uses_net_input_convention(provider)
        else inclusive_input_total
    )
    if reported_total_tokens <= 0:
        return derived_total, "reported_missing"
    if reported_total_tokens in {inclusive_input_total, additive_total}:
        return reported_total_tokens, "reported_consistent"
    return derived_total, "reported_inconsistent"


def _uses_net_input_convention(provider: str | None) -> bool:
    if not provider:
        return False
    return provider.strip().lower() in {"anthropic", "claude", "claude-code"}


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
        "totals; total_tokens was derived from normalized buckets"
    ]


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
