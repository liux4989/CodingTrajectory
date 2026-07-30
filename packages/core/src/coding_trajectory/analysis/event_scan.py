"""Event-scan projection helpers."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from coding_trajectory.analysis.projection_utils import (
    match_filter,
    truncate_payload_strings,
)
from coding_trajectory.ingestion.common import prune_nones
from coding_trajectory.ingestion.models import EventType, SessionGraph


def build_event_scan(
    session_graph: SessionGraph,
    *,
    event_type: str,
    filters: list[str] | None = None,
    event_ids: set[UUID] | None = None,
) -> dict[str, Any]:
    valid_types = {event.value for event in EventType}
    usage_alias = event_type == "usage"
    if event_type not in valid_types and not usage_alias:
        valid = ", ".join(sorted(valid_types))
        raise ValueError(
            f"unknown event type {event_type!r}. Valid types: usage, {valid}"
        )

    matches: list[dict[str, Any]] = []
    for session in session_graph.sessions:
        for event in session.events:
            if event_ids is not None and event.event_id not in event_ids:
                continue
            if usage_alias:
                if (
                    event.type != EventType.VENDOR_RAW
                    or event.payload.get("transcript_kind") != "usage"
                ):
                    continue
            elif event.type.value != event_type:
                continue
            payload = event.payload
            if filters and not all(match_filter(payload, expr) for expr in filters):
                continue
            matches.append(
                prune_nones(
                    {
                        "event_id": str(event.event_id),
                        "session_id": str(event.session_id),
                        "timestamp": (
                            event.timestamp.isoformat().replace("+00:00", "Z")
                            if event.timestamp
                            else None
                        ),
                        "type": event.type.value,
                        "payload": truncate_payload_strings(payload) or None,
                    }
                )
            )

    return {
        "root_session_id": str(session_graph.root_session_id),
        "type": event_type,
        "matches": matches,
    }
