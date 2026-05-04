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

from coding_trajectory.ingestion.adapters.base import BaseAdapter
from coding_trajectory.ingestion.common import (
    extract_exit_code,
    infer_tool_success,
    parse_iso_timestamp,
)
from coding_trajectory.ingestion.models import (
    CodexExtensions,
    Session,
    SessionStatus,
    TurnStatus,
    ToolStatus,
    Vendor,
    VendorExtensions,
)
from coding_trajectory.ingestion.transcript import TranscriptRecord, events_from_transcript, project_transcript

logger = logging.getLogger(__name__)


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


def _parse_uuid_candidate(value: Any) -> UUID | None:
    if isinstance(value, UUID):
        return value

    raw = _as_non_empty_str(value)
    if raw is None:
        return None

    for candidate in (raw, raw.removeprefix("T-")):
        try:
            return UUID(candidate)
        except ValueError:
            continue

    match = re.search(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        raw,
    )
    if match is None:
        return None

    try:
        return UUID(match.group(0))
    except ValueError:
        return None


def _extract_thread_spawn(meta: dict[str, Any]) -> dict[str, Any] | None:
    source = meta.get("source")
    if not isinstance(source, dict):
        return None
    return _extract_nested_map(source, "subagent", "thread_spawn")


def _extract_codex_parent_session_id(meta: dict[str, Any]) -> UUID | None:
    forked_parent = _parse_uuid_candidate(meta.get("forked_from_id"))
    if forked_parent is not None:
        return forked_parent

    thread_spawn = _extract_thread_spawn(meta)
    if thread_spawn is None:
        return None

    return _parse_uuid_candidate(thread_spawn.get("parent_thread_id"))


def _extract_codex_agent_nickname(meta: dict[str, Any]) -> str | None:
    for key in ("nickname", "agent_nickname"):
        nickname = _as_non_empty_str(meta.get(key))
        if nickname is not None:
            return nickname

    thread_spawn = _extract_thread_spawn(meta)
    if thread_spawn is None:
        return None
    return _as_non_empty_str(thread_spawn.get("agent_nickname"))


def _extract_codex_agent_role(meta: dict[str, Any]) -> str | None:
    role = _as_non_empty_str(meta.get("agent_role"))
    if role is not None:
        return role

    source = meta.get("source")
    source_name = _as_non_empty_str(source)
    if source_name is not None:
        return source_name

    thread_spawn = _extract_thread_spawn(meta)
    if thread_spawn is None:
        return None
    return _as_non_empty_str(thread_spawn.get("agent_role"))


def _extract_collaboration_mode(ctx: dict[str, Any]) -> str | None:
    collaboration_mode = ctx.get("collaboration_mode")
    if isinstance(collaboration_mode, dict):
        return _as_non_empty_str(collaboration_mode.get("mode"))
    return _as_non_empty_str(collaboration_mode)


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


class CodexAdapter(BaseAdapter):
    """Ingest Codex CLI JSONL rollout files from ~/.codex/sessions/."""

    vendor = Vendor.CODEX_CLI

    @dataclass
    class _ParseState:
        session_meta: dict[str, Any] = field(default_factory=dict)
        turn_context: dict[str, Any] = field(default_factory=dict)
        session_id: UUID = field(default_factory=uuid4)

    def ingest_file(self, path: Path) -> Session:
        self._reset_ingest_state()
        records = self._load_records(path)
        state = self._ParseState()
        transcript = self._build_transcript(records, state)
        return self._build_session(path, transcript, state)

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
        sandbox_policy = ctx.get("sandbox_policy") if isinstance(ctx.get("sandbox_policy"), dict) else {}
        thread_spawn = _extract_thread_spawn(meta) or {}
        parent_session_id = _extract_codex_parent_session_id(meta)
        forked_from_id = _as_non_empty_str(meta.get("forked_from_id"))
        spawn_parent_thread_id = _as_non_empty_str(thread_spawn.get("parent_thread_id"))
        spawn_depth = thread_spawn.get("depth") if isinstance(thread_spawn.get("depth"), int) else None
        events = events_from_transcript(session_id=state.session_id, records=transcript)

        extensions = VendorExtensions(
            codex=CodexExtensions(
                sandbox_id=_as_non_empty_str(meta.get("id")),
                sandbox_mode=_as_non_empty_str(sandbox_policy.get("type")),
                approval_policy=_as_non_empty_str(ctx.get("approval_policy")),
                collaboration_mode=_extract_collaboration_mode(ctx),
                agent_nickname=_extract_codex_agent_nickname(meta),
                agent_role=_extract_codex_agent_role(meta),
                cwd=_as_non_empty_str(meta.get("cwd")),
                forked_from_id=forked_from_id,
                spawn_parent_thread_id=spawn_parent_thread_id,
                spawn_depth=spawn_depth,
                spawn_agent_nickname=_as_non_empty_str(thread_spawn.get("agent_nickname")),
                spawn_agent_role=_as_non_empty_str(thread_spawn.get("agent_role")),
            )
        )

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
            trajectory_id=uuid4(),
            vendor=Vendor.CODEX_CLI,
            agent_name=extensions.codex.agent_nickname if extensions.codex else None,
            started_at=started_at,
            ended_at=ended_at,
            parent_session_id=parent_session_id,
            events=events,
            turns=turns,
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

            if outer_type == "session_meta":
                sid_str = payload.get("id")
                if sid_str:
                    try:
                        state.session_id = UUID(sid_str)
                    except ValueError:
                        pass
                state.session_meta = payload
                continue

            if outer_type == "turn_context":
                state.turn_context = payload
                continue

            ts = parse_iso_timestamp(record.get("timestamp"))
            if ts is None:
                continue

            if outer_type == "event_msg":
                inner_type = payload.get("type", "")
                turn_id = payload.get("turn_id") or state.turn_context.get("turn_id")

                if inner_type == "user_message":
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
                            },
                        )
                    )

                elif inner_type == "agent_message":
                    continue

                elif inner_type == "task_complete":
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
                    transcript.append(
                        TranscriptRecord(
                            sequence=len(transcript),
                            timestamp=ts,
                            vendor=Vendor.CODEX_CLI,
                            role="runtime",
                            kind="usage",
                            data={
                                "turn_id_raw": turn_id,
                                "raw_type": "token_count",
                                "model": state.turn_context.get("model"),
                                "info": payload.get("info"),
                                "rate_limits": payload.get("rate_limits"),
                                "vendor_data": {
                                    "model": state.turn_context.get("model"),
                                    **(payload.get("info") or {}).get("last_token_usage", {}),
                                } if isinstance(payload.get("info"), dict) else {},
                            },
                            fidelity="synthetic",
                        )
                    )

                elif inner_type == "context_compacted":
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
                            },
                        )
                    )

                elif inner_type == "function_call_output":
                    raw_output = payload.get("output")
                    output = _parse_json_blob(raw_output)
                    success = infer_tool_success(raw_output)
                    status = (
                        ToolStatus.FAILED.value if success is False else ToolStatus.COMPLETED.value
                    )
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
                                "status": status,
                            },
                        )
                    )

                elif inner_type == "reasoning":
                    transcript.append(
                        TranscriptRecord(
                            sequence=len(transcript),
                            timestamp=ts,
                            vendor=Vendor.CODEX_CLI,
                            role="assistant",
                            kind="runtime",
                            data={"raw_type": "reasoning"},
                        )
                    )
                    continue

                elif inner_type == "message":
                    if payload.get("role") == "assistant":
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
                                    "flush_before": True,
                                    "flush_after": True,
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
