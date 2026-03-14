"""Codex CLI-specific session enricher."""

from __future__ import annotations

from uuid import UUID, uuid4

from coding_trajectory.consumer.base import SessionEnricher
from coding_trajectory.ingestion.models import (
    Event,
    EventCategory,
    EventType,
    Session,
    TextDetail,
    ToolCallDetail,
    ToolCallKind,
)

_TYPE_TO_CATEGORY: dict[EventType, EventCategory] = {
    EventType.SESSION_STARTED:              EventCategory.SESSION,
    EventType.SESSION_RESUMED:              EventCategory.SESSION,
    EventType.SESSION_ENDED:                EventCategory.SESSION,
    EventType.USER_PROMPT_SUBMITTED:        EventCategory.USER_INTERACTION,
    EventType.AGENT_RESPONSE_COMPLETED:     EventCategory.USER_INTERACTION,
    EventType.TASK_COMPLETED:               EventCategory.USER_INTERACTION,
    EventType.LLM_REQUEST_STARTED:          EventCategory.LLM_INFERENCE,
    EventType.LLM_REQUEST_COMPLETED:        EventCategory.LLM_INFERENCE,
    EventType.LLM_STREAM_EVENT:             EventCategory.LLM_INFERENCE,
    EventType.TOOL_CALL_REQUESTED:          EventCategory.TOOL_CALL,
    EventType.TOOL_CALL_STARTED:            EventCategory.TOOL_CALL,
    EventType.TOOL_CALL_SUCCEEDED:          EventCategory.TOOL_CALL,
    EventType.TOOL_CALL_FAILED:             EventCategory.TOOL_CALL,
}


class CodexSessionEnricher(SessionEnricher):
    """Enriches Sessions produced by CodexAdapter."""

    def enrich(self, session: Session) -> None:
        # Pass 1 — tool_call_id → (group_id, req_event_id)
        tool_groups: dict[str, tuple[UUID, UUID]] = {}
        for event in session.events:
            if event.type == EventType.TOOL_CALL_REQUESTED:
                tcid = event.payload.get("tool_call_id")
                if tcid and tcid not in tool_groups:
                    tool_groups[tcid] = (uuid4(), event.event_id)

        # Pass 2 — enrich each event
        for event in session.events:
            self._enrich_event(event, tool_groups)

    def _enrich_event(
        self,
        event: Event,
        tool_groups: dict[str, tuple[UUID, UUID]],
    ) -> None:
        event.category = _TYPE_TO_CATEGORY.get(event.type)
        p = event.payload

        if event.type == EventType.TOOL_CALL_REQUESTED:
            tcid = p.get("tool_call_id")
            tool_name = p.get("tool_name")
            if tcid and tcid in tool_groups:
                event.event_group_id = tool_groups[tcid][0]
            event.tool_call = ToolCallDetail(
                tool_call_id=tcid,
                tool_name=tool_name,
                kind=ToolCallKind.REGULAR,
                input=p.get("tool_input"),
            )

        elif event.type in (EventType.TOOL_CALL_SUCCEEDED, EventType.TOOL_CALL_FAILED):
            tcid = p.get("tool_call_id")
            if tcid and tcid in tool_groups:
                group_id, req_event_id = tool_groups[tcid]
                event.event_group_id = group_id
                event.parent_event_id = req_event_id
            status = "done" if event.type == EventType.TOOL_CALL_SUCCEEDED else "failed"
            result = p.get("output") or p.get("raw_output")
            event.tool_call = ToolCallDetail(
                tool_call_id=tcid,
                tool_name=None,
                kind=ToolCallKind.REGULAR,
                result=result,
                status=status,
            )

        elif event.type == EventType.USER_PROMPT_SUBMITTED:
            text = p.get("text")
            if text:
                event.text = TextDetail(text=text)

        elif event.type == EventType.AGENT_RESPONSE_COMPLETED:
            text = p.get("text")
            if text:
                event.text = TextDetail(text=text)
