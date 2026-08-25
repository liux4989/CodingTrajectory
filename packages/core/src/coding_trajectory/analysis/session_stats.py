"""Session-stats projection over the canonical metrics result.

The metrics layer produces a raw stats result (context window, provider
usage buckets, billed token usage). The *presentation* concerns that used
to live in the service handler - the ``scope`` label and per-session sections
carrying workflow ``role``/``relationship``/``parent_session_id`` labels -
are projection concerns and belong here, not in the canonical service layer.

Field names and shapes are part of the public ``session.stats`` contract
and are preserved exactly; this module only relocates the assembly logic.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from coding_trajectory.ingestion.common import prune_nones
from coding_trajectory.ingestion.indexes import (
    SessionGraphIndex,
    build_session_graph_index,
    ordered_sessions,
)
from coding_trajectory.ingestion.models import Session, SessionGraph
from coding_trajectory.token_counter import counter_for_session_graph

if TYPE_CHECKING:
    from coding_trajectory.metrics.models import SessionMetrics

# Edge types that mark a session as a spawned subagent rather than a peer
# member of the graph. Co-located with session_role so the label logic is
# in one place instead of a magic string set in the service handler.
_SUBAGENT_RELATIONSHIPS: frozenset[str] = frozenset(
    {"spawned_subagent", "sidechain_of"}
)


def session_title(session: Session) -> str | None:
    """Return the first available vendor extension title for display."""
    extensions = session.extensions
    if extensions and extensions.codex and extensions.codex.title:
        return extensions.codex.title
    if extensions and extensions.claude_code and extensions.claude_code.title:
        return extensions.claude_code.title
    if extensions and extensions.pi and extensions.pi.title:
        return extensions.pi.title
    return None


def session_preview(session: Session) -> str | None:
    """Return a vendor-provided session preview, distinct from its title."""
    extensions = session.extensions
    if extensions and extensions.codex and extensions.codex.preview:
        return extensions.codex.preview
    return None


def session_graph_title(session_graph: SessionGraph) -> str | None:
    """Return the first available title across the graph's sessions (root first)."""
    by_id = {session.session_id: session for session in session_graph.sessions}
    root = by_id.get(session_graph.root_session_id)
    if root is not None:
        title = session_title(root)
        if title:
            return title
    for session in session_graph.sessions:
        title = session_title(session)
        if title:
            return title
    return None


def session_graph_preview(session_graph: SessionGraph) -> str | None:
    """Return the root preview, otherwise the first available graph preview."""
    by_id = {session.session_id: session for session in session_graph.sessions}
    root = by_id.get(session_graph.root_session_id)
    if root is not None:
        preview = session_preview(root)
        if preview:
            return preview
    for session in session_graph.sessions:
        preview = session_preview(session)
        if preview:
            return preview
    return None


def single_session_graph(source_graph: SessionGraph, session: Session) -> SessionGraph:
    """Build a one-session graph view so a single session can be stat'd in isolation."""
    return SessionGraph(
        root_session_id=session.session_id,
        project_identifier=source_graph.project_identifier,
        sessions=[session],
    )


def session_role(
    session: Session, *, session_graph: SessionGraph, index: SessionGraphIndex
) -> str:
    """Classify a session's workflow role within the graph.

    The root session is ``main``; sessions spawned via a subagent/sidechain
    edge are ``subagent``; everything else is a peer ``member``.
    """
    if session.session_id == session_graph.root_session_id:
        return "main"
    relationship = index.incoming_edge_type.get(session.session_id)
    if relationship in _SUBAGENT_RELATIONSHIPS and index.parent.get(session.session_id):
        return "subagent"
    return relationship or "member"


def session_stats_sections(
    session_graph: SessionGraph,
    *,
    build_session_graph_context_stats: Callable[..., dict[str, Any]],
    build_session_graph_stats_token_usage: Callable[[SessionGraph], dict[str, Any]],
    precomputed_usage_by_session: dict[UUID, dict[str, Any]] | None = None,
    precomputed_counter_name: str | None = None,
    precomputed_metrics_by_session: dict[UUID, SessionMetrics] | None = None,
    precomputed_index: SessionGraphIndex | None = None,
    include_composition: bool = True,
) -> list[dict[str, Any]]:
    """Build a per-session stats section for each session in the graph.

    Composition controls the nested semantic category tree only. Observed
    context totals, runtime, usage, billing, and session identity are retained.
    """
    index = precomputed_index or build_session_graph_index(session_graph)
    precomputed_by_session = precomputed_usage_by_session or {}
    metrics_by_session = precomputed_metrics_by_session or {}
    sections: list[dict[str, Any]] = []
    for session in ordered_sessions(index):
        single = single_session_graph(session_graph, session)
        stats_usage = None
        if (
            precomputed_counter_name
            and counter_for_session_graph(single).name == precomputed_counter_name
        ):
            stats_usage = precomputed_by_session.get(session.session_id)
        if stats_usage is None:
            stats_usage = build_session_graph_stats_token_usage(single)
        section = build_session_graph_context_stats(
            single,
            allocated_usage_by_item=stats_usage["allocated_usage_by_item"],
            allocated_usage_by_context_source=stats_usage[
                "allocated_usage_by_context_source"
            ],
            include_composition=include_composition,
            precomputed_metrics=metrics_by_session.get(session.session_id),
        )
        if stats_usage.get("billed_token_usage"):
            section["billed_token_usage"] = stats_usage["billed_token_usage"]
        section.update(
            prune_nones(
                {
                    "session_id": str(session.session_id),
                    "role": session_role(
                        session, session_graph=session_graph, index=index
                    ),
                    "relationship": index.incoming_edge_type.get(session.session_id),
                    "parent_session_id": (
                        str(index.parent[session.session_id])
                        if index.parent.get(session.session_id)
                        else None
                    ),
                    "agent_name": session.agent_name,
                    "title": session_title(session),
                }
            )
        )
        sections.append(section)
    return sections


def build_session_stats_projection(
    session_graph: SessionGraph,
    stats_result: dict[str, Any],
    *,
    build_session_graph_context_stats: Callable[..., dict[str, Any]],
    build_session_graph_stats_token_usage: Callable[[SessionGraph], dict[str, Any]],
    precomputed_usage_by_session: dict[UUID, dict[str, Any]] | None = None,
    precomputed_counter_name: str | None = None,
    precomputed_metrics_by_session: dict[UUID, SessionMetrics] | None = None,
    precomputed_index: SessionGraphIndex | None = None,
    include_session_composition: bool = True,
) -> dict[str, Any]:
    """Layer the graph-scope presentation fields onto a stats result.

    Stamps the ``session_graph`` scope and attaches per-session sections when
    the graph holds more than one session. Nested per-session composition is an
    optional projection detail. Mutates and returns ``stats_result``.
    """
    if len(session_graph.sessions) == 1:
        stats_result["scope"] = "session"
        stats_result.pop("sessions", None)
        return stats_result

    stats_result["scope"] = "session_graph"
    if len(session_graph.sessions) > 1:
        stats_result["sessions"] = session_stats_sections(
            session_graph,
            build_session_graph_context_stats=build_session_graph_context_stats,
            build_session_graph_stats_token_usage=build_session_graph_stats_token_usage,
            precomputed_usage_by_session=precomputed_usage_by_session,
            precomputed_counter_name=precomputed_counter_name,
            precomputed_metrics_by_session=precomputed_metrics_by_session,
            precomputed_index=precomputed_index,
            include_composition=include_session_composition,
        )
    return stats_result
