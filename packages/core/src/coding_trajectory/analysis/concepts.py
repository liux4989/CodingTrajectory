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
