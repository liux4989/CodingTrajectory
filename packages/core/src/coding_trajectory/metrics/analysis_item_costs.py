"""Per-item real token cost ledger: billed attribution over usage observations."""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable
from uuid import UUID, uuid5
import numpy as np
from coding_trajectory.analysis.content_size import (
    item_input_size,
    item_output_size,
    item_output_text,
    item_text_size,
    output_is_truncated,
    reported_token_count,
    tool_input_summary,
    visible_text_size,
)
from coding_trajectory.ingestion.indexes import (
    index_events_by_id,
)
from coding_trajectory.ingestion.models import (
    Event,
    EventType,
    Item,
    Session,
    Turn,
    is_tool_shaped_item,
)
from coding_trajectory.metrics._build import (
    _is_zero_usage,
    _token_usage_from_mapping,
)
from coding_trajectory.metrics.models import (
    AllocatedRealTokenCost,
    InvokeResponseTokens,
    ItemRealTokenCostFlat,
    MetricSource,
    ReadAfterResult,
    ToolItemFlat,
    ToolTokenAttribution,
    TokenUsage,
    TokenUsageObservation,
)
from coding_trajectory.metrics.pricing import (
    CostEvidenceFlat,
    PriceRule,
    _cost_evidence_values_from_accum,
    _resolve_price_rule,
    _uses_net_input_convention,
)
from coding_trajectory.metrics.usage_math import (
    sum_usage_dicts as _sum_usage_dicts,
)
from coding_trajectory.metrics.accounting import glossary_usage_dict


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


@dataclass(slots=True)
class _CostAccum:
    """Lightweight mutable cost accumulator for hot allocation loops.

    Replaces AllocatedRealTokenCost (pydantic BaseModel) in the inner
    allocation/accumulation paths to avoid ~34M pydantic instantiations.
    Converted to AllocatedRealTokenCost only at output boundaries.
    """

    input_tokens: int = 0
    uncached_input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    allocation_method: str = "usage_observation_weighted_by_visible_item_tokens"


@dataclass(slots=True)
class _EstimatedCostAccum:
    """Primitive, output-equivalent aggregate of request-tier cost slices."""

    amounts: list[float] = field(default_factory=list)
    rows_seen: int = 0
    missing: bool = False
    date_seen: bool = False
    effective_date: str | None = None
    mixed_dates: bool = False

    def add(self, values: tuple[float, str, str | None] | None) -> None:
        self.rows_seen += 1
        if values is None:
            self.missing = True
            self.amounts.clear()
            return
        if self.missing:
            return
        amount, _pricing_source, effective_date = values
        self.amounts.append(amount)
        if not self.date_seen:
            self.date_seen = True
            self.effective_date = effective_date
        elif effective_date != self.effective_date:
            self.mixed_dates = True

    def evidence(self, *, source: str) -> CostEvidenceFlat | None:
        if not self.rows_seen or self.missing:
            return None
        return CostEvidenceFlat(
            value_usd=round(sum(self.amounts), 8),
            confidence="estimated",
            source=source,
            effective_date=None if self.mixed_dates else self.effective_date,
        )


@dataclass(slots=True)
class _ItemRealTokenCostProjection:
    """Selected output over one session's complete primitive allocation."""

    items: list[ItemRealTokenCostFlat]
    allocated_real_token_cost: AllocatedRealTokenCost | None
    allocated_real_token_cost_by_item: dict[UUID, AllocatedRealTokenCost]
    estimated_cost_by_item: dict[UUID, CostEvidenceFlat]


def _array_to_allocated_cost(
    values: np.ndarray,
    allocation_method: str | None,
) -> AllocatedRealTokenCost | None:
    if allocation_method is None:
        return None
    return AllocatedRealTokenCost(
        input_tokens=int(values[0]),
        uncached_input_tokens=int(values[1]),
        cached_input_tokens=int(values[2]),
        cache_creation_input_tokens=int(values[3]),
        output_tokens=int(values[4]),
        reasoning_output_tokens=int(values[5]),
        total_tokens=int(values[6]),
        allocation_method=allocation_method,  # type: ignore[arg-type]
    )


def _sum_allocated_cost_rows(
    values: np.ndarray,
    allocation_methods: list[str | None],
    indices: Iterable[int],
) -> AllocatedRealTokenCost | None:
    """Sum selected primitive rows with the legacy aggregate serialization."""
    allocated_indices = [
        index for index in indices if allocation_methods[index] is not None
    ]
    if not allocated_indices:
        return None
    totals = [
        sum(int(values[index, column]) for index in allocated_indices)
        for column in range(7)
    ]
    return AllocatedRealTokenCost(
        input_tokens=totals[0],
        uncached_input_tokens=totals[1],
        cached_input_tokens=totals[2],
        cache_creation_input_tokens=totals[3],
        output_tokens=totals[4],
        reasoning_output_tokens=totals[5],
        total_tokens=totals[6],
    )


def _stats_cost_entries_for_session(session: Session) -> list[_ItemCostEntry]:
    entries: list[_ItemCostEntry] = []
    events_by_id = index_events_by_id(session.events)
    source_measurements = getattr(session, "measurements", None)
    context_sources = (
        source_measurements.context_sources if source_measurements is not None else None
    )
    for source_index, source in enumerate(
        context_sources if context_sources is not None else session.context_sources
    ):
        if context_sources is not None:
            visible = source.tokens
        else:
            visible = visible_text_size(source.text).tokens
        tokens = visible or (source.reported_tokens or 0)
        if tokens <= 0:
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
                visible_tokens=tokens,
                context_key=source.key,
            )
        )
    entries.extend(
        entry
        for turn in sorted(session.turns, key=lambda item: item.sequence)
        for entry in _item_cost_entries_for_turn(turn, events_by_id=events_by_id)
    )
    return entries


def _build_item_real_token_cost_projection_for_session(
    session: Session,
    *,
    selected_turn_ids: set[UUID] | None,
    selected_item_ids: set[UUID],
    include_items: bool,
    pricing_rule_cache: dict[tuple[str | None, str], PriceRule | None] | None = None,
) -> _ItemRealTokenCostProjection:
    """Billed attribution: sum every API call's per-call usage across present items.

    Iterates *every* usage observation (not just the last per turn) and allocates
    that call's per-call token delta across the items present at the call, weighted
    by visible tokens. The complete primitive allocation always reconciles to
    provider-reported cumulative usage. Output models and request-tier cost
    evidence are then materialized only for the selected ledger/tool rows.
    """
    events_by_id = index_events_by_id(session.events)
    entries = [
        entry
        for turn in sorted(session.turns, key=lambda item: item.sequence)
        for entry in _item_cost_entries_for_turn(turn, events_by_id=events_by_id)
    ]
    if not entries:
        return _ItemRealTokenCostProjection(
            items=[],
            allocated_real_token_cost=None,
            allocated_real_token_cost_by_item={},
            estimated_cost_by_item={},
        )
    if pricing_rule_cache is None:
        pricing_rule_cache = {}

    selected_output_indices = [
        index
        for index, entry in enumerate(entries)
        if selected_turn_ids is None or entry.turn_id in selected_turn_ids
    ]
    selected_item_indices = {
        index
        for index in selected_output_indices
        if entries[index].item_id in selected_item_ids
    }
    priced_indices = (
        set(selected_output_indices) if include_items else selected_item_indices
    )

    entry_indices_by_turn: dict[UUID, list[int]] = {}
    for index, entry in enumerate(entries):
        entry_indices_by_turn.setdefault(entry.turn_id, []).append(index)
    weights_by_turn: dict[UUID, np.ndarray] = {}
    output_weights_by_turn: dict[UUID, np.ndarray] = {}
    times_by_turn: dict[UUID, list[datetime]] = {}
    for tid, turn_indices in entry_indices_by_turn.items():
        turn_indices.sort(key=lambda index: entries[index].started_at)
        times_by_turn[tid] = [entries[index].started_at for index in turn_indices]
        weights_by_turn[tid] = np.array(
            [entries[index].visible_tokens for index in turn_indices], dtype=np.int64
        )
        output_weights_by_turn[tid] = np.array(
            [
                entries[index].visible_tokens if entries[index].output_eligible else 0
                for index in turn_indices
            ],
            dtype=np.int64,
        )

    allocated_by_index = np.zeros((len(entries), 7), dtype=np.int64)
    allocation_method_by_index: list[str | None] = [None] * len(entries)
    estimated_costs: list[_EstimatedCostAccum | None] = [None] * len(entries)
    for observation, turn_id in _session_usage_observations(session):
        if turn_id is None or turn_id not in entry_indices_by_turn:
            continue
        cutoff = bisect.bisect_right(times_by_turn[turn_id], observation.timestamp)
        if cutoff == 0:
            continue
        allocations = _token_cost_allocations(
            observation.usage,
            observation,
            weights_by_turn[turn_id][:cutoff],
            output_weights_by_turn[turn_id][:cutoff],
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
            allocation_method,
        ) = allocations
        present_indices = entry_indices_by_turn[turn_id][:cutoff]
        allocated_by_index[present_indices] += np.column_stack(
            (
                input_a,
                uncached_input_a,
                cached_a,
                cache_creation_a,
                output_a,
                reasoning_a,
                total_a,
            )
        )
        for entry_index in present_indices:
            if allocation_method_by_index[entry_index] is None:
                allocation_method_by_index[entry_index] = allocation_method
        # Resolve in legacy observation order even when this projection has no
        # priced rows. The request-scoped cache is also the pricing-catalog
        # freshness boundary; delaying a first resolution could change which
        # rule later selected rows reuse.
        pricing_rule = _resolve_price_rule(
            observation.model,
            provider=observation.provider,
            cache=pricing_rule_cache,
        )
        pricing_offsets = [
            (offset, entry_index)
            for offset, entry_index in enumerate(present_indices)
            if entry_index in priced_indices
        ]
        if not pricing_offsets:
            continue
        for offset, entry_index in pricing_offsets:
            cost_accum = estimated_costs[entry_index]
            if cost_accum is None:
                cost_accum = _EstimatedCostAccum()
                estimated_costs[entry_index] = cost_accum
            if cost_accum.missing:
                continue
            cost_accum.add(
                _cost_evidence_values_from_accum(
                    int(input_a[offset]),
                    int(uncached_input_a[offset]),
                    int(cached_a[offset]),
                    int(cache_creation_a[offset]),
                    int(output_a[offset]),
                    int(reasoning_a[offset]),
                    model=observation.model,
                    provider=observation.provider,
                    pricing_input_tokens=observation.usage.input_tokens,
                    pricing_rule=pricing_rule,
                )
            )

    session_allocated_cost = _sum_allocated_cost_rows(
        allocated_by_index,
        allocation_method_by_index,
        range(len(entries)),
    )
    _assert_item_real_token_costs_reconcile(
        session,
        entries,
        session_allocated_cost,
    )

    items: list[ItemRealTokenCostFlat] = []
    allocated_real_token_cost_by_item: dict[UUID, AllocatedRealTokenCost] = {}
    estimated_cost_by_item: dict[UUID, CostEvidenceFlat] = {}
    for index in selected_output_indices:
        if not include_items and index not in selected_item_indices:
            continue
        entry = entries[index]
        allocated_cost = _array_to_allocated_cost(
            allocated_by_index[index], allocation_method_by_index[index]
        )
        estimated_cost = (
            estimated_costs[index].evidence(source="request-tier allocated aggregate")
            if estimated_costs[index] is not None
            else None
        )
        if index in selected_item_indices:
            if allocated_cost is not None:
                allocated_real_token_cost_by_item[entry.item_id] = allocated_cost
            if estimated_cost is not None:
                estimated_cost_by_item[entry.item_id] = estimated_cost
        if include_items:
            items.append(
                ItemRealTokenCostFlat(
                    item_id=entry.item_id,
                    session_id=entry.session_id,
                    turn_id=entry.turn_id,
                    sequence=entry.sequence,
                    kind=entry.kind,
                    visible_tokens=entry.visible_tokens,
                    allocated_real_token_cost=allocated_cost,
                    estimated_cost=estimated_cost,
                )
            )
    return _ItemRealTokenCostProjection(
        items=items,
        allocated_real_token_cost=_sum_allocated_cost_rows(
            allocated_by_index,
            allocation_method_by_index,
            selected_output_indices,
        ),
        allocated_real_token_cost_by_item=allocated_real_token_cost_by_item,
        estimated_cost_by_item=estimated_cost_by_item,
    )


def _assert_item_real_token_costs_reconcile(
    session: Session,
    entries: list[_ItemCostEntry],
    allocated_real_token_cost: AllocatedRealTokenCost | None,
) -> None:
    expected = _sum_allocated_usage_for_present_observations(session, entries)
    actual = _allocated_cost_usage_dict(allocated_real_token_cost)
    assert actual == expected, (
        "item real token cost allocation must reconcile to observed session usage"
    )


def _sum_allocated_usage_for_present_observations(
    session: Session,
    entries: list[_ItemCostEntry],
) -> dict[str, int]:
    entries_by_turn: dict[UUID, list[_ItemCostEntry]] = {}
    times_by_turn: dict[UUID, list[datetime]] = {}
    for entry in entries:
        entries_by_turn.setdefault(entry.turn_id, []).append(entry)
    for tid, turn_entries in entries_by_turn.items():
        turn_entries.sort(key=lambda e: e.started_at)
        times_by_turn[tid] = [e.started_at for e in turn_entries]

    total: dict[str, int] = {}
    for observation, turn_id in _session_usage_observations(session):
        if turn_id is None:
            continue
        times = times_by_turn.get(turn_id)
        if times is None:
            continue
        if bisect.bisect_right(times, observation.timestamp) == 0:
            continue
        total = _sum_usage_dicts(
            (
                total,
                _resident_expected_usage(observation),
            )
        )
    return total


def _resident_expected_usage(observation: TokenUsageObservation) -> dict[str, int]:
    return _allocated_cost_usage_dict(
        _allocated_cost_from_usage_observation(observation)
    )


def _allocated_cost_from_usage_observation(
    observation: TokenUsageObservation,
) -> _CostAccum:
    usage = observation.usage
    uncached_input_tokens = _effective_input_token_total(usage, observation)
    return _CostAccum(
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
    cost: _CostAccum | AllocatedRealTokenCost | None,
) -> dict[str, int]:
    if cost is None:
        return {}
    return _allocated_cost_usage_dict_from_values(
        cost.input_tokens,
        cost.uncached_input_tokens,
        cost.cached_input_tokens,
        cost.cache_creation_input_tokens,
        cost.output_tokens,
        cost.reasoning_output_tokens,
        cost.total_tokens,
    )


def _allocated_cost_usage_dict_from_array(values: np.ndarray) -> dict[str, int]:
    return _allocated_cost_usage_dict_from_values(
        int(values[0]),
        int(values[1]),
        int(values[2]),
        int(values[3]),
        int(values[4]),
        int(values[5]),
        int(values[6]),
    )


def _allocated_cost_usage_dict_from_values(
    input_tokens: int,
    uncached_input_tokens: int,
    cached_input_tokens: int,
    cache_creation_input_tokens: int,
    output_tokens: int,
    reasoning_output_tokens: int,
    total_tokens: int,
) -> dict[str, int]:
    return glossary_usage_dict(
        input_tokens=input_tokens,
        uncached_input_tokens=uncached_input_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        output_tokens=output_tokens,
        reasoning_output_tokens=reasoning_output_tokens,
        processed_tokens=total_tokens,
        drop_nonpositive=True,
    )


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
    left: _CostAccum | None,
    right: _CostAccum,
) -> _CostAccum:
    if left is None:
        return right
    left.input_tokens += right.input_tokens
    left.uncached_input_tokens += right.uncached_input_tokens
    left.cached_input_tokens += right.cached_input_tokens
    left.cache_creation_input_tokens += right.cache_creation_input_tokens
    left.output_tokens += right.output_tokens
    left.reasoning_output_tokens += right.reasoning_output_tokens
    left.total_tokens += right.total_tokens
    return left


def _item_cost_entries_for_turn(
    turn: Turn,
    *,
    events_by_id: dict[UUID, Event],
) -> list[_ItemCostEntry]:
    entries: list[_ItemCostEntry] = []
    user_event = (
        events_by_id.get(turn.user_request_event_id)
        if turn.user_request_event_id is not None
        else None
    )
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


def _build_tool_items_for_turn(
    turn: Turn,
    *,
    session_id: UUID,
    turn_observations: list[TokenUsageObservation],
    allocated_real_token_cost_by_item: dict[UUID, AllocatedRealTokenCost] | None = None,
    estimated_cost_by_item: dict[UUID, CostEvidenceFlat] | None = None,
    include_advanced_causality: bool = True,
) -> list[ToolItemFlat]:
    tool_entries = [item for item in turn.items if is_tool_shaped_item(item)]
    if not tool_entries:
        return []

    tool_entries_sorted = sorted(tool_entries, key=lambda item: item.started_at)
    if not include_advanced_causality:
        projected_items: list[ToolItemFlat] = []
        for item in tool_entries_sorted:
            base = _tool_item_flat(
                item,
                session_id=session_id,
                turn_id=turn.turn_id,
            )
            base.token_attribution = _build_token_attribution(item)
            base.allocated_real_token_cost = (
                allocated_real_token_cost_by_item or {}
            ).get(item.item_id)
            base.estimated_cost = (estimated_cost_by_item or {}).get(item.item_id)
            projected_items.append(base)
        return projected_items

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
            base.estimated_cost = (estimated_cost_by_item or {}).get(item.item_id)
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


def _token_cost_allocations(
    usage: TokenUsage,
    observation: TokenUsageObservation,
    all_weights: np.ndarray,
    output_weights: np.ndarray,
    *,
    as_arrays: bool = False,
) -> (
    tuple[
        list[int] | np.ndarray,
        list[int] | np.ndarray,
        list[int] | np.ndarray,
        list[int] | np.ndarray,
        list[int] | np.ndarray,
        list[int] | np.ndarray,
        list[int] | np.ndarray,
        str,
    ]
    | None
):
    """Allocate observation usage across weights; return 7 parts + method.

    Returns None for zero usage. The 7 parts are: input, uncached_input,
    cached, cache_creation, output, reasoning, total. ``as_arrays`` keeps
    these parts in NumPy form for callers that accumulate them in bulk.
    """
    if _is_zero_usage(usage):
        return None
    weight_total = int(all_weights.sum())
    if weight_total > 0:
        method = "usage_observation_weighted_by_visible_item_tokens"
    else:
        method = "usage_observation_even_split"
        all_weights = np.ones(len(all_weights), dtype=np.int64)
    if int(output_weights.sum()) <= 0:
        output_weights = all_weights
    effective_input_total = _effective_input_token_total(usage, observation)
    all_results = _allocate_int_batch(
        [
            usage.input_tokens,
            usage.cached_input_tokens,
            usage.cache_creation_input_tokens,
            effective_input_total,
        ],
        all_weights,
        as_array=as_arrays,
    )
    output_results = _allocate_int_batch(
        [usage.output_tokens, usage.reasoning_output_tokens],
        output_weights,
        as_array=as_arrays,
    )
    input_a, cached_a, cache_creation_a, uncached_input_a = all_results
    output_a, reasoning_a = output_results
    if as_arrays:
        total_a = (
            uncached_input_a + cached_a + cache_creation_a + output_a + reasoning_a
        )
    else:
        n = len(input_a)
        total_a = [
            uncached_input_a[i]
            + cached_a[i]
            + cache_creation_a[i]
            + output_a[i]
            + reasoning_a[i]
            for i in range(n)
        ]
    return (
        input_a,
        uncached_input_a,
        cached_a,
        cache_creation_a,
        output_a,
        reasoning_a,
        total_a,
        method,
    )


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


def _item_visible_token_weight(item: Any) -> int:
    if item.kind in {"agent_message", "reasoning"}:
        return max(item_text_size(item).tokens, 0)
    attribution = _build_token_attribution(item)
    return max(
        int(attribution.tool_input_tokens) + int(attribution.tool_output_tokens),
        0,
    )


def _allocate_int_batch(
    totals: list[int], weights: list[int] | "np.ndarray", *, as_array: bool = False
) -> list[list[int]] | np.ndarray:
    """Allocate multiple totals across the same weights via largest-remainder.

    Vectorized with numpy: the float floors and remainders are computed for all
    totals at once, and the +1 remainder distribution uses per-row argsort (in
    C). Zero-weight entries naturally sort last (frac=0, weight=0) and never
    receive a +1, so the explicit sparse path is unnecessary.
    """
    n = len(weights)
    if n == 0:
        if as_array:
            return np.zeros((len(totals), 0), dtype=np.int64)
        return [[0] * n for _ in totals]
    weights_arr = np.asarray(weights, dtype=np.int64)
    weight_total = int(weights_arr.sum())
    if weight_total <= 0:
        weights_arr = np.ones(n, dtype=np.int64)
        weight_total = n
    totals_arr = np.asarray(totals, dtype=np.int64)
    raw = totals_arr[:, None] * weights_arr[None, :] / weight_total
    floors = raw.astype(np.int64)
    remainders = totals_arr - floors.sum(axis=1)
    neg_weights = -weights_arr
    for row in range(floors.shape[0]):
        remainder = int(remainders[row])
        if remainder <= 0:
            continue
        order = np.lexsort((neg_weights, -raw[row] + floors[row]))[:remainder]
        floors[row, order] += 1
    return floors if as_array else floors.tolist()


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
    measurements = getattr(item, "measurements", None)
    if measurements is not None:
        return ToolItemFlat(
            item_id=item.item_id,
            session_id=session_id,
            turn_id=turn_id,
            tool_name=getattr(item, "tool_name", None),
            status=getattr(item, "status", None),
            input_summary=measurements.input_summary,
            output_chars=measurements.output_chars,
            output_original_tokens=measurements.output_original_tokens,
            output_truncated=measurements.output_truncated,
        )
    output = item_output_text(item)
    return ToolItemFlat(
        item_id=item.item_id,
        session_id=session_id,
        turn_id=turn_id,
        tool_name=getattr(item, "tool_name", None),
        status=getattr(item, "status", None),
        input_summary=tool_input_summary(
            getattr(item, "input", None)
            if item.kind != "command_execution"
            else getattr(item, "command", None)
        ),
        output_chars=len(output),
        output_original_tokens=reported_token_count(output),
        output_truncated=output_is_truncated(output),
    )
