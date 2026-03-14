"""Claude Code adapter — reads ~/.claude/projects/**/*.jsonl and normalises to canonical models."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from coding_trajectory.ingestion.adapters.base import BaseAdapter
from coding_trajectory.ingestion.common import compact_dict, infer_tool_success, parse_timestamp
from coding_trajectory.ingestion.models import (
    ClaudeCodeExtensions,
    EventConfidence,
    Event,
    EventProvenance,
    EventType,
    Session,
    Turn,
    Vendor,
    VendorExtensions,
)

logger = logging.getLogger(__name__)

_DEFAULT_CLAUDE_DIR = Path.home() / ".claude" / "projects"
_COMMAND_NAME_RE = re.compile(r"<command-name>(?P<name>[^<]+)</command-name>")


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

    def _parse_raw_line(self, line: dict) -> Event | None:
        events = self._parse_raw_object(line)
        return events[0] if events else None

    def _parse_record(self, source: Path, record: dict) -> list[Event]:
        return self._parse_raw_object(record)

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
        if raw_type == "system":
            return self._parse_system_line(obj, session_id, timestamp)
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
            event_type = EventType.TOOL_CALL_FAILED if block.get("is_error") or success is False else EventType.TOOL_CALL_SUCCEEDED
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

            if isinstance(tool_use_result, dict) and tool_use_result.get("team_name"):
                events.append(
                    Event(
                        session_id=session_id,
                        timestamp=timestamp,
                        type=EventType.BACKGROUND_TASK_STARTED,
                        vendor_source=self.vendor,
                        actor="system",
                        payload=compact_dict(
                            {
                                **base,
                                "team_name": tool_use_result.get("team_name"),
                                "lead_agent_id": tool_use_result.get("lead_agent_id"),
                            }
                        ),
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

        for thinking in _extract_thinking(content):
            events.append(
                Event(
                    session_id=session_id,
                    timestamp=timestamp,
                    type=EventType.LLM_STREAM_EVENT,
                    vendor_source=self.vendor,
                    actor="assistant",
                    payload=compact_dict({**base, "text": thinking, "usage": usage}),
                )
            )

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

            if tool.get("name") == "TeamCreate":
                events.append(
                    Event(
                        session_id=session_id,
                        timestamp=timestamp,
                        type=EventType.BACKGROUND_TASK_STARTED,
                        vendor_source=self.vendor,
                        actor="assistant",
                        payload=payload,
                    )
                )

        text = _extract_text(content)
        if text:
            event_type = (
                EventType.AGENT_RESPONSE_COMPLETED
                if stop_reason in {"end_turn", "stop_sequence", None}
                else EventType.LLM_REQUEST_COMPLETED
            )
            events.append(
                Event(
                    session_id=session_id,
                    timestamp=timestamp,
                    type=event_type,
                    vendor_source=self.vendor,
                    actor="assistant",
                    payload=compact_dict({**base, "text": text, "stop_reason": stop_reason, "usage": usage}),
                )
            )
        elif not events:
            events.append(
                Event(
                    session_id=session_id,
                    timestamp=timestamp,
                    type=EventType.LLM_REQUEST_COMPLETED,
                    vendor_source=self.vendor,
                    actor="assistant",
                    payload=compact_dict({**base, "stop_reason": stop_reason, "usage": usage}),
                )
            )

        return events

    def _parse_system_line(self, obj: dict, session_id: UUID, timestamp: datetime) -> list[Event]:
        subtype = obj.get("subtype")
        base = _base_payload(obj)
        if subtype == "compact_boundary":
            meta = obj.get("compactMetadata") or {}
            return [
                Event(
                    session_id=session_id,
                    timestamp=timestamp,
                    type=EventType.CONTEXT_COMPACTION_STARTED,
                    vendor_source=self.vendor,
                    actor="system",
                    payload=compact_dict(
                        {
                            **base,
                            "trigger": meta.get("trigger"),
                            "pre_tokens": meta.get("preTokens"),
                            "messages_summarized": meta.get("messagesSummarized"),
                        }
                    ),
                )
            ]

        if subtype == "turn_duration":
            return [
                Event(
                    session_id=session_id,
                    timestamp=timestamp,
                    type=EventType.AGENT_RESPONSE_COMPLETED,
                    vendor_source=self.vendor,
                    actor="system",
                    payload=compact_dict({**base, "duration_ms": obj.get("durationMs")}),
                )
            ]

        if subtype == "api_error":
            return [
                Event(
                    session_id=session_id,
                    timestamp=timestamp,
                    type=EventType.LLM_REQUEST_COMPLETED,
                    vendor_source=self.vendor,
                    actor="system",
                    payload=compact_dict({**base, "error": obj.get("error"), "cause": obj.get("cause")}),
                )
            ]

        if subtype == "local_command":
            content = obj.get("content")
            command_name = None
            if isinstance(content, str):
                match = _COMMAND_NAME_RE.search(content)
                if match:
                    command_name = match.group("name")
            if command_name == "/resume":
                event_type = EventType.SESSION_RESUMED
            elif command_name == "/exit":
                event_type = EventType.SESSION_ENDED
            else:
                return []
            return [
                Event(
                    session_id=session_id,
                    timestamp=timestamp,
                    type=event_type,
                    vendor_source=self.vendor,
                    actor="system",
                    provenance=EventProvenance.DERIVED,
                    confidence=EventConfidence.MEDIUM,
                    payload=compact_dict({**base, "command_name": command_name}),
                )
            ]

        return []

    def _build_session(self, source: Path, events: list[Event]) -> Session:
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
            extensions=self._parse_extensions(source),
        )

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
