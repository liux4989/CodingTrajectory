"""CLI-local command outcome normalization."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any

_MISSING = object()


@dataclass(frozen=True)
class CommandOutcome:
    """Explicit handler outcome: execution health, exit status, and optional payload."""

    exit_code: int = 0
    ok: bool = True
    payload: Any = _MISSING
    error: str | None = None

    @property
    def has_payload(self) -> bool:
        return self.payload is not _MISSING

    @classmethod
    def payload_result(
        cls,
        payload: Any,
        *,
        exit_code: int = 0,
    ) -> CommandOutcome:
        return cls(exit_code=exit_code, ok=True, payload=payload)

    @classmethod
    def completed(
        cls,
        *,
        exit_code: int = 0,
        payload: Any = _MISSING,
    ) -> CommandOutcome:
        return cls(exit_code=exit_code, ok=True, payload=payload)

    @classmethod
    def failed(
        cls,
        *,
        exit_code: int = 1,
        error: str | None = None,
        payload: Any = _MISSING,
    ) -> CommandOutcome:
        return cls(exit_code=exit_code, ok=False, payload=payload, error=error)


@dataclass(frozen=True)
class EarlyDispatchOutcome:
    """Result for argv paths handled before argparse dispatch."""

    command: str
    outcome: CommandOutcome


def normalize_handler_result(
    args: argparse.Namespace,
    result: Any,
) -> CommandOutcome:
    if isinstance(result, CommandOutcome):
        return result
    if isinstance(result, int) and not isinstance(result, bool):
        return _legacy_status_outcome(command_path(args), result)
    return CommandOutcome.payload_result(result)


def command_path(args: argparse.Namespace) -> str:
    command = getattr(args, "command", None) or ""
    action = getattr(args, "action", None)
    if action:
        return f"{command}.{action}"
    plugin_action = getattr(args, "plugin_action", None)
    if plugin_action:
        return f"{command}.{plugin_action}"
    return command or "unknown"


def status_error(command: str, exit_code: int) -> str:
    return f"{command} exited with status {exit_code}"


def _legacy_status_outcome(command: str, exit_code: int) -> CommandOutcome:
    if exit_code == 0:
        return CommandOutcome.completed(exit_code=exit_code)
    return CommandOutcome.failed(
        exit_code=exit_code,
        error=status_error(command, exit_code),
    )
