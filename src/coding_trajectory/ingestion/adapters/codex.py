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
from coding_trajectory.ingestion.models import CodexExtensions, Event, EventType, Session, Vendor, VendorExtensions

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


def _extract_assistant_text(payload: dict[str, Any]) -> str | None:
    content = payload.get("content")
    if isinstance(content, str) and content:
        return content
    if isinstance(content, list):
        texts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") in {"output_text", "text"}
        ]
        joined = " ".join(text for text in texts if text).strip()
        return joined or None
    return None


class CodexAdapter(BaseAdapter):
    """Ingest Codex CLI JSONL rollout files from ~/.codex/sessions/."""

    vendor = Vendor.CODEX_CLI

    @dataclass
    class _ParseState:
        session_meta: dict[str, Any] = field(default_factory=dict)
        turn_context: dict[str, Any] = field(default_factory=dict)
        session_id: UUID = field(default_factory=uuid4)

    def ingest_file(self, path: Path) -> Session:
        events: list[Event] = []
        state = self._ParseState()

        for record in self._load_records(path):
            event = self._parse_raw_line_with_state(record, state)
            if event is not None:
                events.append(event)

        return self._build_session(path, events, state)

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

    def _parse_raw_line(self, line: dict[str, Any]) -> Event | None:  # noqa: C901
        return self._parse_raw_line_with_state(line, self._ParseState())

    def _parse_raw_line_with_state(
        self,
        line: dict[str, Any],
        state: _ParseState,
    ) -> Event | None:  # noqa: C901
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
                return self._event_from(
                    session_id=state.session_id,
                    event_type=EventType.SESSION_STARTED,
                    timestamp=timestamp,
                    actor="system",
                    payload=compact_dict(
                        {
                            "session_id_raw": payload.get("id"),
                            "cwd": payload.get("cwd"),
                            "cli_version": payload.get("cli_version"),
                            "model_provider": payload.get("model_provider"),
                            "originator": payload.get("originator"),
                            "source": payload.get("source"),
                            "git": payload.get("git"),
                            "raw": payload,
                        }
                    ),
                )

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
                        "images": payload.get("images"),
                        "local_images": payload.get("local_images"),
                        "raw": payload,
                    }
                ),
            )

        if inner_type == "agent_reasoning":
            return self._event_from(
                session_id=state.session_id,
                event_type=EventType.LLM_STREAM_EVENT,
                timestamp=timestamp,
                actor="assistant",
                payload=compact_dict(
                    {
                        "turn_id_raw": turn_id,
                        "text": payload.get("text") or payload.get("summary"),
                        "raw": payload,
                    }
                ),
            )

        if inner_type == "agent_message":
            return self._event_from(
                session_id=state.session_id,
                event_type=EventType.AGENT_RESPONSE_COMPLETED,
                timestamp=timestamp,
                actor="assistant",
                payload=compact_dict(
                    {
                        "turn_id_raw": turn_id,
                        "text": _extract_message_text(payload),
                        "raw": payload,
                    }
                ),
            )

        if inner_type == "task_complete":
            return self._event_from(
                session_id=state.session_id,
                event_type=EventType.TASK_COMPLETED,
                timestamp=timestamp,
                actor="assistant",
                payload=compact_dict(
                    {
                        "turn_id_raw": payload.get("turn_id"),
                        "last_agent_message": payload.get("last_agent_message"),
                        "raw": payload,
                    }
                ),
            )

        return None

    def _parse_response_item(
        self,
        payload: dict[str, Any],
        timestamp: datetime,
        state: _ParseState,
    ) -> Event | None:
        inner_type = payload.get("type", "")

        if inner_type == "message":
            if payload.get("role") != "assistant":
                return None
            return self._event_from(
                session_id=state.session_id,
                event_type=EventType.LLM_REQUEST_COMPLETED,
                timestamp=timestamp,
                actor="assistant",
                payload=compact_dict(
                    {
                        "text": _extract_assistant_text(payload),
                        "role": payload.get("role"),
                        "content": payload.get("content"),
                        "raw": payload,
                    }
                ),
            )

        if inner_type == "reasoning":
            return self._event_from(
                session_id=state.session_id,
                event_type=EventType.LLM_STREAM_EVENT,
                timestamp=timestamp,
                actor="assistant",
                payload=compact_dict(
                    {
                        "text": payload.get("summary") or payload.get("content"),
                        "raw": payload,
                    }
                ),
            )

        if inner_type == "function_call":
            tool_input = _parse_json_blob(payload.get("arguments"))
            return self._event_from(
                session_id=state.session_id,
                event_type=EventType.TOOL_CALL_REQUESTED,
                timestamp=timestamp,
                actor="assistant",
                payload=compact_dict(
                    {
                        "tool_call_id": payload.get("call_id"),
                        "tool_name": payload.get("name"),
                        "tool_kind": "function_call",
                        "tool_input": tool_input,
                        "raw": payload,
                    }
                ),
            )

        if inner_type == "function_call_output":
            raw_output = payload.get("output")
            success = infer_tool_success(raw_output)
            event_type = EventType.TOOL_CALL_SUCCEEDED if success is not False else EventType.TOOL_CALL_FAILED
            return self._event_from(
                session_id=state.session_id,
                event_type=event_type,
                timestamp=timestamp,
                actor="tool",
                payload=compact_dict(
                    {
                        "tool_call_id": payload.get("call_id"),
                        "tool_kind": "function_call_output",
                        "exit_code": extract_exit_code(raw_output),
                        "output": _parse_json_blob(raw_output),
                        "raw_output": raw_output,
                        "raw": payload,
                    }
                ),
            )

        if inner_type == "custom_tool_call":
            return self._event_from(
                session_id=state.session_id,
                event_type=EventType.TOOL_CALL_STARTED,
                timestamp=timestamp,
                actor="assistant",
                payload=compact_dict(
                    {
                        "tool_call_id": payload.get("call_id"),
                        "tool_name": payload.get("name"),
                        "tool_kind": "custom_tool_call",
                        "tool_input": payload.get("input"),
                        "tool_status": payload.get("status"),
                        "raw": payload,
                    }
                ),
            )

        if inner_type == "custom_tool_call_output":
            raw_output = payload.get("output")
            parsed_output = _parse_json_blob(raw_output)
            metadata = parsed_output.get("metadata") if isinstance(parsed_output, dict) else None
            return self._event_from(
                session_id=state.session_id,
                event_type=EventType.TOOL_CALL_SUCCEEDED,
                timestamp=timestamp,
                actor="tool",
                payload=compact_dict(
                    {
                        "tool_call_id": payload.get("call_id"),
                        "tool_kind": "custom_tool_call_output",
                        "exit_code": (
                            metadata.get("exit_code")
                            if isinstance(metadata, dict)
                            else extract_exit_code(raw_output)
                        ),
                        "duration_seconds": metadata.get("duration_seconds") if isinstance(metadata, dict) else None,
                        "output": parsed_output,
                        "raw_output": raw_output,
                        "raw": payload,
                    }
                ),
            )

        return None

    def _build_session(
        self,
        source: Path,
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
                model_context_window=None,
            )
        )
        turns = self._group_into_turns(
            state.session_id,
            events,
            end_at_next_user_prompt=True,
        )

        return Session(
            session_id=state.session_id,
            trajectory_id=uuid4(),
            vendor=Vendor.CODEX_CLI,
            started_at=started_at,
            ended_at=ended_at,
            timeline=self._build_timeline(events, turns),
            events=events,
            turns=turns,
            extensions=extensions,
        )

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
