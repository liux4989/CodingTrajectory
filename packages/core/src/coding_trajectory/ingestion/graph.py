"""Graph assembly for canonical multi-session trajectories."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from coding_trajectory.ingestion.common import normalize_project_key as _normalize_project_key
from coding_trajectory.ingestion.models import (
    EventType,
    Session,
    Step,
    Trajectory,
    TrajectoryEdge,
    TrajectorySummary,
    Turn,
)
from coding_trajectory.ingestion.vendor_mechanisms.relation_edges import (
    RelationEdgeInput,
    classify_edge_type,
    is_root_session_candidate,
)


def decorate_sessions(sessions: list[Session]) -> list[Session]:
    return sessions


def assemble_project_trajectories(project_identifier: str, sessions: list[Session]) -> list[Trajectory]:
    sessions = decorate_sessions(sessions)
    key = _normalize_project_key(project_identifier)
    components = _compute_connected_components(sessions)

    trajectories: list[Trajectory] = []
    for component_sessions in components:
        root = _root_session(component_sessions)
        trajectory_id = root.session_id if root else min(
            component_sessions, key=lambda s: (s.started_at, str(s.session_id))
        ).session_id
        normalized_sessions = [
            session.model_copy(update={"trajectory_id": trajectory_id})
            for session in sorted(component_sessions, key=lambda item: (item.started_at, str(item.session_id)))
        ]
        trajectories.append(
            build_trajectory(
                trajectory_id=trajectory_id,
                project_identifier=key,
                sessions=normalized_sessions,
            )
        )

    return trajectories


def _compute_connected_components(sessions: list[Session]) -> list[list[Session]]:
    """Group sessions into connected components via union-find on parent_session_id links."""
    session_map = {s.session_id: s for s in sessions}
    parent: dict[UUID, UUID] = {s.session_id: s.session_id for s in sessions}

    def find(x: UUID) -> UUID:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: UUID, b: UUID) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for session in sessions:
        if session.parent_session_id is not None and session.parent_session_id in session_map:
            union(session.session_id, session.parent_session_id)

    groups: dict[UUID, list[Session]] = {}
    for session in sessions:
        root = find(session.session_id)
        groups.setdefault(root, []).append(session)

    return sorted(
        groups.values(),
        key=lambda g: (min(s.started_at for s in g), min(str(s.session_id) for s in g)),
    )


def build_trajectory(
    *,
    trajectory_id: UUID,
    project_identifier: str,
    sessions: list[Session],
) -> Trajectory:
    summary = build_trajectory_summary(sessions)
    edges = build_edges(sessions)

    return Trajectory(
        trajectory_id=trajectory_id,
        project_identifier=project_identifier,
        summary=summary,
        edges=edges,
        sessions=sessions,
    )


def build_trajectory_summary(sessions: list[Session]) -> TrajectorySummary:
    started_at = min((session.started_at for session in sessions), default=None)
    ended_at = _max_datetime(session.ended_at for session in sessions)
    root_session = _root_session(sessions)
    vendors = sorted({session.vendor for session in sessions}, key=lambda item: item.value)

    return TrajectorySummary(
        root_session_id=root_session.session_id if root_session else None,
        started_at=started_at,
        ended_at=ended_at,
        session_count=len(sessions),
        turn_count=sum(len(session.turns) for session in sessions),
        vendors=vendors,
    )


def build_edges(sessions: list[Session]) -> list[TrajectoryEdge]:
    edges: list[TrajectoryEdge] = []
    session_map = {session.session_id: session for session in sessions}

    for session in sessions:
        if session.parent_session_id is None:
            continue
        parent = session_map.get(session.parent_session_id)
        edge = _build_edge(parent, session)
        if edge is None:
            continue
        edges.append(edge)

    return edges


@dataclass(frozen=True)
class _EdgeOrigin:
    event_id: UUID
    turn_id: UUID | None
    step_id: UUID | None
    tool_name: str | None


def _build_edge(parent: Session | None, child: Session) -> TrajectoryEdge | None:
    if parent is None:
        return None

    origin = _find_edge_origin(parent)
    edge_type = (
        "forked_from"
        if _is_codex_fork(child)
        else classify_edge_type(
            RelationEdgeInput(
                child_is_sidechain=_is_sidechain(child),
                child_parent_session_id_present=child.parent_session_id is not None,
                parent_vendor=parent.vendor,
                origin_tool_name=origin.tool_name if origin else None,
            )
        )
    )
    evidence_ids = [origin.event_id] if origin is not None else []
    metadata = {"tool_name": origin.tool_name} if origin and origin.tool_name else None

    return TrajectoryEdge(
        type=edge_type,
        source_session_id=parent.session_id,
        target_session_id=child.session_id,
        source_turn_id=origin.turn_id if origin else None,
        source_step_id=origin.step_id if origin else None,
        source_event_id=origin.event_id if origin else None,
        provenance="observed" if origin else "derived",
        confidence="high" if origin else "medium",
        evidence_event_ids=evidence_ids,
        metadata=metadata,
    )


def _find_edge_origin(session: Session) -> _EdgeOrigin | None:
    tool_events = [
        event
        for event in session.events
        if event.type == EventType.TOOL_CALL_REQUESTED
    ]
    if not tool_events:
        return None

    step_index = _build_step_event_index(session)
    for event in reversed(tool_events):
        turn, step = step_index.get(event.event_id, (None, None))
        return _EdgeOrigin(
            event_id=event.event_id,
            turn_id=turn.turn_id if turn else None,
            step_id=step.step_id if step else None,
            tool_name=_event_tool_name(event),
        )

    return None


def _build_step_event_index(session: Session) -> dict[UUID, tuple[Turn | None, Step | None]]:
    index: dict[UUID, tuple[Turn | None, Step | None]] = {}
    for turn in session.turns:
        for event_id in turn.event_ids:
            index.setdefault(event_id, (turn, None))
        if turn.user_request_event_id is not None:
            index.setdefault(turn.user_request_event_id, (turn, None))
        for step in turn.steps:
            for event_id in step.event_ids:
                index[event_id] = (turn, step)
            for item in step.items:
                for event_id in item.event_ids:
                    index[event_id] = (turn, step)
    return index


def _event_tool_name(event: Any) -> str | None:
    tool_name = event.payload.get("tool_name")
    if isinstance(tool_name, str) and tool_name.strip():
        return tool_name
    return None


def _root_session(sessions: list[Session]) -> Session | None:
    for session in sessions:
        if is_root_session_candidate(
            parent_session_id_present=session.parent_session_id is not None,
            is_sidechain=_is_sidechain(session),
        ):
            return session
    return min(sessions, key=lambda item: (item.started_at, str(item.session_id)), default=None)


def _is_sidechain(session: Session) -> bool:
    extensions = session.extensions
    return bool(extensions and extensions.claude_code and extensions.claude_code.is_sidechain)


def _is_codex_fork(session: Session) -> bool:
    extensions = session.extensions
    return bool(extensions and extensions.codex and extensions.codex.forked_from_id)


def _max_datetime(values: Any) -> datetime | None:
    filtered = [value for value in values if value is not None]
    return max(filtered) if filtered else None
