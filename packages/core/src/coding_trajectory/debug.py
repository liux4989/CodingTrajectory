"""Per-invocation debug context and the public ``debug.warn`` primitive.

The CLI creates one :class:`DebugContext` per invocation and installs it on a
contextvar. Any layer (adapter, analysis, metrics, service) may call the
module-level :func:`warn` to append a structured warning to the active
context.

Behavior outside an active :func:`debug_scope`:

* :func:`warn` is a **silent no-op** — it does not raise, log, or accumulate.
  Library code can therefore emit warnings unconditionally without knowing
  whether the caller is the CLI.
* Non-CLI consumers (dashboard plugin, tests, external scripts) that want to
  observe warnings must open their own scope with ``with debug.debug_scope() as ctx:``
  and read ``ctx.as_records()`` afterwards.
"""

from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass, field
from typing import Any

MAX_WARNINGS = 256

_current_context: contextvars.ContextVar["DebugContext | None"] = contextvars.ContextVar(
    "coding_trajectory.debug", default=None
)


@dataclass
class DebugWarning:
    message: str
    code: str | None
    severity: str
    context: dict[str, Any]

    def as_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "message": self.message,
            "severity": self.severity,
        }
        if self.code is not None:
            record["code"] = self.code
        if self.context:
            record["context"] = dict(self.context)
        return record


@dataclass
class DebugContext:
    warnings: list[DebugWarning] = field(default_factory=list)
    dropped: int = 0

    def warn(
        self,
        message: str,
        *,
        code: str | None = None,
        severity: str = "warning",
        **context: Any,
    ) -> None:
        if severity not in {"info", "warning", "error"}:
            severity = "warning"
        if len(self.warnings) >= MAX_WARNINGS:
            self.dropped += 1
            return
        self.warnings.append(
            DebugWarning(
                message=str(message),
                code=code,
                severity=severity,
                context={key: value for key, value in context.items() if value is not None},
            )
        )

    def as_records(self) -> list[dict[str, Any]]:
        return [warning.as_record() for warning in self.warnings]


@contextlib.contextmanager
def debug_scope():
    """Install a fresh :class:`DebugContext` for the duration of the block."""

    context = DebugContext()
    token = _current_context.set(context)
    try:
        yield context
    finally:
        _current_context.reset(token)


def warn(
    message: str,
    *,
    code: str | None = None,
    severity: str = "warning",
    **context: Any,
) -> None:
    """Append a warning to the active :class:`DebugContext`, if any."""

    active = _current_context.get()
    if active is None:
        return
    active.warn(message, code=code, severity=severity, **context)


def current() -> DebugContext | None:
    return _current_context.get()
