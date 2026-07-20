"""Query helpers for canonical session_graph JSON documents."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from pydantic import TypeAdapter, ValidationError

from coding_trajectory.ingestion.models import Event, Item, Session, SessionGraph, Turn


class QueryError(Exception):
    """Base class for CLI query failures."""


class DocumentError(QueryError):
    """Raised when the input document cannot be parsed."""


class ResourceNotFoundError(QueryError):
    """Raised when a requested resource is not present."""


@dataclass(slots=True)
class DocumentStore:
    """Indexes canonical resources loaded from JSON."""

    session_graphs: dict[UUID, SessionGraph]
    session_to_root: dict[UUID, UUID]
    sessions: dict[UUID, Session]
    turns: dict[UUID, Turn]
    events: dict[UUID, Event]
    items: dict[UUID, Item] = field(default_factory=dict)

    @classmethod
    def from_path(cls, path: str | Path) -> "DocumentStore":
        source = Path(path)

        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise DocumentError(f"input file not found: {source}") from exc
        except json.JSONDecodeError as exc:
            raise DocumentError(f"invalid JSON in {source}: {exc}") from exc

        return cls.from_data(raw)

    @classmethod
    def from_session_graphs(
        cls, session_graphs_list: list[SessionGraph]
    ) -> "DocumentStore":
        session_graphs: dict[UUID, SessionGraph] = {}
        session_to_root: dict[UUID, UUID] = {}
        sessions: dict[UUID, Session] = {}
        turns: dict[UUID, Turn] = {}
        events: dict[UUID, Event] = {}
        items: dict[UUID, Item] = {}

        for session_graph in session_graphs_list:
            session_graphs[session_graph.root_session_id] = session_graph
            for session in session_graph.sessions:
                session_to_root[session.session_id] = session_graph.root_session_id
                sessions[session.session_id] = session
                for turn in session.turns:
                    turns[turn.turn_id] = turn
                    for item in turn.items:
                        items[item.item_id] = item
                for event in session.events:
                    events[event.event_id] = event

        return cls(
            session_graphs=session_graphs,
            session_to_root=session_to_root,
            sessions=sessions,
            turns=turns,
            events=events,
            items=items,
        )

    @classmethod
    def from_data(cls, raw: object) -> "DocumentStore":
        if (
            isinstance(raw, dict)
            and "result" in raw
            and isinstance(raw["result"], dict)
        ):
            raw = raw["result"]

        session_graphs: dict[UUID, SessionGraph] = {}
        session_to_root: dict[UUID, UUID] = {}
        sessions: dict[UUID, Session] = {}
        turns: dict[UUID, Turn] = {}
        events: dict[UUID, Event] = {}
        items: dict[UUID, Item] = {}

        def add_session_graph(session_graph: SessionGraph) -> None:
            session_graphs[session_graph.root_session_id] = session_graph
            for session in session_graph.sessions:
                add_session(session, session_graph)

        def add_session(
            session: Session, session_graph: SessionGraph | None = None
        ) -> None:
            if session_graph is not None:
                session_to_root[session.session_id] = session_graph.root_session_id
            sessions[session.session_id] = session
            for turn in session.turns:
                turns[turn.turn_id] = turn
                for item in turn.items:
                    items[item.item_id] = item
            for event in session.events:
                events[event.event_id] = event

        try:
            if isinstance(raw, list):
                for item in raw:
                    add_session_graph(SessionGraph.model_validate(item))
            elif isinstance(raw, dict):
                if "session_graphs" in raw:
                    for item in raw.get("session_graphs", []):
                        add_session_graph(SessionGraph.model_validate(item))
                    for item in raw.get("sessions", []):
                        add_session(Session.model_validate(item))
                    for item in raw.get("turns", []):
                        turn = Turn.model_validate(item)
                        turns[turn.turn_id] = turn
                    for item in raw.get("events", []):
                        event = Event.model_validate(item)
                        events[event.event_id] = event
                    for item in raw.get("items", []):
                        parsed_item = _parse_item(item)
                        items[parsed_item.item_id] = parsed_item
                elif "root_session_id" in raw:
                    add_session_graph(SessionGraph.model_validate(raw))
                elif "session_id" in raw and "turns" in raw:
                    add_session(Session.model_validate(raw))
                elif "turn_id" in raw and "items" in raw:
                    turn = Turn.model_validate(raw)
                    turns[turn.turn_id] = turn
                elif "event_id" in raw and "timestamp" in raw:
                    event = Event.model_validate(raw)
                    events[event.event_id] = event
                else:
                    raise DocumentError(
                        "unsupported document shape; expected a session_graph, resource bundle, or JSON-RPC result"
                    )
            else:
                raise DocumentError(
                    "unsupported document shape; expected an object or array"
                )
        except ValidationError as exc:
            raise DocumentError(
                f"input does not match canonical models: {exc}"
            ) from exc

        return cls(
            session_graphs=session_graphs,
            session_to_root=session_to_root,
            sessions=sessions,
            turns=turns,
            events=events,
            items=items,
        )

    def get_session_graph(self, resource_id: UUID) -> SessionGraph:
        try:
            return self.session_graphs[resource_id]
        except KeyError as exc:
            raise ResourceNotFoundError(
                f"session_graph not found: {resource_id}"
            ) from exc

    def get_session_graph_for_session(self, session_id: UUID) -> SessionGraph:
        try:
            root_session_id = self.session_to_root[session_id]
        except KeyError as exc:
            raise ResourceNotFoundError(
                f"session graph not found for session: {session_id}"
            ) from exc
        return self.get_session_graph(root_session_id)

    def get_session_graph_for_turn(self, turn_id: UUID) -> SessionGraph:
        turn = self.get_turn(turn_id)
        return self.get_session_graph_for_session(turn.session_id)

    def get_session(self, resource_id: UUID) -> Session:
        try:
            return self.sessions[resource_id]
        except KeyError as exc:
            raise ResourceNotFoundError(f"session not found: {resource_id}") from exc

    def get_turn(self, resource_id: UUID) -> Turn:
        try:
            return self.turns[resource_id]
        except KeyError as exc:
            raise ResourceNotFoundError(f"turn not found: {resource_id}") from exc

    def get_event(self, resource_id: UUID) -> Event:
        try:
            return self.events[resource_id]
        except KeyError as exc:
            raise ResourceNotFoundError(f"event not found: {resource_id}") from exc

    def get_item(self, resource_id: UUID) -> Item:
        try:
            return self.items[resource_id]
        except KeyError as exc:
            raise ResourceNotFoundError(f"item not found: {resource_id}") from exc


def _parse_item(raw: object) -> Item:
    return TypeAdapter(Item).validate_python(raw)
