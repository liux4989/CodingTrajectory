"""Amp adapter — reads ~/.local/share/amp/threads/T-*.json and normalises to canonical models."""

from __future__ import annotations

import json
import logging
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from coding_trajectory.ingestion.adapters.base import BaseAdapter
from coding_trajectory.ingestion.common import compact_dict, parse_iso_timestamp, parse_timestamp
from coding_trajectory.ingestion.models import (
    AgentType,
    AmpExtensions,
    Event,
    EventType,
    Session,
    Step,
    ToolStatus,
    Turn,
    Vendor,
    VendorExtensions,
)
from coding_trajectory.ingestion.step_items import append_text_item, append_tool_item, derive_status, update_tool_item

logger = logging.getLogger(__name__)

_DEFAULT_AMP_THREADS_DIR = Path.home() / ".local" / "share" / "amp" / "threads"


def _content_text(blocks: list[dict]) -> str | None:
    texts = [
        block.get("text", "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    joined = " ".join(text for text in texts if text).strip()
    return joined or None


def _thinking_blocks(blocks: list[dict]) -> list[str]:
    return [
        block.get("thinking", "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "thinking" and block.get("thinking")
    ]


class AmpAdapter(BaseAdapter):
    """Normalise Amp thread JSON files into canonical Session objects."""

    vendor = Vendor.AMP
    _file_glob = "T-*.json"

    _current_thread: dict | None

    def _reset_ingest_state(self) -> None:
        self._current_thread = None

    def _load_records(self, path: Path) -> list[dict]:
        try:
            thread = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            warnings.warn(f"AmpAdapter: could not parse {path}: {exc}", stacklevel=2)
            return []
        return [thread] if isinstance(thread, dict) else []

    def ingest_default(self) -> list[Session]:
        return self.ingest_directory(_DEFAULT_AMP_THREADS_DIR)

    def _build_session(self, source: Path, records: list[dict]) -> Session:
        if not records:
            return Session(
                session_id=uuid4(),
                trajectory_id=uuid4(),
                vendor=self.vendor,
                started_at=datetime.now(timezone.utc),
            )

        thread = records[0]
        self._current_thread = thread

        thread_id = thread.get("id")
        try:
            session_id = UUID(str(thread_id).removeprefix("T-"))
        except ValueError:
            session_id = uuid4()

        created_at = parse_timestamp(thread.get("created")) or datetime.now(timezone.utc)
        messages = thread.get("messages") or []
        traces = ((thread.get("meta") or {}).get("traces") or [])
        message_timestamps = self._build_message_timestamps(
            thread_created=created_at,
            messages=messages,
            traces=traces,
        )

        # Parse events from messages
        events = self._parse_messages(session_id, messages, message_timestamps)

        ended_at = max((event.timestamp for event in events), default=created_at)

        # Build turns and steps
        turns = self._build_turns(session_id, messages, message_timestamps, events)

        return Session(
            session_id=session_id,
            trajectory_id=uuid4(),
            vendor=self.vendor,
            agent_type=AgentType.MAIN,
            started_at=created_at,
            ended_at=ended_at,
            events=events,
            turns=turns,
            extensions=self._parse_extensions(thread),
        )

    def _build_message_timestamps(
        self,
        *,
        thread_created: datetime,
        messages: list[dict],
        traces: list[dict],
    ) -> list[datetime]:
        trace_by_message = self._trace_timestamps_by_message_id(traces)
        timestamps: list[datetime] = []
        last_ts: datetime | None = None

        for msg in messages:
            meta = msg.get("meta") or {}
            message_id = msg.get("messageId")

            explicit = parse_timestamp(meta.get("sentAt"))
            inferred = explicit or trace_by_message.get(message_id)
            if inferred is None:
                inferred = last_ts or thread_created

            if last_ts is not None and inferred <= last_ts:
                inferred = last_ts + timedelta(milliseconds=1)

            timestamps.append(inferred)
            last_ts = inferred

        return timestamps

    def _trace_timestamps_by_message_id(self, traces: list[dict]) -> dict[Any, datetime]:
        by_message_id: dict[Any, datetime] = {}
        for trace in traces:
            if not isinstance(trace, dict):
                continue

            context = trace.get("context") or {}
            message_id = context.get("messageId")
            if message_id is None:
                continue

            trace_ts = (
                parse_iso_timestamp(trace.get("startTime"))
                or parse_iso_timestamp(trace.get("endTime"))
            )
            if trace_ts is None:
                continue

            existing = by_message_id.get(message_id)
            if existing is None or trace_ts < existing:
                by_message_id[message_id] = trace_ts

        return by_message_id

    def _build_turns(
        self,
        session_id: UUID,
        messages: list[dict],
        message_timestamps: list[datetime],
        all_events: list[Event],
    ) -> list[Turn]:
        """Group assistant messages + their tool results into Steps."""
        turns: list[Turn] = []
        turn_sequence = 0
        step_sequence = 0

        current_turn: Turn | None = None

        # Index events by source message id.
        event_index: dict[Any, list[Event]] = {}
        for ev in all_events:
            message_id = ev.payload.get("message_id")
            if message_id is None:
                continue
            if message_id not in event_index:
                event_index[message_id] = []
            event_index[message_id].append(ev)

        for idx, msg in enumerate(messages):
            role = msg.get("role")
            message_id = msg.get("messageId")
            content = msg.get("content") or []
            ts = message_timestamps[idx]

            if role == "user":
                text = _content_text(content)
                tool_results = [
                    block for block in content
                    if isinstance(block, dict) and block.get("type") == "tool_result"
                ]

                if text and not tool_results:
                    # New turn starting with user prompt
                    if current_turn is not None:
                        # Finalize previous turn
                        current_turn.ended_at = ts
                        turns.append(current_turn)

                    # Find USER_PROMPT_SUBMITTED event
                    user_evs = [
                        e for e in event_index.get(message_id, [])
                        if e.type == EventType.USER_PROMPT_SUBMITTED
                    ]
                    user_ev = user_evs[0] if user_evs else None

                    current_turn = Turn(
                        session_id=session_id,
                        sequence=turn_sequence,
                        started_at=ts,
                        user_request_event_id=user_ev.event_id if user_ev else None,
                    )
                    turn_sequence += 1
                    step_sequence = 0

                elif tool_results and current_turn is not None:
                    # Tool result message — associate with current step
                    # Find corresponding events and attach to current step if there is one
                    if current_turn.steps:
                        last_step = current_turn.steps[-1]
                        result_evs = [
                            e for e in event_index.get(message_id, [])
                            if e.type in (EventType.TOOL_CALL_SUCCEEDED, EventType.TOOL_CALL_FAILED)
                        ]
                        for ev in result_evs:
                            if ev.event_id not in last_step.event_ids:
                                last_step.event_ids.append(ev.event_id)
                            update_tool_item(
                                last_step.items,
                                tool_call_id=ev.payload.get("tool_call_id"),
                                tool_name=ev.payload.get("tool_name"),
                                output=ev.payload.get("tool_result"),
                                status=(
                                    ToolStatus.COMPLETED
                                    if ev.type == EventType.TOOL_CALL_SUCCEEDED
                                    else ToolStatus.FAILED
                                ),
                                event_ids=[ev.event_id],
                            )
                        last_step.status = derive_status(last_step.items)

            elif role == "assistant":
                if current_turn is None:
                    continue

                usage = msg.get("usage") or {}
                thinking = _thinking_blocks(content)
                # Collect events for this step
                step_event_ids: list[UUID] = []
                for ev in event_index.get(message_id, []):
                    if ev.type in (
                        EventType.TOOL_CALL_REQUESTED,
                        EventType.LLM_RESPONSE,
                        EventType.VENDOR_RAW,
                    ):
                        step_event_ids.append(ev.event_id)

                items = []
                llm_event_ids = [
                    ev.event_id for ev in event_index.get(message_id, [])
                    if ev.type == EventType.LLM_RESPONSE
                ]
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if block_type == "text":
                        append_text_item(items, block.get("text"), event_ids=llm_event_ids)
                    elif block_type == "tool_use":
                        tool_id = block.get("id")
                        tool_event_ids = [
                            ev.event_id
                            for ev in event_index.get(message_id, [])
                            if ev.type == EventType.TOOL_CALL_REQUESTED and ev.payload.get("tool_call_id") == tool_id
                        ]
                        append_tool_item(
                            items,
                            tool_name=block.get("name"),
                            tool_call_id=tool_id,
                            input=block.get("input"),
                            status=ToolStatus.REQUESTED,
                            event_ids=tool_event_ids,
                        )

                vendor_data: dict = {}
                if thinking:
                    vendor_data["thinking"] = thinking
                if usage:
                    vendor_data["model"] = usage.get("model")
                    vendor_data["input_tokens"] = usage.get("inputTokens")
                    vendor_data["output_tokens"] = usage.get("outputTokens")

                stop_reason = (msg.get("state") or {}).get("stopReason")
                if stop_reason:
                    vendor_data["stop_reason"] = stop_reason

                step = Step(
                    session_id=session_id,
                    turn_id=current_turn.turn_id,
                    sequence=step_sequence,
                    timestamp=ts,
                    vendor=Vendor.AMP,
                    status=derive_status(items),
                    items=items,
                    vendor_data={k: v for k, v in vendor_data.items() if v is not None},
                    event_ids=step_event_ids,
                )
                step_sequence += 1
                current_turn.steps.append(step)

        # Flush last turn
        if current_turn is not None:
            if current_turn.ended_at is None:
                current_turn.ended_at = max(
                    (e.timestamp for e in all_events), default=current_turn.started_at
                )
            turns.append(current_turn)

        return turns

    def _parse_messages(
        self,
        session_id: UUID,
        messages: list[dict],
        message_timestamps: list[datetime],
    ) -> list[Event]:
        events: list[Event] = []

        # Build tool_id → name mapping
        tool_id_to_name: dict[str, str] = {}
        for msg in messages:
            if msg.get("role") == "assistant":
                for block in (msg.get("content") or []):
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tid = block.get("id") or ""
                        tool_id_to_name[tid] = block.get("name") or ""

        for idx, msg in enumerate(messages):
            role = msg.get("role")
            message_id = msg.get("messageId")
            content = msg.get("content") or []
            timestamp = message_timestamps[idx]

            if role == "user":
                text = _content_text(content)
                tool_results = [
                    block for block in content
                    if isinstance(block, dict) and block.get("type") == "tool_result"
                ]

                if text and not tool_results:
                    events.append(
                        Event(
                            session_id=session_id,
                            timestamp=timestamp,
                            type=EventType.USER_PROMPT_SUBMITTED,
                            vendor_source=self.vendor,
                            actor="user",
                            payload=compact_dict(
                                {
                                    "message_id": message_id,
                                    "text": text,
                                }
                            ),
                        )
                    )

                for block in tool_results:
                    run = block.get("run") or {}
                    status = run.get("status")
                    tool_id = block.get("toolUseID") or ""

                    event_type = (
                        EventType.TOOL_CALL_SUCCEEDED if status == "done" else EventType.TOOL_CALL_FAILED
                    )
                    events.append(
                        Event(
                            session_id=session_id,
                            timestamp=timestamp,
                            type=event_type,
                            vendor_source=self.vendor,
                            actor="tool",
                            payload=compact_dict(
                                {
                                    "message_id": message_id,
                                    "tool_call_id": tool_id,
                                    "tool_name": tool_id_to_name.get(tool_id, ""),
                                    "tool_status": status,
                                    "tool_result": run.get("result"),
                                }
                            ),
                        )
                    )

            elif role == "assistant":
                usage = msg.get("usage") or {}

                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_name = block.get("name") or ""
                        events.append(
                            Event(
                                session_id=session_id,
                                timestamp=timestamp,
                                type=EventType.TOOL_CALL_REQUESTED,
                                vendor_source=self.vendor,
                                actor="assistant",
                                payload=compact_dict(
                                    {
                                        "message_id": message_id,
                                        "tool_call_id": block.get("id"),
                                        "tool_name": tool_name,
                                        "tool_input": block.get("input"),
                                    }
                                ),
                            )
                        )

                text = _content_text(content)
                if text:
                    events.append(
                        Event(
                            session_id=session_id,
                            timestamp=timestamp,
                            type=EventType.LLM_RESPONSE,
                            vendor_source=self.vendor,
                            actor="assistant",
                            payload=compact_dict(
                                {
                                    "message_id": message_id,
                                    "text": text,
                                    "model": usage.get("model"),
                                }
                            ),
                        )
                    )

        return events

    def _parse_extensions(self, thread: dict) -> VendorExtensions | None:
        env = (thread.get("env") or {}).get("initial") or {}
        trees = env.get("trees") or []
        platform = env.get("platform") or {}
        first_tree = trees[0] if trees else {}
        repo = first_tree.get("repository") or {}
        return VendorExtensions(
            amp=AmpExtensions(
                thread_id=thread.get("id"),
                thread_version=thread.get("v"),
                workspace_id=first_tree.get("uri"),
                workspace_name=first_tree.get("displayName"),
                git_url=repo.get("url"),
                git_ref=repo.get("ref"),
                agent_version=platform.get("clientVersion"),
                client_type=platform.get("clientType"),
                os_platform=platform.get("os"),
                title=thread.get("title"),
                agent_mode=thread.get("agentMode"),
            )
        )
