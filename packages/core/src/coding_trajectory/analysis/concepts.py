"""Canonical concept registry — step types and vendor tool name mappings."""

from __future__ import annotations

from enum import Enum


class StepType(str, Enum):
    ASSISTANT_RESPONSE = "assistant_response"
    PLAN_SUBAGENT       = "plan_subagent"
    TODO_LIST           = "todo_list"
    SESSION_HANDOFF     = "session_handoff"
    TOOL_CALL           = "tool_call"


# Maps tool_name -> StepType.
# Only tools that carry semantic meaning beyond TOOL_CALL are listed.
# TOOL_CALL is the implicit fallback for anything not in this map.
TOOL_CONCEPT_MAP: dict[str, StepType] = {
    # --- subagent spawning ---
    "spawn_agent": StepType.PLAN_SUBAGENT,   # Codex
    "Agent":       StepType.PLAN_SUBAGENT,   # Claude Code
    "Task":        StepType.PLAN_SUBAGENT,   # Factory

    # --- todo list / planning ---
    "update_plan": StepType.TODO_LIST,        # Codex
    "TodoWrite":   StepType.TODO_LIST,        # Claude Code
    "TodoRead":    StepType.TODO_LIST,        # Claude Code

    # --- session handoff ---
    "handoff":    StepType.SESSION_HANDOFF,   # Amp
    "handoff_to": StepType.SESSION_HANDOFF,   # Amp
}
