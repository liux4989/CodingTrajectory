"""Service layer implementing the session-api.json contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from coding_trajectory.discovery import normalize_project_key
from coding_trajectory.ingestion.models import Event, Session, Trajectory, Turn
from coding_trajectory.query import DocumentStore


def format_datetime(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def prune_nones(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def serialize_timeline_item(item: Any) -> dict[str, Any]:
    return {
        "kind": item.kind,
        "id": str(item.id),
    }


def serialize_trajectory_detail(trajectory: Trajectory) -> dict[str, Any]:
    return prune_nones(
        {
            "trajectory_id": str(trajectory.trajectory_id),
            "project_identifier": trajectory.project_identifier,
            "task_reference": trajectory.task_reference,
            "multi_agent_mode": trajectory.multi_agent_mode,
            "summary": trajectory.summary.model_dump(mode="json") if trajectory.summary else None,
            "session_ids": [str(session.session_id) for session in trajectory.sessions],
            "session_refs": [item.model_dump(mode="json") for item in trajectory.session_refs],
            "edges": [item.model_dump(mode="json") for item in trajectory.edges],
            "operations": [item.model_dump(mode="json") for item in trajectory.operations],
            "sections": [item.model_dump(mode="json") for item in trajectory.sections],
            "inference_notes": [item.model_dump(mode="json") for item in trajectory.inference_notes],
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
            "timeline": [serialize_timeline_item(item) for item in session.timeline],
            "extensions": session.extensions.model_dump(mode="json") if session.extensions else None,
        }
    )


def serialize_turn_detail(turn: Turn) -> dict[str, Any]:
    return prune_nones(
        {
            "turn_id": str(turn.turn_id),
            "session_id": str(turn.session_id),
            "user_request": turn.user_request,
            "started_at": format_datetime(turn.started_at),
            "ended_at": format_datetime(turn.ended_at),
            "event_ids": [str(event_id) for event_id in turn.event_ids],
        }
    )


def serialize_event_detail(event: Event) -> dict[str, Any]:
    return prune_nones(
        {
            "event_id": str(event.event_id),
            "session_id": str(event.session_id),
            "turn_id": str(event.turn_id) if event.turn_id else None,
            "timestamp": format_datetime(event.timestamp),
            "type": event.type.value,
            "vendor_source": event.vendor_source.value,
            "actor": event.actor,
            "provenance": event.provenance.value,
            "confidence": event.confidence.value,
            # Consumer-layer tree linkage
            "category": event.category.value if event.category else None,
            "event_group_id": str(event.event_group_id) if event.event_group_id else None,
            "parent_event_id": str(event.parent_event_id) if event.parent_event_id else None,
            # Consumer-layer typed detail
            "tool_call": event.tool_call.model_dump(mode="json", exclude_none=True) if event.tool_call else None,
            "llm": event.llm.model_dump(mode="json", exclude_none=True) if event.llm else None,
            "text": event.text.model_dump(mode="json", exclude_none=True) if event.text else None,
            # Raw payload — always present
            "payload": event.payload,
        }
    )


def resolve_resource(store: DocumentStore, resource: str, raw_id: str) -> Trajectory | Session | Turn | Event:
    resource_id = UUID(raw_id)

    if resource == "trajectory":
        return store.get_trajectory(resource_id)
    if resource == "session":
        return store.get_session(resource_id)
    if resource == "turn":
        return store.get_turn(resource_id)
    if resource == "event":
        return store.get_event(resource_id)

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
