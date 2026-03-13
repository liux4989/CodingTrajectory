"""Amp adapter — reads ~/.local/share/amp/threads/T-*.json and normalises to canonical models."""

from __future__ import annotations

import json
import logging
import warnings
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from coding_trajectory.ingestion.adapters.base import BaseAdapter
from coding_trajectory.ingestion.common import compact_dict, parse_iso_timestamp, parse_timestamp
from coding_trajectory.ingestion.models import (
    AmpExtensions,
    EventConfidence,
    Event,
    EventProvenance,
    EventType,
    Session,
    Vendor,
    VendorExtensions,
)

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

    def _parse_raw_line(self, line: dict) -> Event | None:
        return None

    def _parse_record(self, source: Path, record: dict) -> list[Event]:
        self._current_thread = record

        thread_id = record.get("id")
        try:
            session_id = UUID(str(thread_id).removeprefix("T-"))
        except ValueError:
            session_id = uuid4()

        created_at = parse_timestamp(record.get("created")) or datetime.now(timezone.utc)
        events = [
            Event(
                session_id=session_id,
                timestamp=created_at,
                type=EventType.SESSION_STARTED,
                vendor_source=self.vendor,
                actor="system",
                payload=compact_dict({"thread_id": thread_id, "title": record.get("title")}),
            )
        ]
        events.extend(self._parse_messages(session_id, created_at, record.get("messages") or []))
        events.extend(self._parse_traces(session_id, record.get("meta") or {}, record.get("messages") or []))

        ended_at = max((event.timestamp for event in events), default=created_at)
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

    def _build_session(self, source: Path, events: list[Event]) -> Session:
        thread = self._current_thread or {}
        thread_id = thread.get("id")
        try:
            session_id = UUID(str(thread_id).removeprefix("T-"))
        except ValueError:
            session_id = uuid4()

        created_at = parse_timestamp(thread.get("created")) or datetime.now(timezone.utc)
        ended_at = max((event.timestamp for event in events), default=created_at)

        return Session(
            session_id=session_id,
            trajectory_id=uuid4(),
            vendor=self.vendor,
            started_at=created_at,
            ended_at=ended_at,
            turns=self._group_into_turns(session_id, events),
            extensions=self._parse_extensions(thread),
        )

    def _parse_messages(self, session_id: UUID, thread_created: datetime, messages: list[dict]) -> list[Event]:
        events: list[Event] = []
        last_ts = thread_created

        for msg in messages:
            role = msg.get("role")
            message_id = msg.get("messageId")
            content = msg.get("content") or []

            if role == "user":
                timestamp = parse_timestamp((msg.get("meta") or {}).get("sentAt")) or last_ts
                last_ts = timestamp
                text = _content_text(content)
                tool_results = [
                    block for block in content if isinstance(block, dict) and block.get("type") == "tool_result"
                ]
                if text and not tool_results:
                    user_state = msg.get("userState") or {}
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
                                    "sent_at": (msg.get("meta") or {}).get("sentAt"),
                                    "active_editor": user_state.get("activeEditor"),
                                    "visible_files": user_state.get("currentlyVisibleFiles"),
                                    "cursor_location": user_state.get("cursorLocation"),
                                    "interrupted": msg.get("interrupted"),
                                }
                            ),
                        )
                    )

                for block in tool_results:
                    run = block.get("run") or {}
                    status = run.get("status")
                    event_type = EventType.TOOL_CALL_SUCCEEDED if status == "done" else EventType.TOOL_CALL_FAILED
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
                                    "tool_call_id": block.get("toolUseID"),
                                    "tool_status": status,
                                    "tool_result": run.get("result"),
                                    "track_files": run.get("trackFiles"),
                                }
                            ),
                        )
                    )

            elif role == "assistant":
                timestamp = last_ts
                usage = msg.get("usage") or {}
                base_payload = compact_dict(
                    {
                        "message_id": message_id,
                        "stop_reason": (msg.get("state") or {}).get("stopReason"),
                        "turn_elapsed_ms": msg.get("turnElapsedMs"),
                        "model": usage.get("model"),
                        "token_input": usage.get("inputTokens"),
                        "token_output": usage.get("outputTokens"),
                        "token_total": usage.get("totalInputTokens"),
                    }
                )

                for thinking in _thinking_blocks(content):
                    events.append(
                        Event(
                            session_id=session_id,
                            timestamp=timestamp,
                            type=EventType.LLM_STREAM_EVENT,
                            vendor_source=self.vendor,
                            actor="assistant",
                            payload=compact_dict({**base_payload, "text": thinking}),
                        )
                    )

                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        events.append(
                            Event(
                                session_id=session_id,
                                timestamp=timestamp,
                                type=EventType.TOOL_CALL_REQUESTED,
                                vendor_source=self.vendor,
                                actor="assistant",
                                payload=compact_dict(
                                    {
                                        **base_payload,
                                        "tool_call_id": block.get("id"),
                                        "tool_name": block.get("name"),
                                        "tool_input": block.get("input"),
                                        "tool_complete": block.get("complete"),
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
                            type=EventType.AGENT_RESPONSE_COMPLETED,
                            vendor_source=self.vendor,
                            actor="assistant",
                            payload=compact_dict({**base_payload, "text": text}),
                        )
                    )

        return events

    def _parse_traces(self, session_id: UUID, meta: dict, messages: list[dict]) -> list[Event]:
        traces = meta.get("traces") or []
        by_message_id = {msg.get("messageId"): msg for msg in messages if isinstance(msg, dict)}
        events: list[Event] = []

        for trace in traces:
            name = trace.get("name")
            start_ts = parse_iso_timestamp(trace.get("startTime"))
            end_ts = parse_iso_timestamp(trace.get("endTime"))
            message = by_message_id.get((trace.get("context") or {}).get("messageId")) or {}
            message_id = message.get("messageId")
            if name == "inference" and start_ts:
                events.append(
                    Event(
                        session_id=session_id,
                        timestamp=start_ts,
                        type=EventType.LLM_REQUEST_STARTED,
                        vendor_source=self.vendor,
                        actor="system",
                        provenance=EventProvenance.DERIVED,
                        confidence=EventConfidence.MEDIUM,
                        payload=compact_dict({"trace_id": trace.get("id"), "message_id": message_id}),
                    )
                )
            if name == "tools" and start_ts:
                events.append(
                    Event(
                        session_id=session_id,
                        timestamp=start_ts,
                        type=EventType.TOOL_CALL_STARTED,
                        vendor_source=self.vendor,
                        actor="system",
                        provenance=EventProvenance.DERIVED,
                        confidence=EventConfidence.MEDIUM,
                        payload=compact_dict({"trace_id": trace.get("id"), "message_id": message_id}),
                    )
                )
            if name == "agent" and end_ts and (message.get("state") or {}).get("stopReason") == "end_turn":
                events.append(
                    Event(
                        session_id=session_id,
                        timestamp=end_ts,
                        type=EventType.AGENT_RESPONSE_COMPLETED,
                        vendor_source=self.vendor,
                        actor="system",
                        provenance=EventProvenance.DERIVED,
                        confidence=EventConfidence.MEDIUM,
                        payload=compact_dict({"trace_id": trace.get("id"), "message_id": message_id}),
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
