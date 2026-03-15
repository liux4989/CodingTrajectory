"""Codex CLI adapter — reads ~/.codex/sessions/**/*.jsonl rollout files."""

from __future__ import annotations

import json
import logging
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from coding_trajectory.ingestion.adapters.base import BaseAdapter
from coding_trajectory.ingestion.common import (
    compact_dict,
    extract_exit_code,
    infer_tool_success,
    parse_iso_timestamp,
)
from coding_trajectory.ingestion.models import (
    CodexExtensions,
    Event,
    EventType,
    Session,
    Step,
    Turn,
    Vendor,
    VendorExtensions,
)

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


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

        events: list[Event] = []
        for record in records:
            parsed = self._parse_raw_line_with_state(record, state)
            if isinstance(parsed, list):
                events.extend(parsed)
            elif parsed is not None:
                events.append(parsed)

        return self._build_session(path, records, events, state)

    def _build_session(
        self,
        source: Path,
        records: list[dict],
        events: list[Event],
        state: _ParseState | None = None,
    ) -> Session:
        state = state or self._ParseState()
        if not events:
            warnings.warn(f"No events parsed from {source}", stacklevel=2)
            started_at = _now_utc()
            ended_at = None
        else:
            started_at = min(e.timestamp for e in events)
            ended_at = max(e.timestamp for e in events)

        meta = state.session_meta
        ctx = state.turn_context
        sandbox_policy = ctx.get("sandbox_policy") or {}
        normalized_events = [
            event if event.session_id == state.session_id
            else event.model_copy(update={"session_id": state.session_id})
            for event in events
        ]

        extensions = VendorExtensions(
            codex=CodexExtensions(
                sandbox_id=meta.get("id"),
                sandbox_mode=sandbox_policy.get("type"),
                approval_policy=ctx.get("approval_policy"),
                collaboration_mode=(
                    ctx.get("collaboration_mode", {}).get("mode")
                    if isinstance(ctx.get("collaboration_mode"), dict)
                    else None
                ),
                agent_role=meta.get("source"),
                cwd=meta.get("cwd") if isinstance(meta.get("cwd"), str) else None,
            )
        )

        turns = self._build_turns(state.session_id, records, normalized_events)

        return Session(
            session_id=state.session_id,
            trajectory_id=uuid4(),
            vendor=Vendor.CODEX_CLI,
            started_at=started_at,
            ended_at=ended_at,
            events=normalized_events,
            turns=turns,
            extensions=extensions,
        )

    def _build_turns(
        self,
        session_id: UUID,
        records: list[dict],
        events: list[Event],
    ) -> list[Turn]:
        """One Step per turn bounded by task_started → task_complete."""
        turns: list[Turn] = []
        turn_sequence = 0

        # Index events by timestamp for association
        event_index: dict[datetime, list[Event]] = {}
        for ev in events:
            if ev.timestamp not in event_index:
                event_index[ev.timestamp] = []
            event_index[ev.timestamp].append(ev)

        current_turn: Turn | None = None
        current_step_event_ids: list[UUID] = []
        current_step_tool_calls: list[dict] = []
        current_step_commentary: list[str] = []
        current_step_model: str | None = None
        current_step_start_ts: datetime | None = None
        user_request_event_id: UUID | None = None

        def _flush_turn_with_step(last_msg: str | None, end_ts: datetime) -> None:
            nonlocal current_turn, current_step_event_ids, current_step_tool_calls
            nonlocal current_step_commentary, current_step_model, current_step_start_ts
            nonlocal user_request_event_id, turn_sequence

            if current_turn is None:
                return

            vendor_data: dict = {}
            if current_step_model:
                vendor_data["model"] = current_step_model
            if current_step_commentary:
                vendor_data["commentary"] = current_step_commentary
            if current_step_tool_calls:
                vendor_data["tool_calls"] = current_step_tool_calls

            step = Step(
                session_id=session_id,
                turn_id=current_turn.turn_id,
                sequence=0,
                timestamp=current_step_start_ts or current_turn.started_at,
                vendor=Vendor.CODEX_CLI,
                text=last_msg,
                vendor_data=vendor_data,
                event_ids=list(current_step_event_ids),
            )
            current_turn.steps.append(step)
            current_turn.ended_at = end_ts
            turns.append(current_turn)

            current_turn = None
            current_step_event_ids = []
            current_step_tool_calls = []
            current_step_commentary = []
            current_step_model = None
            current_step_start_ts = None
            user_request_event_id = None

        def _get_ts(record: dict) -> datetime:
            return parse_iso_timestamp(record.get("timestamp")) or _now_utc()

        for record in records:
            outer_type = record.get("type", "")
            payload = record.get("payload") or {}
            ts = _get_ts(record)

            if outer_type == "session_meta":
                continue

            if outer_type == "turn_context":
                continue

            if outer_type == "event_msg":
                inner_type = payload.get("type", "")

                if inner_type == "user_message":
                    # Find the USER_PROMPT_SUBMITTED event
                    user_evs = [
                        e for e in event_index.get(ts, [])
                        if e.type == EventType.USER_PROMPT_SUBMITTED
                    ]
                    user_ev = user_evs[0] if user_evs else None

                    # Start a new turn
                    current_turn = Turn(
                        session_id=session_id,
                        sequence=turn_sequence,
                        started_at=ts,
                        user_request_event_id=user_ev.event_id if user_ev else None,
                    )
                    turn_sequence += 1
                    current_step_start_ts = ts
                    if user_ev:
                        current_step_event_ids.append(user_ev.event_id)

                elif inner_type == "agent_message":
                    # Intermediate commentary
                    text = _extract_message_text(payload)
                    phase = payload.get("phase")
                    if phase == "commentary" and text:
                        current_step_commentary.append(text)
                    # Collect event IDs
                    for ev in event_index.get(ts, []):
                        current_step_event_ids.append(ev.event_id)

                elif inner_type == "task_complete":
                    last_msg = payload.get("last_agent_message")
                    if current_turn is not None:
                        # Collect task_complete event IDs
                        for ev in event_index.get(ts, []):
                            if ev.type == EventType.VENDOR_RAW:
                                current_step_event_ids.append(ev.event_id)
                        _flush_turn_with_step(last_msg, ts)

            elif outer_type == "response_item":
                inner_type = payload.get("type", "")
                if current_turn is None:
                    continue

                if inner_type == "function_call":
                    tool_name = payload.get("name")
                    tool_input = _parse_json_blob(payload.get("arguments"))
                    current_step_tool_calls.append({
                        "name": tool_name,
                        "id": payload.get("call_id"),
                        "input": tool_input,
                    })
                    # Collect events
                    for ev in event_index.get(ts, []):
                        if ev.type == EventType.TOOL_CALL_REQUESTED:
                            current_step_event_ids.append(ev.event_id)

                elif inner_type == "function_call_output":
                    for ev in event_index.get(ts, []):
                        if ev.type in (EventType.TOOL_CALL_SUCCEEDED, EventType.TOOL_CALL_FAILED):
                            current_step_event_ids.append(ev.event_id)

                elif inner_type == "reasoning":
                    # Encrypted, skip
                    pass

                elif inner_type == "message":
                    if payload.get("role") == "assistant":
                        for ev in event_index.get(ts, []):
                            current_step_event_ids.append(ev.event_id)

        # Flush any incomplete turn
        if current_turn is not None:
            last_ts = max((e.timestamp for e in events), default=current_turn.started_at)
            _flush_turn_with_step(None, last_ts)

        return turns

    def _event_from(
        self,
        *,
        session_id: UUID,
        event_type: EventType,
        timestamp: datetime,
        actor: str | None,
        payload: dict[str, Any],
    ) -> Event:
        return Event(
            session_id=session_id,
            timestamp=timestamp,
            type=event_type,
            vendor_source=Vendor.CODEX_CLI,
            actor=actor,
            payload=payload,
        )

    def _parse_raw_line_with_state(
        self,
        line: dict[str, Any],
        state: _ParseState,
    ) -> Event | list[Event] | None:  # noqa: C901
        try:
            outer_type: str = line.get("type", "")
            timestamp = parse_iso_timestamp(line.get("timestamp")) or _now_utc()
            payload: dict[str, Any] = line.get("payload") or {}

            if outer_type == "session_meta":
                sid_str = payload.get("id")
                if sid_str:
                    try:
                        state.session_id = UUID(sid_str)
                    except ValueError:
                        pass
                state.session_meta = payload
                return None  # No event emitted for session_meta

            if outer_type == "turn_context":
                state.turn_context = payload
                return None

            if outer_type == "event_msg":
                return self._parse_event_msg(payload, timestamp, state)

            if outer_type == "response_item":
                return self._parse_response_item(payload, timestamp, state)

        except Exception:
            logger.warning("Skipping malformed Codex log line: %r", line, exc_info=True)
            return None

        return None

    def _parse_event_msg(
        self,
        payload: dict[str, Any],
        timestamp: datetime,
        state: _ParseState,
    ) -> Event | None:
        inner_type = payload.get("type", "")
        turn_id = payload.get("turn_id") or state.turn_context.get("turn_id")

        if inner_type == "user_message":
            return self._event_from(
                session_id=state.session_id,
                event_type=EventType.USER_PROMPT_SUBMITTED,
                timestamp=timestamp,
                actor="user",
                payload=compact_dict(
                    {
                        "turn_id_raw": turn_id,
                        "text": _extract_message_text(payload),
                    }
                ),
            )

        if inner_type == "task_complete":
            return self._event_from(
                session_id=state.session_id,
                event_type=EventType.VENDOR_RAW,
                timestamp=timestamp,
                actor="assistant",
                payload=compact_dict(
                    {
                        "turn_id_raw": payload.get("turn_id"),
                        "last_agent_message": payload.get("last_agent_message"),
                        "raw_type": "task_complete",
                    }
                ),
            )

        # agent_message and others → drop
        return None

    def _parse_response_item(
        self,
        payload: dict[str, Any],
        timestamp: datetime,
        state: _ParseState,
    ) -> Event | list[Event] | None:
        inner_type = payload.get("type", "")

        if inner_type == "function_call":
            tool_name = payload.get("name")
            tool_input = _parse_json_blob(payload.get("arguments"))
            base_payload = compact_dict(
                {
                    "tool_call_id": payload.get("call_id"),
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                }
            )
            return self._event_from(
                session_id=state.session_id,
                event_type=EventType.TOOL_CALL_REQUESTED,
                timestamp=timestamp,
                actor="assistant",
                payload=base_payload,
            )

        if inner_type == "function_call_output":
            raw_output = payload.get("output")
            parsed_out = _parse_json_blob(raw_output)
            call_id = payload.get("call_id")

            # close_agent output
            if isinstance(parsed_out, dict) and "status" in parsed_out and isinstance(parsed_out.get("status"), dict):
                return self._event_from(
                    session_id=state.session_id,
                    event_type=EventType.TOOL_CALL_SUCCEEDED,
                    timestamp=timestamp,
                    actor="tool",
                    payload=compact_dict(
                        {
                            "tool_call_id": call_id,
                            "tool_name": "close_agent",
                            "output": parsed_out,
                        }
                    ),
                )

            success = infer_tool_success(raw_output)
            event_type = EventType.TOOL_CALL_SUCCEEDED if success is not False else EventType.TOOL_CALL_FAILED
            return self._event_from(
                session_id=state.session_id,
                event_type=event_type,
                timestamp=timestamp,
                actor="tool",
                payload=compact_dict(
                    {
                        "tool_call_id": call_id,
                        "exit_code": extract_exit_code(raw_output),
                        "output": parsed_out,
                    }
                ),
            )

        # reasoning and others → drop
        return None

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
