"""Shell-intent classification for tool-call summaries."""

from __future__ import annotations

import os
import re
import shlex
from typing import Any, Literal

from coding_trajectory.analysis.shell_parser import split_shell_stages
from coding_trajectory.analysis.tool_summary_shared import (
    EDIT_FILE,
    GREP_FLAG_VALUE_OPTS,
    INFORMATIVE_HEADS,
    LIST_FILES,
    READ_FILE,
    RUN_COMMAND,
    SEARCH_TEXT,
    WRITE_FILE,
    short_command,
    short_path,
)

VerificationKind = Literal["tests", "checks"]

_TEST_RUNNER_HEADS = frozenset(
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
    }
)
_TEST_SUBCOMMAND_RUNNERS = frozenset(
    {"npm", "pnpm", "yarn", "bun", "deno", "cargo", "go", "dotnet", "mix"}
)
_CHECK_RUNNER_HEADS = frozenset(
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
        "clippy",
    }
)
_CHECK_SUBCOMMANDS = frozenset(
    {"build", "compile", "lint", "typecheck", "check", "vet"}
)
_CHECK_SUBCOMMAND_RUNNERS = frozenset(
    {"npm", "pnpm", "yarn", "bun", "deno", "cargo", "go", "dotnet", "mix"}
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

    redirected_path = _stdout_redirect_path(primary)
    if redirected_path is not None:
        return WRITE_FILE, short_path(redirected_path), "shell:write"
    if head in {"cat", "bat", "head", "tail", "less", "more", "nl"}:
        path = first_path_arg(primary, head)
        return READ_FILE, short_path(path) or description, "shell:read"
    if head == "sed" and _sed_edits_in_place(primary):
        path = _sed_edit_path(primary)
        return EDIT_FILE, short_path(path) or description, "shell:edit"
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


def classify_verification_command(tool_input: Any) -> VerificationKind | None:
    """Recognize only commands suitable for the summary verification section."""
    cmd = shell_cmd(tool_input)
    if not cmd and isinstance(tool_input, str):
        cmd = tool_input
    if not cmd:
        return None
    tokens = [
        os.path.basename(token.lower()) for token in safe_split(primary_stage(cmd))
    ]
    if not tokens:
        return None

    head = command_head(tokens)
    if head in _TEST_RUNNER_HEADS or (
        tokens[0] in _TEST_SUBCOMMAND_RUNNERS and "test" in tokens[1:]
    ):
        return "tests"
    if head in _CHECK_RUNNER_HEADS:
        if head in {"black", "isort", "prettier"} and "--check" not in tokens:
            return None
        if head == "ruff" and not any(
            token in {"check", "rule", "analyze"} for token in tokens[1:]
        ):
            return None
        return "checks"
    if tokens[0] in _CHECK_SUBCOMMAND_RUNNERS and any(
        token in _CHECK_SUBCOMMANDS for token in tokens[1:]
    ):
        return "checks"
    return None


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
        head = primary_command(stage)
        if head in INFORMATIVE_HEADS and _stage_has_standalone_subject(stage, head):
            return stage.strip()
    for stage in stages:
        if primary_command(stage) not in _SHELL_SETUP_HEADS:
            return stage.strip()
    return stages[0].strip()


def _stage_has_standalone_subject(stage: str, head: str) -> bool:
    """Whether an informative shell stage identifies its own subject.

    Filters such as ``head -3`` and ``sed -n 1,3p`` often consume a prior
    pipeline stage. Treating them as file reads produces labels whose alleged
    path is only an option or expression. Prefer the command that supplies
    their stdin unless the read stage names a file itself.
    """

    if head in {"cat", "bat", "head", "tail", "less", "more", "nl", "sed"}:
        return first_path_arg(stage, head) is not None
    return True


def _sed_edits_in_place(stage: str) -> bool:
    return any(
        token == "--in-place" or token.startswith(("--in-place=", "-i"))
        for token in safe_split(stage)[1:]
    )


def _sed_edit_path(stage: str) -> str | None:
    """Return the first file operand after sed's expression arguments."""

    tokens = safe_split(stage)[1:]
    operands: list[str] = []
    skip_next = False
    expression_is_explicit = False
    for index, token in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        if token in {"-e", "--expression", "-f", "--file"}:
            expression_is_explicit = True
            skip_next = True
            continue
        if token.startswith(("--expression=", "--file=")) or (
            token.startswith(("-e", "-f")) and len(token) > 2
        ):
            expression_is_explicit = True
            continue
        if token in {"-i", "--in-place"}:
            if index + 1 < len(tokens) and tokens[index + 1] == "":
                skip_next = True
            continue
        if token.startswith("-"):
            continue
        operands.append(token)
    file_index = 0 if expression_is_explicit else 1
    return operands[file_index] if len(operands) > file_index else None


def _stdout_redirect_path(stage: str) -> str | None:
    """Return a regular-file target of the stage's stdout redirection."""

    tokens = safe_split(stage)
    for index, token in enumerate(tokens):
        match = re.fullmatch(r"(?:1)?(?:>>|>\||>)(.*)", token)
        if match is None:
            continue
        target = match.group(1)
        if not target and index + 1 < len(tokens):
            target = tokens[index + 1]
        if target and target != "/dev/null":
            return target
    return None


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
