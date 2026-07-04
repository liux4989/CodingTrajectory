"""Shared constants and helpers for tool-call summaries."""

from __future__ import annotations

import re
from typing import Any

READ_FILE = "ReadFile"
EDIT_FILE = "EditFile"
WRITE_FILE = "WriteFile"
SEARCH_TEXT = "SearchText"
LIST_FILES = "ListFiles"
RUN_COMMAND = "RunCommand"
WEB_FETCH = "WebFetch"
WEB_SEARCH = "WebSearch"
TODO_LIST = "TodoList"
SUBAGENT_TASK = "SubagentTask"
SESSION_HANDOFF = "SessionHandoff"

VENDOR_TOOL_CONCEPT: dict[str, str] = {
    "Read": READ_FILE,
    "read": READ_FILE,
    "View": READ_FILE,
    "Edit": EDIT_FILE,
    "edit": EDIT_FILE,
    "MultiEdit": EDIT_FILE,
    "Write": WRITE_FILE,
    "write": WRITE_FILE,
    "Grep": SEARCH_TEXT,
    "Glob": LIST_FILES,
    "LS": LIST_FILES,
    "WebFetch": WEB_FETCH,
    "WebSearch": WEB_SEARCH,
    "TodoWrite": TODO_LIST,
    "TodoRead": TODO_LIST,
    "Task": SUBAGENT_TASK,
    "Agent": SUBAGENT_TASK,
    "read_file": READ_FILE,
    "read_many_files": READ_FILE,
    "replace": EDIT_FILE,
    "write_file": WRITE_FILE,
    "search_file_content": SEARCH_TEXT,
    "grep_search": SEARCH_TEXT,
    "list_directory": LIST_FILES,
    "glob": LIST_FILES,
    "web_fetch": WEB_FETCH,
    "google_web_search": WEB_SEARCH,
    "apply_patch": EDIT_FILE,
    "update_plan": TODO_LIST,
    "spawn_agent": SUBAGENT_TASK,
    "web_search": WEB_SEARCH,
    "edit_file": EDIT_FILE,
    "create_file": WRITE_FILE,
    "read_web_page": WEB_FETCH,
    "handoff": SESSION_HANDOFF,
    "handoff_to": SESSION_HANDOFF,
}

SHELL_TOOL_NAMES: frozenset[str] = frozenset({
    "bash",
    "Bash",
    "exec_command",
    "run_shell_command",
    "shell",
    "write_stdin",
})

INFORMATIVE_HEADS: frozenset[str] = frozenset({
    "cat", "bat", "head", "tail", "less", "more", "nl", "sed",
    "rg", "grep", "ag", "ack", "rga",
    "ls", "eza", "exa", "tree", "find", "fd",
    "apply_patch", "applypatch",
})

GREP_FLAG_VALUE_OPTS: frozenset[str] = frozenset({
    "-A", "-B", "-C", "-e", "-f", "-g", "--glob", "-m", "--max-count",
    "-t", "--type", "--type-not", "-T", "-r", "--replace", "--include",
    "--exclude", "--exclude-dir",
})

def first_str(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def first_int(data: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, int):
            return value
    return None


def short_path(path: str | None) -> str | None:
    if not path:
        return None
    if len(path) <= 60:
        return path
    parts = path.rstrip("/").split("/")
    if len(parts) >= 2:
        return ".../" + "/".join(parts[-2:])
    return path


def short_command(cmd: str, *, max_len: int = 60) -> str:
    cleaned = re.sub(r"\s+", " ", cmd).strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 1] + "…"
