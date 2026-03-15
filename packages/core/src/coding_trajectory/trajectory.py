"""Trajectory assembly for replay-first multi-session and multi-agent views."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from coding_trajectory.ingestion.decorators import ClaudeCodeDecorator
from coding_trajectory.ingestion.models import (
    Session,
    Trajectory,
    TrajectoryEdge,
    TrajectorySummary,
    Vendor,
)


def decorate_sessions(sessions: list[Session]) -> list[Session]:
    registry = {session.session_id: session for session in sessions}
    decorators = [ClaudeCodeDecorator()]
    updated: list[Session] = []

    for session in sorted(sessions, key=lambda item: (item.started_at, str(item.session_id))):
        enriched = session
        for decorator in decorators:
            enriched = decorator.apply(enriched, registry)
        registry[enriched.session_id] = enriched
        updated.append(enriched)

    return updated


def assemble_project_trajectories(project_identifier: str, sessions: list[Session]) -> list[Trajectory]:
    sessions = decorate_sessions(sessions)
    groups: dict[str, list[Session]] = defaultdict(list)

    for session in sorted(sessions, key=lambda item: (item.started_at, str(item.session_id))):
        groups[_trajectory_group_key(session)].append(session)

    trajectories: list[Trajectory] = []
    for group_key, group_sessions in sorted(groups.items()):
        key = _normalize_project_key(project_identifier)
        trajectory_id = uuid5(NAMESPACE_URL, f"coding-trajectory:{key}:{group_key}")
        normalized_sessions = [
            session.model_copy(update={"trajectory_id": trajectory_id})
            for session in sorted(group_sessions, key=lambda item: (item.started_at, str(item.session_id)))
        ]
        trajectories.append(
            build_trajectory(
                trajectory_id=trajectory_id,
                project_identifier=key,
                sessions=normalized_sessions,
            )
        )

    return trajectories


def build_trajectory(
    *,
    trajectory_id: UUID,
    project_identifier: str,
    sessions: list[Session],
    extra_session_ids: set[UUID] | None = None,
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
        edge_type, evidence_ids = _classify_edge(session, parent)
        edges.append(
            TrajectoryEdge(
                type=edge_type,
                source_session_id=session.session_id,
                target_session_id=session.parent_session_id,
                evidence_event_ids=evidence_ids,
            )
        )

    return edges


def _classify_edge(child: Session, parent: Session | None) -> tuple[str, list]:
    """Return (edge_type, evidence_event_ids) for a child->parent relationship."""
    from coding_trajectory.ingestion.models import EventType
    _SUBAGENT_TOOL_NAMES = {"Agent", "Task", "spawn_agent"}
    if parent is not None:
        for event in parent.events:
            tool_name = event.payload.get("tool_name")
            if event.type == EventType.TOOL_CALL_REQUESTED and tool_name in _SUBAGENT_TOOL_NAMES:
                return "spawned_subagent", [event.event_id]
    return "sidechain_of", []


def _trajectory_group_key(session: Session) -> str:
    parent = session.parent_session_id
    if parent is not None:
        return f"session:{parent}"

    team_name = _team_name(session)
    if session.vendor == Vendor.CLAUDE_CODE and team_name:
        return f"claude-team:{team_name}"

    return f"session:{session.session_id}"


def _normalize_project_key(value: str) -> str:
    import re

    collapsed = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", value.strip())
    return re.sub(r"[^a-zA-Z0-9]+", "-", collapsed).strip("-").lower()


def _root_session(sessions: list[Session]) -> Session | None:
    for session in sessions:
        if session.parent_session_id is None and not _is_sidechain(session):
            return session
    return min(sessions, key=lambda item: (item.started_at, str(item.session_id)), default=None)


def _agent_name(session: Session) -> str | None:
    extensions = session.extensions
    if extensions is None:
        return None
    if extensions.claude_code and extensions.claude_code.agent_name:
        return extensions.claude_code.agent_name
    if extensions.codex and extensions.codex.agent_nickname:
        return extensions.codex.agent_nickname
    return None


def _team_name(session: Session) -> str | None:
    extensions = session.extensions
    if extensions and extensions.claude_code:
        return extensions.claude_code.team_name
    return None


def _is_sidechain(session: Session) -> bool:
    extensions = session.extensions
    return bool(extensions and extensions.claude_code and extensions.claude_code.is_sidechain)


def _max_datetime(values: Any) -> datetime | None:
    filtered = [value for value in values if value is not None]
    return max(filtered) if filtered else None
