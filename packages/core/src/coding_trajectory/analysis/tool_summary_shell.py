"""Shell-intent classification for tool-call summaries."""

from __future__ import annotations

import os
import re
import shlex
from typing import Any

from coding_trajectory.analysis.tool_summary_shared import (
    GREP_FLAG_VALUE_OPTS,
    INFORMATIVE_HEADS,
    LIST_FILES,
    READ_FILE,
    RUN_COMMAND,
    SEARCH_TEXT,
    short_command,
    short_path,
)


def classify_shell(tool_name: str, tool_input: Any) -> tuple[str, str | None]:
    cmd = shell_cmd(tool_input)
    if not cmd:
        if tool_name == "write_stdin":
            return RUN_COMMAND, "stdin"
        return RUN_COMMAND, None

    primary = primary_stage(cmd)
    head = primary_command(primary)
    description = short_command(primary)

    if head in {"cat", "bat", "head", "tail", "less", "more", "nl"}:
        path = first_path_arg(primary, head)
        return READ_FILE, short_path(path) or description
    if head == "sed":
        path = first_path_arg(primary, head)
        return READ_FILE, short_path(path) or description
    if head in {"rg", "grep", "ag", "ack", "rga"}:
        tokens = safe_split(primary)
        if any(token in {"--files", "-l", "--files-with-matches"} for token in tokens):
            return LIST_FILES, description
        pattern, scope = grep_pattern_and_scope(primary, head)
        if pattern and scope:
            return SEARCH_TEXT, f"{pattern!r} within {scope}"
        if pattern:
            return SEARCH_TEXT, repr(pattern)
        return SEARCH_TEXT, description
    if head in {"ls", "eza", "exa", "tree", "find", "fd"}:
        return LIST_FILES, description

    return RUN_COMMAND, description


def primary_stage(cmd: str) -> str:
    stages = [stage for stage in split_shell_stages(cmd) if stage.strip()]
    if not stages:
        return cmd.strip()
    for stage in stages:
        if primary_command(stage) in INFORMATIVE_HEADS:
            return stage.strip()
    return stages[0].strip()


def split_shell_stages(cmd: str) -> list[str]:
    stages: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(cmd):
                buf.append(cmd[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < len(cmd):
            buf.append(ch)
            buf.append(cmd[i + 1])
            i += 2
            continue
        if ch == "&" and i + 1 < len(cmd) and cmd[i + 1] == "&":
            stages.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch == "|" and i + 1 < len(cmd) and cmd[i + 1] == "|":
            stages.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch == "|" or ch == ";":
            stages.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    stages.append("".join(buf))
    return stages


def shell_cmd(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in ("cmd", "command", "shell"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def primary_command(cmd: str) -> str:
    tokens = safe_split(cmd)
    if len(tokens) >= 2 and tokens[0] in {"bash", "sh", "zsh"} and tokens[1] in {"-c", "-lc", "-cl"}:
        if len(tokens) >= 3:
            inner = safe_split(tokens[2])
            if inner:
                return os.path.basename(inner[0])
    if not tokens:
        return ""
    for token in tokens:
        if "=" in token and not token.startswith("-") and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            continue
        if token == "command":
            continue
        return os.path.basename(token)
    return os.path.basename(tokens[0])


def safe_split(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd, posix=True)
    except ValueError:
        return cmd.split()


def first_path_arg(cmd: str, head: str) -> str | None:
    tokens = safe_split(cmd)
    skip_next = False
    saw_head = False
    for token in tokens:
        if not saw_head:
            if os.path.basename(token) == head:
                saw_head = True
            continue
        if skip_next:
            skip_next = False
            continue
        if token in {"-n", "-e"}:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        return token
    return None


def grep_pattern_and_scope(cmd: str, head: str) -> tuple[str | None, str | None]:
    tokens = safe_split(cmd)
    saw_head = False
    pattern: str | None = None
    paths: list[str] = []
    skip_next = False
    for token in tokens:
        if not saw_head:
            if os.path.basename(token) == head:
                saw_head = True
            continue
        if skip_next:
            skip_next = False
            continue
        if token in GREP_FLAG_VALUE_OPTS:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        if pattern is None:
            pattern = token
        else:
            paths.append(token)
    scope = short_path(paths[0]) if paths else None
    return pattern, scope
