"""Service layer implementing the session-api.json contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from coding_trajectory.discovery import normalize_project_key
from coding_trajectory.ingestion.models import Event, EventType, Session, Step, StepItem, Trajectory, Turn
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
            "session_ids": [str(session.session_id) for session in trajectory.sessions],
        }
    )


def serialize_session_detail(session: Session) -> dict[str, Any]:
    return prune_nones(
        {
            "session_id": str(session.session_id),
            "trajectory_id": str(session.trajectory_id),
            "turn_ids": [str(turn.turn_id) for turn in session.turns],
            "event_ids": [str(event.event_id) for event in session.events],
        }
    )


def serialize_turn_detail(turn: Turn) -> dict[str, Any]:
    return prune_nones(
        {
            "turn_id": str(turn.turn_id),
            "session_id": str(turn.session_id),
            "event_ids": [str(event_id) for event_id in turn.event_ids],
            "step_ids": [str(step.step_id) for step in turn.steps],
        }
    )


def serialize_step_detail(step: Step) -> dict[str, Any]:
    return prune_nones(
        {
            "step_id": str(step.step_id),
            "session_id": str(step.session_id),
            "turn_id": str(step.turn_id),
            "items": [serialize_step_item(item) for item in step.items],
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
            "tool_call": serialize_tool_call_detail(event),
            "llm": serialize_llm_detail(event),
            "text": serialize_text_detail(event),
        }
    )


def serialize_step_item(item: StepItem) -> dict[str, Any]:
    return prune_nones(item.model_dump(mode="json"))


def serialize_tool_call_detail(event: Event) -> dict[str, Any] | None:
    if event.type not in {EventType.TOOL_CALL_REQUESTED, EventType.TOOL_CALL_SUCCEEDED, EventType.TOOL_CALL_FAILED}:
        return None

    payload = event.payload
    status_by_type = {
        EventType.TOOL_CALL_REQUESTED: "in_progress",
        EventType.TOOL_CALL_SUCCEEDED: "done",
        EventType.TOOL_CALL_FAILED: "failed",
    }
    return prune_nones(
        {
            "tool_call_id": payload.get("tool_call_id"),
            "tool_name": payload.get("tool_name"),
            "input": payload.get("tool_args") or payload.get("input"),
            "result": payload.get("result") or payload.get("tool_output") or payload.get("tool_text"),
            "status": status_by_type.get(event.type),
        }
    ) or None


def serialize_llm_detail(event: Event) -> dict[str, Any] | None:
    if event.type != EventType.LLM_RESPONSE:
        return None

    usage = event.payload.get("usage") if isinstance(event.payload.get("usage"), dict) else {}
    return prune_nones(
        {
            "model": event.payload.get("model") or event.payload.get("model_version"),
            "input_tokens": usage.get("input_tokens") or usage.get("prompt_tokens"),
            "output_tokens": usage.get("output_tokens") or usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "stop_reason": event.payload.get("stop_reason"),
        }
    ) or None


def serialize_text_detail(event: Event) -> dict[str, Any] | None:
    text = event.payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return {"text": text.strip()}


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
