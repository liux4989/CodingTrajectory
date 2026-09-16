"""Codex parse state and the shared evidence rules over it.

Owns the mutable per-file parse state (`CodexParseState`) together with the
legacy ``exec`` wrapper state (`_PendingExecWrapper`) it correlates, plus the
small evidence-coercion rules that both adapter orchestration (``codex.py``)
and native-item reconstruction (``codex_native_items.py``/``codex_collab.py``)
must apply identically: shell-command text normalization and match keys,
command-origin classification, and tool/wrapper status reading. Keeping these
next to the state removes the previous back-imports of ``CodexAdapter`` and
its private helpers from the reconstruction modules.

This module imports nothing from the other Codex adapter modules.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from coding_trajectory.ingestion.adapters._shared import non_empty_str
from coding_trajectory.ingestion.adapters.codex_exec_parser import (
    StaticExecInvocation,
)
from coding_trajectory.ingestion.common import infer_tool_success
from coding_trajectory.ingestion.models import (
    ContextSourceObservation,
    ContextUsageObservation,
    RuntimeObservation,
    ToolStatus,
)
from coding_trajectory.ingestion.transcript import TranscriptRecord

_as_non_empty_str = non_empty_str

_CODEX_GROUPABLE_COMMAND_SOURCES: frozenset[str] = frozenset(
    {"agent", "unified_exec_startup"}
)


@dataclass
class _PendingExecWrapper:
    """A static ``exec`` code cell awaiting its wrapper result."""

    call_id: str
    started_at: datetime
    call_record: TranscriptRecord
    invocations: list[StaticExecInvocation]
    turn_id: str | None = None
    matched_native_indices: set[int] = field(default_factory=set)
    derived_records: dict[int, TranscriptRecord] = field(default_factory=dict)
    closed: bool = False
    completed_at: datetime | None = None


@dataclass
class CodexParseState:
    session_meta: dict[str, Any] = field(default_factory=dict)
    turn_context: dict[str, Any] = field(default_factory=dict)
    session_id: UUID = field(default_factory=uuid4)
    context_window_tokens: int | None = None
    context_usage: list[ContextUsageObservation] = field(default_factory=list)
    runtime_observations: list[RuntimeObservation] = field(default_factory=list)
    # The first persisted user message is a display preview, never an
    # inferred thread name. Current Codex rollouts can encode it as either
    # a legacy user_message event or a native UserMessage item.
    session_preview: str | None = None
    # One canonical user request per provider lifecycle turn, regardless of
    # whether legacy and native message records are both present.
    projected_turn_ids: set[str] = field(default_factory=set)
    # Most recent reasoning effort seen on a turn_context record (real
    # string only). Drives effort_changed observation emission: a new turn
    # whose effort differs from this baseline marks a cache-key change-point.
    prev_effort: str | None = None
    # Full content-free field hashes from the prior turn_context. Stable
    # turns omit the repeated mapping from the Chronicle artifact while
    # retaining a timestamped snapshot as an evidence-coverage marker.
    prev_runtime_config_hashes: dict[str, str] | None = None
    multi_agent_version: str | None = None
    multi_agent_mode: str | None = None
    # Last cumulative ``total_token_usage`` seen on a Codex token_count
    # event. Codex occasionally re-emits an identical snapshot (cumulative
    # unchanged, last_token_usage repeated) for a non-billable repeat;
    # tracking the prior lets us drop the stale copy before accounting.
    prev_total_token_usage: dict[str, int] | None = None
    # One resident slot per (role, block_name); first emission wins. Codex
    # re-injects base/developer/AGENTS.md blocks after each compaction, so
    # per-block dedup keeps only the first (resident-from-first-injection)
    # copy — its timestamp drives per-call cost attribution.
    context_source_by_block: dict[tuple[str, str], ContextSourceObservation] = field(
        default_factory=dict
    )
    # child agent_thread_id -> spawn tool-call call_id, captured from
    # sub_agent_activity{kind:started} events. Backs the forked_from edge
    # origin with the real spawn call instead of the parent's last tool call.
    spawn_links: dict[str, str] = field(default_factory=dict)
    # Open ``custom_tool_call(name=exec)`` wrapper cells that passed the
    # strict static recognizer. Native Codex items can attach before the
    # wrapper output arrives; older JSONL falls back to derived-static
    # activities at wrapper completion.
    pending_exec_wrappers: dict[str, _PendingExecWrapper] = field(default_factory=dict)
    # Raw terminal identities are needed only while reconstructing legacy
    # exec wrappers. Map each one to a token derived from an already-public
    # tool call id so measurements retention can group polls without
    # retaining a reversible digest of the process/session identifier.
    background_terminal_group_tokens: dict[tuple[str, str, int], str] = field(
        default_factory=dict
    )
    # Visible assistant output flushes Codex's active terminal-wait streak.
    # Compact items discard that text, so include a content-free epoch in
    # wait grouping markers to preserve the same boundary.
    activity_cell_epoch: int = 0
    # Every custom ``exec`` call, including cells whose JavaScript cannot
    # be statically parsed. Its wrapper result can still be failed even
    # though it gives no nested-tool outcome.
    exec_wrapper_call_ids: set[str] = field(default_factory=set)
    # Direct function calls sometimes receive a terminal ThreadItem whose
    # item id is exactly the response-item call id (for example,
    # ``spawn_agent`` -> ``SubAgentActivity``). Keep the original call as
    # the canonical action and enrich it from that stronger terminal fact.
    direct_function_calls: dict[str, TranscriptRecord] = field(default_factory=dict)
    native_direct_result_records: dict[str, TranscriptRecord] = field(
        default_factory=dict
    )
    native_direct_output_authoritative: set[str] = field(default_factory=set)
    # Native CommandExecution ids already emitted from item_started. A
    # later item_completed updates the same canonical item rather than
    # creating a second command activity.
    native_command_ids: set[str] = field(default_factory=set)
    # Native CommandExecution id -> static wrapper invocation. Needed when
    # an item_started arrives after an old wrapper's derived placeholder.
    native_command_bindings: dict[str, tuple[_PendingExecWrapper, int]] = field(
        default_factory=dict
    )
    # Native non-command item ids and their optional static-wrapper child
    # binding. The tuple key keeps FileChange/Plan/WebSearch ids separate
    # even if a provider reuses an identifier across item variants.
    native_activity_ids: set[tuple[str, str]] = field(default_factory=set)
    native_activity_bindings: dict[tuple[str, str], tuple[_PendingExecWrapper, int]] = (
        field(default_factory=dict)
    )


def _native_command_text(value: Any) -> str | None:
    """Normalize a native CommandExecution payload to its shell command text."""

    if isinstance(value, str) and value.strip():
        return value.strip()
    if not isinstance(value, list):
        return None
    parts = [part for part in value if isinstance(part, str)]
    for index, part in enumerate(parts[:-1]):
        if part == "-lc" and parts[index + 1].strip():
            return parts[index + 1].strip()
    return " ".join(parts).strip() or None


def _command_match_key(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _codex_command_activity_source(value: Any) -> str:
    """Map Codex's native command origin to the shared cell authority.

    Codex TUI groups only agent and unified-exec-startup commands. Historical
    user-shell and unrecognized sources remain individual boundaries.
    """

    source = _as_non_empty_str(value)
    if source is not None and source.lower() in _CODEX_GROUPABLE_COMMAND_SOURCES:
        return "agent"
    return "unknown"


def _tool_status(
    value: Any, *, default: ToolStatus = ToolStatus.REQUESTED
) -> ToolStatus:
    normalized = (
        re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value).replace("-", "_").lower()
        if isinstance(value, str)
        else None
    )
    if normalized == "completed":
        return ToolStatus.COMPLETED
    if normalized in {"failed", "declined"}:
        return ToolStatus.FAILED
    if normalized == "in_progress":
        return ToolStatus.IN_PROGRESS
    return default


def _tool_result_status(
    payload: dict[str, Any], output: Any, *, exec_wrapper: bool = False
) -> ToolStatus:
    if isinstance(payload.get("success"), bool):
        return ToolStatus.COMPLETED if payload["success"] else ToolStatus.FAILED
    status = _tool_status(payload.get("status"), default=ToolStatus.COMPLETED)
    if status != ToolStatus.COMPLETED:
        return status
    # Custom ``exec`` cells often keep their own transport status as
    # ``completed`` even when the JavaScript body failed.  This establishes
    # only the wrapper's result—it must never be applied to a statically
    # reconstructed nested action.
    if exec_wrapper and _is_exec_wrapper_failure(output):
        return ToolStatus.FAILED
    success = infer_tool_success(output)
    return ToolStatus.FAILED if success is False else ToolStatus.COMPLETED


def _walk_text_values(value: Any) -> Iterator[str]:
    """Yield text leaves from a JSON-like tool result without coercing data."""

    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _walk_text_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_text_values(nested)


def _is_exec_syntax_error(output: Any) -> bool:
    """Return whether a failed exec wrapper could not parse before running.

    A failed wrapper normally cannot establish the outcome of a nested call:
    post-processing such as ``text(r.content)`` can fail after a native action
    succeeded. A JavaScript syntax error is different—the body never executes,
    so the raw ``exec`` failure must remain visible rather than becoming a
    derived unknown action.
    """

    return any(
        "script error" in text.lower() and "syntaxerror" in text.lower()
        for text in _walk_text_values(output)
    )


def _is_exec_wrapper_failure(output: Any) -> bool:
    """Return whether a custom exec wrapper reports its own failure."""

    return any(
        "script failed" in text.lower() or "script error:" in text.lower()
        for text in _walk_text_values(output)
    )


def _has_explicit_exec_wrapper_result(output: Any) -> bool:
    """Return whether an exec wrapper carries result content beyond its banner.

    ``Script completed`` is a runtime status for the JavaScript wrapper, not
    outcome evidence for a nested call.  A single lexically known nested call
    can instead use its wrapper output as a historical fallback only when the
    wrapper also persisted actual result content.
    """

    for text in _walk_text_values(output):
        cleaned = re.sub(
            r"^\s*script completed\s*\n(?:wall time[^\n]*\n)?output:\s*",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
        if cleaned:
            return True
    return False
