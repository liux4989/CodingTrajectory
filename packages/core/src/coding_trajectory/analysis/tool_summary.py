"""Compact, readable per-tool-call summaries for analysis views."""

from __future__ import annotations

import re
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
    measurements = getattr(item, "measurements", None)
    if measurements is not None:
        return dict(measurements.tool_summary) if measurements.tool_summary else None
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

    tool_input = (
        item.command
        if item.kind == "command_execution"
        else getattr(item, "input", None)
    )
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
    else:
        result["breakdown"] = _other_output_breakdown(tool_name, tool_input)
    status = getattr(item, "status", None)
    if status in {ToolStatus.FAILED, ToolStatus.FAILED.value, "failed"}:
        result["status"] = "failed"
    return result


def _other_output_breakdown(tool_name: str, tool_input: Any) -> str:
    """Return a compact label for output outside the known tool taxonomy.

    The desktop ``exec`` tool can orchestrate several shell commands in one
    tool item. Its output is therefore attributable to the wrapper as a whole,
    not to each nested command independently. Preserve the nested labels when
    available so session stats can expose the expensive wrapper call without
    inventing an inner-command token split.
    """
    if tool_name != "exec" or not isinstance(tool_input, str):
        return f"tool: {tool_name}"

    labels = re.findall(r"\[\s*[\"']([^\"']{1,96})[\"']\s*,", tool_input)
    unique_labels = list(
        dict.fromkeys(label.strip() for label in labels if label.strip())
    )
    if unique_labels:
        shown = ", ".join(unique_labels[:4])
        remaining = len(unique_labels) - 4
        suffix = f", +{remaining} more" if remaining > 0 else ""
        count_label = "command" if len(unique_labels) == 1 else "commands"
        return f"exec ({len(unique_labels)} {count_label}): {shown}{suffix}"
    commands = _embedded_exec_commands(tool_input)
    if commands:
        command_heads = [
            classify_command_family({"cmd": command})[1] for command in commands
        ]
        unique_heads = list(dict.fromkeys(command_heads))
        shown = ", ".join(unique_heads[:4])
        remaining = len(unique_heads) - 4
        suffix = f", +{remaining} more" if remaining > 0 else ""
        count_label = "command" if len(unique_heads) == 1 else "commands"
        return f"exec ({len(unique_heads)} {count_label}): {shown}{suffix}"
    return "exec orchestration"


def _embedded_exec_commands(value: str) -> list[str]:
    commands: list[str] = []
    for match in re.finditer(r"\bcmd\s*:\s*([\"'`])", value):
        quote = match.group(1)
        start = match.end()
        cursor = start
        while cursor < len(value):
            if value[cursor] == "\\":
                cursor += 2
                continue
            if value[cursor] == quote:
                command = value[start:cursor].strip()
                if command:
                    commands.append(command)
                break
            cursor += 1
    return commands


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
        return short_path(
            first_str(
                tool_input,
                ("file_path", "path", "target_file", "absolute_path", "file"),
            )
        )

    if concept == SEARCH_TEXT:
        pattern = first_str(tool_input, ("pattern", "query", "regex"))
        scope = short_path(
            first_str(tool_input, ("path", "include", "include_pattern", "dir_path"))
        )
        if pattern and scope:
            return f"{pattern!r} within {scope}"
        if pattern:
            return repr(pattern)
        return scope

    if concept == LIST_FILES:
        return short_path(
            first_str(tool_input, ("path", "dir_path", "pattern", "directory"))
        )

    if concept == WEB_FETCH:
        return first_str(tool_input, ("url", "uri"))

    if concept == WEB_SEARCH:
        return first_str(tool_input, ("query", "q"))

    if concept == TODO_LIST:
        todos = (
            tool_input.get("todos") or tool_input.get("plan") or tool_input.get("items")
        )
        if isinstance(todos, list):
            return f"{len(todos)} item(s)"
        return None

    if concept == SUBAGENT_TASK:
        return first_str(
            tool_input, ("subagent_type", "agent_type", "description", "prompt")
        )

    return None
