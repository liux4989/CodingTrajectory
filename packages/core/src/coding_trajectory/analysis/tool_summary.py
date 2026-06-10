"""Compact, readable per-tool-call summaries for analysis views."""

from __future__ import annotations

from typing import Any

from coding_trajectory.analysis.tool_summary_shell import (
    classify_command_family,
    classify_shell,
)
from coding_trajectory.analysis.tool_summary_shared import (
    EDIT_FILE,
    LIST_FILES,
    READ_FILE,
    SEARCH_TEXT,
    SHELL_TOOL_NAMES,
    SUBAGENT_TASK,
    TODO_LIST,
    VENDOR_TOOL_CONCEPT,
    WEB_FETCH,
    WEB_SEARCH,
    WRITE_FILE,
    first_str,
    short_path,
)
from coding_trajectory.ingestion.models import Item, ToolStatus


def summarize_tool_call(item: Item) -> dict[str, Any] | None:
    tool_name_raw = getattr(item, "tool_name", None)
    tool_name = (tool_name_raw or "").strip() if isinstance(tool_name_raw, str) else ""
    if not tool_name:
        tool_name = {
            "command_execution": "exec_command",
            "file_change": "apply_patch",
            "plan": "update_plan",
        }.get(item.kind, "")
    if not tool_name:
        return None

    tool_input = item.command if item.kind == "command_execution" else getattr(item, "input", None)
    concept, description, optimization_profile = _classify(tool_name, tool_input)

    result: dict[str, Any] = {"name": concept}
    if optimization_profile:
        result["optimization_profile"] = optimization_profile
    if description:
        result["description"] = description
    if concept == "RunCommand":
        family, command = classify_command_family(tool_input)
        result["command_family"] = family
        result["command"] = command
    status = getattr(item, "status", None)
    if status in {ToolStatus.FAILED, ToolStatus.FAILED.value, "failed"}:
        result["status"] = "failed"
    return result


def _classify(tool_name: str, tool_input: Any) -> tuple[str, str | None, str | None]:
    if tool_name in SHELL_TOOL_NAMES:
        concept, description, profile = classify_shell(tool_name, tool_input)
        return concept, description, profile

    concept = VENDOR_TOOL_CONCEPT.get(tool_name, tool_name)
    description = _describe_structured(concept, tool_input)
    return concept, description, None


def _describe_structured(concept: str, tool_input: Any) -> str | None:
    if not isinstance(tool_input, dict):
        return None

    if concept in {READ_FILE, EDIT_FILE, WRITE_FILE}:
        return short_path(first_str(tool_input, ("file_path", "path", "target_file", "absolute_path", "file")))

    if concept == SEARCH_TEXT:
        pattern = first_str(tool_input, ("pattern", "query", "regex"))
        scope = short_path(first_str(tool_input, ("path", "include", "include_pattern", "dir_path")))
        if pattern and scope:
            return f"{pattern!r} within {scope}"
        if pattern:
            return repr(pattern)
        return scope

    if concept == LIST_FILES:
        return short_path(first_str(tool_input, ("path", "dir_path", "pattern", "directory")))

    if concept == WEB_FETCH:
        return first_str(tool_input, ("url", "uri"))

    if concept == WEB_SEARCH:
        return first_str(tool_input, ("query", "q"))

    if concept == TODO_LIST:
        todos = tool_input.get("todos") or tool_input.get("plan") or tool_input.get("items")
        if isinstance(todos, list):
            return f"{len(todos)} item(s)"
        return None

    if concept == SUBAGENT_TASK:
        return first_str(tool_input, ("subagent_type", "agent_type", "description", "prompt"))

    return None
