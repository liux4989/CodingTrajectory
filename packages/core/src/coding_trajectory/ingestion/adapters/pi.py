"""Pi coding agent adapter — reads ~/.pi/agent/sessions/**/*.jsonl session files."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from coding_trajectory.ingestion.adapters.base import BaseAdapter
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
from coding_trajectory.ingestion.transcript import TranscriptRecord, events_from_transcript, project_transcript
from coding_trajectory.ingestion.vendor_mechanisms.usage_metrics import normalize_claude_usage

logger = logging.getLogger(__name__)

_DEFAULT_PI_SESSIONS_DIR = Path.home() / ".pi" / "agent" / "sessions"


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
        block for block in content
        if isinstance(block, dict) and block.get("type") == "toolCall"
    ]


def _thinking_blocks(content: list[dict]) -> list[str]:
    if not isinstance(content, list):
        return []
    return [
        block.get("thinking", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "thinking" and block.get("thinking")
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
    _pending_bash_tool_call_ids: list[str]

    def _reset_ingest_state(self) -> None:
        self._current_provider = None
        self._current_model = None
        self._current_thinking_level = None
        self._current_cwd = None
        self._session_title = None
        self._pending_bash_tool_call_ids = []

    def _build_session(self, source: Path, records: list[dict]) -> Session:
        transcript = self._build_transcript(records)
        session_id = self._session_id(source, records)
        if not transcript:
            raise ValueError(f"PiAdapter: no transcript records parsed from {source}")

        events = events_from_transcript(session_id=session_id, records=transcript)
        turns = project_transcript(
            session_id=session_id,
            vendor=Vendor.PI,
            records=transcript,
        )

        started_at = min(record.timestamp for record in transcript)
        ended_at = max(record.timestamp for record in transcript)

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
            extensions=extensions,
            status=SessionStatus.COMPLETED,
        )

    def _build_transcript(self, records: list[dict]) -> list[TranscriptRecord]:
        transcript: list[TranscriptRecord] = []

        for record in records:
            entry_type = record.get("type", "")

            if entry_type == "session":
                self._current_cwd = record.get("cwd")
                continue

            if entry_type == "model_change":
                self._current_provider = record.get("provider")
                self._current_model = record.get("modelId")
                continue

            if entry_type == "thinking_level_change":
                self._current_thinking_level = record.get("thinkingLevel")
                continue

            if entry_type == "session_info":
                self._session_title = record.get("name") or self._session_title
                continue

            if entry_type not in ("message",):
                continue

            message = record.get("message")
            if not isinstance(message, dict):
                continue

            role = message.get("role")
            ts = parse_iso_timestamp(record.get("timestamp"))
            if ts is None:
                continue

            if role == "user":
                content = message.get("content", [])
                if _is_real_user_message(message):
                    text = _content_text(content)
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

            elif role == "assistant":
                content = message.get("content") or []
                text_parts = [
                    block for block in content
                    if isinstance(block, dict) and block.get("type") in ("text",)
                ]
                text = _content_text(text_parts)
                thinking = _thinking_blocks(content)
                usage = _parse_usage(message)
                vendor_data: dict = {}
                if thinking:
                    vendor_data["thinking"] = thinking
                if usage:
                    normalized = normalize_claude_usage(
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
                            "vendor_data": {k: v for k, v in vendor_data.items() if v is not None},
                            "flush_after": not tool_calls,
                        },
                    )
                )

                for index, tc in enumerate(tool_calls):
                    tool_call_id = tc.get("id")
                    if tc.get("name") == "bash" and isinstance(tool_call_id, str) and tool_call_id:
                        self._pending_bash_tool_call_ids.append(tool_call_id)
                    transcript.append(
                        TranscriptRecord(
                            sequence=len(transcript),
                            timestamp=ts,
                            vendor=Vendor.PI,
                            role="assistant",
                            kind="tool_call",
                            data={
                                "tool_name": tc.get("name"),
                                "tool_call_id": tool_call_id,
                                "input": tc.get("arguments"),
                                "flush_after": index == len(tool_calls) - 1,
                            },
                        )
                    )

            elif role == "toolResult":
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
                status = ToolStatus.FAILED.value if (is_error or success is False) else ToolStatus.COMPLETED.value

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
                            "attach_to_previous_step": True,
                        },
                        fidelity="synthetic",
                    )
                )

            elif role == "bashExecution":
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
                            "status": ToolStatus.FAILED.value if exit_code and exit_code != 0 else ToolStatus.COMPLETED.value,
                            "attach_to_previous_step": True,
                        },
                        fidelity="synthetic",
                    )
                )

        return transcript

    def _session_id(self, source: Path, records: list[dict]) -> UUID:
        for record in records:
            if record.get("type") != "session":
                continue
            raw_id = record.get("id")
            if isinstance(raw_id, str):
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
