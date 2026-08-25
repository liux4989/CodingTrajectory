"""Reusable economics contribution for one canonical session graph.

The contribution is the core-owned seam between immutable transcript ingestion
and derived consumers such as the datahub.  It computes shared token metrics
and graph indexes once, preserves the existing public projection payloads, and
keeps expensive item/context evidence behind an explicit detail level.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coding_trajectory.analysis.content_size import scoped_content_size_cache
from coding_trajectory.analysis.projections import build_session_graph_overview
from coding_trajectory.analysis.session_stats import build_session_stats_projection
from coding_trajectory.ingestion.indexes import (
    SessionGraphIndex,
    build_session_graph_index,
)
from coding_trajectory.ingestion.models import Session, SessionGraph
from coding_trajectory.metrics.analysis import (
    _StatsTokenUsageBreakdown,
    _build_session_graph_stats_usage_breakdown,
    _build_session_graph_tool_usage,
    _sum_usage_dicts,
    build_session_graph_context_stats,
    build_session_graph_billed_token_usage,
    build_session_graph_full_metrics,
    build_session_graph_model_usage,
    build_session_graph_stats_token_usage,
    build_session_graph_usage,
)
from coding_trajectory.metrics.models import SessionGraphMetrics
from coding_trajectory.token_counter import (
    counter_for_session_graph,
    scoped_counter,
    session_scoped,
)


EconomicsDetail = Literal["core", "evidence"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EconomicsReconciliation(_StrictModel):
    """Proof that attributed usage retains the billed accounting total."""

    basis: Literal["canonical_billed_total", "item_context_allocation"]
    billed_token_usage: dict[str, int] = Field(default_factory=dict)
    attributed_token_usage: dict[str, int] = Field(default_factory=dict)
    reconciled: Literal[True] = True

    @model_validator(mode="after")
    def _validate_reconciliation(self) -> EconomicsReconciliation:
        if self.billed_token_usage != self.attributed_token_usage:
            raise ValueError(
                "attributed token usage must reconcile to billed_token_usage"
            )
        return self


class EconomicsContribution(_StrictModel):
    """Stable core economics plus optional expensive supporting evidence."""

    schema_version: Literal[1] = 1
    detail: EconomicsDetail
    root_session_id: UUID
    project: str | None = None
    usage: dict[str, Any]
    model_usage: dict[str, Any]
    reconciliation: EconomicsReconciliation
    tool_usage: dict[str, Any] | None = None
    stats: dict[str, Any] | None = None
    overview: dict[str, Any] | None = None


class GraphEconomicsBundle(_StrictModel):
    """One graph computation shared by every public session entrypoint."""

    schema_version: Literal[1] = 1
    detail: EconomicsDetail
    root_session_id: UUID
    sessions: dict[UUID, EconomicsContribution]
    entrypoint_sessions: dict[UUID, UUID]



@session_scoped
def build_economics_contribution(
    session_graph: SessionGraph,
    *,
    detail: EconomicsDetail = "core",
    entrypoint_id: str | UUID | None = None,
    include_item_real_token_costs: bool = False,
    include_advanced_causality: bool = False,
) -> EconomicsContribution:
    """Build one reusable, exactly reconciled graph economics contribution.

    ``core`` answers where tokens, time, and money went and retains the billed
    usage reconciliation. ``evidence`` adds tool/item attribution, context
    statistics, and the activity overview without changing core accounting.
    """

    index = build_session_graph_index(session_graph)
    selected = _selected_session(
        session_graph, index=index, entrypoint_id=entrypoint_id
    )
    single = _single_session_graph(session_graph, selected)
    with scoped_content_size_cache(), scoped_counter(counter_for_session_graph(single)):
        return _build_session_contribution(
            single,
            session=selected,
            session_metrics=build_session_graph_full_metrics(single),
            detail=detail,
            include_item_real_token_costs=include_item_real_token_costs,
            include_advanced_causality=include_advanced_causality,
        )


@session_scoped
def build_graph_economics_bundle(
    session_graph: SessionGraph,
    *,
    detail: EconomicsDetail = "core",
    include_item_real_token_costs: bool = False,
    include_advanced_causality: bool = False,
) -> GraphEconomicsBundle:
    """Materialize a reusable Pydantic bundle for every session entrypoint.

    Core service methods locate the containing graph, then ``session.*`` selects
    one canonical session. Consumers with bounded memory requirements should
    prefer :func:`iter_graph_economics_contributions`, which produces the same
    contributions without retaining every session payload simultaneously.
    """

    index = build_session_graph_index(session_graph)
    contributions = dict(
        iter_graph_economics_contributions(
            session_graph,
            detail=detail,
            include_item_real_token_costs=include_item_real_token_costs,
            include_advanced_causality=include_advanced_causality,
        )
    )
    entrypoint_sessions = {
        session_id: session_id for session_id in index.session_ids_in_order
    }
    entrypoint_sessions.update(index.session_by_turn_id)
    root_session = _selected_session(session_graph, index=index, entrypoint_id=None)
    entrypoint_sessions[session_graph.root_session_id] = root_session.session_id
    return GraphEconomicsBundle(
        detail=detail,
        root_session_id=session_graph.root_session_id,
        sessions=contributions,
        entrypoint_sessions=entrypoint_sessions,
    )


def iter_graph_economics_contributions(
    session_graph: SessionGraph,
    *,
    detail: EconomicsDetail = "core",
    include_item_real_token_costs: bool = False,
    include_advanced_causality: bool = False,
) -> Iterator[tuple[UUID, EconomicsContribution]]:
    """Yield exact session contributions while bounding intermediate lifetime."""

    if detail not in {"core", "evidence"}:
        raise ValueError("economics detail must be core or evidence")
    for session in session_graph.sessions:
        single = _single_session_graph(session_graph, session)
        with (
            scoped_content_size_cache(),
            scoped_counter(counter_for_session_graph(single)),
        ):
            yield (
                session.session_id,
                _build_session_contribution(
                    single,
                    session=session,
                    session_metrics=build_session_graph_full_metrics(single),
                    detail=detail,
                    include_item_real_token_costs=include_item_real_token_costs,
                    include_advanced_causality=include_advanced_causality,
                ),
            )


def _build_session_contribution(
    session_graph: SessionGraph,
    *,
    session: Session,
    session_metrics: SessionGraphMetrics,
    detail: EconomicsDetail,
    include_item_real_token_costs: bool,
    include_advanced_causality: bool,
) -> EconomicsContribution:
    index = build_session_graph_index(session_graph)
    billed_usage = build_session_graph_billed_token_usage(
        session_graph,
        precomputed_metrics=session_metrics,
    )
    usage = build_session_graph_usage(
        session_graph,
        precomputed_metrics=session_metrics,
        precomputed_index=index,
    )
    model_usage = build_session_graph_model_usage(
        session_graph,
        precomputed_metrics=session_metrics,
    )
    if detail == "core":
        return EconomicsContribution(
            detail=detail,
            root_session_id=session.session_id,
            project=session_graph.project_identifier,
            usage=usage,
            model_usage=model_usage,
            reconciliation=EconomicsReconciliation(
                basis="canonical_billed_total",
                billed_token_usage=billed_usage,
                attributed_token_usage=billed_usage,
            ),
        )

    stats_breakdown = _build_session_graph_stats_usage_breakdown(session_graph)
    reconciliation = _reconciliation(
        stats_breakdown,
        expected_billed_usage=billed_usage,
    )
    tool_usage = _build_session_graph_tool_usage(
        session_graph,
        include_item_real_token_costs=include_item_real_token_costs,
        include_advanced_causality=include_advanced_causality,
        precomputed_metrics=session_metrics,
    )
    stats = build_session_graph_stats(
        session_graph,
        precomputed_metrics=session_metrics,
        precomputed_index=index,
        precomputed_breakdown=stats_breakdown,
    )
    overview = build_session_graph_overview(session_graph, index=index)
    return EconomicsContribution(
        detail=detail,
        root_session_id=session.session_id,
        project=session_graph.project_identifier,
        usage=usage,
        model_usage=model_usage,
        reconciliation=reconciliation,
        tool_usage=tool_usage,
        stats=stats,
        overview=overview,
    )


def _single_session_graph(source_graph: SessionGraph, session: Session) -> SessionGraph:
    return SessionGraph(
        root_session_id=session.session_id,
        project_identifier=source_graph.project_identifier,
        sessions=[session],
    )


def _selected_session(
    session_graph: SessionGraph,
    *,
    index: SessionGraphIndex,
    entrypoint_id: str | UUID | None,
) -> Session:
    selected: Session | None = None
    if entrypoint_id is not None:
        resource_id = _parse_entrypoint_id(entrypoint_id)
        selected = index.sessions_by_id.get(resource_id)
        if selected is None:
            session_id = index.session_by_turn_id.get(resource_id)
            selected = index.sessions_by_id.get(session_id) if session_id else None
    if selected is None:
        selected = index.sessions_by_id.get(session_graph.root_session_id)
    if selected is None:
        selected = min(
            session_graph.sessions,
            key=lambda item: (item.started_at, str(item.session_id)),
            default=None,
        )
    if selected is None:
        raise ValueError("session_graph has no sessions")
    return selected


def build_session_graph_stats(
    session_graph: SessionGraph,
    *,
    include_session_composition: bool = True,
    precomputed_metrics: SessionGraphMetrics | None = None,
    precomputed_index: SessionGraphIndex | None = None,
    precomputed_breakdown: _StatsTokenUsageBreakdown | None = None,
) -> dict[str, Any]:
    """Build the canonical stats response while accepting shared intermediates."""

    stats_breakdown = precomputed_breakdown or (
        _build_session_graph_stats_usage_breakdown(session_graph)
    )
    stats_usage = stats_breakdown.graph_usage
    full_metrics = precomputed_metrics or build_session_graph_full_metrics(
        session_graph
    )
    result = build_session_graph_context_stats(
        session_graph,
        allocated_usage_by_item=stats_usage["allocated_usage_by_item"],
        allocated_usage_by_context_source=stats_usage[
            "allocated_usage_by_context_source"
        ],
        precomputed_metrics=full_metrics,
    )
    if stats_usage.get("billed_token_usage"):
        result["billed_token_usage"] = stats_usage["billed_token_usage"]
    return build_session_stats_projection(
        session_graph,
        result,
        build_session_graph_context_stats=build_session_graph_context_stats,
        build_session_graph_stats_token_usage=build_session_graph_stats_token_usage,
        precomputed_usage_by_session=stats_breakdown.usage_by_session,
        precomputed_counter_name=stats_breakdown.counter_name,
        precomputed_metrics_by_session={
            session.session_id: session for session in full_metrics.sessions
        },
        precomputed_index=precomputed_index,
        include_session_composition=include_session_composition,
    )


def _parse_entrypoint_id(entrypoint_id: str | UUID) -> UUID:
    try:
        return entrypoint_id if isinstance(entrypoint_id, UUID) else UUID(entrypoint_id)
    except ValueError as exc:
        raise ValueError(f"invalid economics entrypoint id: {entrypoint_id}") from exc


def _reconciliation(
    stats_breakdown: _StatsTokenUsageBreakdown,
    *,
    expected_billed_usage: dict[str, int] | None = None,
) -> EconomicsReconciliation:
    usage = stats_breakdown.graph_usage
    attributed = _sum_usage_dicts(
        (
            _sum_usage_dicts(usage["allocated_usage_by_item"].values()),
            _sum_usage_dicts(usage["allocated_usage_by_context_source"].values()),
        )
    )
    billed = dict(usage.get("billed_token_usage") or {})
    if expected_billed_usage is not None and billed != expected_billed_usage:
        raise ValueError("evidence allocation changed canonical billed_token_usage")
    return EconomicsReconciliation(
        basis="item_context_allocation",
        billed_token_usage=billed,
        attributed_token_usage=attributed,
    )


__all__ = [
    "EconomicsContribution",
    "EconomicsDetail",
    "EconomicsReconciliation",
    "GraphEconomicsBundle",
    "build_economics_contribution",
    "build_graph_economics_bundle",
    "build_session_graph_stats",
    "iter_graph_economics_contributions",
]
