"""Codex ingestion state and shared provider evidence coercions.

Native item decoding and adapter orchestration share command-origin, literal
command, and status normalization. Source inspection and script ownership live
in the shared execution layer, independently of provider-native decoding.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from coding_trajectory.ingestion.adapters._shared import non_empty_str
from coding_trajectory.ingestion.common import infer_tool_success
from coding_trajectory.ingestion.models import (
    ContextSourceObservation,
    ContextUsageObservation,
    RuntimeObservation,
    ToolStatus,
)

_as_non_empty_str = non_empty_str

_CODEX_GROUPABLE_COMMAND_SOURCES: frozenset[str] = frozenset(
    {"agent", "unified_exec_startup"}
)


@dataclass
class CodexParseState:
    session_meta: dict[str, Any] = field(default_factory=dict)
    turn_context: dict[str, Any] = field(default_factory=dict)
    session_id: UUID = field(default_factory=uuid4)
    context_window_tokens: int | None = None
    context_usage: list[ContextUsageObservation] = field(default_factory=list)
    runtime_observations: list[RuntimeObservation] = field(default_factory=list)
    compaction_history: list[ContextSourceObservation] = field(default_factory=list)
    # Older rollouts follow a compacted record with a context_compacted event.
    # Pair once across metadata records, never across substantive activity.
    pending_compaction_event: bool = False
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
    # Every custom ``exec`` call, including cells whose JavaScript cannot
    # be statically parsed. Its wrapper result can still be failed even
    # though it gives no nested-tool outcome.
    exec_wrapper_call_ids: set[str] = field(default_factory=set)
    # Native CommandExecution ids already emitted from item_started. A
    # later item_completed updates the same canonical item rather than
    # creating a second command activity.
    native_command_ids: set[str] = field(default_factory=set)
    native_activity_ids: set[tuple[str, str]] = field(default_factory=set)


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
    # only the wrapper's result; external calls retain their own outcomes.
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


def _is_exec_wrapper_failure(output: Any) -> bool:
    """Return whether a custom exec wrapper reports its own failure."""

    return any(
        "script failed" in text.lower() or "script error:" in text.lower()
        for text in _walk_text_values(output)
    )
