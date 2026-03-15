"""Service layer implementing the session-api.json contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from coding_trajectory.discovery import normalize_project_key
from coding_trajectory.ingestion.models import Event, Session, Step, Trajectory, Turn
from coding_trajectory.query import DocumentStore


def format_datetime(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def prune_nones(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def serialize_trajectory_detail(trajectory: Trajectory) -> dict[str, Any]:
    return prune_nones(
        {
            "trajectory_id": str(trajectory.trajectory_id),
            "project_identifier": trajectory.project_identifier,
            "summary": trajectory.summary.model_dump(mode="json") if trajectory.summary else None,
            "session_ids": [str(session.session_id) for session in trajectory.sessions],
            "edges": [item.model_dump(mode="json") for item in trajectory.edges],
        }
    )


def serialize_session_detail(session: Session) -> dict[str, Any]:
    return prune_nones(
        {
            "session_id": str(session.session_id),
            "trajectory_id": str(session.trajectory_id),
            "parent_session_id": str(session.parent_session_id) if session.parent_session_id else None,
            "vendor": session.vendor.value,
            "started_at": format_datetime(session.started_at),
            "ended_at": format_datetime(session.ended_at),
            "turn_ids": [str(turn.turn_id) for turn in session.turns],
            "extensions": session.extensions.model_dump(mode="json") if session.extensions else None,
        }
    )


def serialize_turn_detail(turn: Turn) -> dict[str, Any]:
    return prune_nones(
        {
            "turn_id": str(turn.turn_id),
            "session_id": str(turn.session_id),
            "sequence": turn.sequence,
            "started_at": format_datetime(turn.started_at),
            "ended_at": format_datetime(turn.ended_at),
            "user_request_event_id": str(turn.user_request_event_id) if turn.user_request_event_id else None,
            "step_ids": [str(step.step_id) for step in turn.steps],
        }
    )


def serialize_step_detail(step: Step) -> dict[str, Any]:
    return prune_nones(
        {
            "step_id": str(step.step_id),
            "session_id": str(step.session_id),
            "turn_id": str(step.turn_id),
            "sequence": step.sequence,
            "timestamp": format_datetime(step.timestamp),
            "vendor": step.vendor.value,
            "text": step.text,
            "vendor_data": step.vendor_data or None,
            "event_ids": [str(eid) for eid in step.event_ids],
        }
    )


def serialize_event_detail(event: Event) -> dict[str, Any]:
    return prune_nones(
        {
            "event_id": str(event.event_id),
            "session_id": str(event.session_id),
            "timestamp": format_datetime(event.timestamp),
            "type": event.type.value,
            "vendor_source": event.vendor_source.value,
            "actor": event.actor,
            "payload": event.payload,
        }
    )


def resolve_resource(store: DocumentStore, resource: str, raw_id: str) -> Trajectory | Session | Turn | Event | Step:
    resource_id = UUID(raw_id)

    if resource == "trajectory":
        return store.get_trajectory(resource_id)
    if resource == "session":
        return store.get_session(resource_id)
    if resource == "turn":
        return store.get_turn(resource_id)
    if resource == "event":
        return store.get_event(resource_id)
    if resource == "step":
        return store.get_step(resource_id)

    raise ValueError(f"unsupported resource: {resource}")


def resolve_collection(
    store: DocumentStore,
    resource: str,
    *,
    global_scope: bool = False,
    trajectory_id: str | None = None,
    current_dir: Path | None = None,
) -> list[Trajectory | Session]:
    if resource == "trajectory":
        trajectories = list(store.trajectories.values())
        if not global_scope and current_dir is not None:
            current_project = normalize_project_key(current_dir.name)
            trajectories = [
                item
                for item in trajectories
                if item.project_identifier and normalize_project_key(item.project_identifier) == current_project
            ]
        return sorted(trajectories, key=lambda item: (item.project_identifier or "", str(item.trajectory_id)))

    if resource == "session":
        sessions = list(store.sessions.values())
        if trajectory_id:
            tid = UUID(trajectory_id)
            sessions = [item for item in sessions if item.trajectory_id == tid]
        return sorted(sessions, key=lambda item: (item.started_at, str(item.session_id)))

    raise ValueError(f"unsupported resource: {resource}")
