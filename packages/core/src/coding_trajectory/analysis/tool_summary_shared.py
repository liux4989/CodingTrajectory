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
# Collaboration operations on an already-spawned agent (send_input, wait,
# resume, close) — distinct from spawning one.
AGENT_COLLAB = "AgentCollab"
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
    "collab_agent": AGENT_COLLAB,
    "web_search": WEB_SEARCH,
    "edit_file": EDIT_FILE,
    "create_file": WRITE_FILE,
    "read_web_page": WEB_FETCH,
    "handoff": SESSION_HANDOFF,
    "handoff_to": SESSION_HANDOFF,
}

SHELL_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "bash",
        "Bash",
        "exec_command",
        "run_shell_command",
        "shell",
        "write_stdin",
    }
)

INFORMATIVE_HEADS: frozenset[str] = frozenset(
    {
        "cat",
        "bat",
        "head",
        "tail",
        "less",
        "more",
        "nl",
        "sed",
        "rg",
        "grep",
        "ag",
        "ack",
        "rga",
        "ls",
        "eza",
        "exa",
        "tree",
        "find",
        "fd",
        "apply_patch",
        "applypatch",
    }
)

GREP_FLAG_VALUE_OPTS: frozenset[str] = frozenset(
    {
        "-A",
        "-B",
        "-C",
        "-e",
        "-f",
        "-g",
        "--glob",
        "-m",
        "--max-count",
        "-t",
        "--type",
        "--type-not",
        "-T",
        "-r",
        "--replace",
        "--include",
        "--exclude",
        "--exclude-dir",
    }
)


def first_str(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
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
    """Return a bounded command while preserving its action and final target.

    Generic shell commands are already the most faithful available description;
    they should not be replaced by a speculative category.  Keeping both ends
    also avoids making commands that differ only in their final argument look
    identical in static activity views.
    """
    cleaned = re.sub(r"\s+", " ", cmd).strip()
    if len(cleaned) <= max_len:
        return cleaned
    marker = " … "
    if max_len <= len(marker):
        return marker[:max_len]
    parts = cleaned.split(" ")
    if len(parts) > 1 and len(parts[0] + marker + parts[-1]) <= max_len:
        head = parts[0]
        for part in parts[1:-1]:
            candidate = f"{head} {part}{marker}{parts[-1]}"
            if len(candidate) > max_len:
                break
            head = f"{head} {part}"
        return head + marker + parts[-1]
    tail_len = max(12, (max_len - len(marker)) // 2)
    head_len = max_len - len(marker) - tail_len
    return cleaned[:head_len].rstrip() + marker + cleaned[-tail_len:].lstrip()
