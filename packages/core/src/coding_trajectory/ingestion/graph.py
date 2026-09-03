"""Graph assembly for canonical multi-session session_graphs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from coding_trajectory.ingestion.common import (
    normalize_project_key as _normalize_project_key,
)
from coding_trajectory.ingestion.indexes import index_event_owners
from coding_trajectory.ingestion.models import (
    CanonicalSpawnOrigin,
    Event,
    EventType,
    Item,
    Session,
    SessionEdge,
    SessionGraph,
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
    summary = build_session_graph_summary(sessions, root_session_id=root_session_id)
    edges = build_edges(sessions)

    return SessionGraph(
        root_session_id=root_session_id,
        project_identifier=project_identifier,
        summary=summary,
        edges=edges,
        sessions=sessions,
    )


def build_session_graph_summary(
    sessions: list[Session], *, root_session_id: UUID | None = None
) -> SessionGraphSummary:
    started_at = min((session.started_at for session in sessions), default=None)
    ended_at = _max_datetime(session.ended_at for session in sessions)
    if root_session_id is None:
        root_session = _root_session(sessions)
        root_session_id = root_session.session_id if root_session else None
    vendors = sorted(
        {session.vendor for session in sessions}, key=lambda item: item.value
    )

    return SessionGraphSummary(
        root_session_id=root_session_id,
        started_at=started_at,
        ended_at=ended_at,
        session_count=len(sessions),
        turn_count=sum(len(session.turns) for session in sessions),
        vendors=vendors,
    )


def build_edges(sessions: list[Session]) -> list[SessionEdge]:
    edges: list[SessionEdge] = []
    session_map = {session.session_id: session for session in sessions}
    parent_indexes: dict[UUID, _EdgeOriginIndex] = {}

    for session in sessions:
        if session.parent_session_id is None:
            continue
        parent = session_map.get(session.parent_session_id)
        if parent is None:
            continue
        index = parent_indexes.get(parent.session_id)
        if index is None:
            index = _EdgeOriginIndex.from_session(parent)
            parent_indexes[parent.session_id] = index
        origin = (
            None
            if _is_codex_fork(session) and not _is_codex_spawn(session)
            else index.origin_for(session.session_id)
        )
        edge = _build_edge(parent, session, origin)
        edges.append(edge)

    return edges


@dataclass(frozen=True, slots=True)
class _EdgeOrigin:
    event_id: UUID
    turn_id: UUID | None
    item_id: UUID | None
    tool_name: str | None


@dataclass(frozen=True, slots=True)
class _EdgeOriginIndex:
    canonical_origins_by_child_id: dict[str, _EdgeOrigin]
    spawn_call_id_by_child_id: dict[str, str]
    origins_by_call_id: dict[str, _EdgeOrigin]
    latest_tool_origin: _EdgeOrigin | None

    @classmethod
    def from_session(cls, session: Session) -> _EdgeOriginIndex:
        """Index the parent facts shared by all of its child edges once."""
        extensions = session.extensions
        spawn_links = (
            dict(extensions.codex.spawn_links)
            if extensions and extensions.codex
            else {}
        )
        canonical_origins = (
            {
                child_id: _EdgeOrigin(
                    event_id=origin.event_id,
                    turn_id=origin.turn_id,
                    item_id=origin.item_id,
                    tool_name=origin.tool_name,
                )
                for child_id, origin in extensions.codex.canonical_spawn_origins.items()
            }
            if extensions and extensions.codex
            else {}
        )
        spawn_call_ids = set(spawn_links.values())
        first_events_by_call_id: dict[str, Event] = {}
        latest_tool_event: Event | None = None
        for event in session.events:
            if event.type != EventType.TOOL_CALL_REQUESTED:
                continue
            latest_tool_event = event
            call_id = event.payload.get("tool_call_id")
            if (
                isinstance(call_id, str)
                and call_id in spawn_call_ids
                and call_id not in first_events_by_call_id
            ):
                first_events_by_call_id[call_id] = event

        if latest_tool_event is None:
            return cls(
                canonical_origins_by_child_id=canonical_origins,
                spawn_call_id_by_child_id=spawn_links,
                origins_by_call_id={},
                latest_tool_origin=None,
            )

        item_index = index_event_owners(session.turns)
        return cls(
            canonical_origins_by_child_id=canonical_origins,
            spawn_call_id_by_child_id=spawn_links,
            origins_by_call_id={
                call_id: _edge_origin(event, item_index)
                for call_id, event in first_events_by_call_id.items()
            },
            latest_tool_origin=_edge_origin(latest_tool_event, item_index),
        )

    def origin_for(self, child_session_id: UUID) -> _EdgeOrigin | None:
        """Resolve a child's spawn origin, falling back to the latest tool call."""
        canonical = self.canonical_origins_by_child_id.get(str(child_session_id))
        if canonical is not None:
            return canonical
        spawn_call_id = self.spawn_call_id_by_child_id.get(str(child_session_id))
        if spawn_call_id is not None:
            origin = self.origins_by_call_id.get(spawn_call_id)
            if origin is not None:
                return origin

        return self.latest_tool_origin


def canonical_spawn_origins(session: Session) -> dict[str, CanonicalSpawnOrigin]:
    """Resolve private provider call IDs into portable canonical edge origins."""

    index = _EdgeOriginIndex.from_session(session)
    origins: dict[str, CanonicalSpawnOrigin] = {}
    for child_id in index.spawn_call_id_by_child_id:
        origin = index.origin_for(UUID(child_id))
        if origin is not None:
            origins[child_id] = CanonicalSpawnOrigin(
                event_id=origin.event_id,
                turn_id=origin.turn_id,
                item_id=origin.item_id,
                tool_name=origin.tool_name,
            )
    return origins


def _build_edge(
    parent: Session, child: Session, origin: _EdgeOrigin | None
) -> SessionEdge:
    edge_type = (
        "spawned_subagent"
        if _is_codex_spawn(child)
        else "forked_from"
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


def _edge_origin(
    event: Event,
    item_index: dict[UUID, tuple[Turn, Item | None]],
) -> _EdgeOrigin:
    owner = item_index.get(event.event_id)
    turn = owner[0] if owner else None
    item = owner[1] if owner else None
    return _EdgeOrigin(
        event_id=event.event_id,
        turn_id=turn.turn_id if turn else None,
        item_id=item.item_id if item else None,
        tool_name=_event_tool_name(event),
    )


def _event_tool_name(event: Event) -> str | None:
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


def _is_codex_spawn(session: Session) -> bool:
    extensions = session.extensions
    return bool(
        extensions and extensions.codex and extensions.codex.spawn_parent_thread_id
    )


def _max_datetime(values: Any) -> datetime | None:
    filtered = [value for value in values if value is not None]
    return max(filtered) if filtered else None
