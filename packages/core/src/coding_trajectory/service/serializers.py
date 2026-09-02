"""Serialization helpers and id parsing for the service layer."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from coding_trajectory.analysis.session_stats import (
    session_graph_preview,
    session_graph_title,
)
from coding_trajectory.ingestion.common import format_datetime, prune_nones
from coding_trajectory.ingestion.models import (
    Event,
    EventType,
    Item,
    SessionGraph,
)


def _optional_positive_int(params: dict[str, Any], key: str) -> int | None:
    value = params.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{key} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a positive integer") from exc
    if parsed < 1:
        raise ValueError(f"{key} must be a positive integer")
    return parsed


def _public_output_for_session_graph(_session_graph: SessionGraph, payload: Any) -> Any:
    """Identity seam over canonical session-graph output.

    The recursive public/internal session-id remapping machinery that lived
    here has been removed: it produced an identical payload in practice
    (every session id in canonical output is already a canonical UUID string),
    so the deep-copy walk was pure overhead. Kept as a no-op wrapper so the
    session.* handlers read uniformly; inline at the call sites if it ever
    needs to diverge.
    """
    return payload


def serialize_session_graph_detail(session_graph: SessionGraph) -> dict[str, Any]:
    vendors = sorted(
        {session.vendor.value for session in session_graph.sessions if session.vendor}
    )
    return prune_nones(
        {
            "graph_id": str(session_graph.root_session_id),
            "root_session_id": str(session_graph.root_session_id),
            "title": session_graph_title(session_graph),
            "preview": session_graph_preview(session_graph),
            "vendors": vendors or None,
            "session_ids": [
                str(session.session_id) for session in session_graph.sessions
            ],
        }
    )


def serialize_event_detail(
    event: Event,
    *,
    related_item: Item | None = None,
) -> dict[str, Any]:
    return prune_nones(
        {
            "event_id": str(event.event_id),
            "session_id": str(event.session_id),
            "timestamp": format_datetime(event.timestamp),
            "type": event.type.value,
            "tool_call": serialize_tool_call_detail(
                event,
                related_item=related_item,
            ),
            "llm": serialize_llm_detail(event),
            "usage": serialize_usage_detail(event),
            "text": serialize_text_detail(event),
        }
    )


def serialize_tool_call_detail(
    event: Event,
    *,
    related_item: Item | None = None,
) -> dict[str, Any] | None:
    if event.type not in {
        EventType.TOOL_CALL_REQUESTED,
        EventType.TOOL_CALL_SUCCEEDED,
        EventType.TOOL_CALL_FAILED,
    }:
        return None

    payload = event.payload
    status_by_type = {
        EventType.TOOL_CALL_REQUESTED: "in_progress",
        EventType.TOOL_CALL_SUCCEEDED: "done",
        EventType.TOOL_CALL_FAILED: "failed",
    }
    item_result = (
        getattr(related_item, "output", None)
        if related_item is not None
        and event.type
        in {
            EventType.TOOL_CALL_SUCCEEDED,
            EventType.TOOL_CALL_FAILED,
        }
        else None
    )
    result = next(
        (
            value
            for value in (
                payload.get("result"),
                payload.get("tool_output"),
                payload.get("tool_text"),
                item_result,
            )
            if value is not None
        ),
        None,
    )
    return (
        prune_nones(
            {
                "tool_call_id": payload.get("tool_call_id"),
                "tool_name": payload.get("tool_name"),
                "input": payload.get("tool_args") or payload.get("input"),
                "result": result,
                "status": status_by_type.get(event.type),
            }
        )
        or None
    )


def serialize_usage_detail(event: Event) -> dict[str, Any] | None:
    if (
        event.type != EventType.VENDOR_RAW
        or event.payload.get("transcript_kind") != "usage"
    ):
        return None
    metrics = event.payload.get("metrics")
    usage = (
        metrics.get("usage") or metrics.get("last_token_usage")
        if isinstance(metrics, dict)
        else None
    )
    if not isinstance(usage, dict):
        return None
    return prune_nones(
        {
            "provider": (
                metrics.get("provider") if isinstance(metrics, dict) else None
            ),
            "model": metrics.get("model") if isinstance(metrics, dict) else None,
            **usage,
        }
    )


def serialize_llm_detail(event: Event) -> dict[str, Any] | None:
    if event.type != EventType.LLM_RESPONSE:
        return None

    usage = (
        event.payload.get("usage")
        if isinstance(event.payload.get("usage"), dict)
        else {}
    )
    return (
        prune_nones(
            {
                "model": event.payload.get("model")
                or event.payload.get("model_version"),
                "prompt_tokens": usage.get("prompt_tokens")
                or usage.get("input_tokens"),
                "completion_tokens": usage.get("completion_tokens")
                or usage.get("output_tokens"),
                "reported_total_tokens": usage.get("reported_total_tokens")
                or usage.get("total_tokens"),
                "stop_reason": event.payload.get("stop_reason"),
            }
        )
        or None
    )


def serialize_text_detail(event: Event) -> dict[str, Any] | None:
    text = event.payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return {"text": text.strip()}


def _parse_user_id(raw_id: str) -> UUID:
    try:
        return UUID(raw_id)
    except ValueError as exc:
        raise ValueError(f"invalid id: {raw_id!r} is not a valid UUID") from exc


def _normalize_user_id(raw_id: str) -> str:
    return str(_parse_user_id(raw_id))
