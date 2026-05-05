"""Event-scan projection helpers."""

from __future__ import annotations

from typing import Any

from coding_trajectory.analysis.projection_utils import match_filter, truncate_payload_strings
from coding_trajectory.ingestion.common import prune_nones
from coding_trajectory.ingestion.models import EventType, Trajectory


def build_event_scan(
    trajectory: Trajectory,
    *,
    event_type: str,
    filters: list[str] | None = None,
) -> dict[str, Any]:
    valid_types = {event.value for event in EventType}
    if event_type not in valid_types:
        valid = ", ".join(sorted(valid_types))
        raise ValueError(f"unknown event type {event_type!r}. Valid types: {valid}")

    matches: list[dict[str, Any]] = []
    for session in trajectory.sessions:
        for event in session.events:
            if event.type.value != event_type:
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
        "trajectory_id": str(trajectory.trajectory_id),
        "type": event_type,
        "matches": matches,
    }
