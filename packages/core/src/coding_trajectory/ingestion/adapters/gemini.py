"""Gemini CLI adapter — reads session JSON files and normalises them into canonical models."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from coding_trajectory.ingestion.adapters.base import BaseAdapter
from coding_trajectory.ingestion.common import compact_dict, parse_iso_timestamp
from coding_trajectory.ingestion.models import (
    Event,
    EventType,
    GeminiExtensions,
    Session,
    Step,
    ToolStatus,
    Turn,
    Vendor,
    VendorExtensions,
)
from coding_trajectory.ingestion.step_items import append_text_item, append_tool_item

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
        events = self._parse_events(session_id, str(source), messages)

        turns = self._build_turns(session_id, messages, events)

        extensions = VendorExtensions(
            gemini=GeminiExtensions(
                session_file=str(source),
                raw_tool_type=None,
                model_version=None,
                realtime_active=None,
            )
        )

        return Session(
            session_id=session_id,
            trajectory_id=uuid4(),
            vendor=self.vendor,
            started_at=started_at,
            ended_at=ended_at,
            events=events,
            turns=turns,
            extensions=extensions,
        )

    def _parse_events(
        self,
        session_id: UUID,
        session_file: str,
        messages: list[dict],
    ) -> list[Event]:
        events: list[Event] = []

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            events.extend(self._ingest_message(msg, session_id, session_file))

        return events

    def _build_turns(
        self,
        session_id: UUID,
        messages: list[dict],
        all_events: list[Event],
    ) -> list[Turn]:
        """Group messages into turns. Each user message starts a new turn."""
        turns: list[Turn] = []
        turn_sequence = 0
        step_sequence = 0
        current_turn: Turn | None = None

        # Index events by timestamp
        event_index: dict[datetime, list[Event]] = {}
        for ev in all_events:
            if ev.timestamp not in event_index:
                event_index[ev.timestamp] = []
            event_index[ev.timestamp].append(ev)

        for msg in messages:
            if not isinstance(msg, dict):
                continue

            msg_type = msg.get("type", "")
            timestamp = parse_iso_timestamp(msg.get("timestamp"))
            if timestamp is None:
                continue

            if msg_type == "user":
                # New turn
                if current_turn is not None:
                    if current_turn.ended_at is None:
                        current_turn.ended_at = timestamp
                    turns.append(current_turn)

                user_evs = [
                    e for e in event_index.get(timestamp, [])
                    if e.type == EventType.USER_PROMPT_SUBMITTED
                ]
                user_ev = user_evs[0] if user_evs else None

                current_turn = Turn(
                    session_id=session_id,
                    sequence=turn_sequence,
                    started_at=timestamp,
                    user_request_event_id=user_ev.event_id if user_ev else None,
                    event_ids=[event.event_id for event in event_index.get(timestamp, [])],
                )
                turn_sequence += 1
                step_sequence = 0

            elif msg_type == "gemini":
                if current_turn is None:
                    continue

                content = msg.get("content")
                thoughts = msg.get("thoughts") or []
                tool_calls = msg.get("toolCalls") or []
                model = msg.get("model")
                tokens = msg.get("tokens") or {}

                # Collect event IDs for this step
                step_event_ids: list[UUID] = []
                for ev in event_index.get(timestamp, []):
                    if ev.event_id not in current_turn.event_ids:
                        current_turn.event_ids.append(ev.event_id)
                    if ev.type in (
                        EventType.TOOL_CALL_REQUESTED,
                        EventType.TOOL_CALL_SUCCEEDED,
                        EventType.TOOL_CALL_FAILED,
                        EventType.LLM_RESPONSE,
                    ):
                        step_event_ids.append(ev.event_id)
                    # Also collect tool call events at tool timestamps
                for tc in tool_calls:
                    tc_ts = parse_iso_timestamp(tc.get("timestamp")) or timestamp
                    if tc_ts != timestamp:
                        for ev in event_index.get(tc_ts, []):
                            if ev.type in (
                                EventType.TOOL_CALL_REQUESTED,
                                EventType.TOOL_CALL_SUCCEEDED,
                                EventType.TOOL_CALL_FAILED,
                            ):
                                if ev.event_id not in current_turn.event_ids:
                                    current_turn.event_ids.append(ev.event_id)
                                if ev.event_id not in step_event_ids:
                                    step_event_ids.append(ev.event_id)

                vendor_data: dict = {}
                if thoughts:
                    vendor_data["thoughts"] = [
                        {"subject": t.get("subject"), "description": t.get("description")}
                        for t in thoughts
                        if isinstance(t, dict)
                    ]
                if model:
                    vendor_data["model"] = model
                if tokens:
                    vendor_data["tokens"] = tokens

                items = []
                response_event_ids = [
                    ev.event_id
                    for ev in event_index.get(timestamp, [])
                    if ev.type == EventType.LLM_RESPONSE
                ]
                append_text_item(items, _content_to_text(content), event_ids=response_event_ids)
                for tc in tool_calls:
                    tc_ts = parse_iso_timestamp(tc.get("timestamp")) or timestamp
                    tool_event_ids = [
                        ev.event_id
                        for ev in event_index.get(tc_ts, [])
                        if ev.payload.get("tool_call_id") == tc.get("id")
                    ]
                    status = tc.get("status")
                    tool_status = ToolStatus.REQUESTED
                    if status == "success":
                        tool_status = ToolStatus.COMPLETED
                    elif status in ("error", "cancelled"):
                        tool_status = ToolStatus.FAILED
                    elif status in ("running", "in_progress"):
                        tool_status = ToolStatus.IN_PROGRESS
                    append_tool_item(
                        items,
                        tool_name=tc.get("name"),
                        tool_call_id=tc.get("id"),
                        input=tc.get("args"),
                        output=tc.get("resultDisplay") or tc.get("result"),
                        status=tool_status,
                        event_ids=tool_event_ids,
                    )

                step = Step(
                    session_id=session_id,
                    turn_id=current_turn.turn_id,
                    sequence=step_sequence,
                    timestamp=timestamp,
                    vendor=Vendor.GEMINI_CLI,
                    items=items,
                    vendor_data=vendor_data,
                    event_ids=step_event_ids,
                )
                step_sequence += 1
                current_turn.steps.append(step)
                current_turn.ended_at = timestamp

        if current_turn is not None:
            if current_turn.ended_at is None and all_events:
                current_turn.ended_at = max(e.timestamp for e in all_events)
            turns.append(current_turn)

        return turns

    def _ingest_message(self, msg: dict, session_id: UUID, session_file: str) -> list[Event]:
        events: list[Event] = []
        msg_type = msg.get("type", "")
        msg_id = msg.get("id")
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

        # thoughts → go into vendor_data (not emitted as events)

        for tool_call in tool_calls:
            tool_ts = parse_iso_timestamp(tool_call.get("timestamp")) or timestamp
            tool_payload = compact_dict(
                {
                    **base_payload,
                    "tool_call_id": tool_call.get("id"),
                    "tool_name": tool_call.get("name"),
                    "tool_args": tool_call.get("args"),
                    "tool_status": tool_call.get("status"),
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
            result_payload = compact_dict(
                {
                    **tool_payload,
                    "result": tool_call.get("result"),
                    "result_display": tool_call.get("resultDisplay"),
                }
            )

            if status == "success":
                event_type: EventType | None = EventType.TOOL_CALL_SUCCEEDED
            elif status in ("error", "cancelled"):
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
                        payload=result_payload,
                    )
                )

        response_text = _content_to_text(content)
        if response_text:
            events.append(
                Event(
                    session_id=session_id,
                    timestamp=timestamp,
                    type=EventType.LLM_RESPONSE,
                    vendor_source=self.vendor,
                    actor="assistant",
                    payload=compact_dict(
                        {
                            **base_payload,
                            "text": response_text,
                            "model": model,
                        }
                    ),
                )
            )

        return events
