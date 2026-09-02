"""Codex CLI adapter — reads ~/.codex/sessions/**/*.jsonl rollout files."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from itertools import chain
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from coding_trajectory.ingestion.adapters._shared import (
    SHARED_FILE_TOOL_NAMES,
    SHARED_PLAN_TOOL_NAMES,
    ToolTaxonomy,
    content_block_texts,
    extract_uuid_text,
    non_empty_str,
    preview_text,
)
from coding_trajectory.ingestion.adapters.base import BaseAdapter, SessionHeader
from coding_trajectory.ingestion.adapters.codex_exec_parser import (
    StaticExecInvocation,
    extract_static_exec_invocations,
)
from coding_trajectory.ingestion.assembly import AssemblyHooks, assemble_session
from coding_trajectory.ingestion.common import (
    extract_exit_code,
    infer_tool_success,
    parse_iso_timestamp,
    source_is_living,
)
from coding_trajectory.ingestion.models import (
    ContextSourceObservation,
    ContextUsageObservation,
    RuntimeObservation,
    Session,
    SessionStatus,
    ToolStatus,
    TurnStatus,
    Vendor,
)
from coding_trajectory.ingestion.provenance import RecordSpan, SessionProvenance
from coding_trajectory.ingestion.retention import CanonicalRetention
from coding_trajectory.ingestion.transcript import TranscriptRecord
from coding_trajectory.ingestion.vendor_mechanisms.codex_multi_agent import (
    CodexMultiAgentInput,
    CodexThreadSpawn,
)
from coding_trajectory.ingestion.vendor_mechanisms.codex_multi_agent import (
    extensions as codex_extensions,
)
from coding_trajectory.ingestion.vendor_mechanisms.codex_multi_agent import (
    parent_session_id as codex_parent_session_id,
)
from coding_trajectory.ingestion.vendor_mechanisms.usage_metrics import (
    context_usage_observation,
    normalize_codex_token_count,
)

logger = logging.getLogger(__name__)

_DEFAULT_CODEX_SESSION_INDEX = Path.home() / ".codex" / "session_index.jsonl"
_CODEX_PREVIEW_MAX_LEN = 96

_CODEX_TOOL_TAXONOMY = ToolTaxonomy(
    plan_names=SHARED_PLAN_TOOL_NAMES,
    file_change_names=SHARED_FILE_TOOL_NAMES
    | frozenset(
        {
            "read_file",
            "read_many_files",
            "replace",
            "write_file",
            "edit_file",
            "create_file",
            "apply_patch",
        }
    ),
)

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


def _codex_item_kind(*, tool_name: str | None, inner_type: str) -> str:
    # Codex native inner types outrank the tool-name taxonomy.
    if inner_type == "local_shell_call":
        return "command_execution"
    if inner_type == "reasoning":
        return "reasoning"
    return _CODEX_TOOL_TAXONOMY.classify(tool_name)


def _parse_json_blob(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


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


def _extract_message_text(payload: dict[str, Any]) -> str | None:
    message = payload.get("message")
    return message if isinstance(message, str) and message else None


def _extract_response_text(payload: dict[str, Any]) -> str | None:
    content = payload.get("content")
    if not isinstance(content, list):
        return None
    return content_block_texts(content, text_type="output_text")


_as_non_empty_str = non_empty_str


def _extract_nested_map(payload: dict[str, Any], *keys: str) -> dict[str, Any] | None:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if isinstance(current, dict) else None


_extract_uuid_text = extract_uuid_text


def _codex_session_title(
    session_id: UUID,
    index_path: Path = _DEFAULT_CODEX_SESSION_INDEX,
) -> str | None:
    """Return the explicit Codex thread name from its local name index only."""
    if not index_path.is_file():
        return None

    title: str | None = None
    try:
        with index_path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("id") != str(session_id):
                    continue
                candidate = _as_non_empty_str(
                    record.get("thread_name")
                ) or _as_non_empty_str(record.get("title"))
                if candidate is not None:
                    title = candidate
    except OSError:
        return None
    return title


def _codex_session_preview(transcript: Iterable[TranscriptRecord]) -> str | None:
    """Return a bounded first-user-message preview without inventing a title."""
    for record in transcript:
        if record.kind != "user_message":
            continue
        text = _codex_preview_text(record.data.get("text"))
        if text is None:
            continue
        return text
    return None


def _codex_preview_text(value: Any) -> str | None:
    return preview_text(value, max_len=_CODEX_PREVIEW_MAX_LEN)


def _extract_content_text(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    texts = [
        part.get("text")
        for part in value
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    return " ".join(text for text in texts if text).strip() or None


def _capture_codex_session_preview(state: Any, value: Any) -> None:
    if state.session_preview is None:
        state.session_preview = _codex_preview_text(value)


def _codex_multi_agent_input(
    meta: dict[str, Any], ctx: dict[str, Any], *, session_id: UUID
) -> CodexMultiAgentInput:
    sandbox_policy = (
        ctx.get("sandbox_policy") if isinstance(ctx.get("sandbox_policy"), dict) else {}
    )
    source = meta.get("source")
    thread_spawn_raw = (
        _extract_nested_map(source, "subagent", "thread_spawn")
        if isinstance(source, dict)
        else None
    )
    source_name = _as_non_empty_str(source)
    collaboration_mode = ctx.get("collaboration_mode")
    return CodexMultiAgentInput(
        sandbox_id=_as_non_empty_str(meta.get("id")),
        sandbox_mode=_as_non_empty_str(sandbox_policy.get("type")),
        approval_policy=_as_non_empty_str(ctx.get("approval_policy")),
        collaboration_mode=(
            _as_non_empty_str(collaboration_mode.get("mode"))
            if isinstance(collaboration_mode, dict)
            else _as_non_empty_str(collaboration_mode)
        ),
        multi_agent_version=_as_non_empty_str(
            ctx.get("multi_agent_version") or meta.get("multi_agent_version")
        ),
        multi_agent_mode=_as_non_empty_str(
            ctx.get("multi_agent_mode") or meta.get("multi_agent_mode")
        ),
        agent_path=_as_non_empty_str(meta.get("agent_path"))
        or (
            _as_non_empty_str(thread_spawn_raw.get("agent_path"))
            if thread_spawn_raw is not None
            else None
        ),
        agent_nickname=_as_non_empty_str(meta.get("nickname"))
        or _as_non_empty_str(meta.get("agent_nickname")),
        agent_role=_as_non_empty_str(meta.get("agent_role")) or source_name,
        cwd=_as_non_empty_str(meta.get("cwd")),
        title=_codex_session_title(session_id),
        forked_from_id=_extract_uuid_text(meta.get("forked_from_id")),
        thread_spawn=(
            CodexThreadSpawn(
                parent_thread_id=_extract_uuid_text(
                    thread_spawn_raw.get("parent_thread_id")
                ),
                depth=thread_spawn_raw.get("depth")
                if isinstance(thread_spawn_raw.get("depth"), int)
                else None,
                agent_path=_as_non_empty_str(thread_spawn_raw.get("agent_path")),
                agent_nickname=_as_non_empty_str(
                    thread_spawn_raw.get("agent_nickname")
                ),
                agent_role=_as_non_empty_str(thread_spawn_raw.get("agent_role")),
            )
            if thread_spawn_raw is not None
            else None
        ),
    )


def _session_forked_from_id(records: Iterable[dict]) -> str | None:
    for record in records:
        if record.get("type") != "session_meta":
            continue
        ffid = (
            record.get("payload", {}).get("forked_from_id")
            if isinstance(record.get("payload"), dict)
            else None
        )
        return _extract_uuid_text(ffid)
    return None


def _iter_own_records(
    records: Iterable[tuple[dict, RecordSpan]],
    parent_started_turn_ids: set[str],
) -> Iterator[tuple[dict, RecordSpan]]:
    """Stream ``_cut_inherited_records`` semantics without materializing records.

    Keeps the leading ``session_meta`` prefix, drops the inherited segment a
    forked rollout re-materializes, and passes through everything from the
    first foreign ``task_started`` on.  Non-forks pass through unchanged.
    """
    iterator = iter(records)
    head: list[tuple[dict, RecordSpan]] = []
    leading = 0
    prefix_open = True
    meta_seen = False
    forked_from: str | None = None
    for pair in iterator:
        record = pair[0]
        head.append(pair)
        if record.get("type") == "session_meta":
            if not meta_seen:
                meta_seen = True
                payload = record.get("payload")
                ffid = (
                    payload.get("forked_from_id") if isinstance(payload, dict) else None
                )
                forked_from = _extract_uuid_text(ffid)
        else:
            prefix_open = False
        if prefix_open:
            leading += 1
        if meta_seen and not prefix_open:
            break
    if not meta_seen or forked_from is None:
        yield from head
        yield from iterator
        return
    yield from head[:leading]
    for record, _span in chain(head[leading:], iterator):
        payload = record.get("payload") or {}
        if payload.get("type") != "task_started":
            continue
        turn_id = payload.get("turn_id")
        if isinstance(turn_id, str) and turn_id not in parent_started_turn_ids:
            yield record, _span
            break
    else:
        # Fork has no own turns (inherited only): keep just its session_meta.
        return
    yield from iterator


def _cut_inherited_records(
    records: list[dict], parent_started_turn_ids: set[str] | None
) -> list[dict]:
    """Drop the inherited-history segment a forked rollout re-materializes.

    A forked continuation window copies the source's recent turns verbatim
    (including their ``task_started``/``task_complete``/``token_count``/
    ``sub_agent_activity`` records). Re-projecting that copy double-counts
    turns/tokens and re-emits inherited spawn edges. The fork's own turns begin
    at the first ``task_started`` whose ``turn_id`` is absent from the parent's
    raw ``task_started`` set (validated: a clean cut for every fork - all
    preceding turns are inherited, all from here are own, and no ``spawn_agent``
    call lands in the dropped segment).

    The leading ``session_meta`` record(s) are always kept (they are the fork's
    own). When the parent set is unavailable (single-file ingestion) or the file
    is not a fork, records are returned unchanged.
    """
    if parent_started_turn_ids is None:
        return records
    if _session_forked_from_id(records) is None:
        return records
    leading = 0
    for record in records:
        if record.get("type") == "session_meta":
            leading += 1
            continue
        break
    first_foreign = None
    for index in range(leading, len(records)):
        payload = records[index].get("payload") or {}
        if payload.get("type") != "task_started":
            continue
        turn_id = payload.get("turn_id")
        if isinstance(turn_id, str) and turn_id not in parent_started_turn_ids:
            first_foreign = index
            break
    if first_foreign is None:
        # Fork has no own turns (inherited only): keep just its session_meta.
        return records[:leading]
    return records[:leading] + records[first_foreign:]


def _derive_session_status(turns: list) -> SessionStatus:
    """Map a thread's current turn state to reversible session liveness."""

    return (
        SessionStatus.LIVING
        if any(turn.status == TurnStatus.RUNNING for turn in turns)
        else SessionStatus.NOT_LIVING
    )


def _codex_prompt_block_name(text: str, index: int) -> str:
    stripped = text.lstrip()
    if stripped.startswith("<") and ">" in stripped:
        tag = stripped[1 : stripped.index(">")].strip().split()[0]
        if tag:
            return tag
    return f"developer_block_{index}"


def _codex_user_prompt_block_name(text: str) -> str | None:
    stripped = text.lstrip()
    if stripped.startswith("# AGENTS.md instructions"):
        return "agents_md"
    return None


_CONTEXT_SOURCE_LABELS = {
    "base_system": "Base instructions",
    "developer_instructions": "Developer instructions",
    "agents_md": "AGENTS.md",
    "skills": "Skills",
    "mcp": "Tools / MCP",
    "memory": "Memory",
}


def _codex_context_source_key(*, block: str, role: str, text: str) -> str:
    haystack = f"{block}\n{text}".lower()
    if block == "base_instructions":
        return "base_system"
    if "agents.md" in haystack:
        return "agents_md"
    if "skills_instructions" in block or "### available skills" in haystack:
        return "skills"
    if "plugins_instructions" in block or "### available plugins" in haystack:
        return "mcp"
    if (
        "memory_summary" in haystack
        or "memory layout" in haystack
        or "## memory" in haystack
    ):
        return "memory"
    if "mcp" in haystack or "tools are grouped" in haystack:
        return "mcp"
    if role == "developer":
        return "developer_instructions"
    return "base_system"


def _context_source_observation(
    *,
    timestamp: Any,
    block: str,
    role: str,
    text: str,
) -> ContextSourceObservation:
    key = _codex_context_source_key(block=block, role=role, text=text)
    return ContextSourceObservation(
        timestamp=timestamp,
        key=key,
        label=_CONTEXT_SOURCE_LABELS[key],
        text=text,
        source="codex_prompt_block",
    )


def _record_context_source(
    state: Any,
    observation: ContextSourceObservation,
    *,
    block: str,
    role: str,
) -> None:
    """Keep one observation per (role, block_name); first emission wins.

    Codex re-injects the base/developer/AGENTS.md prompt blocks after a context
    compaction. Each re-injection shares the same (role, block_name) identity as
    the resident prefix block, so per-block dedup collapses them. The first
    emission is kept: the block is resident from first injection through end of
    session (Codex re-attaches it after every compaction), so the earliest
    timestamp is what makes the accounting attribute its per-call cost across
    every API call that carried the block.
    """
    state.context_source_by_block.setdefault((role, block), observation)


class CodexAdapter(BaseAdapter):
    """Ingest Codex CLI JSONL rollout files from ~/.codex/sessions/."""

    vendor = Vendor.CODEX_CLI

    @dataclass
    class _ParseState:
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
        projected_turn_ids: set[str] = field(default_factory=set)
        # Most recent reasoning effort seen on a turn_context record (real
        # string only). Drives effort_changed observation emission: a new turn
        # whose effort differs from this baseline marks a cache-key change-point.
        prev_effort: str | None = None
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
        context_source_by_block: dict[tuple[str, str], ContextSourceObservation] = (
            field(default_factory=dict)
        )
        # child agent_thread_id -> spawn tool-call call_id, captured from
        # sub_agent_activity{kind:started} events. Backs the forked_from edge
        # origin with the real spawn call instead of the parent's last tool call.
        spawn_links: dict[str, str] = field(default_factory=dict)
        # Open ``custom_tool_call(name=exec)`` wrapper cells that passed the
        # strict static recognizer. Native Codex items can attach before the
        # wrapper output arrives; older JSONL falls back to derived-static
        # activities at wrapper completion.
        pending_exec_wrappers: dict[str, _PendingExecWrapper] = field(
            default_factory=dict
        )
        # Every custom ``exec`` call, including cells whose JavaScript cannot
        # be statically parsed. Its wrapper result can still be failed even
        # though it gives no nested-tool outcome.
        exec_wrapper_call_ids: set[str] = field(default_factory=set)
        # Direct function calls sometimes receive a terminal ThreadItem whose
        # item id is exactly the response-item call id (for example,
        # ``spawn_agent`` -> ``SubAgentActivity``). Keep the original call as
        # the canonical action and enrich it from that stronger terminal fact.
        direct_function_calls: dict[str, TranscriptRecord] = field(
            default_factory=dict
        )
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
        native_activity_bindings: dict[
            tuple[str, str], tuple[_PendingExecWrapper, int]
        ] = field(default_factory=dict)

    def ingest_file(
        self,
        path: Path,
        *,
        parent_started_turn_ids: set[str] | None = None,
        retention: CanonicalRetention = "trajectory",
    ) -> Session:
        self._reset_ingest_state()
        self.last_provenance: SessionProvenance | None = None
        if retention == "measurements":
            records: Iterable[tuple[dict, RecordSpan | None]] = self._iter_record_spans(
                path
            )
            if parent_started_turn_ids is not None:
                records = _iter_own_records(records, parent_started_turn_ids)
        else:
            records = (
                (record, None)
                for record in _cut_inherited_records(
                    self._load_records(path), parent_started_turn_ids
                )
            )
        state = self._ParseState()
        transcript = self._build_transcript(records, state)
        return self._build_session(path, transcript, state, retention=retention)

    def build_canonical_session(
        self,
        source: Path,
        records: Iterable[dict],
        *,
        parent_started_turn_ids: set[str] | None = None,
    ) -> Session:
        """In-memory-record seam: cut inherited fork history, then assemble."""
        self._reset_ingest_state()
        self.last_provenance: SessionProvenance | None = None
        cut = _cut_inherited_records(list(records), parent_started_turn_ids)
        state = self._ParseState()
        transcript = self._build_transcript(
            ((record, None) for record in cut), state
        )
        return self._build_session(source, transcript, state, retention="trajectory")

    def scan_started_turn_ids(self, source: Path) -> set[str] | None:
        started: set[str] = set()
        for record in self._iter_records(source):
            payload = record.get("payload") or {}
            if payload.get("type") == "task_started" and isinstance(
                payload.get("turn_id"), str
            ):
                started.add(payload["turn_id"])
        return started

    def scan_identity(self, source: Path) -> SessionHeader | None:
        """Read the leading ``session_meta`` without searching for a title."""

        for record in self._iter_records(source):
            if record.get("type") != "session_meta":
                continue
            meta = record.get("payload") or {}
            if not isinstance(meta, dict):
                return None
            try:
                session_id = UUID(meta.get("id"))
            except (ValueError, TypeError):
                return None
            mechanism = _codex_multi_agent_input(meta, {}, session_id=session_id)
            return SessionHeader(
                session_id=session_id,
                vendor=Vendor.CODEX_CLI,
                parent_session_id=codex_parent_session_id(mechanism),
                title=mechanism.title,
                cwd=mechanism.cwd,
            )
        return None

    def scan_header(self, source: Path) -> SessionHeader | None:
        """Read static identity and explicit title without deriving one from a message."""
        return self.scan_identity(source)

    def _build_session(
        self,
        source: Path,
        transcript: list[TranscriptRecord],
        state: _ParseState | None = None,
        *,
        retention: CanonicalRetention = "trajectory",
    ) -> Session:
        state = state or self._ParseState()
        if not transcript:
            raise ValueError(
                f"CodexAdapter: no transcript records parsed from {source}"
            )

        meta = state.session_meta
        ctx = state.turn_context
        mechanism = _codex_multi_agent_input(meta, ctx, session_id=state.session_id)
        if state.multi_agent_version is not None:
            mechanism.multi_agent_version = state.multi_agent_version
        if state.multi_agent_mode is not None:
            mechanism.multi_agent_mode = state.multi_agent_mode
        parent_session_id = codex_parent_session_id(mechanism)
        extensions = codex_extensions(mechanism)
        if extensions.codex is not None:
            extensions.codex.preview = state.session_preview or _codex_session_preview(
                transcript
            )
            if state.spawn_links:
                extensions.codex.spawn_links = dict(state.spawn_links)

        hooks = AssemblyHooks(
            active_status=(
                TurnStatus.RUNNING
                if source_is_living(source)
                else TurnStatus.INCOMPLETE
            ),
            default_previous_turn_status=TurnStatus.INTERRUPTED,
            # Codex's authoritative turn delimiter is the task_started/task_complete
            # lifecycle boundary; user_message is an in-turn item. Prefer lifecycle
            # mode so turns (incl. compaction-only turns) project correctly and
            # spawn calls are turn-attributed.
            prefer_lifecycle=True,
            extensions=extensions,
            parent_session_id=parent_session_id,
            runtime_observations=state.runtime_observations,
            session_fields={
                "model": _as_non_empty_str(ctx.get("model")),
                "reasoning_effort": _as_non_empty_str(ctx.get("effort")),
                "agent_name": extensions.codex.agent_nickname
                if extensions.codex
                else None,
            },
            build_context_usage=lambda _records: state.context_usage,
            build_context_sources=lambda _context: list(
                state.context_source_by_block.values()
            ),
            build_session_fields=lambda context: {
                "status": _derive_session_status(context.turns)
            },
            provenance_sink=lambda provenance: setattr(
                self, "last_provenance", provenance
            ),
        )
        return assemble_session(
            vendor=Vendor.CODEX_CLI,
            source=source,
            session_id=state.session_id,
            transcript=transcript,
            retention=retention,
            hooks=hooks,
        )

    def _build_transcript(
        self,
        records: Iterable[tuple[dict, RecordSpan | None]],
        state: _ParseState,
    ) -> list[TranscriptRecord]:
        """Extract only CT-useful transcript facts from Codex JSONL records."""
        transcript: list[TranscriptRecord] = []
        for record, span in records:
            before = len(transcript)
            self._translate_record(record, state, transcript)
            if span is not None:
                for entry in transcript[before:]:
                    entry.origin = span
        return transcript

    def _translate_record(
        self,
        record: dict,
        state: _ParseState,
        transcript: list[TranscriptRecord],
    ) -> None:
        outer_type = record.get("type", "")
        payload = record.get("payload") or {}
        ts = parse_iso_timestamp(record.get("timestamp"))

        if outer_type == "session_meta":
            self._handle_session_meta(payload, ts, state, transcript)
            return

        if outer_type == "turn_context":
            self._handle_turn_context(payload, ts, state)
            return

        if ts is None:
            return

        if outer_type == "event_msg":
            self._handle_event_msg(payload, ts, state, transcript)

        elif outer_type == "response_item":
            self._handle_response_item(payload, ts, state, transcript)

        elif outer_type == "compacted":
            self._handle_compacted(payload, ts, state, transcript)

    def _handle_response_item(
        self,
        payload: dict,
        ts: datetime,
        state: _ParseState,
        transcript: list[TranscriptRecord],
    ) -> None:
        """Project a Codex ``response_item`` record into transcript facts."""
        # Lazy: codex_native_items imports shared helpers from this module.
        from coding_trajectory.ingestion.adapters import codex_native_items

        inner_type = payload.get("type", "")

        if inner_type == "function_call":
            tool_name = payload.get("name")
            tool_input = _parse_json_blob(payload.get("arguments"))
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="assistant",
                    kind="tool_call",
                    data={
                        "tool_name": tool_name,
                        "tool_call_id": payload.get("call_id"),
                        "input": tool_input,
                        "item_kind": _codex_item_kind(
                            tool_name=tool_name, inner_type=inner_type
                        ),
                    },
                )
            )

        elif inner_type == "function_call_output":
            raw_output = payload.get("output")
            output = _parse_json_blob(raw_output)
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="tool",
                    kind="tool_result",
                    data={
                        "tool_call_id": payload.get("call_id"),
                        "exit_code": extract_exit_code(raw_output),
                        "output": output,
                        "status": _tool_result_status(payload, raw_output).value,
                    },
                )
            )

        elif inner_type == "custom_tool_call":
            tool_name = payload.get("name")
            tool_input = _parse_json_blob(payload.get("input"))
            call_record = TranscriptRecord(
                sequence=len(transcript),
                timestamp=ts,
                vendor=Vendor.CODEX_CLI,
                role="assistant",
                kind="tool_call",
                data={
                    "tool_name": tool_name,
                    "tool_call_id": payload.get("call_id"),
                    "input": tool_input,
                    "item_kind": _codex_item_kind(
                        tool_name=tool_name, inner_type=inner_type
                    ),
                },
            )
            transcript.append(call_record)
            call_id = _as_non_empty_str(payload.get("call_id"))
            if tool_name == "exec" and call_id is not None:
                state.exec_wrapper_call_ids.add(call_id)
            invocations = (
                extract_static_exec_invocations(tool_input)
                if tool_name == "exec"
                else None
            )
            if call_id is not None and invocations is not None:
                state.pending_exec_wrappers[call_id] = _PendingExecWrapper(
                    call_id=call_id,
                    started_at=ts,
                    call_record=call_record,
                    invocations=invocations,
                    turn_id=_as_non_empty_str(state.turn_context.get("turn_id")),
                )

        elif inner_type == "custom_tool_call_output":
            raw_output = payload.get("output")
            call_id = _as_non_empty_str(payload.get("call_id"))
            wrapper_status = _tool_result_status(
                payload,
                raw_output,
                exec_wrapper=(
                    call_id is not None and call_id in state.exec_wrapper_call_ids
                ),
            )
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="tool",
                    kind="tool_result",
                    data={
                        "tool_name": payload.get("name"),
                        "tool_call_id": payload.get("call_id"),
                        "exit_code": extract_exit_code(raw_output),
                        "output": _parse_json_blob(raw_output),
                        "status": wrapper_status.value,
                    },
                )
            )
            codex_native_items.handle_static_exec_wrapper_output(payload, ts, state, transcript)

        elif inner_type == "tool_search_call":
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="assistant",
                    kind="tool_call",
                    data={
                        "tool_name": "tool_search",
                        "tool_call_id": payload.get("call_id"),
                        "input": payload.get("arguments"),
                        "status": _tool_status(payload.get("status")).value,
                        "item_kind": "tool_call",
                    },
                )
            )

        elif inner_type == "tool_search_output":
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="tool",
                    kind="tool_result",
                    data={
                        "tool_name": "tool_search",
                        "tool_call_id": payload.get("call_id"),
                        "output": payload.get("tools"),
                        "status": _tool_result_status(
                            payload, payload.get("tools")
                        ).value,
                    },
                )
            )

        elif inner_type == "web_search_call":
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="assistant",
                    kind="tool_call",
                    data={
                        "tool_name": "web_search",
                        "tool_call_id": f"web_search:{len(transcript)}",
                        "input": payload.get("action"),
                        "status": _tool_status(
                            payload.get("status"),
                            default=ToolStatus.COMPLETED,
                        ).value,
                        "item_kind": "tool_call",
                    },
                )
            )

        elif inner_type == "local_shell_call":
            command_source = _codex_command_activity_source(payload.get("source"))
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="assistant",
                    kind="tool_call",
                    data={
                        "tool_name": "local_shell",
                        "tool_call_id": payload.get("call_id"),
                        "input": payload.get("action"),
                        "command": payload.get("action"),
                        "status": _tool_status(payload.get("status")).value,
                        "item_kind": "command_execution",
                        "vendor_data": {
                            "activity": {
                                "kind": "command",
                                "source": command_source,
                                "fidelity": "observed_native",
                                "provenance": {
                                    "source": "response_item.local_shell_call",
                                    "source_kind": _as_non_empty_str(
                                        payload.get("source")
                                    ),
                                },
                            }
                        },
                    },
                )
            )

        elif inner_type == "image_generation_call":
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="assistant",
                    kind="tool_call",
                    data={
                        "tool_name": "image_generation",
                        "tool_call_id": payload.get("id"),
                        "input": {"revised_prompt": payload.get("revised_prompt")},
                        "output": payload.get("result"),
                        "status": _tool_status(
                            payload.get("status"),
                            default=ToolStatus.COMPLETED,
                        ).value,
                        "item_kind": "tool_call",
                    },
                )
            )

        elif inner_type == "reasoning":
            state.runtime_observations.append(
                RuntimeObservation(timestamp=ts, kind="reasoning")
            )
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="assistant",
                    kind="tool_call",
                    data={
                        "tool_name": "reasoning",
                        "tool_call_id": f"reasoning:{len(transcript)}",
                        "text": payload.get("content") or payload.get("text"),
                        "item_kind": "reasoning",
                    },
                )
            )
            return

        elif inner_type == "message":
            message_role = payload.get("role")
            if message_role in {"developer", "system"}:
                content = payload.get("content")
                if isinstance(content, list):
                    for index, item in enumerate(content):
                        if not isinstance(item, dict):
                            continue
                        text = item.get("text")
                        if not isinstance(text, str) or not text:
                            continue
                        block_name = _codex_prompt_block_name(text, index)
                        _record_context_source(
                            state,
                            _context_source_observation(
                                timestamp=ts,
                                block=block_name,
                                role=message_role,
                                text=text,
                            ),
                            block=block_name,
                            role=message_role,
                        )
                        transcript.append(
                            TranscriptRecord(
                                sequence=len(transcript),
                                timestamp=ts,
                                vendor=Vendor.CODEX_CLI,
                                role="runtime",
                                kind="runtime",
                                data={
                                    "raw_type": "prompt_block",
                                    "prompt_role": message_role,
                                    "prompt_block": block_name,
                                    "text": text,
                                },
                                fidelity="synthetic",
                            )
                        )
            elif message_role == "user":
                content = payload.get("content")
                if isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        text = item.get("text")
                        if not isinstance(text, str) or not text:
                            continue
                        block_name = _codex_user_prompt_block_name(text)
                        if block_name is None:
                            continue
                        _record_context_source(
                            state,
                            _context_source_observation(
                                timestamp=ts,
                                block=block_name,
                                role=message_role,
                                text=text,
                            ),
                            block=block_name,
                            role=message_role,
                        )
                        transcript.append(
                            TranscriptRecord(
                                sequence=len(transcript),
                                timestamp=ts,
                                vendor=Vendor.CODEX_CLI,
                                role="runtime",
                                kind="runtime",
                                data={
                                    "raw_type": "prompt_block",
                                    "prompt_role": message_role,
                                    "prompt_block": block_name,
                                    "text": text,
                                },
                                fidelity="synthetic",
                            )
                        )
            elif message_role == "assistant":
                phase = payload.get("phase")
                text = _extract_response_text(payload)
                transcript.append(
                    TranscriptRecord(
                        sequence=len(transcript),
                        timestamp=ts,
                        vendor=Vendor.CODEX_CLI,
                        role="assistant",
                        kind="assistant_message",
                        data={
                            "text": text,
                            "phase": phase,
                        },
                    )
                )

    def _handle_compacted(
        self,
        payload: dict,
        ts: datetime,
        state: _ParseState,
        transcript: list[TranscriptRecord],
    ) -> None:
        """Project a Codex ``compacted`` rollout record.

        Codex writes this record via ``replace_compacted_history`` after every
        compaction (local, remote v1/v2, and token-budget). It carries the
        replacement history, window chain metadata, and (for local compaction)
        the summary text. The ``context_compacted`` event_msg already produces
        the runtime observation that drives compaction counting and the
        eviction boundary; this handler ensures the record is not silently
        ignored and records the window metadata for future use.

        The ``replacement_history`` items are intentionally NOT re-projected
        here: they overlap with pre-compaction ``response_item`` records already
        in the transcript, and the eviction boundary (driven by
        ``context_compacted``) correctly marks those originals as evicted.
        Re-projecting would double-count the surviving subset.
        """
        message = _as_non_empty_str(payload.get("message"))
        window_number = payload.get("window_number")
        window_id = _as_non_empty_str(payload.get("window_id"))
        transcript.append(
            TranscriptRecord(
                sequence=len(transcript),
                timestamp=ts,
                vendor=Vendor.CODEX_CLI,
                role="runtime",
                kind="runtime",
                data={
                    "raw_type": "compacted",
                    "compaction_message": message,
                    "window_number": window_number,
                    "window_id": window_id,
                },
                fidelity="synthetic",
            )
        )

    def _handle_event_msg(
        self,
        payload: dict,
        ts: datetime,
        state: _ParseState,
        transcript: list[TranscriptRecord],
    ) -> None:
        """Project a Codex ``event_msg`` record into transcript facts."""
        # codex_native_items/codex_collab import shared helpers from this
        # module; the reverse edge stays lazy to keep imports acyclic.
        from coding_trajectory.ingestion.adapters import (
            codex_collab,
            codex_native_items,
        )

        inner_type = payload.get("type", "")
        turn_id = payload.get("turn_id") or state.turn_context.get("turn_id")

        if inner_type == "item_started":
            codex_native_items.handle_native_command_execution(
                payload,
                ts,
                state,
                transcript,
                completed=False,
            )
            codex_native_items.handle_native_file_change(
                payload,
                ts,
                state,
                transcript,
                completed=False,
            )
            codex_native_items.handle_native_web_search(
                payload,
                ts,
                state,
                transcript,
                completed=False,
            )
            codex_native_items.handle_native_extension_web_search(
                payload,
                ts,
                state,
                transcript,
                completed=False,
            )
            codex_native_items.handle_native_plan(
                payload,
                ts,
                state,
                transcript,
                completed=False,
            )
            codex_collab.handle_native_collab_agent_tool_call(
                payload,
                ts,
                state,
                transcript,
                completed=False,
            )

        elif inner_type == "item_completed":
            item = payload.get("item")
            if isinstance(item, dict) and item.get("type") == "UserMessage":
                _capture_codex_session_preview(
                    state, _extract_content_text(item.get("content"))
                )
            codex_native_items.handle_native_command_execution(
                payload,
                ts,
                state,
                transcript,
                completed=True,
            )
            codex_native_items.handle_native_file_change(
                payload,
                ts,
                state,
                transcript,
                completed=True,
            )
            codex_native_items.handle_native_web_search(
                payload,
                ts,
                state,
                transcript,
                completed=True,
            )
            codex_native_items.handle_native_extension_web_search(
                payload,
                ts,
                state,
                transcript,
                completed=True,
            )
            codex_native_items.handle_native_plan(
                payload,
                ts,
                state,
                transcript,
                completed=True,
            )
            codex_collab.handle_native_collab_agent_tool_call(
                payload,
                ts,
                state,
                transcript,
                completed=True,
            )
            codex_native_items.handle_native_terminal_item(
                payload,
                ts,
                state,
                transcript,
            )

        elif inner_type == "user_message":
            _capture_codex_session_preview(state, _extract_message_text(payload))
            turn_id_text = _as_non_empty_str(turn_id)
            starts_turn = (
                turn_id_text is None or turn_id_text not in state.projected_turn_ids
            )
            if turn_id_text is not None:
                state.projected_turn_ids.add(turn_id_text)
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="user",
                    kind="user_message",
                    data={
                        "turn_id_raw": turn_id,
                        "text": _extract_message_text(payload),
                        "previous_turn_status": TurnStatus.INTERRUPTED.value,
                        "starts_turn": starts_turn,
                    },
                )
            )

        elif inner_type == "agent_message":
            return

        elif inner_type == "task_complete":
            state.runtime_observations.append(
                RuntimeObservation(
                    timestamp=ts,
                    kind="turn_completed",
                    turn_id_raw=_as_non_empty_str(payload.get("turn_id")),
                    duration_ms=(
                        payload.get("duration_ms")
                        if isinstance(payload.get("duration_ms"), int)
                        else None
                    ),
                    time_to_first_token_ms=(
                        payload.get("time_to_first_token_ms")
                        if isinstance(payload.get("time_to_first_token_ms"), int)
                        else None
                    ),
                )
            )
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="runtime",
                    kind="task_complete",
                    data={
                        "turn_id_raw": payload.get("turn_id"),
                        "raw_type": "task_complete",
                        "text": payload.get("last_agent_message"),
                        "status": TurnStatus.COMPLETED.value,
                    },
                    fidelity="synthetic",
                )
            )

        elif inner_type == "token_count":
            info = payload.get("info")
            # Codex occasionally re-emits a token_count snapshot whose
            # cumulative ``total_token_usage`` is byte-identical to the
            # prior event's (a stale re-emission, not a new model call);
            # its ``last_token_usage`` repeats too, so counting it would
            # double-charge the call. Drop it before any accounting.
            total_usage = (
                info.get("total_token_usage") if isinstance(info, dict) else None
            )
            if (
                isinstance(total_usage, dict)
                and total_usage == state.prev_total_token_usage
            ):
                return
            if isinstance(total_usage, dict):
                state.prev_total_token_usage = total_usage
            normalized_metrics = normalize_codex_token_count(
                model=state.turn_context.get("model"),
                info=info,
            )
            usage_record = TranscriptRecord(
                sequence=len(transcript),
                timestamp=ts,
                vendor=Vendor.CODEX_CLI,
                role="runtime",
                kind="usage",
                data={
                    "turn_id_raw": turn_id,
                    "raw_type": "token_count",
                    **normalized_metrics,
                    "vendor_data": {
                        "metrics": normalized_metrics.get("metrics"),
                    }
                    if normalized_metrics.get("metrics")
                    else {},
                },
                fidelity="synthetic",
            )
            observation = context_usage_observation(
                timestamp=ts,
                source="codex_token_count",
                normalized=normalized_metrics,
                source_event_id=usage_record.record_id,
                provider="openai",
            )
            if observation is not None:
                if observation.context_window_tokens is None:
                    observation.context_window_tokens = state.context_window_tokens
                state.context_usage.append(observation)
            transcript.append(usage_record)

        elif inner_type == "context_compacted":
            state.runtime_observations.append(
                RuntimeObservation(timestamp=ts, kind="context_compacted")
            )
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="runtime",
                    kind="runtime",
                    data={
                        "turn_id_raw": turn_id,
                        "raw_type": "context_compacted",
                    },
                    fidelity="synthetic",
                )
            )

        elif inner_type == "turn_aborted":
            state.runtime_observations.append(
                RuntimeObservation(
                    timestamp=ts,
                    kind="turn_aborted",
                    turn_id_raw=_as_non_empty_str(payload.get("turn_id")) or turn_id,
                    duration_ms=(
                        payload.get("duration_ms")
                        if isinstance(payload.get("duration_ms"), int)
                        else None
                    ),
                    reason=_as_non_empty_str(payload.get("reason")),
                )
            )
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="runtime",
                    kind="task_complete",
                    data={
                        "turn_id_raw": payload.get("turn_id") or turn_id,
                        "raw_type": "turn_aborted",
                        "status": TurnStatus.INTERRUPTED.value,
                    },
                    fidelity="synthetic",
                )
            )

        elif inner_type == "thread_rolled_back":
            state.runtime_observations.append(
                RuntimeObservation(
                    timestamp=ts,
                    kind="thread_rolled_back",
                    num_turns=(
                        payload.get("num_turns")
                        if isinstance(payload.get("num_turns"), int)
                        else None
                    ),
                )
            )

        elif inner_type == "task_started":
            context_window = payload.get("model_context_window")
            if isinstance(context_window, int) and not isinstance(context_window, bool):
                state.context_window_tokens = context_window
            state.runtime_observations.append(
                RuntimeObservation(
                    timestamp=ts,
                    kind="turn_started",
                    turn_id_raw=_as_non_empty_str(payload.get("turn_id")) or turn_id,
                    trace_id=_as_non_empty_str(payload.get("trace_id")),
                )
            )
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="runtime",
                    kind="turn_started",
                    data={
                        "turn_id_raw": turn_id,
                        "raw_type": "task_started",
                        "model_context_window": payload.get("model_context_window"),
                        "collaboration_mode_kind": payload.get(
                            "collaboration_mode_kind"
                        ),
                    },
                    fidelity="synthetic",
                )
            )

        elif inner_type == "sub_agent_activity":
            codex_collab.record_spawn_link(state, payload)

    def _handle_session_meta(
        self,
        payload: dict,
        ts: datetime | None,
        state: _ParseState,
        transcript: list[TranscriptRecord],
    ) -> None:
        """Capture the first session_meta record and its base_instructions block."""
        if state.session_meta:
            return
        sid_str = payload.get("id")
        if sid_str:
            try:
                state.session_id = UUID(sid_str)
            except ValueError:
                pass
        state.session_meta = payload
        base_instructions = payload.get("base_instructions")
        base_text = (
            base_instructions.get("text")
            if isinstance(base_instructions, dict)
            else None
        )
        if ts is not None and isinstance(base_text, str) and base_text:
            _record_context_source(
                state,
                _context_source_observation(
                    timestamp=ts,
                    block="base_instructions",
                    role="system",
                    text=base_text,
                ),
                block="base_instructions",
                role="system",
            )
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="runtime",
                    kind="runtime",
                    data={
                        "raw_type": "prompt_block",
                        "prompt_role": "system",
                        "prompt_block": "base_instructions",
                        "text": base_text,
                    },
                    fidelity="synthetic",
                )
            )

    def _handle_turn_context(
        self,
        payload: dict,
        ts: datetime | None,
        state: _ParseState,
    ) -> None:
        """Record turn_context and detect reasoning-effort change-points.

        Codex emits a fresh turn_context per turn carrying the active
        ``effort``; a value differing from the prior turn's marks a cache-key
        change (the warm prefix is served from a different effort-bucket cache).
        """
        state.turn_context = payload
        multi_agent_version = _as_non_empty_str(payload.get("multi_agent_version"))
        if multi_agent_version is not None:
            state.multi_agent_version = multi_agent_version
        multi_agent_mode = _as_non_empty_str(payload.get("multi_agent_mode"))
        if multi_agent_mode is not None:
            state.multi_agent_mode = multi_agent_mode
        effort = _as_non_empty_str(payload.get("effort"))
        if (
            effort is not None
            and state.prev_effort is not None
            and effort != state.prev_effort
            and ts is not None
        ):
            state.runtime_observations.append(
                RuntimeObservation(
                    timestamp=ts,
                    kind="effort_changed",
                    turn_id_raw=_as_non_empty_str(payload.get("turn_id")),
                    effort_from=state.prev_effort,
                    effort_to=effort,
                )
            )
        if effort is not None:
            state.prev_effort = effort
