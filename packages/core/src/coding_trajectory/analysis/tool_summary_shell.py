"""Shell-intent classification for tool-call summaries."""

from __future__ import annotations

import os
import re
import shlex
from typing import Any, Literal

from coding_trajectory.analysis.tool_summary_shared import (
    EDIT_FILE,
    GREP_FLAG_VALUE_OPTS,
    INFORMATIVE_HEADS,
    LIST_FILES,
    READ_FILE,
    RUN_COMMAND,
    SEARCH_TEXT,
    short_command,
    short_path,
)
from coding_trajectory.analysis.shell_parser import split_shell_stages


CommandFamily = Literal[
    "cli_report",
    "search",
    "list_files",
    "read_file",
    "tests",
    "build",
    "code_fix",
    "repository",
    "dependency",
    "diagnostic",
    "external",
    "runtime",
    "other",
]

_TEST_TOKENS = frozenset(
    {
        "pytest",
        "jest",
        "vitest",
        "mocha",
        "rspec",
        "phpunit",
        "unittest",
        "tox",
        "ctest",
        "test",
        "deno",
    }
)
_BUILD_TOKENS = frozenset(
    {
        "tsc",
        "mypy",
        "ruff",
        "eslint",
        "flake8",
        "pylint",
        "black",
        "isort",
        "prettier",
        "make",
        "cmake",
        "webpack",
        "rollup",
        "vite",
        "esbuild",
        "clippy",
        "build",
        "compile",
        "lint",
        "typecheck",
        "check",
        "vet",
    }
)
_PACKAGE_MANAGERS = frozenset(
    {
        "npm",
        "pnpm",
        "yarn",
        "bun",
        "pip",
        "pip3",
        "uv",
        "poetry",
        "pipenv",
        "cargo",
        "gem",
        "bundle",
        "brew",
        "conda",
        "apt",
        "apt-get",
    }
)
_DEPENDENCY_TOKENS = frozenset(
    {
        "install",
        "add",
        "ci",
        "sync",
        "get",
        "lock",
        "update",
        "upgrade",
        "remove",
    }
)
_COMMAND_RUNNERS = frozenset(
    {
        "uv",
        "poetry",
        "pdm",
        "pipenv",
        "rye",
        "hatch",
        "npx",
        "bunx",
        "pnpm",
        "yarn",
        "bun",
        "deno",
    }
)
_RUNNER_SUBWORDS = frozenset({"run", "exec", "dlx", "tool", "task"})
_CODE_FIX_TOKENS = frozenset({"fmt", "format", "fix", "fixer"})
_DIAGNOSTIC_HEADS = frozenset(
    {"pwd", "date", "which", "where", "whoami", "uname", "env", "printenv"}
)
_EXTERNAL_HEADS = frozenset(
    {
        "curl",
        "wget",
        "http",
        "https",
        "wrangler",
        "aws",
        "gcloud",
        "az",
        "fly",
        "flyctl",
        "vercel",
        "netlify",
        "ssh",
        "scp",
        "rsync",
    }
)
_RUNTIME_TOKENS = frozenset(
    {"dev", "serve", "server", "start", "up", "runserver", "preview"}
)
_SHELL_SETUP_HEADS = frozenset({"cd", "pushd", "popd", "export", "set", "unset"})


def classify_shell(tool_name: str, tool_input: Any) -> tuple[str, str | None, str]:
    cmd = shell_cmd(tool_input)
    if not cmd:
        if tool_name == "write_stdin":
            return RUN_COMMAND, "stdin", "shell:command"
        return RUN_COMMAND, None, "shell:command"

    primary = primary_stage(cmd)
    head = primary_command(primary)
    description = short_command(primary)

    if head in {"cat", "bat", "head", "tail", "less", "more", "nl"}:
        path = first_path_arg(primary, head)
        return READ_FILE, short_path(path) or description, "shell:read"
    if head == "sed":
        path = first_path_arg(primary, head)
        return READ_FILE, short_path(path) or description, "shell:read"
    if head in {"rg", "grep", "ag", "ack", "rga"}:
        tokens = safe_split(primary)
        if any(token in {"--files", "-l", "--files-with-matches"} for token in tokens):
            return LIST_FILES, description, "shell:list"
        pattern, scope = grep_pattern_and_scope(primary, head)
        if pattern and scope:
            return SEARCH_TEXT, f"{pattern!r} within {scope}", "shell:search"
        if pattern:
            return SEARCH_TEXT, repr(pattern), "shell:search"
        return SEARCH_TEXT, description, "shell:search"
    if head in {"ls", "eza", "exa", "tree", "find", "fd"}:
        return LIST_FILES, description, "shell:list"
    if head in {"apply_patch", "applypatch"}:
        return EDIT_FILE, description, "shell:edit"

    return RUN_COMMAND, description, "shell:command"


def classify_command_family(tool_input: Any) -> tuple[CommandFamily, str]:
    cmd = shell_cmd(tool_input)
    if not cmd and isinstance(tool_input, str):
        cmd = tool_input
    if not cmd:
        return "other", "command"
    tokens = [
        os.path.basename(token.lower()) for token in safe_split(primary_stage(cmd))
    ]
    if not tokens:
        return "other", "command"

    head = command_head(tokens)
    token_set = set(tokens)
    if head in {"rg", "grep", "ag", "ack", "rga"}:
        return "search", head
    if head in {"ls", "eza", "exa", "tree", "find", "fd"}:
        return "list_files", head
    if head in {"cat", "bat", "head", "tail", "less", "more", "nl", "sed"}:
        return "read_file", head
    if head == "ct":
        return "cli_report", head
    if head in {"git", "gh", "hg", "svn"} or tokens[0] in {"git", "gh", "hg", "svn"}:
        return "repository", head
    if token_set & _TEST_TOKENS:
        return "tests", head
    if token_set & _CODE_FIX_TOKENS:
        return "code_fix", head
    if token_set & _BUILD_TOKENS:
        return "build", head
    if token_set & _PACKAGE_MANAGERS and token_set & _DEPENDENCY_TOKENS:
        return "dependency", head
    if head in _DIAGNOSTIC_HEADS or "--version" in token_set or "-v" in token_set:
        return "diagnostic", head
    if head in _EXTERNAL_HEADS:
        return "external", head
    if token_set & _RUNTIME_TOKENS:
        return "runtime", head
    return "other", head


def command_head(tokens: list[str]) -> str:
    index = 0
    while (
        index < len(tokens)
        and "=" in tokens[index]
        and not tokens[index].startswith("-")
    ):
        index += 1
    if index < len(tokens) and tokens[index] in _COMMAND_RUNNERS:
        index += 1
        while index < len(tokens) and tokens[index] in _RUNNER_SUBWORDS:
            index += 1
    if (
        index + 2 < len(tokens)
        and tokens[index] in {"python", "python3"}
        and tokens[index + 1] == "-m"
    ):
        return tokens[index + 2]
    return tokens[index] if index < len(tokens) else "command"


def primary_stage(cmd: str) -> str:
    cmd = unwrap_shell_command(cmd)
    stages = [stage for stage in split_shell_stages(cmd) if stage.strip()]
    if not stages:
        return cmd.strip()
    for stage in stages:
        if primary_command(stage) in INFORMATIVE_HEADS:
            return stage.strip()
    for stage in stages:
        if primary_command(stage) not in _SHELL_SETUP_HEADS:
            return stage.strip()
    return stages[0].strip()


def shell_cmd(tool_input: Any) -> str:
    if isinstance(tool_input, str):
        return tool_input.strip()
    if not isinstance(tool_input, dict):
        return ""
    for key in ("cmd", "command", "shell"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def primary_command(cmd: str) -> str:
    cmd = unwrap_shell_command(cmd)
    tokens = safe_split(cmd)
    if not tokens:
        return ""
    for token in tokens:
        if (
            "=" in token
            and not token.startswith("-")
            and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token)
        ):
            continue
        if token == "command":
            continue
        return os.path.basename(token)
    return os.path.basename(tokens[0])


def unwrap_shell_command(cmd: str) -> str:
    tokens = safe_split(cmd)
    if (
        len(tokens) >= 3
        and os.path.basename(tokens[0]) in {"bash", "sh", "zsh"}
        and tokens[1] in {"-c", "-lc", "-cl"}
    ):
        return tokens[2].strip()
    return cmd


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
