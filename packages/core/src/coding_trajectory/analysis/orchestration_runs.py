"""Project unified session lineage into individual orchestration runs."""

from __future__ import annotations

from collections import defaultdict, deque
from uuid import UUID

from coding_trajectory.analysis.session_stats import session_title
from coding_trajectory.ingestion.graph import build_session_graph
from coding_trajectory.ingestion.models import Session, SessionGraph


def orchestration_runs(session_graph: SessionGraph) -> list[SessionGraph]:
    """Split one lineage component at ordinary conversation-fork edges."""
    sessions_by_id = {session.session_id: session for session in session_graph.sessions}
    orchestration_children: dict[UUID, list[UUID]] = defaultdict(list)
    orchestration_targets: set[UUID] = set()
    for edge in session_graph.edges:
        if edge.type == "forked_from":
            continue
        orchestration_children[edge.source_session_id].append(edge.target_session_id)
        orchestration_targets.add(edge.target_session_id)

    roots = [
        session
        for session in session_graph.sessions
        if session.session_id not in orchestration_targets
    ]
    return [
        _build_run(session_graph, root, sessions_by_id, orchestration_children)
        for root in sorted(roots, key=_session_sort_key)
    ]


def orchestration_run_for_entrypoint(
    session_graph: SessionGraph, entrypoint_id: UUID | None
) -> SessionGraph:
    """Return the run containing a session or turn entry point."""
    selected_session_id = _entrypoint_session_id(session_graph, entrypoint_id)
    for run in orchestration_runs(session_graph):
        if any(session.session_id == selected_session_id for session in run.sessions):
            return run
    raise ValueError(
        f"session is not part of an orchestration run: {selected_session_id}"
    )


def build_conversation_tree(session_graph: SessionGraph) -> dict[str, object]:
    """Describe ordinary conversation branches and their owned agent runs."""
    runs = orchestration_runs(session_graph)
    owner_by_session_id = {
        session.session_id: run.root_session_id
        for run in runs
        for session in run.sessions
    }
    fork_by_target = {
        edge.target_session_id: edge
        for edge in session_graph.edges
        if edge.type == "forked_from"
    }
    root_ids = {run.root_session_id for run in runs}
    branches: list[dict[str, object]] = []
    for run in runs:
        root = next(
            session
            for session in run.sessions
            if session.session_id == run.root_session_id
        )
        fork = fork_by_target.get(root.session_id)
        parent_branch_id = (
            owner_by_session_id.get(fork.source_session_id) if fork else None
        )
        branches.append(
            {
                "session_id": str(root.session_id),
                "parent_session_id": (
                    str(parent_branch_id)
                    if parent_branch_id is not None and parent_branch_id in root_ids
                    else None
                ),
                "source_turn_id": (
                    str(fork.source_turn_id)
                    if fork is not None and fork.source_turn_id is not None
                    else None
                ),
                "vendor": root.vendor.value,
                "status": root.status,
                "title": session_title(root),
                "agent_name": root.agent_name,
                "cwd": root.cwd,
                "started_at": root.started_at.isoformat(),
                "turn_count": len(root.turns),
                "graph_session_count": len(run.sessions),
                "spawned_agent_count": sum(
                    edge.type == "spawned_subagent" for edge in run.edges
                ),
            }
        )
    return {
        "root_session_id": str(session_graph.root_session_id),
        "branches": branches,
    }


def _build_run(
    source: SessionGraph,
    root: Session,
    sessions_by_id: dict[UUID, Session],
    children: dict[UUID, list[UUID]],
) -> SessionGraph:
    included: set[UUID] = set()
    queue = deque([root.session_id])
    while queue:
        session_id = queue.popleft()
        if session_id in included:
            continue
        included.add(session_id)
        queue.extend(children.get(session_id, []))
    sessions = sorted(
        (sessions_by_id[session_id] for session_id in included),
        key=_session_sort_key,
    )
    return build_session_graph(
        root_session_id=root.session_id,
        project_identifier=source.project_identifier or "",
        sessions=sessions,
    )


def _entrypoint_session_id(
    session_graph: SessionGraph, entrypoint_id: UUID | None
) -> UUID:
    if entrypoint_id is None:
        return session_graph.root_session_id
    for session in session_graph.sessions:
        if session.session_id == entrypoint_id:
            return session.session_id
        if any(turn.turn_id == entrypoint_id for turn in session.turns):
            return session.session_id
    raise ValueError(f"entry point is not part of the session lineage: {entrypoint_id}")


def _session_sort_key(session: Session) -> tuple[object, str]:
    return session.started_at, str(session.session_id)
