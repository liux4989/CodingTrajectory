"""Command evidence for the summary verification section, not tool categories."""

from __future__ import annotations

import os
import re
from typing import Any, Literal

from coding_trajectory.analysis.shell_parser import split_shell_stages
from coding_trajectory.analysis.tool_summary_shared import (
    safe_split,
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
SHELL_CHAIN_PREFIX = "Shell chain: "


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
