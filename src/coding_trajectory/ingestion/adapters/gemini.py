"""Gemini CLI adapter — reads session JSON files and normalises them into canonical models."""

from __future__ import annotations

import json
import logging
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from coding_trajectory.ingestion.adapters.base import BaseAdapter
from coding_trajectory.ingestion.common import compact_dict, parse_iso_timestamp
from coding_trajectory.ingestion.models import (
    EventConfidence,
    Event,
    EventProvenance,
    EventType,
    GeminiExtensions,
    Session,
    Vendor,
    VendorExtensions,
)

logger = logging.getLogger(__name__)


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


def _token_payload(tokens: dict[str, Any] | None) -> dict[str, Any]:
    tokens = tokens or {}
    return compact_dict(
        {
            "token_input": tokens.get("input"),
            "token_output": tokens.get("output"),
            "token_cached": tokens.get("cached"),
            "token_thoughts": tokens.get("thoughts"),
            "token_tool": tokens.get("tool"),
            "token_total": tokens.get("total"),
        }
    )


class GeminiAdapter(BaseAdapter):
    """Adapter for Gemini CLI session JSON files."""

    vendor = Vendor.GEMINI_CLI
    _file_glob = "*.json"

    _current_record: dict[str, Any] | None

    def _reset_ingest_state(self) -> None:
        self._current_record = None

    def _parse_raw_line(self, line: dict) -> Event | None:
        warnings.warn(
            "_parse_raw_line called directly on GeminiAdapter; use ingest_file instead.",
            stacklevel=2,
        )
        return None

    def _load_records(self, path: Path) -> list[dict]:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("GeminiAdapter: failed to read %s: %s", path, exc)
            return []
        return [record] if isinstance(record, dict) else []

    def _parse_record(self, source: Path, record: dict) -> list[Event]:
        self._current_record = record

        raw_sid = record.get("sessionId")
        try:
            session_id = UUID(raw_sid) if raw_sid else uuid4()
        except ValueError:
            session_id = uuid4()

        started_at = parse_iso_timestamp(record.get("startTime")) or datetime.now(timezone.utc)
        ended_at = parse_iso_timestamp(record.get("lastUpdated"))

        events: list[Event] = [
            Event(
                session_id=session_id,
                timestamp=started_at,
                type=EventType.SESSION_STARTED,
                vendor_source=self.vendor,
                actor="system",
                payload=compact_dict(
                    {
                        "project_hash": record.get("projectHash"),
                        "summary": record.get("summary"),
                        "kind": record.get("kind"),
                    }
                ),
            )
        ]

        for msg in record.get("messages") or []:
            if isinstance(msg, dict):
                events.extend(self._ingest_message(msg, session_id, str(source)))

        if ended_at is not None:
            events.append(
                Event(
                    session_id=session_id,
                    timestamp=ended_at,
                    type=EventType.SESSION_ENDED,
                    vendor_source=self.vendor,
                    actor="system",
                    payload={},
                )
            )

        return events

    def _ingest_message(self, msg: dict, session_id: UUID, session_file: str) -> list[Event]:
        events: list[Event] = []
        msg_type = msg.get("type", "")
        msg_id = msg.get("id", str(uuid4()))
        timestamp = parse_iso_timestamp(msg.get("timestamp"))
        if timestamp is None:
            logger.warning("GeminiAdapter: missing or bad timestamp in message %s", msg_id)
            return events

        content = msg.get("content")
        base_payload = compact_dict(
            {
                "message_id": msg_id,
                "session_file": session_file,
            }
        )

        if msg_type == "user":
            content_parts = content if isinstance(content, list) else None
            events.append(
                Event(
                    session_id=session_id,
                    timestamp=timestamp,
                    type=EventType.USER_PROMPT_SUBMITTED,
                    vendor_source=self.vendor,
                    actor="user",
                    payload=compact_dict(
                        {
                            **base_payload,
                            "text": _content_to_text(content),
                            "content_parts": content_parts,
                        }
                    ),
                )
            )
            return events

        if msg_type != "gemini":
            return events

        model = msg.get("model")
        tokens = msg.get("tokens") or {}
        thoughts = msg.get("thoughts") or []
        tool_calls = msg.get("toolCalls") or []

        for thought in thoughts:
            thought_ts = parse_iso_timestamp(thought.get("timestamp")) or timestamp
            events.append(
                Event(
                    session_id=session_id,
                    timestamp=thought_ts,
                    type=EventType.LLM_STREAM_EVENT,
                    vendor_source=self.vendor,
                    actor="assistant",
                    payload=compact_dict(
                        {
                            **base_payload,
                            "thought_subject": thought.get("subject"),
                            "thought_description": thought.get("description"),
                            "thought_timestamp": thought.get("timestamp"),
                        }
                    ),
                )
            )

        for tool_call in tool_calls:
            tool_ts = parse_iso_timestamp(tool_call.get("timestamp")) or timestamp
            tool_payload = compact_dict(
                {
                    **base_payload,
                    "tool_call_id": tool_call.get("id"),
                    "tool_name": tool_call.get("name"),
                    "tool_display_name": tool_call.get("displayName"),
                    "tool_description": tool_call.get("description"),
                    "tool_args": tool_call.get("args"),
                    "tool_status": tool_call.get("status"),
                    "render_output_as_markdown": tool_call.get("renderOutputAsMarkdown"),
                    "model": model,
                }
            )
            events.append(
                Event(
                    session_id=session_id,
                    timestamp=tool_ts,
                    type=EventType.TOOL_CALL_REQUESTED,
                    vendor_source=self.vendor,
                    actor="assistant",
                    payload=tool_payload,
                )
            )

            status = tool_call.get("status")
            result_text = json.dumps(tool_call.get("result")) if not isinstance(tool_call.get("result"), str) else tool_call.get("result")
            result_payload = compact_dict(
                {
                    **tool_payload,
                    "result": tool_call.get("result"),
                    "result_display": tool_call.get("resultDisplay"),
                }
            )

            if status == "success":
                event_type = EventType.TOOL_CALL_SUCCEEDED
            elif status == "error":
                event_type = EventType.TOOL_CALL_FAILED
            elif status == "cancelled" and result_text and "user did not allow tool call" in result_text.lower():
                event_type = EventType.PERMISSION_DENIED
            elif status == "cancelled":
                event_type = EventType.TOOL_CALL_FAILED
            else:
                event_type = None

            if event_type is not None:
                events.append(
                    Event(
                        session_id=session_id,
                        timestamp=tool_ts,
                        type=event_type,
                        vendor_source=self.vendor,
                        actor="tool",
                        provenance=(
                            EventProvenance.DERIVED
                            if event_type == EventType.PERMISSION_DENIED
                            else EventProvenance.OBSERVED
                        ),
                        confidence=(
                            EventConfidence.LOW
                            if event_type == EventType.PERMISSION_DENIED
                            else EventConfidence.HIGH
                        ),
                        payload=result_payload,
                    )
                )

        response_text = _content_to_text(content)
        llm_payload = compact_dict(
            {
                **base_payload,
                "text": response_text,
                "model": model,
                "thought_count": len(thoughts),
                **_token_payload(tokens),
            }
        )
        events.append(
            Event(
                session_id=session_id,
                timestamp=timestamp,
                type=EventType.LLM_REQUEST_COMPLETED,
                vendor_source=self.vendor,
                actor="assistant",
                payload=llm_payload,
            )
        )
        if response_text:
            events.append(
                Event(
                    session_id=session_id,
                    timestamp=timestamp,
                    type=EventType.AGENT_RESPONSE_COMPLETED,
                    vendor_source=self.vendor,
                    actor="assistant",
                    payload=llm_payload,
                )
            )

        return events

    def _build_session(self, source: Path, events: list[Event]) -> Session:
        record = self._current_record or {}
        raw_sid = record.get("sessionId")
        try:
            session_id = UUID(raw_sid) if raw_sid else uuid4()
        except ValueError:
            session_id = uuid4()

        started_at = parse_iso_timestamp(record.get("startTime")) or datetime.now(timezone.utc)
        ended_at = parse_iso_timestamp(record.get("lastUpdated"))

        extensions = VendorExtensions(
            gemini=GeminiExtensions(
                session_file=str(source),
                raw_tool_type=None,
                model_version=None,
                realtime_active=None,
            )
        )
        turns = self._group_into_turns(session_id, events)

        return Session(
            session_id=session_id,
            trajectory_id=uuid4(),
            vendor=self.vendor,
            started_at=started_at,
            ended_at=ended_at,
            timeline=self._build_timeline(events, turns),
            events=events,
            turns=turns,
            extensions=extensions,
        )
