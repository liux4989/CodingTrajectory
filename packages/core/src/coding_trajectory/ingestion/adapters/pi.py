"""Pi coding agent adapter — reads ~/.pi/agent/sessions/**/*.jsonl session files."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from coding_trajectory.ingestion.adapters._shared import (
    SHARED_FILE_TOOL_NAMES,
    SHARED_PLAN_TOOL_NAMES,
    HeaderFacts,
    ToolTaxonomy,
    collapse_whitespace,
    content_block_field_texts,
    content_block_texts,
    content_blocks,
    scan_header_records,
)
from coding_trajectory.ingestion.adapters.base import BaseAdapter, SessionHeader
from coding_trajectory.ingestion.assembly import AssemblyHooks, assemble_session
from coding_trajectory.ingestion.common import (
    extract_exit_code,
    infer_tool_success,
    parse_iso_timestamp,
)
from coding_trajectory.ingestion.models import (
    PiExtensions,
    Session,
    ToolStatus,
    Vendor,
    VendorExtensions,
)
from coding_trajectory.ingestion.provenance import RecordSpan
from coding_trajectory.ingestion.retention import CanonicalRetention
from coding_trajectory.ingestion.transcript import TranscriptRecord
from coding_trajectory.ingestion.vendor_mechanisms.usage_metrics import (
    context_usage_observation,
    normalize_pi_usage,
)

logger = logging.getLogger(__name__)


_PI_TOOL_TAXONOMY = ToolTaxonomy(
    command_names=frozenset({"bash"}),
    plan_names=SHARED_PLAN_TOOL_NAMES,
    file_change_names=SHARED_FILE_TOOL_NAMES
    | frozenset(
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
        }
    ),
)


def _pi_item_kind(tool_name: str | None) -> str:
    return _PI_TOOL_TAXONOMY.classify(tool_name)


def _content_text(content: Any) -> str | None:
    if not isinstance(content, str | list):
        return None
    return content_block_texts(content)


def _tool_calls_from_content(content: list[dict]) -> list[dict]:
    return content_blocks(content, "toolCall")


def _thinking_blocks(content: list[dict]) -> list[str]:
    return content_block_field_texts(content, "thinking", "thinking")


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
        id_resolved = False

        def extract(record: dict) -> HeaderFacts:
            nonlocal id_resolved
            entry_type = record.get("type")
            session_id: UUID | None = None
            cwd: str | None = None
            title: str | None = None
            if entry_type == "session" and not id_resolved:
                raw_id = record.get("id")
                if isinstance(raw_id, str):
                    try:
                        session_id = UUID(raw_id)
                    except ValueError:
                        session_id = uuid5(NAMESPACE_URL, f"pi:{source}:{raw_id}")
                    id_resolved = True
                cwd = record.get("cwd") or None
            elif entry_type == "session_info":
                name = record.get("name")
                title = name if name else None
            elif entry_type == "message":
                message = record.get("message")
                if (
                    isinstance(message, dict)
                    and message.get("role") == "user"
                    and _is_real_user_message(message)
                ):
                    text = _content_text(message.get("content", []))
                    if text:
                        title = collapse_whitespace(text) or None
            return HeaderFacts(session_id=session_id, title=title, cwd=cwd)

        facts = scan_header_records(
            self._iter_records(source),
            extract=extract,
            lookahead=self._TITLE_LOOKAHEAD,
        )
        session_id = facts.session_id
        if session_id is None:
            session_id = uuid5(NAMESPACE_URL, f"pi:{source}")
        return SessionHeader(
            session_id=session_id,
            vendor=Vendor.PI,
            parent_session_id=None,
            title=facts.title,
            cwd=facts.cwd,
        )

    def _build_session(
        self,
        source: Path,
        records: Iterable[tuple[dict, RecordSpan | None]],
        *,
        retention: CanonicalRetention = "trajectory",
    ) -> Session:
        transcript = self._build_transcript(records)
        session_id = self._resolved_session_id(source)
        if not transcript:
            raise ValueError(f"PiAdapter: no transcript records parsed from {source}")

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
        hooks = AssemblyHooks(
            extensions=extensions,
            build_context_usage=lambda records_: [
                observation
                for record in records_
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
            ],
            provenance_sink=lambda provenance: setattr(
                self, "last_provenance", provenance
            ),
        )
        return assemble_session(
            vendor=Vendor.PI,
            source=source,
            session_id=session_id,
            transcript=transcript,
            retention=retention,
            hooks=hooks,
        )

    def _build_transcript(
        self, records: Iterable[tuple[dict, RecordSpan | None]]
    ) -> list[TranscriptRecord]:
        """Extract only CT-useful transcript facts from Pi JSONL records."""
        transcript: list[TranscriptRecord] = []

        for record, span in records:
            before = len(transcript)
            self._translate_record(record, transcript)
            if span is not None:
                for entry in transcript[before:]:
                    entry.origin = span

        return transcript

    def _translate_record(
        self, record: dict, transcript: list[TranscriptRecord]
    ) -> None:
        if self._handle_state_record(record):
            return

        if record.get("type", "") != "message":
            return

        message = record.get("message")
        if not isinstance(message, dict):
            return

        role = message.get("role")
        ts = parse_iso_timestamp(record.get("timestamp"))
        if ts is None:
            return

        if role == "user":
            self._handle_user_message(message, ts, transcript)
        elif role == "assistant":
            self._handle_assistant_message(message, ts, transcript)
        elif role == "toolResult":
            self._handle_tool_result_message(message, ts, transcript)
        elif role == "bashExecution":
            self._handle_bash_execution(message, ts, transcript)

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
                self._session_title = collapse_whitespace(text) or None
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
