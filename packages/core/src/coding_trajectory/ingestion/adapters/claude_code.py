"""Claude Code adapter — reads ~/.claude/projects/**/*.jsonl and normalises to canonical models."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from coding_trajectory.ingestion.adapters.base import BaseAdapter
from coding_trajectory.ingestion.common import compact_dict, infer_tool_success, parse_timestamp
from coding_trajectory.ingestion.models import (
    ClaudeCodeExtensions,
    Event,
    EventType,
    Session,
    Step,
    ToolStatus,
    Turn,
    Vendor,
    VendorExtensions,
)
from coding_trajectory.ingestion.step_items import append_text_item, append_tool_item, update_tool_item

logger = logging.getLogger(__name__)

_DEFAULT_CLAUDE_DIR = Path.home() / ".claude" / "projects"


def _extract_text(content: str | list | None) -> str | None:
    if content is None:
        return None
    if isinstance(content, str):
        return content or None
    texts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    joined = " ".join(text for text in texts if text).strip()
    return joined or None


def _extract_thinking(content: list | None) -> list[str]:
    if not isinstance(content, list):
        return []
    return [
        block.get("thinking", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "thinking" and block.get("thinking")
    ]


def _tool_result_blocks(content: list | None) -> list[dict]:
    if not isinstance(content, list):
        return []
    return [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]


def _tool_use_blocks(content: list | None) -> list[dict]:
    if not isinstance(content, list):
        return []
    return [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]


def _append_unique_event_ids(target: list[UUID], event_ids: list[UUID]) -> None:
    for event_id in event_ids:
        if event_id not in target:
            target.append(event_id)


def _is_real_user_prompt(obj: dict) -> bool:
    if obj.get("isMeta"):
        return False
    content = obj.get("message", {}).get("content")
    if isinstance(content, list) and _tool_result_blocks(content):
        return False
    return True


def _base_payload(obj: dict) -> dict:
    return compact_dict(
        {
            "uuid": obj.get("uuid"),
            "parent_uuid": obj.get("parentUuid"),
            "logical_parent_uuid": obj.get("logicalParentUuid"),
            "request_id": obj.get("requestId"),
            "is_sidechain": obj.get("isSidechain"),
            "team_name": obj.get("teamName"),
            "agent_id": obj.get("agentId"),
            "agent_name": obj.get("agentName") or obj.get("slug"),
            "version": obj.get("version"),
            "cwd": obj.get("cwd"),
            "git_branch": obj.get("gitBranch"),
            "permission_mode": obj.get("permissionMode"),
        }
    )


class ClaudeCodeAdapter(BaseAdapter):
    """Normalise Claude Code JSONL session files into canonical Session objects."""

    vendor = Vendor.CLAUDE_CODE

    def _build_session(self, source: Path, records: list[dict]) -> Session:
        # First pass: collect all events
        events: list[Event] = []
        for record in records:
            events.extend(self._parse_raw_object(record))

        if not events:
            return Session(
                session_id=uuid4(),
                trajectory_id=uuid4(),
                vendor=self.vendor,
                started_at=datetime.now(timezone.utc),
            )

        session_id = events[0].session_id
        started_at = min(event.timestamp for event in events)
        ended_at = max(event.timestamp for event in events)

        # Build turns and steps from records directly
        turns = self._build_turns(session_id, records, events)
        extensions = self._parse_extensions(source)

        return Session(
            session_id=session_id,
            trajectory_id=uuid4(),
            vendor=self.vendor,
            agent_name=extensions.claude_code.agent_name if extensions and extensions.claude_code else None,
            started_at=started_at,
            ended_at=ended_at,
            events=events,
            turns=turns,
            extensions=extensions,
        )

    def _build_turns(self, session_id: UUID, records: list[dict], all_events: list[Event]) -> list[Turn]:
        """Build Turn+Step objects from raw records."""
        # Build a mapping from uuid -> event for easy lookup
        event_by_uuid: dict[str, Event] = {}
        for record in records:
            uid = record.get("uuid")
            ts = parse_timestamp(record.get("timestamp"))
            sid_str = record.get("sessionId")
            if uid and ts and sid_str:
                # Find matching events in all_events
                for ev in all_events:
                    if ev.timestamp == ts and str(ev.session_id) == sid_str:
                        event_by_uuid[uid] = ev
                        break

        # Build event lookup by uuid for records
        # We need to process records in order to build turns
        turns: list[Turn] = []
        current_turn: Turn | None = None
        turn_sequence = 0
        step_sequence = 0
        current_turn_last_ts: datetime | None = None  # track last timestamp seen in current turn

        # State machine: COLLECTING_LLM | COLLECTING_RESULTS
        # We process records in sequence
        # A new turn starts when we see a real user prompt
        # A new step starts at first assistant record after a user prompt or after tool results

        # Track current step being built
        current_step_thinking: list[str] = []
        current_step_items = []
        current_step_usage: dict | None = None
        current_step_stop_reason: str | None = None
        current_step_timestamp: datetime | None = None
        current_step_event_ids: list[UUID] = []
        in_step = False

        def _flush_step(turn: Turn) -> None:
            nonlocal current_step_thinking, current_step_items, current_step_usage
            nonlocal current_step_stop_reason, current_step_timestamp
            nonlocal current_step_event_ids, in_step, step_sequence

            if not in_step:
                return
            if current_step_timestamp is None:
                return

            vendor_data: dict = {}
            if current_step_thinking:
                vendor_data["thinking"] = current_step_thinking
            if current_step_usage:
                vendor_data["usage"] = current_step_usage
            if current_step_stop_reason:
                vendor_data["stop_reason"] = current_step_stop_reason

            step = Step(
                session_id=turn.session_id,
                turn_id=turn.turn_id,
                sequence=step_sequence,
                timestamp=current_step_timestamp,
                vendor=Vendor.CLAUDE_CODE,
                items=list(current_step_items),
                vendor_data=vendor_data,
                event_ids=list(current_step_event_ids),
            )
            step_sequence += 1
            turn.steps.append(step)

            # Reset step state
            current_step_thinking = []
            current_step_items = []
            current_step_usage = None
            current_step_stop_reason = None
            current_step_timestamp = None
            current_step_event_ids = []
            in_step = False

        def _start_step(ts: datetime) -> None:
            nonlocal current_step_timestamp, in_step
            current_step_timestamp = ts
            in_step = True

        # We also need to track tool result event IDs to associate with current step
        # The flow: user_prompt → [turn start] → assistant (thinking+tool_use) → [step] → tool_results → assistant... → [step end]

        # Let's find events by timestamp+session_id for each record
        event_index: dict[tuple, list[Event]] = {}
        for ev in all_events:
            key = (ev.timestamp, ev.session_id)
            if key not in event_index:
                event_index[key] = []
            event_index[key].append(ev)

        def _get_events_for_record(rec: dict) -> list[Event]:
            ts = parse_timestamp(rec.get("timestamp"))
            sid_str = rec.get("sessionId")
            if ts is None or sid_str is None:
                return []
            try:
                sid = UUID(sid_str)
            except (ValueError, AttributeError):
                return []
            return event_index.get((ts, sid), [])

        # State: we're either in a turn or not
        # Within a turn, we alternate: assistant_block -> tool_results -> assistant_block -> ...
        # Each assistant_block + subsequent tool_results = one Step

        collecting_results = False  # True after first terminal assistant in current step

        for record in records:
            raw_type = record.get("type")
            ts = parse_timestamp(record.get("timestamp"))
            if ts is None:
                continue

            sid_str = record.get("sessionId")
            if not sid_str:
                continue

            if raw_type == "user":
                message = record.get("message", {})
                content = message.get("content")
                rec_events = _get_events_for_record(record)

                if _is_real_user_prompt(record):
                    # End current turn
                    if current_turn is not None:
                        _flush_step(current_turn)
                        # Use last seen timestamp for ended_at to avoid going backwards in time
                        current_turn.ended_at = current_turn_last_ts or current_turn.started_at
                        turns.append(current_turn)

                    # Find the USER_PROMPT_SUBMITTED event
                    user_event = next(
                        (e for e in rec_events if e.type == EventType.USER_PROMPT_SUBMITTED),
                        None
                    )
                    current_turn = Turn(
                        session_id=session_id,
                        sequence=turn_sequence,
                        started_at=ts,
                        user_request_event_id=user_event.event_id if user_event else None,
                        event_ids=[event.event_id for event in rec_events],
                    )
                    current_turn_last_ts = ts  # reset for new turn
                    turn_sequence += 1
                    step_sequence = 0
                    in_step = False
                    collecting_results = False
                else:
                    # Tool results
                    if current_turn is not None:
                        # Update last seen timestamp for current turn
                        if current_turn_last_ts is None or ts > current_turn_last_ts:
                            current_turn_last_ts = ts
                        _append_unique_event_ids(current_turn.event_ids, [event.event_id for event in rec_events])
                        tool_result_events = [
                            e for e in rec_events
                            if e.type in (EventType.TOOL_CALL_SUCCEEDED, EventType.TOOL_CALL_FAILED)
                        ]
                        for ev in tool_result_events:
                            _append_unique_event_ids(current_step_event_ids, [ev.event_id])
                            update_tool_item(
                                current_step_items,
                                tool_call_id=ev.payload.get("tool_call_id"),
                                tool_name=ev.payload.get("tool_name"),
                                output=ev.payload.get("tool_output", ev.payload.get("tool_text")),
                                status=ToolStatus.FAILED if ev.type == EventType.TOOL_CALL_FAILED else ToolStatus.COMPLETED,
                                event_ids=[ev.event_id],
                            )

                        # If we were collecting results and this is a tool result,
                        # mark that we've completed collecting for current step
                        # (step flush happens when next assistant record arrives)
                        collecting_results = True

            elif raw_type == "assistant":
                if current_turn is None:
                    continue

                # Update last seen timestamp for current turn
                if current_turn_last_ts is None or ts > current_turn_last_ts:
                    current_turn_last_ts = ts

                message = record.get("message", {})
                content = message.get("content", [])
                stop_reason = message.get("stop_reason")
                usage = message.get("usage")
                rec_events = _get_events_for_record(record)
                _append_unique_event_ids(current_turn.event_ids, [event.event_id for event in rec_events])

                # If we were collecting results, flush the previous step before starting a new one
                if collecting_results and in_step:
                    _flush_step(current_turn)
                    collecting_results = False

                if not in_step:
                    _start_step(ts)

                # Accumulate thinking
                thinking_blocks = _extract_thinking(content)
                current_step_thinking.extend(thinking_blocks)

                tool_uses = _tool_use_blocks(content)
                llm_event_ids = [ev.event_id for ev in rec_events if ev.type == EventType.LLM_RESPONSE]
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        append_text_item(current_step_items, block.get("text"), event_ids=llm_event_ids)
                    elif block.get("type") == "tool_use":
                        tool_id = block.get("id")
                        tool_event_ids = [
                            ev.event_id
                            for ev in rec_events
                            if ev.type == EventType.TOOL_CALL_REQUESTED and ev.payload.get("tool_call_id") == tool_id
                        ]
                        append_tool_item(
                            current_step_items,
                            tool_name=block.get("name"),
                            tool_call_id=tool_id,
                            input=block.get("input"),
                            status=ToolStatus.REQUESTED,
                            event_ids=tool_event_ids,
                        )

                # Usage and stop_reason from terminal record
                if stop_reason is not None:
                    current_step_stop_reason = stop_reason
                    if usage:
                        current_step_usage = usage

                # Collect event IDs
                for ev in rec_events:
                    if ev.type in (
                        EventType.LLM_RESPONSE,
                        EventType.TOOL_CALL_REQUESTED,
                        EventType.VENDOR_RAW,
                    ):
                        _append_unique_event_ids(current_step_event_ids, [ev.event_id])

                # If this record has tool_uses but stop_reason=None, it's intermediate
                # If stop_reason is not None with no tool_uses → end_turn → flush step
                if stop_reason is not None and not tool_uses:
                    # Terminal with no tool calls → flush now
                    _flush_step(current_turn)
                    collecting_results = False
                elif stop_reason is not None and tool_uses:
                    # Has tool_uses with stop_reason="tool_use" → wait for tool results
                    collecting_results = False  # will flip to True when tool results arrive

            elif raw_type == "system":
                # Drop system records (api_error, turn_duration, etc.)
                continue

        # Flush any remaining turn
        if current_turn is not None:
            _flush_step(current_turn)
            if current_turn.ended_at is None:
                # set ended_at from events
                if all_events:
                    current_turn.ended_at = max(e.timestamp for e in all_events)
            turns.append(current_turn)

        return turns

    def _parse_raw_object(self, obj: dict) -> list[Event]:  # noqa: C901
        raw_type = obj.get("type")
        session_id_str = obj.get("sessionId")
        timestamp = parse_timestamp(obj.get("timestamp"))
        if not session_id_str or timestamp is None:
            return []

        try:
            session_id = UUID(session_id_str)
        except (ValueError, AttributeError):
            return []

        if raw_type == "user":
            return self._parse_user_line(obj, session_id, timestamp)
        if raw_type == "assistant":
            return self._parse_assistant_line(obj, session_id, timestamp)
        # Drop system records entirely (api_error, turn_duration, compact_boundary, local_command, etc.)
        return []

    def _parse_user_line(self, obj: dict, session_id: UUID, timestamp: datetime) -> list[Event]:
        message = obj.get("message", {})
        content = message.get("content")
        base = _base_payload(obj)

        if _is_real_user_prompt(obj):
            return [
                Event(
                    session_id=session_id,
                    timestamp=timestamp,
                    type=EventType.USER_PROMPT_SUBMITTED,
                    vendor_source=self.vendor,
                    actor="user",
                    payload=compact_dict({**base, "text": _extract_text(content)}),
                )
            ]

        tool_results = _tool_result_blocks(content)
        events: list[Event] = []
        for block in tool_results:
            tool_use_result = obj.get("toolUseResult")
            success = infer_tool_success(tool_use_result)
            event_type = (
                EventType.TOOL_CALL_FAILED
                if block.get("is_error") or success is False
                else EventType.TOOL_CALL_SUCCEEDED
            )
            tool_payload = compact_dict(
                {
                    **base,
                    "tool_call_id": block.get("tool_use_id") or block.get("toolUseID"),
                    "tool_output": tool_use_result,
                    "tool_text": block.get("content"),
                    "source_tool_assistant_uuid": obj.get("sourceToolAssistantUUID"),
                }
            )
            events.append(
                Event(
                    session_id=session_id,
                    timestamp=timestamp,
                    type=event_type,
                    vendor_source=self.vendor,
                    actor="tool",
                    payload=tool_payload,
                )
            )

        return events

    def _parse_assistant_line(self, obj: dict, session_id: UUID, timestamp: datetime) -> list[Event]:
        message = obj.get("message", {})
        content = message.get("content", [])
        stop_reason = message.get("stop_reason")
        usage = message.get("usage")
        base = _base_payload(obj)
        events: list[Event] = []

        tool_uses = _tool_use_blocks(content)
        for tool in tool_uses:
            payload = compact_dict(
                {
                    **base,
                    "tool_call_id": tool.get("id"),
                    "tool_name": tool.get("name"),
                    "tool_input": tool.get("input"),
                    "usage": usage,
                }
            )
            events.append(
                Event(
                    session_id=session_id,
                    timestamp=timestamp,
                    type=EventType.TOOL_CALL_REQUESTED,
                    vendor_source=self.vendor,
                    actor="assistant",
                    payload=payload,
                )
            )

        text = _extract_text(content)
        if text:
            events.append(
                Event(
                    session_id=session_id,
                    timestamp=timestamp,
                    type=EventType.LLM_RESPONSE,
                    vendor_source=self.vendor,
                    actor="assistant",
                    payload=compact_dict({**base, "text": text, "stop_reason": stop_reason, "usage": usage}),
                )
            )
        elif not events and stop_reason is not None:
            # Emit a vendor_raw event for completeness (e.g. tool_use only records)
            events.append(
                Event(
                    session_id=session_id,
                    timestamp=timestamp,
                    type=EventType.VENDOR_RAW,
                    vendor_source=self.vendor,
                    actor="assistant",
                    payload=compact_dict({**base, "stop_reason": stop_reason, "usage": usage}),
                )
            )

        return events

    def _parse_extensions(self, source: Path) -> VendorExtensions | None:
        try:
            with source.open(encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not obj.get("sessionId"):
                        continue

                    return VendorExtensions(
                        claude_code=ClaudeCodeExtensions(
                            team_name=obj.get("teamName"),
                            is_sidechain=obj.get("isSidechain"),
                            permission_mode=obj.get("permissionMode"),
                            parent_uuid=obj.get("parentUuid"),
                            request_id=obj.get("uuid"),
                            agent_name=obj.get("agentId") or obj.get("agentName") or obj.get("slug"),
                        )
                    )
        except OSError as exc:
            logger.warning("ClaudeCodeAdapter: could not read %s for extensions: %s", source, exc)
        return None

    def ingest_default(self) -> list[Session]:
        sessions: list[Session] = []
        for jsonl_path in sorted(_DEFAULT_CLAUDE_DIR.rglob("*.jsonl")):
            try:
                sessions.append(self.ingest_file(jsonl_path))
            except Exception as exc:
                logger.warning("ClaudeCodeAdapter: failed to ingest %s: %s", jsonl_path, exc)
        return sessions
