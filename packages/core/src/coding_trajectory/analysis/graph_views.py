"""Explicit graph-level projections over connected coding sessions."""

from __future__ import annotations

from typing import Any

from coding_trajectory.analysis.session_graph_views import build_session_graph_narrative
from coding_trajectory.analysis.session_stats import session_title
from coding_trajectory.ingestion.common import format_datetime, prune_nones
from coding_trajectory.ingestion.indexes import build_session_graph_index
from coding_trajectory.ingestion.models import SessionGraph


def _graph_orchestration_summary(session_graph: SessionGraph) -> dict[str, Any]:
    """Summarize observed orchestration facts without inventing workflow state."""
    edge_counts: dict[str, int] = {}
    multi_agent_versions: set[str] = set()
    multi_agent_modes: set[str] = set()
    agent_paths: list[str] = []
    vendors: set[str] = set()
    spawned_agent_count = 0

    for session in session_graph.sessions:
        vendors.add(session.vendor.value)
        codex = session.extensions.codex if session.extensions else None
        if codex is None:
            continue
        spawned_agent_count += len(codex.spawn_links)
        if codex.multi_agent_version:
            multi_agent_versions.add(codex.multi_agent_version)
        if codex.multi_agent_mode:
            multi_agent_modes.add(codex.multi_agent_mode)
        if codex.agent_path:
            agent_paths.append(codex.agent_path)

    for edge in session_graph.edges:
        edge_counts[edge.type] = edge_counts.get(edge.type, 0) + 1

    if "v2" in multi_agent_versions or edge_counts.get("spawned_subagent", 0):
        kind = "multi_agent"
    elif len(session_graph.sessions) > 1:
        kind = "multi_session"
    else:
        kind = "single_session"

    return prune_nones(
        {
            "kind": kind,
            "vendors": sorted(vendors),
            "session_count": len(session_graph.sessions),
            "spawned_agent_count": spawned_agent_count,
            "multi_agent_versions": sorted(multi_agent_versions) or None,
            "multi_agent_modes": sorted(multi_agent_modes) or None,
            "edge_counts": edge_counts or None,
            "agent_paths": sorted(agent_paths) or None,
        }
    )


def build_graph_overview(
    session_graph: SessionGraph,
    *,
    num_turns: int | None = None,
    drop_turns: int | None = None,
    include_narrative: bool = True,
) -> dict[str, Any]:
    """Return the complete tree, including spawned-agent sessions.

    The narrative projection carries turn requests, assistant responses, and
    item references. It can be omitted while retaining graph topology and
    session metadata. Defaults preserve the legacy response shape.
    """
    narrative = (
        build_session_graph_narrative(
            session_graph,
            num_turns=num_turns,
            drop_turns=drop_turns,
        )
        if include_narrative
        else {"sessions": []}
    )
    index = build_session_graph_index(session_graph)
    sessions = []
    narrative_by_id = {
        str(session.get("session_id")): session for session in narrative["sessions"]
    }
    for session in session_graph.sessions:
        node = dict(narrative_by_id.get(str(session.session_id), {}))
        if not include_narrative:
            node.update(
                prune_nones(
                    {
                        "vendor": session.vendor.value,
                        "status": session.status,
                        "agent_name": session.agent_name,
                        "title": session_title(session),
                        "cwd": session.cwd,
                    }
                )
            )
        codex = session.extensions.codex if session.extensions else None
        node.update(
            prune_nones(
                {
                    "session_id": str(session.session_id),
                    "parent_session_id": (
                        str(index.parent[session.session_id])
                        if index.parent.get(session.session_id)
                        else None
                    ),
                    "edge_type": index.incoming_edge_type.get(session.session_id),
                    "started_at": format_datetime(session.started_at),
                    "ended_at": format_datetime(session.ended_at),
                    "multi_agent_version": (
                        codex.multi_agent_version if codex else None
                    ),
                    "multi_agent_mode": codex.multi_agent_mode if codex else None,
                    "agent_path": codex.agent_path if codex else None,
                }
            )
        )
        sessions.append(node)

    return {
        "graph_id": str(session_graph.root_session_id),
        "root_session_id": str(session_graph.root_session_id),
        "project": session_graph.project_identifier,
        "graph": {
            "id": str(session_graph.root_session_id),
            "orchestration": _graph_orchestration_summary(session_graph),
        },
        "summary": (
            session_graph.summary.model_dump(mode="json")
            if session_graph.summary
            else None
        ),
        "sessions": sessions,
        "edges": [edge.model_dump(mode="json") for edge in session_graph.edges],
    }
