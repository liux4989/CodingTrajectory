"""SessionGraph indexes derived from canonical ingestion models."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from uuid import UUID

from coding_trajectory.ingestion.models import (
    Event,
    Item,
    Session,
    SessionGraph,
    SessionEdge,
    Turn,
)


@dataclass(frozen=True)
class SessionGraphIndex:
    session_ids_in_order: list[UUID]
    sessions_by_id: dict[UUID, Session]
    turns_by_id: dict[UUID, Turn]
    items_by_id: dict[UUID, Item]
    events_by_id: dict[UUID, Event]
    session_by_turn_id: dict[UUID, UUID]
    session_by_item_id: dict[UUID, UUID]
    parent: dict[UUID, UUID | None]
    children: dict[UUID, list[UUID]]
    incoming_edge_by_target: dict[UUID, SessionEdge]
    incoming_edge_type: dict[UUID, str | None]
    outgoing_edges_by_source_item: dict[UUID, list[SessionEdge]]
    roots: list[UUID]


def build_session_graph_index(session_graph: SessionGraph) -> SessionGraphIndex:
    sessions_by_id: dict[UUID, Session] = {}
    turns_by_id: dict[UUID, Turn] = {}
    items_by_id: dict[UUID, Item] = {}
    events_by_id: dict[UUID, Event] = {}
    session_by_turn_id: dict[UUID, UUID] = {}
    session_by_item_id: dict[UUID, UUID] = {}

    for session in session_graph.sessions:
        sessions_by_id[session.session_id] = session
        for turn in session.turns:
            turns_by_id[turn.turn_id] = turn
            session_by_turn_id[turn.turn_id] = session.session_id
            for item in turn.items:
                items_by_id[item.item_id] = item
                session_by_item_id[item.item_id] = session.session_id
        for event in session.events:
            events_by_id[event.event_id] = event

    incoming_edge_by_target: dict[UUID, SessionEdge] = {}
    outgoing_edges_by_source_item: dict[UUID, list[SessionEdge]] = defaultdict(list)
    for edge in session_graph.edges:
        incoming_edge_by_target.setdefault(edge.target_session_id, edge)
        if edge.source_item_id is not None:
            outgoing_edges_by_source_item[edge.source_item_id].append(edge)

    parent: dict[UUID, UUID | None] = {}
    for session in session_graph.sessions:
        parent_id = session.parent_session_id
        if parent_id is None:
            edge = incoming_edge_by_target.get(session.session_id)
            if edge is not None:
                parent_id = edge.source_session_id
        parent[session.session_id] = parent_id

    children: dict[UUID, list[UUID]] = defaultdict(list)
    for session_id, parent_id in parent.items():
        if parent_id is not None:
            children[parent_id].append(session_id)
    for child_ids in children.values():
        child_ids.sort(key=str)

    roots = sorted(
        (session_id for session_id, parent_id in parent.items() if parent_id is None),
        key=str,
    )
    incoming_edge_type = {
        session.session_id: (
            incoming_edge_by_target[session.session_id].type
            if session.session_id in incoming_edge_by_target
            else None
        )
        for session in session_graph.sessions
    }

    return SessionGraphIndex(
        session_ids_in_order=[session.session_id for session in session_graph.sessions],
        sessions_by_id=sessions_by_id,
        turns_by_id=turns_by_id,
        items_by_id=items_by_id,
        events_by_id=events_by_id,
        session_by_turn_id=session_by_turn_id,
        session_by_item_id=session_by_item_id,
        parent=parent,
        children=dict(children),
        incoming_edge_by_target=incoming_edge_by_target,
        incoming_edge_type=incoming_edge_type,
        outgoing_edges_by_source_item=dict(outgoing_edges_by_source_item),
        roots=roots,
    )


def ordered_sessions(index: SessionGraphIndex) -> list[Session]:
    ordered: list[Session] = []
    visited: set[UUID] = set()
    queue: list[UUID] = list(index.roots)

    while queue:
        session_id = queue.pop(0)
        if session_id in visited:
            continue
        visited.add(session_id)
        session = index.sessions_by_id.get(session_id)
        if session is not None:
            ordered.append(session)
        queue.extend(index.children.get(session_id, []))

    for session_id in index.session_ids_in_order:
        if session_id in visited:
            continue
        session = index.sessions_by_id.get(session_id)
        if session is not None:
            ordered.append(session)

    return ordered


def incoming_edge(index: SessionGraphIndex, session_id: UUID) -> SessionEdge | None:
    return index.incoming_edge_by_target.get(session_id)


def events_for_item(index: SessionGraphIndex, item: Item) -> list[Event]:
    return [
        index.events_by_id[event_id]
        for event_id in item.event_ids
        if event_id in index.events_by_id
    ]


def events_for_turn(index: SessionGraphIndex, turn: Turn) -> list[Event]:
    return [
        index.events_by_id[event_id]
        for event_id in turn.event_ids
        if event_id in index.events_by_id
    ]


def event_for_turn_user_request(index: SessionGraphIndex, turn: Turn) -> Event | None:
    if turn.user_request_event_id is None:
        return None
    return index.events_by_id.get(turn.user_request_event_id)


def target_session_id_for_item(
    index: SessionGraphIndex,
    item: Item,
    *,
    edge_type: str,
) -> UUID | None:
    for edge in index.outgoing_edges_by_source_item.get(item.item_id, []):
        if edge.type == edge_type:
            return edge.target_session_id
    return None
