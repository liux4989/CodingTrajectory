"""Collab agent tool-call and spawn-link handling, split out of ``codex.py``."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from coding_trajectory.ingestion.adapters.codex import (
    CodexAdapter,
    _as_non_empty_str,
    _tool_status,
)
from coding_trajectory.ingestion.adapters.codex_native_items import (
    native_item_timing,
    record_native_activity,
)
from coding_trajectory.ingestion.models import ToolStatus
from coding_trajectory.ingestion.transcript import TranscriptRecord

# Moved signatures keep their original ``_ParseState`` spelling.
_ParseState = CodexAdapter._ParseState

_CODEX_STATIC_COLLAB_NATIVE_TOOLS: dict[str, frozenset[str]] = {
    "followup_task": frozenset({"resume_agent", "send_input"}),
    "interrupt_agent": frozenset({"close_agent"}),
    "send_message": frozenset({"send_input"}),
    "spawn_agent": frozenset({"spawn_agent"}),
    "wait_agent": frozenset({"wait"}),
}

def handle_native_collab_agent_tool_call(
    payload: dict,
    timestamp: datetime,
    state: _ParseState,
    transcript: list[TranscriptRecord],
    *,
    completed: bool,
) -> None:
    item = payload.get("item")
    if not isinstance(item, dict) or item.get("type") != "CollabAgentToolCall":
        return
    native_id = _as_non_empty_str(item.get("id"))
    action = _as_non_empty_str(item.get("tool"))
    if native_id is None or action is None:
        return
    input_data: dict[str, Any] = {"action": action}
    for key in (
        "sender_thread_id",
        "receiver_thread_ids",
        "receiver_agents",
        "agents_states",
        "prompt",
        "model",
        "reasoning_effort",
    ):
        if item.get(key) is not None:
            input_data[key] = item[key]
    status = _tool_status(
        item.get("status"),
        default=ToolStatus.COMPLETED if completed else ToolStatus.IN_PROGRESS,
    )
    started_at, completed_at, timing = native_item_timing(payload, timestamp)
    record_native_activity(
        state=state,
        transcript=transcript,
        timestamp=timestamp,
        native_type="CollabAgentToolCall",
        native_id=native_id,
        tool_name="spawn_agent" if action == "spawn_agent" else "collab_agent",
        item_kind="tool_call",
        input_data=input_data,
        status=status,
        completed=completed,
        predicate=lambda invocation: (
            action
            in _CODEX_STATIC_COLLAB_NATIVE_TOOLS.get(invocation.method, frozenset())
        ),
        turn_id=_as_non_empty_str(payload.get("turn_id")),
        started_at=started_at,
        completed_at=completed_at,
        provenance={"native_item_kind": "CollabAgentToolCall", **timing},
    )


def record_spawn_link(state: _ParseState, payload: dict[str, Any]) -> None:
    """Record child agent_thread_id -> spawn call_id from sub_agent_activity.

    kind=started carries the spawned child's agent_thread_id (== child
    session id) and event_id (== spawn tool-call call_id). The link lets the
    forked_from edge origin resolve to the real spawn call instead of the
    parent's last tool call.
    """
    if payload.get("kind") != "started":
        return
    child_id = _as_non_empty_str(payload.get("agent_thread_id"))
    spawn_call_id = _as_non_empty_str(payload.get("event_id"))
    if child_id and spawn_call_id and child_id not in state.spawn_links:
        state.spawn_links[child_id] = spawn_call_id
