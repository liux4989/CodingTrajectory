"""Claude Code-specific session enricher.

Reads the raw payload keys that ClaudeCodeAdapter writes and populates the
typed detail fields (tool_call, llm, text), event category, event_group_id,
and parent_event_id on each Event.
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

# Claude Code tool-name → ToolCallKind
_SUBAGENT_TOOLS = {"TeamCreate"}


def _tool_kind(tool_name: str | None) -> ToolCallKind:
    if tool_name in _SUBAGENT_TOOLS:
        return ToolCallKind.SUBAGENT
    return ToolCallKind.REGULAR


_TYPE_TO_CATEGORY: dict[EventType, EventCategory] = {
    EventType.SESSION_STARTED:              EventCategory.SESSION,
    EventType.SESSION_RESUMED:              EventCategory.SESSION,
    EventType.SESSION_ENDED:                EventCategory.SESSION,
    EventType.USER_PROMPT_SUBMITTED:        EventCategory.USER_INTERACTION,
    EventType.AGENT_RESPONSE_COMPLETED:     EventCategory.USER_INTERACTION,
    EventType.LLM_REQUEST_STARTED:          EventCategory.LLM_INFERENCE,
    EventType.LLM_REQUEST_COMPLETED:        EventCategory.LLM_INFERENCE,
    EventType.LLM_STREAM_EVENT:             EventCategory.LLM_INFERENCE,
    EventType.TOOL_CALL_REQUESTED:          EventCategory.TOOL_CALL,
    EventType.TOOL_CALL_STARTED:            EventCategory.TOOL_CALL,
    EventType.TOOL_CALL_SUCCEEDED:          EventCategory.TOOL_CALL,
    EventType.TOOL_CALL_FAILED:             EventCategory.TOOL_CALL,
    EventType.BACKGROUND_TASK_STARTED:      EventCategory.BACKGROUND_TASK,
    EventType.BACKGROUND_TASK_COMPLETED:    EventCategory.BACKGROUND_TASK,
    EventType.TASK_COMPLETED:               EventCategory.BACKGROUND_TASK,
    EventType.PERMISSION_REQUESTED:         EventCategory.PERMISSION,
    EventType.PERMISSION_APPROVED:          EventCategory.PERMISSION,
    EventType.PERMISSION_DENIED:            EventCategory.PERMISSION,
    EventType.CONTEXT_COMPACTION_STARTED:   EventCategory.CONTEXT_COMPACTION,
    EventType.CONTEXT_COMPACTION_COMPLETED: EventCategory.CONTEXT_COMPACTION,
}


class ClaudeCodeSessionEnricher(SessionEnricher):
    """Enriches Sessions produced by ClaudeCodeAdapter."""

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
            self._enrich_tool_requested(event, p, tool_groups)

        elif event.type in (EventType.TOOL_CALL_SUCCEEDED, EventType.TOOL_CALL_FAILED):
            self._enrich_tool_result(event, p, tool_groups)

        elif event.type == EventType.BACKGROUND_TASK_STARTED:
            # Claude Code emits BACKGROUND_TASK_STARTED with team_name / lead_agent_id.
            # It shares the group of the TeamCreate TOOL_CALL_REQUESTED.
            tcid = p.get("tool_call_id")
            if tcid and tcid in tool_groups:
                group_id, req_event_id = tool_groups[tcid]
                event.event_group_id = group_id
                event.parent_event_id = req_event_id
            tool_name = p.get("tool_name")
            event.tool_call = ToolCallDetail(
                tool_call_id=tcid,
                tool_name=tool_name,
                kind=_tool_kind(tool_name),
            )

        elif event.type in (EventType.AGENT_RESPONSE_COMPLETED, EventType.LLM_REQUEST_COMPLETED,
                            EventType.LLM_STREAM_EVENT):
            self._enrich_llm_or_response(event, p)

        elif event.type == EventType.USER_PROMPT_SUBMITTED:
            text = p.get("text")
            if text:
                event.text = TextDetail(text=text)

        elif event.type == EventType.CONTEXT_COMPACTION_STARTED:
            # LLM metadata embedded in compaction event
            usage = p.get("usage") or {}
            if usage:
                event.llm = LLMDetail(
                    input_tokens=_nested_int(usage, "input_tokens"),
                    output_tokens=_nested_int(usage, "output_tokens"),
                )

    def _enrich_tool_requested(
        self,
        event: Event,
        p: dict,
        tool_groups: dict[str, tuple[UUID, UUID]],
    ) -> None:
        tcid = p.get("tool_call_id")
        tool_name = p.get("tool_name")
        if tcid and tcid in tool_groups:
            event.event_group_id = tool_groups[tcid][0]

        event.tool_call = ToolCallDetail(
            tool_call_id=tcid,
            tool_name=tool_name,
            kind=_tool_kind(tool_name),
            input=p.get("tool_input"),
        )

    def _enrich_tool_result(
        self,
        event: Event,
        p: dict,
        tool_groups: dict[str, tuple[UUID, UUID]],
    ) -> None:
        tcid = p.get("tool_call_id")
        if tcid and tcid in tool_groups:
            group_id, req_event_id = tool_groups[tcid]
            event.event_group_id = group_id
            event.parent_event_id = req_event_id

        status = "done" if event.type == EventType.TOOL_CALL_SUCCEEDED else "failed"
        # Claude Code stores result in tool_output (dict) or tool_text (str)
        result = p.get("tool_output") or p.get("tool_text")
        event.tool_call = ToolCallDetail(
            tool_call_id=tcid,
            tool_name=None,   # not available in result event; consumer can look up from group
            kind=ToolCallKind.REGULAR,
            result=result,
            status=status,
        )

    def _enrich_llm_or_response(self, event: Event, p: dict) -> None:
        usage = p.get("usage") or {}
        event.llm = LLMDetail(
            model=_nested_str(usage, "model"),
            input_tokens=_nested_int(usage, "input_tokens"),
            output_tokens=_nested_int(usage, "output_tokens"),
            stop_reason=p.get("stop_reason"),
        )
        text = p.get("text")
        if text:
            event.text = TextDetail(text=text)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _nested_int(d: dict, key: str) -> int | None:
    v = d.get(key)
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _nested_str(d: dict, key: str) -> str | None:
    v = d.get(key)
    return str(v) if v is not None else None
