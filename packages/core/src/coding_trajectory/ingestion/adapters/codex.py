"""Codex CLI adapter — reads ~/.codex/sessions/**/*.jsonl rollout files."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from coding_trajectory.ingestion.adapters.base import BaseAdapter, SessionHeader
from coding_trajectory.ingestion.common import (
    extract_exit_code,
    infer_tool_success,
    parse_iso_timestamp,
)
from coding_trajectory.ingestion.models import (
    ContextSourceObservation,
    ContextUsageObservation,
    RuntimeObservation,
    Session,
    SessionStatus,
    TurnStatus,
    ToolStatus,
    Vendor,
)
from coding_trajectory.ingestion.transcript import TranscriptRecord, events_from_transcript, project_transcript
from coding_trajectory.ingestion.vendor_mechanisms.codex_multi_agent import (
    CodexMultiAgentInput,
    CodexThreadSpawn,
    extensions as codex_extensions,
    parent_session_id as codex_parent_session_id,
)
from coding_trajectory.ingestion.vendor_mechanisms.usage_metrics import (
    context_usage_observation,
    normalize_codex_token_count,
)

logger = logging.getLogger(__name__)

_DEFAULT_CODEX_SESSION_INDEX = Path.home() / ".codex" / "session_index.jsonl"
_CODEX_FALLBACK_TITLE_MAX_LEN = 96

_CODEX_FILE_TOOL_NAMES: frozenset[str] = frozenset({
    "Read", "Edit", "MultiEdit", "Write", "View",
    "read_file", "read_many_files", "replace", "write_file",
    "edit_file", "create_file", "apply_patch",
})
_CODEX_PLAN_TOOL_NAMES: frozenset[str] = frozenset({
    "TodoWrite", "TodoRead", "update_plan",
})


def _codex_item_kind(*, tool_name: str | None, inner_type: str) -> str:
    if inner_type == "local_shell_call":
        return "command_execution"
    if inner_type == "reasoning":
        return "reasoning"
    if tool_name in _CODEX_PLAN_TOOL_NAMES:
        return "plan"
    if tool_name in _CODEX_FILE_TOOL_NAMES:
        return "file_change"
    return "tool_call"


def _parse_json_blob(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _tool_status(value: Any, *, default: ToolStatus = ToolStatus.REQUESTED) -> ToolStatus:
    if value == "completed":
        return ToolStatus.COMPLETED
    if value in {"failed", "declined"}:
        return ToolStatus.FAILED
    if value == "in_progress":
        return ToolStatus.IN_PROGRESS
    return default


def _tool_result_status(payload: dict[str, Any], output: Any) -> ToolStatus:
    if isinstance(payload.get("success"), bool):
        return ToolStatus.COMPLETED if payload["success"] else ToolStatus.FAILED
    status = _tool_status(payload.get("status"), default=ToolStatus.COMPLETED)
    if status != ToolStatus.COMPLETED:
        return status
    success = infer_tool_success(output)
    return ToolStatus.FAILED if success is False else ToolStatus.COMPLETED


def _extract_message_text(payload: dict[str, Any]) -> str | None:
    message = payload.get("message")
    return message if isinstance(message, str) and message else None


def _extract_response_text(payload: dict[str, Any]) -> str | None:
    content = payload.get("content")
    if not isinstance(content, list):
        return None

    texts = [
        part.get("text", "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "output_text"
    ]
    joined = " ".join(text for text in texts if text).strip()
    return joined or None


def _as_non_empty_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _extract_nested_map(payload: dict[str, Any], *keys: str) -> dict[str, Any] | None:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if isinstance(current, dict) else None


def _extract_uuid_text(value: Any) -> str | None:
    raw = _as_non_empty_str(value)
    if raw is None:
        return None
    for candidate in (raw, raw.removeprefix("T-")):
        try:
            UUID(candidate)
            return candidate
        except ValueError:
            continue
    match = re.search(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        raw,
    )
    return match.group(0) if match else raw


def _codex_session_title(session_id: UUID, meta: dict[str, Any], index_path: Path = _DEFAULT_CODEX_SESSION_INDEX) -> str | None:
    title = _as_non_empty_str(meta.get("title")) or _as_non_empty_str(meta.get("thread_name"))
    if title is not None:
        return title
    if not index_path.is_file():
        return None

    try:
        with index_path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("id") != str(session_id):
                    continue
                return _as_non_empty_str(record.get("thread_name")) or _as_non_empty_str(record.get("title"))
    except OSError:
        return None
    return None


def _codex_fallback_title(value: Any) -> str | None:
    text = _as_non_empty_str(value)
    if text is None:
        return None
    text = " ".join(text.split())
    if len(text) <= _CODEX_FALLBACK_TITLE_MAX_LEN:
        return text
    return f"{text[:_CODEX_FALLBACK_TITLE_MAX_LEN - 3].rstrip()}..."


def _codex_multi_agent_input(meta: dict[str, Any], ctx: dict[str, Any], *, session_id: UUID) -> CodexMultiAgentInput:
    sandbox_policy = ctx.get("sandbox_policy") if isinstance(ctx.get("sandbox_policy"), dict) else {}
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
        agent_nickname=_as_non_empty_str(meta.get("nickname")) or _as_non_empty_str(meta.get("agent_nickname")),
        agent_role=_as_non_empty_str(meta.get("agent_role")) or source_name,
        cwd=_as_non_empty_str(meta.get("cwd")),
        title=_codex_session_title(session_id, meta),
        forked_from_id=_extract_uuid_text(meta.get("forked_from_id")),
        thread_spawn=(
            CodexThreadSpawn(
                parent_thread_id=_extract_uuid_text(thread_spawn_raw.get("parent_thread_id")),
                depth=thread_spawn_raw.get("depth") if isinstance(thread_spawn_raw.get("depth"), int) else None,
                agent_nickname=_as_non_empty_str(thread_spawn_raw.get("agent_nickname")),
                agent_role=_as_non_empty_str(thread_spawn_raw.get("agent_role")),
            )
            if thread_spawn_raw is not None
            else None
        ),
    )


def _is_source_active(source: Path | None, *, active_seconds: int = 300) -> bool:
    if source is None:
        return False
    try:
        return time.time() - source.stat().st_mtime <= active_seconds
    except OSError:
        return False


def _derive_session_status(turns: list) -> SessionStatus:
    if any(turn.status == TurnStatus.RUNNING for turn in turns):
        return SessionStatus.ACTIVE
    if turns and turns[-1].status == TurnStatus.INCOMPLETE:
        return SessionStatus.INCOMPLETE
    return SessionStatus.COMPLETED


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
    if "memory_summary" in haystack or "memory layout" in haystack or "## memory" in haystack:
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


def _record_context_source(state: Any, observation: ContextSourceObservation) -> None:
    """Append a context-source observation, deduplicating exact text repeats.

    Codex re-injects the base/developer/AGENTS.md prompt blocks after a context
    compaction, so the same block is captured again and would be sized twice in
    the composition. Dedup by exact text keeps only the (resident) copy.
    """
    if observation.text in state.context_source_texts:
        return
    state.context_source_texts.add(observation.text)
    state.context_sources.append(observation)


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
        context_sources: list[ContextSourceObservation] = field(default_factory=list)
        runtime_observations: list[RuntimeObservation] = field(default_factory=list)
        projected_turn_ids: set[str] = field(default_factory=set)
        # Codex re-injects base/developer/AGENTS.md blocks after each compaction;
        # track seen text so the duplicate (re-injected) copy is not sized twice.
        context_source_texts: set[str] = field(default_factory=set)

    def ingest_file(self, path: Path) -> Session:
        self._reset_ingest_state()
        records = self._load_records(path)
        state = self._ParseState()
        transcript = self._build_transcript(records, state)
        return self._build_session(path, transcript, state)

    def scan_header(self, source: Path) -> SessionHeader | None:
        header: SessionHeader | None = None
        for record in self._iter_records(source):
            outer_type = record.get("type")
            if outer_type == "session_meta" and header is None:
                meta = record.get("payload") or {}
                if not isinstance(meta, dict):
                    return None
                try:
                    session_id = UUID(meta.get("id"))
                except (ValueError, TypeError):
                    return None
                mechanism = _codex_multi_agent_input(meta, {}, session_id=session_id)
                header = SessionHeader(
                    session_id=session_id,
                    vendor=Vendor.CODEX_CLI,
                    parent_session_id=codex_parent_session_id(mechanism),
                    title=mechanism.title,
                    cwd=mechanism.cwd,
                )
                if header.title:
                    return header
                continue

            if header is None or outer_type != "event_msg":
                continue
            payload = record.get("payload") or {}
            if not isinstance(payload, dict) or payload.get("type") != "user_message":
                continue
            title = _codex_fallback_title(_extract_message_text(payload))
            if title is not None:
                return SessionHeader(
                    session_id=header.session_id,
                    vendor=header.vendor,
                    parent_session_id=header.parent_session_id,
                    title=title,
                    cwd=header.cwd,
                )
        return header

    def _build_session(
        self,
        source: Path,
        transcript: list[TranscriptRecord],
        state: _ParseState | None = None,
    ) -> Session:
        state = state or self._ParseState()
        if not transcript:
            raise ValueError(f"CodexAdapter: no transcript records parsed from {source}")
        else:
            started_at = min(record.timestamp for record in transcript)
            ended_at = max(record.timestamp for record in transcript)

        meta = state.session_meta
        ctx = state.turn_context
        mechanism = _codex_multi_agent_input(meta, ctx, session_id=state.session_id)
        parent_session_id = codex_parent_session_id(mechanism)
        events = events_from_transcript(session_id=state.session_id, records=transcript)
        extensions = codex_extensions(mechanism)

        turns = project_transcript(
            session_id=state.session_id,
            vendor=Vendor.CODEX_CLI,
            records=transcript,
            active_status=TurnStatus.RUNNING if _is_source_active(source) else TurnStatus.INCOMPLETE,
            default_previous_turn_status=TurnStatus.INTERRUPTED,
        )
        session_status = _derive_session_status(turns)

        return Session(
            session_id=state.session_id,
            vendor=Vendor.CODEX_CLI,
            agent_name=extensions.codex.agent_nickname if extensions.codex else None,
            started_at=started_at,
            ended_at=ended_at,
            parent_session_id=parent_session_id,
            events=events,
            turns=turns,
            context_usage=state.context_usage,
            context_sources=state.context_sources,
            runtime_observations=state.runtime_observations,
            extensions=extensions,
            status=session_status,
        )

    def _build_transcript(
        self,
        records: list[dict],
        state: _ParseState,
    ) -> list[TranscriptRecord]:
        """Extract only CT-useful transcript facts from Codex JSONL records."""
        transcript: list[TranscriptRecord] = []
        for record in records:
            outer_type = record.get("type", "")
            payload = record.get("payload") or {}
            ts = parse_iso_timestamp(record.get("timestamp"))

            if outer_type == "session_meta":
                if state.session_meta:
                    continue
                sid_str = payload.get("id")
                if sid_str:
                    try:
                        state.session_id = UUID(sid_str)
                    except ValueError:
                        pass
                state.session_meta = payload
                base_instructions = payload.get("base_instructions")
                base_text = base_instructions.get("text") if isinstance(base_instructions, dict) else None
                if ts is not None and isinstance(base_text, str) and base_text:
                    _record_context_source(
                        state,
                        _context_source_observation(
                            timestamp=ts,
                            block="base_instructions",
                            role="system",
                            text=base_text,
                        ),
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
                continue

            if outer_type == "turn_context":
                state.turn_context = payload
                continue

            if ts is None:
                continue

            if outer_type == "event_msg":
                inner_type = payload.get("type", "")
                turn_id = payload.get("turn_id") or state.turn_context.get("turn_id")

                if inner_type == "user_message":
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
                    continue

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
                            } if normalized_metrics.get("metrics") else {},
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
                            kind="runtime",
                            data={
                                "turn_id_raw": turn_id,
                                "raw_type": "task_started",
                                "model_context_window": payload.get("model_context_window"),
                                "collaboration_mode_kind": payload.get("collaboration_mode_kind"),
                            },
                            fidelity="synthetic",
                        )
                    )

            elif outer_type == "response_item":
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
                                "item_kind": _codex_item_kind(tool_name=tool_name, inner_type=inner_type),
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
                                "input": _parse_json_blob(payload.get("input")),
                                "item_kind": _codex_item_kind(tool_name=tool_name, inner_type=inner_type),
                            },
                        )
                    )

                elif inner_type == "custom_tool_call_output":
                    raw_output = payload.get("output")
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
                                "status": _tool_result_status(payload, raw_output).value,
                            },
                        )
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
                                "status": _tool_result_status(payload, payload.get("tools")).value,
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
                    continue

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

        return transcript

    def ingest_codex_home(self, codex_dir: Path | None = None) -> list[Session]:
        """Ingest all rollout JSONL files under the Codex home directory."""
        if codex_dir is None:
            codex_dir = Path.home() / ".codex"
        sessions_dir = codex_dir / "sessions"
        if not sessions_dir.is_dir():
            logger.warning("Codex sessions directory not found: %s", sessions_dir)
            return []

        sessions: list[Session] = []
        for jsonl_file in sorted(sessions_dir.rglob("*.jsonl")):
            try:
                sessions.append(self.ingest_file(jsonl_file))
            except Exception:
                logger.warning("Failed to ingest %s", jsonl_file, exc_info=True)
        return sessions
