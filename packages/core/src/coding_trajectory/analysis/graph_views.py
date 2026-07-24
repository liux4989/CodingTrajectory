"""Explicit graph-level projections over connected coding sessions."""

from __future__ import annotations

from typing import Any

from coding_trajectory.analysis.session_graph_views import build_session_graph_narrative
from coding_trajectory.ingestion.common import format_datetime, prune_nones
from coding_trajectory.ingestion.indexes import build_session_graph_index
from coding_trajectory.ingestion.models import SessionGraph


def build_graph_overview(
    session_graph: SessionGraph,
    *,
    num_turns: int | None = None,
    drop_turns: int | None = None,
) -> dict[str, Any]:
    """Return the complete tree, including spawned-agent sessions."""
    narrative = build_session_graph_narrative(
        session_graph,
        num_turns=num_turns,
        drop_turns=drop_turns,
    )
    index = build_session_graph_index(session_graph)
    sessions = []
    narrative_by_id = {
        str(session.get("session_id")): session for session in narrative["sessions"]
    }
    for session in session_graph.sessions:
        node = dict(narrative_by_id.get(str(session.session_id), {}))
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
        "root_session_id": str(session_graph.root_session_id),
        "project": session_graph.project_identifier,
        "summary": (
            session_graph.summary.model_dump(mode="json")
            if session_graph.summary
            else None
        ),
        "sessions": sessions,
        "edges": [edge.model_dump(mode="json") for edge in session_graph.edges],
    }
