"""Graph assembly for canonical multi-session session_graphs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from coding_trajectory.ingestion.common import (
    normalize_project_key as _normalize_project_key,
)
from coding_trajectory.ingestion.models import (
    EventType,
    Item,
    Session,
    SessionGraph,
    SessionEdge,
    SessionGraphSummary,
    Turn,
)
from coding_trajectory.ingestion.vendor_mechanisms.relation_edges import (
    RelationEdgeInput,
    classify_edge_type,
    is_root_session_candidate,
)


def decorate_sessions(sessions: list[Session]) -> list[Session]:
    return sessions


def assemble_project_session_graphs(
    project_identifier: str, sessions: list[Session]
) -> list[SessionGraph]:
    sessions = decorate_sessions(sessions)
    key = _normalize_project_key(project_identifier)
    components = _compute_connected_components(sessions)

    session_graphs: list[SessionGraph] = []
    for component_sessions in components:
        root = _root_session(component_sessions)
        root_session_id = (
            root.session_id
            if root
            else min(
                component_sessions, key=lambda s: (s.started_at, str(s.session_id))
            ).session_id
        )
        normalized_sessions = sorted(
            component_sessions, key=lambda item: (item.started_at, str(item.session_id))
        )
        session_graphs.append(
            build_session_graph(
                root_session_id=root_session_id,
                project_identifier=key,
                sessions=normalized_sessions,
            )
        )

    return session_graphs


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
        if (
            session.parent_session_id is not None
            and session.parent_session_id in session_map
        ):
            union(session.session_id, session.parent_session_id)

    groups: dict[UUID, list[Session]] = {}
    for session in sessions:
        root = find(session.session_id)
        groups.setdefault(root, []).append(session)

    return sorted(
        groups.values(),
        key=lambda g: (min(s.started_at for s in g), min(str(s.session_id) for s in g)),
    )


def build_session_graph(
    *,
    root_session_id: UUID,
    project_identifier: str,
    sessions: list[Session],
) -> SessionGraph:
    summary = build_session_graph_summary(sessions)
    edges = build_edges(sessions)

    return SessionGraph(
        root_session_id=root_session_id,
        project_identifier=project_identifier,
        summary=summary,
        edges=edges,
        sessions=sessions,
    )


def build_session_graph_summary(sessions: list[Session]) -> SessionGraphSummary:
    started_at = min((session.started_at for session in sessions), default=None)
    ended_at = _max_datetime(session.ended_at for session in sessions)
    root_session = _root_session(sessions)
    vendors = sorted(
        {session.vendor for session in sessions}, key=lambda item: item.value
    )

    return SessionGraphSummary(
        root_session_id=root_session.session_id if root_session else None,
        started_at=started_at,
        ended_at=ended_at,
        session_count=len(sessions),
        turn_count=sum(len(session.turns) for session in sessions),
        vendors=vendors,
    )


def build_edges(sessions: list[Session]) -> list[SessionEdge]:
    edges: list[SessionEdge] = []
    session_map = {session.session_id: session for session in sessions}
    item_index_by_parent: dict[UUID, dict[UUID, tuple[Turn | None, Item | None]]] = {}

    for session in sessions:
        if session.parent_session_id is None:
            continue
        parent = session_map.get(session.parent_session_id)
        if parent is None:
            continue
        item_index = item_index_by_parent.get(parent.session_id)
        if item_index is None:
            item_index = _build_item_event_index(parent)
            item_index_by_parent[parent.session_id] = item_index
        edge = _build_edge(parent, session, item_index=item_index)
        if edge is None:
            continue
        edges.append(edge)

    return edges


@dataclass(frozen=True)
class _EdgeOrigin:
    event_id: UUID
    turn_id: UUID | None
    item_id: UUID | None
    tool_name: str | None


def _build_edge(
    parent: Session | None,
    child: Session,
    *,
    item_index: dict[UUID, tuple[Turn | None, Item | None]] | None = None,
) -> SessionEdge | None:
    if parent is None:
        return None

    origin = _find_edge_origin(parent, child.session_id, item_index=item_index)
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

    return SessionEdge(
        type=edge_type,
        source_session_id=parent.session_id,
        target_session_id=child.session_id,
        source_turn_id=origin.turn_id if origin else None,
        source_item_id=origin.item_id if origin else None,
        source_event_id=origin.event_id if origin else None,
        provenance="observed" if origin else "derived",
        confidence="high" if origin else "medium",
        evidence_event_ids=evidence_ids,
        metadata=metadata,
    )


def _find_edge_origin(
    session: Session,
    child_session_id: UUID | None = None,
    *,
    item_index: dict[UUID, tuple[Turn | None, Item | None]] | None = None,
) -> _EdgeOrigin | None:
    # Prefer the real spawn call: codex sub_agent_activity{kind:started} links
    # the child's agent_thread_id to the spawn tool-call call_id, recorded on
    # the parent's codex extensions. Resolve that call to its turn/item so the
    # edge provenance reflects the actual spawn, not the parent's last tool call.
    spawn_call_id = _spawn_call_id_for(session, child_session_id)
    if spawn_call_id is not None:
        origin = _find_tool_call_by_call_id(
            session, spawn_call_id, item_index=item_index
        )
        if origin is not None:
            return origin

    tool_events = [
        event for event in session.events if event.type == EventType.TOOL_CALL_REQUESTED
    ]
    if not tool_events:
        return None

    if item_index is None:
        item_index = _build_item_event_index(session)
    for event in reversed(tool_events):
        turn, item = item_index.get(event.event_id, (None, None))
        return _EdgeOrigin(
            event_id=event.event_id,
            turn_id=turn.turn_id if turn else None,
            item_id=item.item_id if item else None,
            tool_name=_event_tool_name(event),
        )

    return None


def _spawn_call_id_for(session: Session, child_session_id: UUID | None) -> str | None:
    """Return the spawn tool-call call_id recorded for ``child_session_id``."""
    if child_session_id is None:
        return None
    extensions = session.extensions
    if not extensions or not extensions.codex:
        return None
    return extensions.codex.spawn_links.get(str(child_session_id))


def _find_tool_call_by_call_id(
    session: Session,
    call_id: str,
    *,
    item_index: dict[UUID, tuple[Turn | None, Item | None]] | None = None,
) -> _EdgeOrigin | None:
    """Resolve a tool call by its provider call_id to its turn/item origin."""
    if item_index is None:
        item_index = _build_item_event_index(session)
    for event in session.events:
        if event.type != EventType.TOOL_CALL_REQUESTED:
            continue
        if event.payload.get("tool_call_id") != call_id:
            continue
        turn, item = item_index.get(event.event_id, (None, None))
        return _EdgeOrigin(
            event_id=event.event_id,
            turn_id=turn.turn_id if turn else None,
            item_id=item.item_id if item else None,
            tool_name=_event_tool_name(event),
        )
    return None


def _build_item_event_index(
    session: Session,
) -> dict[UUID, tuple[Turn | None, Item | None]]:
    index: dict[UUID, tuple[Turn | None, Item | None]] = {}
    for turn in session.turns:
        for event_id in turn.event_ids:
            index.setdefault(event_id, (turn, None))
        if turn.user_request_event_id is not None:
            index.setdefault(turn.user_request_event_id, (turn, None))
        for item in turn.items:
            for event_id in item.event_ids:
                index[event_id] = (turn, item)
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
    return min(
        sessions, key=lambda item: (item.started_at, str(item.session_id)), default=None
    )


def _is_sidechain(session: Session) -> bool:
    extensions = session.extensions
    return bool(
        extensions and extensions.claude_code and extensions.claude_code.is_sidechain
    )


def _is_codex_fork(session: Session) -> bool:
    extensions = session.extensions
    return bool(extensions and extensions.codex and extensions.codex.forked_from_id)


def _max_datetime(values: Any) -> datetime | None:
    filtered = [value for value in values if value is not None]
    return max(filtered) if filtered else None
