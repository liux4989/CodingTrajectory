"""Canonical concept registry — item kinds and vendor tool name mappings."""

from __future__ import annotations

from enum import Enum


class ItemKind(str, Enum):
    ASSISTANT_RESPONSE = "assistant_response"
    PLAN_SUBAGENT = "plan_subagent"
    TODO_LIST = "todo_list"
    SESSION_HANDOFF = "session_handoff"
    TOOL_CALL = "tool_call"
    FILE_CHANGE = "file_change"
    COMMAND_EXECUTION = "command_execution"
    REASONING = "reasoning"


# Maps tool_name -> ItemKind for tool names that carry semantic meaning beyond
# TOOL_CALL. The remaining tools fall back to TOOL_CALL.
TOOL_CONCEPT_MAP: dict[str, ItemKind] = {
    # --- subagent spawning ---
    "spawn_agent": ItemKind.PLAN_SUBAGENT,  # Codex
    "Agent": ItemKind.PLAN_SUBAGENT,  # Claude Code
    "Task": ItemKind.PLAN_SUBAGENT,  # Factory
    # --- todo list / planning ---
    "update_plan": ItemKind.TODO_LIST,  # Codex
    "TodoWrite": ItemKind.TODO_LIST,  # Claude Code
    "TodoRead": ItemKind.TODO_LIST,  # Claude Code
}
