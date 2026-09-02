"""Stats-table token attribution over starting context and items."""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import numpy as np

from coding_trajectory.ingestion.models import SessionGraph
from coding_trajectory.metrics._build import _build_full_metrics
from coding_trajectory.metrics.attribution.item_costs import (
    _CostAccum,
    _add_allocated_real_token_cost,
    _allocated_cost_from_usage_observation,
    _allocated_cost_usage_dict,
    _allocated_cost_usage_dict_from_array,
    _session_usage_observations,
    _stats_cost_entries_for_session,
    _token_cost_allocations,
)
from coding_trajectory.metrics.models import SessionGraphMetrics
from coding_trajectory.metrics.usage_math import sum_usage_dicts
from coding_trajectory.token_counter import get_current_counter, session_scoped


@dataclass(slots=True)
class StatsTokenUsageBreakdown:
    graph_usage: dict[str, Any]
    usage_by_session: dict[UUID, dict[str, Any]]
    counter_name: str


@session_scoped
def build_session_graph_stats_usage_breakdown(
    session_graph: SessionGraph,
) -> StatsTokenUsageBreakdown:
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
                session_usage_by_context_source[key] = sum_usage_dicts(
                    (existing, usage)
                )

        session_billed_usage = _allocated_cost_usage_dict(session_billed_token_usage)
        assert (
            sum_usage_dicts(
                (
                    sum_usage_dicts(session_usage_by_item.values()),
                    sum_usage_dicts(session_usage_by_context_source.values()),
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
                usage if existing is None else sum_usage_dicts((existing, usage))
            )
        if session_billed_token_usage is not None:
            billed_token_usage = _add_allocated_real_token_cost(
                billed_token_usage, session_billed_token_usage
            )

    assert sum_usage_dicts(
        (
            sum_usage_dicts(allocated_usage_by_item.values()),
            sum_usage_dicts(allocated_usage_by_context_source.values()),
        )
    ) == _allocated_cost_usage_dict(billed_token_usage), (
        "session stats attribution must reconcile to billed token usage"
    )
    return StatsTokenUsageBreakdown(
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
    return build_session_graph_stats_usage_breakdown(session_graph).graph_usage


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
