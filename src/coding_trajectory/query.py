"""Query helpers for canonical trajectory JSON documents."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from coding_trajectory.ingestion.models import Event, Session, Trajectory, Turn


class QueryError(Exception):
    """Base class for CLI query failures."""


class DocumentError(QueryError):
    """Raised when the input document cannot be parsed."""


class ResourceNotFoundError(QueryError):
    """Raised when a requested resource is not present."""


@dataclass(slots=True)
class DocumentStore:
    """Indexes canonical resources loaded from JSON."""

    trajectories: dict[UUID, Trajectory]
    sessions: dict[UUID, Session]
    turns: dict[UUID, Turn]
    events: dict[UUID, Event]

    @classmethod
    def from_path(cls, path: str | Path) -> "DocumentStore":
        source = Path(path)

        try:
            raw = json.loads(source.read_text())
        except FileNotFoundError as exc:
            raise DocumentError(f"input file not found: {source}") from exc
        except json.JSONDecodeError as exc:
            raise DocumentError(f"invalid JSON in {source}: {exc}") from exc

        return cls.from_data(raw)

    @classmethod
    def from_trajectories(cls, trajectories_list: list[Trajectory]) -> "DocumentStore":
        trajectories: dict[UUID, Trajectory] = {}
        sessions: dict[UUID, Session] = {}
        turns: dict[UUID, Turn] = {}
        events: dict[UUID, Event] = {}

        for trajectory in trajectories_list:
            trajectories[trajectory.trajectory_id] = trajectory
            for session in trajectory.sessions:
                sessions[session.session_id] = session
                for turn in session.turns:
                    turns[turn.turn_id] = turn
                for event in session.events:
                    events[event.event_id] = event

        return cls(trajectories=trajectories, sessions=sessions, turns=turns, events=events)

    @classmethod
    def from_data(cls, raw: object) -> "DocumentStore":
        if isinstance(raw, dict) and "result" in raw and isinstance(raw["result"], dict):
            raw = raw["result"]

        trajectories: dict[UUID, Trajectory] = {}
        sessions: dict[UUID, Session] = {}
        turns: dict[UUID, Turn] = {}
        events: dict[UUID, Event] = {}

        def add_trajectory(trajectory: Trajectory) -> None:
            trajectories[trajectory.trajectory_id] = trajectory
            for session in trajectory.sessions:
                add_session(session, trajectory)

        def add_session(session: Session, trajectory: Trajectory | None = None) -> None:
            if trajectory is not None and session.trajectory_id != trajectory.trajectory_id:
                session = session.model_copy(update={"trajectory_id": trajectory.trajectory_id})

            sessions[session.session_id] = session
            for turn in session.turns:
                turns[turn.turn_id] = turn
            for event in session.events:
                events[event.event_id] = event

        try:
            if isinstance(raw, list):
                for item in raw:
                    add_trajectory(Trajectory.model_validate(item))
            elif isinstance(raw, dict):
                if "trajectories" in raw:
                    for item in raw.get("trajectories", []):
                        add_trajectory(Trajectory.model_validate(item))
                    for item in raw.get("sessions", []):
                        add_session(Session.model_validate(item))
                    for item in raw.get("turns", []):
                        turn = Turn.model_validate(item)
                        turns[turn.turn_id] = turn
                    for item in raw.get("events", []):
                        event = Event.model_validate(item)
                        events[event.event_id] = event
                elif "trajectory_id" in raw:
                    add_trajectory(Trajectory.model_validate(raw))
                elif "session_id" in raw and "timeline" in raw:
                    add_session(Session.model_validate(raw))
                elif "turn_id" in raw and "event_ids" in raw:
                    turn = Turn.model_validate(raw)
                    turns[turn.turn_id] = turn
                elif "event_id" in raw and "timestamp" in raw:
                    event = Event.model_validate(raw)
                    events[event.event_id] = event
                else:
                    raise DocumentError(
                        "unsupported document shape; expected a trajectory, resource bundle, or JSON-RPC result"
                    )
            else:
                raise DocumentError("unsupported document shape; expected an object or array")
        except ValidationError as exc:
            raise DocumentError(f"input does not match canonical models: {exc}") from exc

        return cls(
            trajectories=trajectories,
            sessions=sessions,
            turns=turns,
            events=events,
        )

    def get_trajectory(self, resource_id: UUID) -> Trajectory:
        try:
            return self.trajectories[resource_id]
        except KeyError as exc:
            raise ResourceNotFoundError(f"trajectory not found: {resource_id}") from exc

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
