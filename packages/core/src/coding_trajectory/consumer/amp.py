"""Amp-specific session enricher.

Reads the raw payload keys that AmpAdapter writes and populates the typed
detail fields (tool_call, llm, text), event category, event_group_id, and
parent_event_id on each Event.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from coding_trajectory.consumer.base import SessionEnricher
from coding_trajectory.ingestion.models import (
    Event,
    EventCategory,
    EventType,
    LLMDetail,
    Session,
    TextDetail,
    ToolCallDetail,
    ToolCallKind,
)

# ---------------------------------------------------------------------------
# Tool-name → ToolCallKind classification
# ---------------------------------------------------------------------------

_SUBAGENT_TOOLS = {"Task"}
_ORACLE_TOOLS = {"oracle"}
_LIBRARIAN_TOOLS = {"librarian"}
_GITHUB_TOOLS = {"search_github", "read_github", "glob_github", "list_repositories"}


def _tool_kind(tool_name: str | None) -> ToolCallKind:
    if tool_name in _SUBAGENT_TOOLS:
        return ToolCallKind.SUBAGENT
    if tool_name in _ORACLE_TOOLS:
        return ToolCallKind.ORACLE
    if tool_name in _LIBRARIAN_TOOLS:
        return ToolCallKind.LIBRARIAN
    if tool_name in _GITHUB_TOOLS:
        return ToolCallKind.GITHUB
    return ToolCallKind.REGULAR


# ---------------------------------------------------------------------------
# EventType → EventCategory mapping
# ---------------------------------------------------------------------------

_TYPE_TO_CATEGORY: dict[EventType, EventCategory] = {
    EventType.SESSION_STARTED:            EventCategory.SESSION,
    EventType.SESSION_RESUMED:            EventCategory.SESSION,
    EventType.SESSION_ENDED:              EventCategory.SESSION,
    EventType.USER_PROMPT_SUBMITTED:      EventCategory.USER_INTERACTION,
    EventType.AGENT_RESPONSE_COMPLETED:   EventCategory.USER_INTERACTION,
    EventType.LLM_REQUEST_STARTED:        EventCategory.LLM_INFERENCE,
    EventType.LLM_REQUEST_COMPLETED:      EventCategory.LLM_INFERENCE,
    EventType.LLM_STREAM_EVENT:           EventCategory.LLM_INFERENCE,
    EventType.TOOL_CALL_REQUESTED:        EventCategory.TOOL_CALL,
    EventType.TOOL_CALL_STARTED:          EventCategory.TOOL_CALL,
    EventType.TOOL_CALL_SUCCEEDED:        EventCategory.TOOL_CALL,
    EventType.TOOL_CALL_FAILED:           EventCategory.TOOL_CALL,
    EventType.BACKGROUND_TASK_STARTED:    EventCategory.BACKGROUND_TASK,
    EventType.BACKGROUND_TASK_COMPLETED:  EventCategory.BACKGROUND_TASK,
    EventType.TASK_COMPLETED:             EventCategory.BACKGROUND_TASK,
    EventType.PERMISSION_REQUESTED:       EventCategory.PERMISSION,
    EventType.PERMISSION_APPROVED:        EventCategory.PERMISSION,
    EventType.PERMISSION_DENIED:          EventCategory.PERMISSION,
    EventType.CONTEXT_COMPACTION_STARTED: EventCategory.CONTEXT_COMPACTION,
    EventType.CONTEXT_COMPACTION_COMPLETED: EventCategory.CONTEXT_COMPACTION,
}


class AmpSessionEnricher(SessionEnricher):
    """Enriches Sessions produced by AmpAdapter."""

    def enrich(self, session: Session) -> None:
        # Pass 1 — collect tool_call_id → (group_id, event_id) for root tool calls
        # so later events (SUCCEEDED, STARTED, derived sub-tools) can look them up.
        tool_groups: dict[str, tuple[UUID, UUID]] = {}  # tool_call_id → (group_id, req_event_id)

        for event in session.events:
            if event.type == EventType.TOOL_CALL_REQUESTED:
                tcid = event.payload.get("tool_call_id")
                if tcid and tcid not in tool_groups:
                    tool_groups[tcid] = (uuid4(), event.event_id)

        # Pass 2 — set category + typed detail + tree linkage on every event
        for event in session.events:
            self._enrich_event(event, tool_groups)

    # ------------------------------------------------------------------

    def _enrich_event(
        self,
        event: Event,
        tool_groups: dict[str, tuple[UUID, UUID]],
    ) -> None:
        event.category = _TYPE_TO_CATEGORY.get(event.type)
        p = event.payload

        if event.type == EventType.TOOL_CALL_REQUESTED:
            self._enrich_tool_requested(event, p, tool_groups)

        elif event.type in (EventType.BACKGROUND_TASK_STARTED, EventType.BACKGROUND_TASK_COMPLETED):
            self._enrich_background_task(event, p, tool_groups)

        elif event.type in (EventType.TOOL_CALL_SUCCEEDED, EventType.TOOL_CALL_FAILED):
            self._enrich_tool_result(event, p, tool_groups)

        elif event.type == EventType.AGENT_RESPONSE_COMPLETED:
            self._enrich_agent_response(event, p, tool_groups)

        elif event.type in (EventType.LLM_REQUEST_STARTED, EventType.LLM_REQUEST_COMPLETED,
                            EventType.LLM_STREAM_EVENT):
            self._enrich_llm(event, p)

        elif event.type == EventType.USER_PROMPT_SUBMITTED:
            self._enrich_user_prompt(event, p)

    # ------------------------------------------------------------------
    # Per-type helpers
    # ------------------------------------------------------------------

    def _enrich_tool_requested(
        self,
        event: Event,
        p: dict,
        tool_groups: dict[str, tuple[UUID, UUID]],
    ) -> None:
        tcid = p.get("tool_call_id")
        tool_name = p.get("tool_name")
        kind = _tool_kind(tool_name)

        if tcid and tcid in tool_groups:
            group_id, _ = tool_groups[tcid]
            event.event_group_id = group_id
            # Root TOOL_CALL_REQUESTED has no parent within the group

        event.tool_call = ToolCallDetail(
            tool_call_id=tcid,
            tool_name=tool_name,
            kind=kind,
            input=p.get("tool_input"),
        )

    def _enrich_background_task(
        self,
        event: Event,
        p: dict,
        tool_groups: dict[str, tuple[UUID, UUID]],
    ) -> None:
        tcid = p.get("tool_call_id")
        tool_name = p.get("tool_name")

        if tcid and tcid in tool_groups:
            group_id, req_event_id = tool_groups[tcid]
            event.event_group_id = group_id
            # BACKGROUND_TASK_STARTED/COMPLETED is a sibling of TOOL_CALL_REQUESTED;
            # point parent to the TOOL_CALL_REQUESTED that opened the group.
            event.parent_event_id = req_event_id

        event.tool_call = ToolCallDetail(
            tool_call_id=tcid,
            tool_name=tool_name,
            kind=_tool_kind(tool_name),
            result=p.get("tool_result"),
            status="done" if event.type == EventType.BACKGROUND_TASK_COMPLETED else None,
        )

    def _enrich_tool_result(
        self,
        event: Event,
        p: dict,
        tool_groups: dict[str, tuple[UUID, UUID]],
    ) -> None:
        tcid = p.get("tool_call_id")
        tool_name = p.get("tool_name")

        # Derived sub-tool events use parent_tool_call_id instead of tool_call_id
        parent_tcid = p.get("parent_tool_call_id")
        if parent_tcid and parent_tcid in tool_groups:
            group_id, req_event_id = tool_groups[parent_tcid]
            event.event_group_id = group_id
            event.parent_event_id = req_event_id
        elif tcid and tcid in tool_groups:
            group_id, req_event_id = tool_groups[tcid]
            event.event_group_id = group_id
            event.parent_event_id = req_event_id

        status = p.get("tool_status") or (
            "done" if event.type == EventType.TOOL_CALL_SUCCEEDED else "failed"
        )
        event.tool_call = ToolCallDetail(
            tool_call_id=tcid or parent_tcid,
            tool_name=tool_name or p.get("parent_tool_name"),
            kind=_tool_kind(tool_name or p.get("parent_tool_name")),
            result=p.get("tool_result"),
            status=status,
        )

    def _enrich_agent_response(
        self,
        event: Event,
        p: dict,
        tool_groups: dict[str, tuple[UUID, UUID]],
    ) -> None:
        # oracle/librarian responses carry tool_call_id → link to parent tool group
        tcid = p.get("tool_call_id")
        if tcid and tcid in tool_groups:
            group_id, req_event_id = tool_groups[tcid]
            event.event_group_id = group_id
            event.parent_event_id = req_event_id

        text = p.get("text")
        if text:
            event.text = TextDetail(text=text)

        # Also capture LLM metadata that oracle embeds in its response event
        if any(p.get(k) for k in ("model", "token_input", "token_output", "inference_ms", "reasoning_effort")):
            event.llm = LLMDetail(
                model=p.get("model"),
                input_tokens=_to_int(p.get("token_input")),
                output_tokens=_to_int(p.get("token_output")),
                inference_ms=_to_float(p.get("inference_ms")),
                reasoning_effort=p.get("reasoning_effort"),
            )

    def _enrich_llm(self, event: Event, p: dict) -> None:
        event.llm = LLMDetail(
            model=p.get("model"),
            input_tokens=_to_int(p.get("token_input")),
            output_tokens=_to_int(p.get("token_output")),
            total_tokens=_to_int(p.get("token_total")),
            inference_ms=_to_float(p.get("inference_ms")),
            reasoning_effort=p.get("reasoning_effort"),
            stop_reason=p.get("stop_reason"),
        )
        text = p.get("text")
        if text:
            event.text = TextDetail(text=text)

    def _enrich_user_prompt(self, event: Event, p: dict) -> None:
        text = p.get("text")
        if text:
            event.text = TextDetail(
                text=text,
                active_editor=p.get("active_editor"),
                visible_files=p.get("visible_files"),
                cursor_location=p.get("cursor_location"),
                interrupted=p.get("interrupted"),
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_int(v: object) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _to_float(v: object) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
