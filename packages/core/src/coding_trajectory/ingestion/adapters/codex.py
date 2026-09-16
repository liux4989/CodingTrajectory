"""Codex CLI adapter — reads ~/.codex/sessions/**/*.jsonl rollout files."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from coding_trajectory.ingestion.adapters import (
    codex_collab,
    codex_context,
    codex_native_items,
)
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
from coding_trajectory.ingestion.adapters.codex_context import (
    _codex_prompt_block_name,
    _codex_user_prompt_block_name,
    _context_source_observation,
    _record_context_source,
)
from coding_trajectory.ingestion.adapters.codex_exec_parser import (
    extract_static_exec_invocations,
)
from coding_trajectory.ingestion.adapters.codex_state import (
    CodexParseState,
    _codex_command_activity_source,
    _PendingExecWrapper,
    _tool_result_status,
    _tool_status,
)
from coding_trajectory.ingestion.assembly import AssemblyHooks, assemble_session
from coding_trajectory.ingestion.common import (
    extract_exit_code,
    parse_iso_timestamp,
    source_is_living,
)
from coding_trajectory.ingestion.models import (
    EventType,
    RuntimeObservation,
    Session,
    SessionStatus,
    ToolStatus,
    TurnStatus,
    Vendor,
)
from coding_trajectory.ingestion.provenance import RecordSpan
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


_SESSION_INDEX_CACHE: dict[Path, tuple[int, dict[str, str]]] = {}


def _codex_session_index_titles(index_path: Path) -> dict[str, str]:
    """Parse the name index at most once per (path, mtime); later duplicates win."""
    try:
        mtime = index_path.stat().st_mtime_ns
    except OSError:
        return {}
    cached = _SESSION_INDEX_CACHE.get(index_path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    titles: dict[str, str] = {}
    try:
        with index_path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                record_id = record.get("id")
                if not isinstance(record_id, str):
                    continue
                candidate = _as_non_empty_str(
                    record.get("thread_name")
                ) or _as_non_empty_str(record.get("title"))
                if candidate is not None:
                    titles[record_id] = candidate
    except OSError:
        return {}
    _SESSION_INDEX_CACHE[index_path] = (mtime, titles)
    return titles


def _codex_session_title(
    session_id: UUID,
    index_path: Path = _DEFAULT_CODEX_SESSION_INDEX,
) -> str | None:
    """Return the explicit Codex thread name from its local name index only."""
    if not index_path.is_file():
        return None

    return _codex_session_index_titles(index_path).get(str(session_id))


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
        payload = record.get("payload")
        return _record_parent_id(payload if isinstance(payload, dict) else {})
    return None


def _record_parent_id(meta: dict) -> str | None:
    direct = _extract_uuid_text(meta.get("forked_from_id"))
    if direct is not None:
        return direct
    source = meta.get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    return (
        _extract_uuid_text(spawn.get("parent_thread_id"))
        if isinstance(spawn, dict)
        else None
    )


def _iter_own_records(
    records: Iterable[tuple[dict, RecordSpan | None]],
    parent_started_turn_ids: set[str],
) -> Iterator[tuple[dict, RecordSpan | None]]:
    """Keep child-owned records even when copied parent turns are interleaved."""
    materialized = list(records)
    if _session_forked_from_id(record for record, _span in materialized) is None:
        yield from materialized
        return

    started: list[str] = []
    completed: set[str] = set()
    for record, _span in materialized:
        payload = record.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        turn_id = payload.get("turn_id")
        if not isinstance(turn_id, str):
            continue
        if payload.get("type") == "task_started":
            started.append(turn_id)
        elif payload.get("type") == "task_complete":
            completed.add(turn_id)
    owned_turn_ids = {
        turn_id
        for turn_id in started
        if turn_id not in parent_started_turn_ids and turn_id in completed
    }
    if started and started[-1] not in parent_started_turn_ids:
        # The final lifecycle may be a legitimate in-progress child turn.
        owned_turn_ids.add(started[-1])

    keep_active_window = False
    for record, span in materialized:
        if record.get("type") in {"session_meta", "compacted"}:
            yield record, span
            continue
        payload = record.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        explicit_turn_id = _nested_turn_id(payload)
        if payload.get("type") == "task_started":
            keep_active_window = explicit_turn_id in owned_turn_ids
        if explicit_turn_id in parent_started_turn_ids:
            continue
        if explicit_turn_id in owned_turn_ids or (
            explicit_turn_id is None and keep_active_window
        ):
            yield record, span


def _nested_turn_id(value: object) -> str | None:
    """Find explicit provider ownership on a record before using active-window state."""

    if not isinstance(value, dict):
        return None
    turn_id = value.get("turn_id")
    if isinstance(turn_id, str):
        return turn_id
    for nested in value.values():
        found = _nested_turn_id(nested)
        if found is not None:
            return found
    return None


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
    return [
        record
        for record, _span in _iter_own_records(
            ((record, None) for record in records),
            parent_started_turn_ids,
        )
    ]


def _derive_session_status(turns: list) -> SessionStatus:
    """Map a thread's current turn state to reversible session liveness."""

    return (
        SessionStatus.LIVING
        if any(turn.status == TurnStatus.RUNNING for turn in turns)
        else SessionStatus.NOT_LIVING
    )


class CodexAdapter(BaseAdapter):
    """Ingest Codex CLI JSONL rollout files from ~/.codex/sessions/."""

    vendor = Vendor.CODEX_CLI

    # Compatibility spelling for the parse state owned by ``codex_state``;
    # reconstruction modules alias it as ``_ParseState`` from there too.
    _ParseState = CodexParseState

    def ingest_file(
        self,
        path: Path,
        *,
        parent_started_turn_ids: set[str] | None = None,
        retention: CanonicalRetention = "trajectory",
    ) -> Session:
        self._reset_ingest_state()
        self._reset_source_provenance()
        records: Iterable[tuple[dict, RecordSpan | None]] = self._iter_record_spans(
            path
        )
        if parent_started_turn_ids is not None:
            records = _iter_own_records(records, parent_started_turn_ids)
        try:
            state = self._ParseState()
            transcript = self._build_transcript(records, state)
            session = self._build_session(path, transcript, state, retention=retention)
        except Exception:
            self._finish_source_provenance(path)
            raise
        self._finish_source_provenance(path, session_id=session.session_id)
        return session

    def build_canonical_session(
        self,
        source: Path,
        records: Iterable[dict],
        *,
        parent_started_turn_ids: set[str] | None = None,
        retention: CanonicalRetention = "trajectory",
    ) -> Session:
        """In-memory-record seam: cut inherited fork history, then assemble."""
        self._reset_ingest_state()
        self._reset_source_provenance()
        cut = _cut_inherited_records(list(records), parent_started_turn_ids)
        state = self._ParseState()
        transcript = self._build_transcript(((record, None) for record in cut), state)
        return self._build_session(source, transcript, state, retention=retention)

    def scan_started_turn_ids(self, source: Path) -> set[str] | None:
        return self.scan_started_turn_ids_records(self._iter_records(source))

    def scan_started_turn_ids_records(self, records: Iterable[dict]) -> set[str] | None:
        started: set[str] = set()
        for record in records:
            payload = record.get("payload") or {}
            if payload.get("type") == "task_started" and isinstance(
                payload.get("turn_id"), str
            ):
                started.add(payload["turn_id"])
        return started

    def scan_identity_records(
        self, source: Path, records: Iterable[dict]
    ) -> SessionHeader | None:
        return self._identity_from_records(records)

    def scan_identity(self, source: Path) -> SessionHeader | None:
        """Read the leading ``session_meta`` without searching for a title."""

        return self._identity_from_records(self._iter_records(source))

    def _identity_from_records(self, records: Iterable[dict]) -> SessionHeader | None:
        for record in records:
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
        session = assemble_session(
            vendor=Vendor.CODEX_CLI,
            source=source,
            session_id=state.session_id,
            transcript=transcript,
            retention=retention,
            hooks=hooks,
        )
        # A Codex lifecycle turn has one canonical request. Native streams can
        # repeat UserMessage items during steering or inherited incomplete
        # turns; keep only the request selected by the projected turn so event
        # counts and Chronicle replay describe the same canonical hierarchy.
        request_event_ids = {
            turn.user_request_event_id
            for turn in session.turns
            if turn.user_request_event_id is not None
        }
        retained = session.model_copy(
            update={
                "events": [
                    event
                    for event in session.events
                    if event.type != EventType.USER_PROMPT_SUBMITTED
                    or event.event_id in request_event_ids
                ]
            }
        )
        return retained

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
            if any(
                entry.kind == "assistant_message"
                and isinstance(entry.data.get("text"), str)
                and bool(entry.data["text"].strip())
                for entry in transcript[before:]
            ):
                state.activity_cell_epoch += 1
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
            codex_context.handle_turn_context(payload, ts, state)
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
            codex_native_items.handle_static_exec_wrapper_output(
                payload, ts, state, transcript
            )

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
                self._record_user_message(
                    text=_extract_content_text(item.get("content")),
                    turn_id=turn_id,
                    ts=ts,
                    state=state,
                    transcript=transcript,
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
            self._record_user_message(
                text=_extract_message_text(payload),
                turn_id=turn_id,
                ts=ts,
                state=state,
                transcript=transcript,
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
            codex_context.handle_token_count(
                payload, ts, state, transcript, turn_id=turn_id
            )

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

    @staticmethod
    def _record_user_message(
        *,
        text: str | None,
        turn_id: Any,
        ts: datetime,
        state: _ParseState,
        transcript: list[TranscriptRecord],
    ) -> None:
        """Project legacy and native Codex user-message records identically."""

        turn_id_text = _as_non_empty_str(turn_id)
        if turn_id_text is not None and turn_id_text in state.projected_turn_ids:
            return
        _capture_codex_session_preview(state, text)
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
                    "text": text,
                    "previous_turn_status": TurnStatus.INTERRUPTED.value,
                    "starts_turn": starts_turn,
                },
            )
        )

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
