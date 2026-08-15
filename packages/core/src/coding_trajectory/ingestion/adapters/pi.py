"""Pi coding agent adapter — reads ~/.pi/agent/sessions/**/*.jsonl session files."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from coding_trajectory.ingestion.adapters.base import BaseAdapter, SessionHeader
from coding_trajectory.ingestion.common import (
    extract_exit_code,
    infer_tool_success,
    parse_iso_timestamp,
)
from coding_trajectory.ingestion.models import (
    PiExtensions,
    Session,
    SessionStatus,
    ToolStatus,
    Vendor,
    VendorExtensions,
)
from coding_trajectory.ingestion.retention import (
    CanonicalRetention,
    compact_context_usage_observation,
)
from coding_trajectory.ingestion.transcript import (
    TranscriptRecord,
    TranscriptStabilizer,
    compact_session_cwd,
    events_from_transcript,
    project_transcript,
)
from coding_trajectory.ingestion.vendor_mechanisms.usage_metrics import (
    context_usage_observation,
    normalize_pi_usage,
)

logger = logging.getLogger(__name__)

_DEFAULT_PI_SESSIONS_DIR = Path.home() / ".pi" / "agent" / "sessions"

_PI_FILE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "readFile",
        "read_file",
        "read_many_files",
        "writeFile",
        "write_file",
        "create_file",
        "editFile",
        "edit_file",
        "replace",
        "apply_patch",
        "Read",
        "Edit",
        "MultiEdit",
        "Write",
        "View",
    }
)
_PI_PLAN_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "TodoWrite",
        "TodoRead",
        "update_plan",
    }
)


def _pi_item_kind(tool_name: str | None) -> str:
    if tool_name == "bash":
        return "command_execution"
    if tool_name in _PI_PLAN_TOOL_NAMES:
        return "plan"
    if tool_name in _PI_FILE_TOOL_NAMES:
        return "file_change"
    return "tool_call"


def _content_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        texts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        joined = " ".join(text for text in texts if text).strip()
        return joined or None
    return None


def _tool_calls_from_content(content: list[dict]) -> list[dict]:
    if not isinstance(content, list):
        return []
    return [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "toolCall"
    ]


def _thinking_blocks(content: list[dict]) -> list[str]:
    if not isinstance(content, list):
        return []
    return [
        block.get("thinking", "")
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "thinking"
        and block.get("thinking")
    ]


def _is_real_user_message(message: dict) -> bool:
    content = message.get("content", [])
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "toolResult" for b in content):
            return False
    return True


def _parse_usage(message: dict) -> dict | None:
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    return {
        "input": usage.get("input"),
        "output": usage.get("output"),
        "cacheRead": usage.get("cacheRead"),
        "cacheWrite": usage.get("cacheWrite"),
        "totalTokens": usage.get("totalTokens"),
        "cost": usage.get("cost"),
    }


class PiAdapter(BaseAdapter):
    """Normalise Pi coding agent JSONL session files into canonical Session objects."""

    vendor = Vendor.PI
    _file_glob = "*.jsonl"

    _current_provider: str | None
    _current_model: str | None
    _current_thinking_level: str | None
    _current_cwd: str | None
    _session_title: str | None
    _session_raw_id: str | None
    _pending_bash_tool_call_ids: list[str]

    def _reset_ingest_state(self) -> None:
        self._current_provider = None
        self._current_model = None
        self._current_thinking_level = None
        self._current_cwd = None
        self._session_title = None
        self._session_raw_id = None
        self._pending_bash_tool_call_ids = []

    _TITLE_LOOKAHEAD = 50

    def scan_header(self, source: Path) -> SessionHeader | None:
        session_id: UUID | None = None
        cwd: str | None = None
        title: str | None = None
        since_session = 0
        for record in self._iter_records(source):
            entry_type = record.get("type")
            if entry_type == "session" and session_id is None:
                raw_id = record.get("id")
                if isinstance(raw_id, str):
                    try:
                        session_id = UUID(raw_id)
                    except ValueError:
                        session_id = uuid5(NAMESPACE_URL, f"pi:{source}:{raw_id}")
                cwd = record.get("cwd") or cwd
            elif entry_type == "session_info" and title is None:
                title = record.get("name") or title
            elif entry_type == "message" and title is None:
                message = record.get("message")
                if (
                    isinstance(message, dict)
                    and message.get("role") == "user"
                    and _is_real_user_message(message)
                ):
                    text = _content_text(message.get("content", []))
                    if text:
                        title = " ".join(text.split()) or None
            if session_id is not None and title is not None:
                break
            if session_id is not None:
                since_session += 1
                if since_session >= self._TITLE_LOOKAHEAD:
                    break
        if session_id is None:
            session_id = uuid5(NAMESPACE_URL, f"pi:{source}")
        return SessionHeader(
            session_id=session_id,
            vendor=Vendor.PI,
            parent_session_id=None,
            title=title,
            cwd=cwd,
        )

    def _build_session(
        self,
        source: Path,
        records: Iterable[dict],
        *,
        retention: CanonicalRetention = "trajectory",
    ) -> Session:
        transcript = self._build_transcript(records)
        session_id = self._resolved_session_id(source)
        if not transcript:
            raise ValueError(f"PiAdapter: no transcript records parsed from {source}")

        compact = (
            TranscriptStabilizer(vendor=Vendor.PI, source=source)
            if retention == "measurements"
            else None
        )
        events = events_from_transcript(
            session_id=session_id, records=transcript, stabilizer=compact
        )
        turns = project_transcript(
            session_id=session_id,
            vendor=Vendor.PI,
            records=transcript,
            compact=compact,
        )

        started_at = min(record.timestamp for record in transcript)
        ended_at = max(record.timestamp for record in transcript)
        context_usage = [
            observation
            for record in transcript
            if (
                observation := context_usage_observation(
                    timestamp=record.timestamp,
                    source="pi_usage_block",
                    normalized=record.data.get("vendor_data", {}),
                    source_event_id=record.record_id,
                    provider=self._current_provider,
                    category_source="pi_usage_block",
                )
            )
            is not None
        ]
        if compact is not None:
            context_usage = [
                compact_context_usage_observation(observation, compact.event_ids)
                for observation in context_usage
            ]

        extensions = VendorExtensions(
            pi=PiExtensions(
                session_file=str(source),
                cwd=self._current_cwd,
                title=self._session_title,
                provider=self._current_provider,
                model=self._current_model,
                thinking_level=self._current_thinking_level,
            )
        )

        return Session(
            session_id=session_id,
            vendor=self.vendor,
            started_at=started_at,
            ended_at=ended_at,
            events=events,
            turns=turns,
            context_usage=context_usage,
            extensions=extensions,
            status=SessionStatus.COMPLETED,
            cwd=(
                compact_session_cwd(
                    vendor=Vendor.PI,
                    source=source,
                    extensions=extensions,
                    payload_cwd=compact.cwd,
                )
                if compact is not None
                else None
            ),
        )

    def _build_transcript(self, records: Iterable[dict]) -> list[TranscriptRecord]:
        """Extract only CT-useful transcript facts from Pi JSONL records."""
        transcript: list[TranscriptRecord] = []

        for record in records:
            if self._handle_state_record(record):
                continue

            if record.get("type", "") != "message":
                continue

            message = record.get("message")
            if not isinstance(message, dict):
                continue

            role = message.get("role")
            ts = parse_iso_timestamp(record.get("timestamp"))
            if ts is None:
                continue

            if role == "user":
                self._handle_user_message(message, ts, transcript)
            elif role == "assistant":
                self._handle_assistant_message(message, ts, transcript)
            elif role == "toolResult":
                self._handle_tool_result_message(message, ts, transcript)
            elif role == "bashExecution":
                self._handle_bash_execution(message, ts, transcript)

        return transcript

    def _handle_state_record(self, record: dict) -> bool:
        """Apply a non-message session-state record; return True if handled.

        Pi interleaves small state-setup records (session cwd, model change,
        thinking-level change, session title) with message records. These mutate
        adapter tracking state and emit no transcript records.
        """
        entry_type = record.get("type", "")
        if entry_type == "session":
            self._current_cwd = record.get("cwd")
            raw_id = record.get("id")
            if self._session_raw_id is None and isinstance(raw_id, str):
                self._session_raw_id = raw_id
            return True
        if entry_type == "model_change":
            self._current_provider = record.get("provider")
            self._current_model = record.get("modelId")
            return True
        if entry_type == "thinking_level_change":
            self._current_thinking_level = record.get("thinkingLevel")
            return True
        if entry_type == "session_info":
            self._session_title = record.get("name") or self._session_title
            return True
        return False

    def _handle_user_message(
        self,
        message: dict,
        ts: datetime,
        transcript: list[TranscriptRecord],
    ) -> None:
        """Project a Pi user message into a user_message transcript record."""
        content = message.get("content", [])
        if _is_real_user_message(message):
            text = _content_text(content)
            if text and not self._session_title:
                self._session_title = " ".join(text.split()) or None
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.PI,
                    role="user",
                    kind="user_message",
                    data={"text": text},
                )
            )

    def _handle_assistant_message(
        self,
        message: dict,
        ts: datetime,
        transcript: list[TranscriptRecord],
    ) -> None:
        """Project a Pi assistant message: text + usage vendor_data + tool calls."""
        content = message.get("content") or []
        text_parts = [
            block
            for block in content
            if isinstance(block, dict) and block.get("type") in ("text",)
        ]
        text = _content_text(text_parts)
        thinking = _thinking_blocks(content)
        usage = _parse_usage(message)
        vendor_data: dict = {}
        if thinking:
            vendor_data["thinking"] = thinking
        if usage:
            normalized = normalize_pi_usage(
                provider=message.get("provider") or self._current_provider,
                model=message.get("model") or self._current_model,
                usage=usage,
            )
            vendor_data.update(normalized)
        stop_reason = message.get("stopReason")
        if stop_reason:
            vendor_data["stop_reason"] = stop_reason

        tool_calls = _tool_calls_from_content(content)
        transcript.append(
            TranscriptRecord(
                sequence=len(transcript),
                timestamp=ts,
                vendor=Vendor.PI,
                role="assistant",
                kind="assistant_message",
                data={
                    "text": text,
                    "vendor_data": {
                        k: v for k, v in vendor_data.items() if v is not None
                    },
                },
            )
        )

        for tc in tool_calls:
            tool_call_id = tc.get("id")
            tool_name = tc.get("name")
            if tool_name == "bash" and isinstance(tool_call_id, str) and tool_call_id:
                self._pending_bash_tool_call_ids.append(tool_call_id)
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.PI,
                    role="assistant",
                    kind="tool_call",
                    data={
                        "tool_name": tool_name,
                        "tool_call_id": tool_call_id,
                        "input": tc.get("arguments"),
                        "item_kind": _pi_item_kind(tool_name),
                    },
                )
            )

    def _handle_tool_result_message(
        self,
        message: dict,
        ts: datetime,
        transcript: list[TranscriptRecord],
    ) -> None:
        """Project a Pi toolResult message into a tool_result transcript record."""
        output = message.get("content")
        tool_name = message.get("toolName", "")
        tool_call_id = message.get("toolCallId", "")
        is_error = message.get("isError", False)
        details = message.get("details")

        exit_code = None
        if tool_name == "bash" and isinstance(details, dict):
            exit_code = details.get("exitCode")
            if exit_code is None and isinstance(output, list):
                for block in output:
                    if isinstance(block, dict) and block.get("type") == "text":
                        exit_code = extract_exit_code(block.get("text", ""))
                        break

        success = infer_tool_success(output)
        status = (
            ToolStatus.FAILED.value
            if (is_error or success is False)
            else ToolStatus.COMPLETED.value
        )

        transcript.append(
            TranscriptRecord(
                sequence=len(transcript),
                timestamp=ts,
                vendor=Vendor.PI,
                role="tool",
                kind="tool_result",
                data={
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "output": output,
                    "exit_code": exit_code,
                    "status": status,
                },
                fidelity="synthetic",
            )
        )

    def _handle_bash_execution(
        self,
        message: dict,
        ts: datetime,
        transcript: list[TranscriptRecord],
    ) -> None:
        """Project a Pi bashExecution message into a tool_result transcript record."""
        command = message.get("command", "")
        output = message.get("output", "")
        exit_code = message.get("exitCode")
        text_content = f"$ {command}\n{output}"
        if message.get("cancelled"):
            text_content = f"$ {command}\n[cancelled]\n{output}"
        tool_call_id = (
            self._pending_bash_tool_call_ids.pop(0)
            if self._pending_bash_tool_call_ids
            else None
        )
        transcript.append(
            TranscriptRecord(
                sequence=len(transcript),
                timestamp=ts,
                vendor=Vendor.PI,
                role="tool",
                kind="tool_result",
                data={
                    "tool_call_id": tool_call_id,
                    "tool_name": "bash",
                    "output": text_content,
                    "exit_code": exit_code,
                    "status": ToolStatus.FAILED.value
                    if exit_code and exit_code != 0
                    else ToolStatus.COMPLETED.value,
                },
                fidelity="synthetic",
            )
        )

    def _resolved_session_id(self, source: Path) -> UUID:
        raw_id = self._session_raw_id
        if raw_id is not None:
            try:
                return UUID(raw_id)
            except ValueError:
                return uuid5(NAMESPACE_URL, f"pi:{source}:{raw_id}")
        return uuid5(NAMESPACE_URL, f"pi:{source}")

    def ingest_default(self) -> list[Session]:
        sessions: list[Session] = []
        for jsonl_path in sorted(_DEFAULT_PI_SESSIONS_DIR.rglob("*.jsonl")):
            try:
                sessions.append(self.ingest_file(jsonl_path))
            except Exception as exc:
                logger.warning("PiAdapter: failed to ingest %s: %s", jsonl_path, exc)
        return sessions
