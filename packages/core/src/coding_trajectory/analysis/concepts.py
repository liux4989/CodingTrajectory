"""Canonical concept registry — item kinds and vendor tool name mappings."""

from __future__ import annotations

from enum import Enum

from coding_trajectory.analysis.tool_summary_shared import (
    SUBAGENT_TASK,
    TODO_LIST,
    VENDOR_TOOL_CONCEPT,
)


class ItemKind(str, Enum):
    ASSISTANT_RESPONSE = "assistant_response"
    PLAN_SUBAGENT = "plan_subagent"
    TODO_LIST = "todo_list"
    SESSION_HANDOFF = "session_handoff"
    TOOL_CALL = "tool_call"
    FILE_CHANGE = "file_change"
    COMMAND_EXECUTION = "command_execution"
    REASONING = "reasoning"


# ItemKind follows the display concept for spawn/plan tools. AGENT_COLLAB is
# deliberately absent: ``collab_agent`` names Codex collaboration operations
# (send_input, wait_agent, ...), not spawns, so those items stay TOOL_CALL.
_ITEM_KIND_FROM_TOOL_CONCEPT: dict[str, ItemKind] = {
    SUBAGENT_TASK: ItemKind.PLAN_SUBAGENT,
    TODO_LIST: ItemKind.TODO_LIST,
}

# Maps tool_name -> ItemKind for tool names that carry semantic meaning beyond
# TOOL_CALL. Derived from VENDOR_TOOL_CONCEPT so the two registries cannot
# drift. The remaining tools fall back to TOOL_CALL.
TOOL_CONCEPT_MAP: dict[str, ItemKind] = {
    tool_name: _ITEM_KIND_FROM_TOOL_CONCEPT[concept]
    for tool_name, concept in VENDOR_TOOL_CONCEPT.items()
    if concept in _ITEM_KIND_FROM_TOOL_CONCEPT
}
