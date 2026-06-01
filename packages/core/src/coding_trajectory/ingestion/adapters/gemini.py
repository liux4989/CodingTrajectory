"""Gemini CLI adapter — reads session JSON files and normalises them into canonical models."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from coding_trajectory.ingestion.adapters.base import BaseAdapter
from coding_trajectory.ingestion.common import parse_iso_timestamp
from coding_trajectory.ingestion.models import (
    GeminiExtensions,
    Session,
    ToolStatus,
    Vendor,
    VendorExtensions,
)
from coding_trajectory.ingestion.transcript import TranscriptRecord, events_from_transcript, project_transcript
from coding_trajectory.ingestion.vendor_mechanisms.usage_metrics import normalize_gemini_usage

logger = logging.getLogger(__name__)


def _as_non_empty_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _record_title(record: dict[str, Any]) -> str | None:
    for key in ("title", "sessionTitle", "conversationTitle", "threadName"):
        title = _as_non_empty_str(record.get(key))
        if title:
            return title
    return None


def _content_to_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        texts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and "text" in part
        ]
        joined = " ".join(text for text in texts if text).strip()
        return joined or None
    return None


class GeminiAdapter(BaseAdapter):
    """Adapter for Gemini CLI session JSON files."""

    vendor = Vendor.GEMINI_CLI
    _file_glob = "*.json"

    _current_record: dict[str, Any] | None

    def _reset_ingest_state(self) -> None:
        self._current_record = None

    def _load_records(self, path: Path) -> list[dict]:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("GeminiAdapter: failed to read %s: %s", path, exc)
            return []
        return [record] if isinstance(record, dict) else []

    def _build_session(self, source: Path, records: list[dict]) -> Session:
        if not records:
            raise ValueError(f"GeminiAdapter: no valid records found in {source}")

        record = records[0]
        self._current_record = record

        raw_sid = record.get("sessionId")
        try:
            session_id = UUID(raw_sid) if raw_sid else uuid4()
        except ValueError:
            session_id = uuid4()

        started_at = parse_iso_timestamp(record.get("startTime")) or datetime.now(timezone.utc)
        ended_at = parse_iso_timestamp(record.get("lastUpdated"))

        messages = record.get("messages") or []
        transcript = self._build_transcript(str(source), messages)
        events = events_from_transcript(session_id=session_id, records=transcript)
        turns = project_transcript(
            session_id=session_id,
            vendor=Vendor.GEMINI_CLI,
            records=transcript,
        )

        extensions = VendorExtensions(
            gemini=GeminiExtensions(
                session_file=str(source),
                title=_record_title(record),
                raw_tool_type=None,
                model_version=None,
                realtime_active=None,
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
        )

    def _build_transcript(
        self,
        session_file: str,
        messages: list[dict],
    ) -> list[TranscriptRecord]:
        """Extract only CT-useful transcript facts from Gemini messages."""
        transcript: list[TranscriptRecord] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue

            msg_type = msg.get("type", "")
            timestamp = parse_iso_timestamp(msg.get("timestamp"))
            if timestamp is None:
                continue

            if msg_type == "user":
                transcript.append(
                    TranscriptRecord(
                        sequence=len(transcript),
                        timestamp=timestamp,
                        vendor=Vendor.GEMINI_CLI,
                        role="user",
                        kind="user_message",
                        data={
                            "message_id": msg.get("id"),
                            "session_file": session_file,
                            "text": _content_to_text(msg.get("content")),
                        },
                    )
                )

            elif msg_type == "gemini":
                content = msg.get("content")
                thoughts = msg.get("thoughts") or []
                tool_calls = msg.get("toolCalls") or []
                model = msg.get("model")
                tokens = msg.get("tokens") or {}
                vendor_data: dict = {}
                if thoughts:
                    vendor_data["thoughts"] = [
                        {"subject": t.get("subject"), "description": t.get("description")}
                        for t in thoughts
                        if isinstance(t, dict)
                    ]
                if tokens:
                    vendor_data.update(normalize_gemini_usage(model=model, tokens=tokens))

                transcript.append(
                    TranscriptRecord(
                        sequence=len(transcript),
                        timestamp=timestamp,
                        vendor=Vendor.GEMINI_CLI,
                        role="assistant",
                        kind="assistant_message",
                        data={
                            "message_id": msg.get("id"),
                            "session_file": session_file,
                            "text": _content_to_text(content),
                            "vendor_data": vendor_data,
                            "flush_after": not tool_calls,
                        },
                    )
                )

                for index, tc in enumerate(tool_calls):
                    tc_ts = parse_iso_timestamp(tc.get("timestamp")) or timestamp
                    status = tc.get("status")
                    tool_status = ToolStatus.REQUESTED.value
                    if status == "success":
                        tool_status = ToolStatus.COMPLETED.value
                    elif status in ("error", "cancelled"):
                        tool_status = ToolStatus.FAILED.value
                    elif status in ("running", "in_progress"):
                        tool_status = ToolStatus.IN_PROGRESS.value
                    else:
                        tool_status = ToolStatus.REQUESTED.value
                    transcript.append(
                        TranscriptRecord(
                            sequence=len(transcript),
                            timestamp=tc_ts,
                            vendor=Vendor.GEMINI_CLI,
                            role="assistant",
                            kind="tool_call",
                            data={
                                "message_id": msg.get("id"),
                                "session_file": session_file,
                                "tool_name": tc.get("name"),
                                "tool_call_id": tc.get("id"),
                                "input": tc.get("args"),
                                "output": tc.get("resultDisplay") or tc.get("result"),
                                "status": tool_status,
                                "flush_after": index == len(tool_calls) - 1,
                            },
                            fidelity="synthetic",
                        )
                    )

        return transcript
