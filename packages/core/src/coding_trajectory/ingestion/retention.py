"""Consumer-neutral retention policies for canonical ingestion.

The immutable vendor logs remain the evidence authority.  Retention controls
which canonical fields stay resident after a source has been normalized; it
does not change parsing, identifiers, hierarchy, or accounting semantics.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from coding_trajectory.ingestion.models import (
    ContextUsageObservation,
    Event,
    EventType,
    Item,
)


CanonicalRetention = Literal["trajectory", "measurements"]


_MEASUREMENT_EVENT_TYPES = frozenset(
    {
        EventType.USER_PROMPT_SUBMITTED,
        EventType.TOOL_CALL_REQUESTED,
        EventType.TOOL_CALL_SUCCEEDED,
        EventType.TOOL_CALL_FAILED,
    }
)
_USER_REQUEST_KEYS = frozenset({"text", "message", "content", "team_request_summary"})
_TOOL_EVENT_KEYS = frozenset(
    {
        "tool_call_id",
        "tool_name",
        "name",
        "status",
        "exit_code",
        "child_session_id",
        "thread_id",
    }
)


def retain_event_for_measurements(event: Event) -> Event | None:
    """Return the small event evidence required by canonical measurements."""

    if event.type not in _MEASUREMENT_EVENT_TYPES:
        return None
    keys = (
        _USER_REQUEST_KEYS
        if event.type == EventType.USER_PROMPT_SUBMITTED
        else _TOOL_EVENT_KEYS
    )
    payload = {key: value for key, value in event.payload.items() if key in keys}
    return event.model_copy(update={"payload": payload})


def retain_item_for_measurements(item: Item) -> Item:
    """Drop transcript bodies while preserving timing and tool identity."""

    update: dict[str, Any] = {"vendor_data": {}}
    for field in ("text", "input", "output", "command"):
        if hasattr(item, field):
            update[field] = None
    return item.model_copy(update=update)


def compact_usage_mapping(value: dict[str, Any]) -> dict[str, Any]:
    """Retain provider accounting fields and discard unrelated request data."""

    keys = {
        "input_tokens",
        "inputTokens",
        "cached_input_tokens",
        "cachedInputTokens",
        "cache_creation_input_tokens",
        "cacheCreationInputTokens",
        "output_tokens",
        "outputTokens",
        "reasoning_output_tokens",
        "reasoningOutputTokens",
        "total_tokens",
        "totalTokens",
        "uncached_input_tokens",
        "uncachedInputTokens",
        "cost_usd",
        "costUsd",
    }
    return {key: value for key, value in value.items() if key in keys}


def compact_context_usage_observation(
    observation: ContextUsageObservation,
    event_ids: dict[UUID, UUID],
) -> ContextUsageObservation:
    """Apply measurements-retention shaping to one usage observation inline.

    Identical to the post-assembly path in ``stabilize_session``: remap the
    source event reference, keep provider accounting fields, and drop the
    cumulative snapshot and composition categories.
    """

    return observation.model_copy(
        update={
            "source_event_id": event_ids.get(
                observation.source_event_id, observation.source_event_id
            ),
            "usage": compact_usage_mapping(observation.usage),
            "cumulative_usage": None,
            "categories": [],
        }
    )


__all__ = [
    "CanonicalRetention",
    "compact_context_usage_observation",
    "compact_usage_mapping",
    "retain_event_for_measurements",
    "retain_item_for_measurements",
]
