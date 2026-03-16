"""Codex CLI adapter — reads ~/.codex/sessions/**/*.jsonl rollout files."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
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
    StepTextItem,
    StepToolItem,
    Turn,
    ToolStatus,
    Vendor,
    VendorExtensions,
)

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


def _append_unique_event_ids(target: list[UUID], event_ids: list[UUID]) -> None:
    for event_id in event_ids:
        if event_id not in target:
            target.append(event_id)


def _append_text_item(items: list[StepTextItem | StepToolItem], text: str | None, *, event_ids: list[UUID] | None = None) -> None:
    if not text:
        return
    cleaned = text.strip()
    if not cleaned:
        return
    if items and isinstance(items[-1], StepTextItem) and items[-1].text == cleaned:
        if event_ids:
            _append_unique_event_ids(items[-1].event_ids, event_ids)
        return
    items.append(StepTextItem(text=cleaned, event_ids=list(event_ids or [])))


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
            raise ValueError(f"CodexAdapter: no events parsed from {source}")
        else:
            started_at = min(e.timestamp for e in events)
            ended_at = max(e.timestamp for e in events)

        meta = state.session_meta
        ctx = state.turn_context
        sandbox_policy = ctx.get("sandbox_policy") if isinstance(ctx.get("sandbox_policy"), dict) else {}
        thread_spawn = _extract_thread_spawn(meta) or {}
        parent_session_id = _extract_codex_parent_session_id(meta)
        forked_from_id = _as_non_empty_str(meta.get("forked_from_id"))
        spawn_parent_thread_id = _as_non_empty_str(thread_spawn.get("parent_thread_id"))
        spawn_depth = thread_spawn.get("depth") if isinstance(thread_spawn.get("depth"), int) else None
        normalized_events = [
            event if event.session_id == state.session_id
            else event.model_copy(update={"session_id": state.session_id})
            for event in events
        ]

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

        turns = self._build_turns(state.session_id, records, normalized_events)

        return Session(
            session_id=state.session_id,
            trajectory_id=uuid4(),
            vendor=Vendor.CODEX_CLI,
            agent_name=extensions.codex.agent_nickname if extensions.codex else None,
            started_at=started_at,
            ended_at=ended_at,
            parent_session_id=parent_session_id,
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
        current_step_items: list[StepTextItem | StepToolItem] = []
        current_step_tools_by_call_id: dict[str, StepToolItem] = {}
        current_step_start_ts: datetime | None = None

        def _flush_turn_with_step(last_msg: str | None, end_ts: datetime) -> None:
            nonlocal current_turn, current_step_event_ids, current_step_items
            nonlocal current_step_tools_by_call_id, current_step_start_ts, turn_sequence

            if current_turn is None:
                return

            _append_text_item(current_step_items, last_msg)

            step = Step(
                session_id=session_id,
                turn_id=current_turn.turn_id,
                sequence=0,
                timestamp=current_step_start_ts or current_turn.started_at,
                vendor=Vendor.CODEX_CLI,
                items=list(current_step_items),
                event_ids=list(current_step_event_ids),
            )
            current_turn.steps.append(step)
            current_turn.ended_at = end_ts
            turns.append(current_turn)

            current_turn = None
            current_step_event_ids = []
            current_step_items = []
            current_step_tools_by_call_id = {}
            current_step_start_ts = None

        for record in records:
            outer_type = record.get("type", "")
            payload = record.get("payload") or {}

            if outer_type == "session_meta":
                continue

            if outer_type == "turn_context":
                continue

            ts = parse_iso_timestamp(record.get("timestamp"))
            if ts is None:
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
                        event_ids=[user_ev.event_id] if user_ev else [],
                    )
                    turn_sequence += 1
                    current_step_start_ts = ts
                    if user_ev:
                        _append_unique_event_ids(current_step_event_ids, [user_ev.event_id])

                elif inner_type == "agent_message":
                    # Intermediate commentary
                    text = _extract_message_text(payload)
                    phase = payload.get("phase")
                    if phase == "commentary" and text:
                        _append_text_item(current_step_items, text)
                    # Collect event IDs
                    _append_unique_event_ids(
                        current_step_event_ids,
                        [ev.event_id for ev in event_index.get(ts, [])],
                    )
                    if current_turn is not None:
                        _append_unique_event_ids(
                            current_turn.event_ids,
                            [ev.event_id for ev in event_index.get(ts, [])],
                        )

                elif inner_type == "task_complete":
                    last_msg = payload.get("last_agent_message")
                    if current_turn is not None:
                        # Collect task_complete event IDs
                        turn_event_ids = [ev.event_id for ev in event_index.get(ts, [])]
                        _append_unique_event_ids(current_turn.event_ids, turn_event_ids)
                        _append_unique_event_ids(
                            current_step_event_ids,
                            [ev.event_id for ev in event_index.get(ts, []) if ev.type == EventType.VENDOR_RAW],
                        )
                        _flush_turn_with_step(last_msg, ts)

            elif outer_type == "response_item":
                inner_type = payload.get("type", "")
                if current_turn is None:
                    continue

                if inner_type == "function_call":
                    tool_name = payload.get("name")
                    tool_input = _parse_json_blob(payload.get("arguments"))
                    request_event_ids = [
                        ev.event_id
                        for ev in event_index.get(ts, [])
                        if ev.type == EventType.TOOL_CALL_REQUESTED
                    ]
                    tool_item = StepToolItem(
                        tool_name=tool_name,
                        tool_call_id=payload.get("call_id"),
                        input=tool_input,
                        status=ToolStatus.REQUESTED,
                        event_ids=request_event_ids,
                    )
                    current_step_items.append(tool_item)
                    call_id = payload.get("call_id")
                    if isinstance(call_id, str) and call_id:
                        current_step_tools_by_call_id[call_id] = tool_item
                    _append_unique_event_ids(current_step_event_ids, request_event_ids)
                    if current_turn is not None:
                        _append_unique_event_ids(current_turn.event_ids, request_event_ids)

                elif inner_type == "function_call_output":
                    result_event_ids = [
                        ev.event_id
                        for ev in event_index.get(ts, [])
                        if ev.type in (EventType.TOOL_CALL_SUCCEEDED, EventType.TOOL_CALL_FAILED)
                    ]
                    _append_unique_event_ids(current_step_event_ids, result_event_ids)
                    if current_turn is not None:
                        _append_unique_event_ids(current_turn.event_ids, result_event_ids)
                    call_id = payload.get("call_id")
                    tool_item = current_step_tools_by_call_id.get(call_id) if isinstance(call_id, str) else None
                    if tool_item is not None:
                        tool_item.output = _parse_json_blob(payload.get("output"))
                        tool_item.status = (
                            ToolStatus.FAILED
                            if any(
                                ev.type == EventType.TOOL_CALL_FAILED
                                for ev in event_index.get(ts, [])
                            )
                            else ToolStatus.COMPLETED
                        )
                        _append_unique_event_ids(tool_item.event_ids, result_event_ids)

                elif inner_type == "reasoning":
                    # Encrypted, skip
                    pass

                elif inner_type == "message":
                    if payload.get("role") == "assistant":
                        response_event_ids = [ev.event_id for ev in event_index.get(ts, [])]
                        text = _extract_response_text(payload)
                        _append_text_item(current_step_items, text, event_ids=list(response_event_ids))
                        _append_unique_event_ids(current_step_event_ids, response_event_ids)
                        if current_turn is not None:
                            _append_unique_event_ids(current_turn.event_ids, response_event_ids)

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

            timestamp = parse_iso_timestamp(line.get("timestamp"))
            if timestamp is None:
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

        if inner_type == "context_compacted":
            details = {
                key: value
                for key, value in payload.items()
                if key not in {"type", "turn_id"}
            }
            return self._event_from(
                session_id=state.session_id,
                event_type=EventType.VENDOR_RAW,
                timestamp=timestamp,
                actor="assistant",
                payload=compact_dict(
                    {
                        "turn_id_raw": turn_id,
                        "raw_type": "context_compacted",
                        "details": details or None,
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

        if inner_type == "message" and payload.get("role") == "assistant":
            text = _extract_response_text(payload)
            if not text:
                return None
            return self._event_from(
                session_id=state.session_id,
                event_type=EventType.LLM_RESPONSE,
                timestamp=timestamp,
                actor="assistant",
                payload=compact_dict(
                    {
                        "text": text,
                        "phase": payload.get("phase"),
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
