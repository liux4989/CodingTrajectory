"""Compact, readable per-tool-call summaries for the trajectory overview.

Each `StepToolItem` is reduced to a small dict shaped like::

    {
        "name":        "SearchText",
        "description": "'btw' within src",
        "summary":     "Found 7 matches",
        "status":      "failed",   # only when not completed
    }

Design follows two patterns surfaced in modern coding-agent CLIs:

* **Canonical concepts (Gemini-style):** vendor tool names are mapped onto a
  small set of concepts (ReadFile, SearchText, ListFiles, ...). The summary
  is built from the actual tool *output* so counts ("Found 7 matches",
  "Read lines 270-420 of 444") reflect reality.
* **Shell-intent classification (Codex-style):** generic shell tools
  (`exec_command`, `Bash`, `run_shell_command`) are parsed by command head
  to detect Read/Search/List intent before falling back to RunCommand.
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Any

from coding_trajectory.ingestion.models import StepToolItem, ToolStatus


# ---------------------------------------------------------------------------
# Canonical concepts
# ---------------------------------------------------------------------------

READ_FILE      = "ReadFile"
EDIT_FILE      = "EditFile"
WRITE_FILE     = "WriteFile"
SEARCH_TEXT    = "SearchText"
LIST_FILES     = "ListFiles"
RUN_COMMAND    = "RunCommand"
WEB_FETCH      = "WebFetch"
WEB_SEARCH     = "WebSearch"
TODO_LIST      = "TodoList"
SUBAGENT_TASK  = "SubagentTask"
SESSION_HANDOFF = "SessionHandoff"

# Vendor tool name -> canonical concept. Tools missing here either map via
# `_classify_shell_command` (the shell-style tools below) or pass through
# verbatim as the concept name.
_VENDOR_TOOL_CONCEPT: dict[str, str] = {
    # Claude Code
    "Read":      READ_FILE,
    "View":      READ_FILE,
    "Edit":      EDIT_FILE,
    "MultiEdit": EDIT_FILE,
    "Write":     WRITE_FILE,
    "Grep":      SEARCH_TEXT,
    "Glob":      LIST_FILES,
    "LS":        LIST_FILES,
    "WebFetch":  WEB_FETCH,
    "WebSearch": WEB_SEARCH,
    "TodoWrite": TODO_LIST,
    "TodoRead":  TODO_LIST,
    "Task":      SUBAGENT_TASK,
    # Gemini CLI
    "read_file":           READ_FILE,
    "read_many_files":     READ_FILE,
    "replace":             EDIT_FILE,
    "write_file":          WRITE_FILE,
    "search_file_content": SEARCH_TEXT,
    "grep_search":         SEARCH_TEXT,
    "list_directory":      LIST_FILES,
    "glob":                LIST_FILES,
    "web_fetch":           WEB_FETCH,
    "google_web_search":   WEB_SEARCH,
    # Codex CLI
    "apply_patch":  EDIT_FILE,
    "update_plan":  TODO_LIST,
    "spawn_agent":  SUBAGENT_TASK,
    "web_search":   WEB_SEARCH,
    # Amp
    "edit_file":      EDIT_FILE,
    "create_file":    WRITE_FILE,
    "read_web_page":  WEB_FETCH,
    "handoff":        SESSION_HANDOFF,
    "handoff_to":     SESSION_HANDOFF,
}

# Tools whose body is a shell command line that needs parsing for intent.
_SHELL_TOOL_NAMES: frozenset[str] = frozenset({
    "exec_command",       # Codex
    "write_stdin",        # Codex (continuation of a running shell)
    "Bash",               # Claude Code
    "run_shell_command",  # Gemini
    "shell",              # Amp / generic
})


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def summarize_tool_call(item: StepToolItem) -> dict[str, Any] | None:
    """Return a compact `{name, description, summary, status?}` dict for *item*.

    Returns ``None`` when *item* has no tool name (mid-stream / malformed).
    """
    tool_name = (item.tool_name or "").strip()
    if not tool_name:
        return None

    concept, description = _classify(tool_name, item.input)
    summary = _build_summary(concept, item.input, item.output)

    result: dict[str, Any] = {"name": concept}
    if description:
        result["description"] = description
    if summary:
        result["summary"] = summary
    if item.status == ToolStatus.FAILED:
        result["status"] = "failed"
    return result


# ---------------------------------------------------------------------------
# Classification (vendor tool name + input -> (concept, description))
# ---------------------------------------------------------------------------


def _classify(tool_name: str, tool_input: Any) -> tuple[str, str | None]:
    if tool_name in _SHELL_TOOL_NAMES:
        return _classify_shell(tool_name, tool_input)

    concept = _VENDOR_TOOL_CONCEPT.get(tool_name, tool_name)
    description = _describe_structured(concept, tool_input)
    return concept, description


def _describe_structured(concept: str, tool_input: Any) -> str | None:
    if not isinstance(tool_input, dict):
        return None

    if concept in {READ_FILE, EDIT_FILE, WRITE_FILE}:
        return _short_path(_first_str(tool_input, ("file_path", "path", "target_file", "absolute_path", "file")))

    if concept == SEARCH_TEXT:
        pattern = _first_str(tool_input, ("pattern", "query", "regex"))
        scope = _short_path(_first_str(tool_input, ("path", "include", "include_pattern", "dir_path")))
        if pattern and scope:
            return f"{pattern!r} within {scope}"
        if pattern:
            return repr(pattern)
        return scope

    if concept == LIST_FILES:
        return _short_path(_first_str(tool_input, ("path", "dir_path", "pattern", "directory")))

    if concept == WEB_FETCH:
        return _first_str(tool_input, ("url", "uri"))

    if concept == WEB_SEARCH:
        return _first_str(tool_input, ("query", "q"))

    if concept == TODO_LIST:
        todos = tool_input.get("todos") or tool_input.get("plan") or tool_input.get("items")
        if isinstance(todos, list):
            return f"{len(todos)} item(s)"
        return None

    if concept == SUBAGENT_TASK:
        return _first_str(tool_input, ("subagent_type", "agent_type", "description", "prompt"))

    return None


# ---------------------------------------------------------------------------
# Shell command intent parsing
# ---------------------------------------------------------------------------


def _classify_shell(tool_name: str, tool_input: Any) -> tuple[str, str | None]:
    cmd = _shell_cmd(tool_input)
    if not cmd:
        # `write_stdin` and similar continuations may carry only a `chars` field
        if tool_name == "write_stdin":
            return RUN_COMMAND, "stdin"
        return RUN_COMMAND, None

    primary = _primary_stage(cmd)
    head = _primary_command(primary)
    description = _short_command(primary)

    if head in {"cat", "bat", "head", "tail", "less", "more", "nl"}:
        path = _first_path_arg(primary, head)
        return READ_FILE, _short_path(path) or description
    if head == "sed":
        path = _first_path_arg(primary, head)
        return READ_FILE, _short_path(path) or description
    if head in {"rg", "grep", "ag", "ack", "rga"}:
        # `--files` / `-l` => listing mode
        tokens = _safe_split(primary)
        if any(t in {"--files", "-l", "--files-with-matches"} for t in tokens):
            return LIST_FILES, description
        pattern, scope = _grep_pattern_and_scope(primary, head)
        if pattern and scope:
            return SEARCH_TEXT, f"{pattern!r} within {scope}"
        if pattern:
            return SEARCH_TEXT, repr(pattern)
        return SEARCH_TEXT, description
    if head in {"ls", "eza", "exa", "tree", "find", "fd"}:
        return LIST_FILES, description

    return RUN_COMMAND, description


# We pick the most informative pipeline stage (the one whose head is a
# recognized read/search/list tool) so that `pwd && rg --files | head -200`
# gets classified as ListFiles.
_INFORMATIVE_HEADS: frozenset[str] = frozenset({
    "cat", "bat", "head", "tail", "less", "more", "nl", "sed",
    "rg", "grep", "ag", "ack", "rga",
    "ls", "eza", "exa", "tree", "find", "fd",
})


def _primary_stage(cmd: str) -> str:
    """Return the most informative pipeline stage for intent classification."""
    stages = [s for s in _split_shell_stages(cmd) if s.strip()]
    if not stages:
        return cmd.strip()
    for stage in stages:
        if _primary_command(stage) in _INFORMATIVE_HEADS:
            return stage.strip()
    return stages[0].strip()


def _split_shell_stages(cmd: str) -> list[str]:
    """Split *cmd* on `&&`, `||`, `;`, `|` while respecting quotes."""
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
        # Connector detection (outside quotes)
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


def _shell_cmd(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in ("cmd", "command", "shell"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _primary_command(cmd: str) -> str:
    """Return the leading executable, peeling past `bash -lc`, `&&`, pipes, etc."""
    # Strip wrappers like "bash -lc 'real cmd'"
    tokens = _safe_split(cmd)
    if len(tokens) >= 2 and tokens[0] in {"bash", "sh", "zsh"} and tokens[1] in {"-c", "-lc", "-cl"}:
        if len(tokens) >= 3:
            inner = _safe_split(tokens[2])
            if inner:
                return os.path.basename(inner[0])
    if not tokens:
        return ""
    # Skip env-var assignments (FOO=bar) and `command`
    for tok in tokens:
        if "=" in tok and not tok.startswith("-") and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            continue
        if tok == "command":
            continue
        return os.path.basename(tok)
    return os.path.basename(tokens[0])


def _safe_split(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd, posix=True)
    except ValueError:
        return cmd.split()


def _first_path_arg(cmd: str, head: str) -> str | None:
    tokens = _safe_split(cmd)
    skip_next = False
    saw_head = False
    for tok in tokens:
        if not saw_head:
            if os.path.basename(tok) == head:
                saw_head = True
            continue
        if skip_next:
            skip_next = False
            continue
        if tok in {"-n", "-e"}:  # consumes a value
            skip_next = True
            continue
        if tok.startswith("-"):
            continue
        # sed/grep first non-flag arg may be a script/pattern; prefer something
        # that looks like a path for sed (contains "." or "/")
        return tok
    return None


_GREP_FLAG_VALUE_OPTS: frozenset[str] = frozenset({
    "-A", "-B", "-C", "-e", "-f", "-g", "--glob", "-m", "--max-count",
    "-t", "--type", "--type-not", "-T", "-r", "--replace", "--include",
    "--exclude", "--exclude-dir",
})


def _grep_pattern_and_scope(cmd: str, head: str) -> tuple[str | None, str | None]:
    tokens = _safe_split(cmd)
    # Skip until past the executable
    saw_head = False
    pattern: str | None = None
    paths: list[str] = []
    skip_next = False
    for tok in tokens:
        if not saw_head:
            if os.path.basename(tok) == head:
                saw_head = True
            continue
        if skip_next:
            skip_next = False
            continue
        if tok in _GREP_FLAG_VALUE_OPTS:
            skip_next = True
            continue
        if tok.startswith("-"):
            continue
        if pattern is None:
            pattern = tok
        else:
            paths.append(tok)
    scope = _short_path(paths[0]) if paths else None
    return pattern, scope


# ---------------------------------------------------------------------------
# Output summarization
# ---------------------------------------------------------------------------

# Codex wraps every `exec_command` result in a small preamble. Strip it so
# downstream counters see the actual command output.
_CODEX_OUTPUT_RE = re.compile(
    r"^Chunk ID:.*?\nWall time:.*?\n"
    r"(?:Process exited with code (?P<exit>-?\d+)|Process running with session ID \d+)\n"
    r"Original token count:.*?\n"
    r"Output:\n",
    re.DOTALL,
)


def _build_summary(concept: str, tool_input: Any, output: Any) -> str | None:
    # Vendor pre-built summary (Gemini-style structured returnDisplay) wins.
    pre_built = _prebuilt_summary(output)
    if pre_built:
        return pre_built

    text, exit_code = _normalize_output(output)
    if exit_code is not None and exit_code != 0:
        return f"Exit code {exit_code}"
    if not text:
        return None

    if concept == READ_FILE:
        return _summarize_read(text, tool_input)
    if concept == SEARCH_TEXT:
        return _summarize_search(text, output)
    if concept == LIST_FILES:
        return _summarize_list(text, output)
    if concept in {EDIT_FILE, WRITE_FILE}:
        return _summarize_edit(text)
    if concept == RUN_COMMAND:
        return _summarize_run(text)
    return None


def _prebuilt_summary(output: Any) -> str | None:
    """Return ``output['summary']`` when the vendor already provided one."""
    if isinstance(output, dict):
        summary = output.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary.strip().rstrip(".")
    return None


def _normalize_output(output: Any) -> tuple[str, int | None]:
    """Strip Codex's wrapper preamble, return ``(body_text, exit_code)``."""
    if output is None:
        return "", None
    if isinstance(output, dict):
        # e.g. Gemini's structured response — extract a textual body
        for key in ("output", "text", "result", "content"):
            value = output.get(key)
            if isinstance(value, str):
                return value, None
        return "", None
    if isinstance(output, list):
        # Sometimes the output is a list of content blocks or match strings
        if all(isinstance(b, str) for b in output):
            return "\n".join(output), None
        joined = "\n".join(
            block.get("text", "")
            for block in output
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
        return joined, None
    if not isinstance(output, str):
        return "", None

    match = _CODEX_OUTPUT_RE.match(output)
    if match:
        body = output[match.end():]
        exit_raw = match.group("exit")
        exit_code = int(exit_raw) if exit_raw is not None else None
        return body, exit_code
    return output, None


_SED_RANGE_RE = re.compile(r"['\"]?\s*(\d+)\s*,\s*(\d+)\s*p\s*['\"]?")


def _summarize_read(text: str, tool_input: Any) -> str:
    line_count = text.count("\n") + (0 if text.endswith("\n") else 1) if text else 0
    if isinstance(tool_input, dict):
        # `read_range: [start, end]` (Amp, 1-indexed inclusive)
        rng = tool_input.get("read_range")
        if isinstance(rng, (list, tuple)) and len(rng) == 2 and all(isinstance(v, int) for v in rng):
            return f"Read lines {rng[0]}-{rng[1]}"
        offset = _first_int(tool_input, ("offset", "line_offset"))
        limit = _first_int(tool_input, ("limit", "line_limit", "line_count"))
        if offset is not None and limit is not None:
            return f"Read lines {offset + 1}-{offset + limit}"
        cmd = _first_str(tool_input, ("cmd", "command", "shell"))
        if cmd:
            sed_match = _SED_RANGE_RE.search(cmd)
            if sed_match:
                start, end = sed_match.group(1), sed_match.group(2)
                return f"Read lines {start}-{end}"
    return f"Read {line_count} line(s)" if line_count else "Empty"


def _summarize_search(text: str, output: Any) -> str:
    # If the structured output exposes the match list, prefer counting that.
    if isinstance(output, dict):
        matches_field = output.get("matches")
        if isinstance(matches_field, list):
            n = len(matches_field)
            return f"Found {n} match{'es' if n != 1 else ''}" if n else "No matches"
    if isinstance(output, list) and all(isinstance(m, str) for m in output):
        n = len(output)
        return f"Found {n} match{'es' if n != 1 else ''}" if n else "No matches"
    # `rg`/`grep` may emit either "<path>:<line>:<content>" (multi-file) or
    # "<line>:<content>" (single-file or piped) per match.
    lines = text.splitlines()
    matches = sum(
        1 for line in lines
        if _GREP_MATCH_WITH_PATH_RE.match(line) or _GREP_MATCH_LINE_ONLY_RE.match(line)
    )
    if matches == 0:
        non_blank = [line for line in lines if line.strip()]
        if not non_blank:
            return "No matches"
        return f"{len(non_blank)} line(s)"
    return f"Found {matches} match{'es' if matches != 1 else ''}"


_GREP_MATCH_WITH_PATH_RE = re.compile(r"^[^:\n]+:\d+:")
_GREP_MATCH_LINE_ONLY_RE = re.compile(r"^\s*\d+:")


def _summarize_list(text: str, output: Any) -> str:
    if isinstance(output, dict):
        files = output.get("files")
        if isinstance(files, list):
            n = len(files)
            return f"Found {n} item(s)" if n else "Empty"
    items = [line for line in text.splitlines() if line.strip()]
    if not items:
        return "Empty"
    return f"Found {len(items)} item(s)"


def _summarize_edit(text: str) -> str | None:
    # Look for diff stats like "+5 -2" or apply_patch's "Done" message
    stripped = text.strip()
    if not stripped:
        return None
    first_line = stripped.splitlines()[0]
    if len(first_line) > 80:
        return f"{len(stripped):,} chars"
    return first_line


def _summarize_run(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return "No output"
    line_count = stripped.count("\n") + 1
    if line_count > 1:
        return f"{line_count} line(s)"
    if len(stripped) > 80:
        return f"{len(stripped):,} chars"
    return stripped


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _first_str(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_int(data: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, int):
            return value
    return None


def _short_path(path: str | None) -> str | None:
    if not path:
        return None
    if len(path) <= 60:
        return path
    # Keep the trailing two segments
    parts = path.rstrip("/").split("/")
    if len(parts) >= 2:
        return ".../" + "/".join(parts[-2:])
    return path


def _short_command(cmd: str, *, max_len: int = 60) -> str:
    cleaned = re.sub(r"\s+", " ", cmd).strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 1] + "…"
