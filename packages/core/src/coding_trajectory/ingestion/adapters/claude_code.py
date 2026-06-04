"""Claude Code adapter — reads ~/.claude/projects/**/*.jsonl and normalises to canonical models."""

from __future__ import annotations

import logging
import json
import re
from pathlib import Path
from uuid import UUID, uuid4

from coding_trajectory.ingestion.adapters.base import BaseAdapter, infer_account_identity
from coding_trajectory.ingestion.common import compact_dict, infer_tool_success, parse_timestamp
from coding_trajectory.ingestion.models import (
    Session,
    ToolStatus,
    Vendor,
)
from coding_trajectory.ingestion.transcript import TranscriptRecord, events_from_transcript, project_transcript
from coding_trajectory.ingestion.vendor_mechanisms.claude_subagent import (
    ClaudeSubagentInput,
    canonical_session_ids,
    extensions as claude_extensions,
)
from coding_trajectory.ingestion.vendor_mechanisms.claude_team import (
    ClaudeTeamMessage,
    ClaudeTeamStateInput,
    build_turn_team_state,
    high_value_teammate_request,
)
from coding_trajectory.ingestion.vendor_mechanisms.usage_metrics import normalize_claude_usage

logger = logging.getLogger(__name__)

_DEFAULT_CLAUDE_DIR = Path.home() / ".claude" / "projects"
_TEAMMATE_MESSAGE_RE = re.compile(r"<teammate-message(?P<attrs>[^>]*)>(?P<body>.*?)</teammate-message>", re.DOTALL)
_TEAMMATE_ATTR_RE = re.compile(r'(\w+)="(.*?)"')


def _parse_team_messages(raw: str | None) -> list[ClaudeTeamMessage]:
    if not raw:
        return []

    messages: list[ClaudeTeamMessage] = []
    for match in _TEAMMATE_MESSAGE_RE.finditer(raw):
        attrs = {key: value for key, value in _TEAMMATE_ATTR_RE.findall(match.group("attrs") or "")}
        body = (match.group("body") or "").strip()
        payload: dict | None = None
        if body.startswith("{") and body.endswith("}"):
            try:
                loaded = json.loads(body)
            except json.JSONDecodeError:
                loaded = None
            payload = loaded if isinstance(loaded, dict) else None
        messages.append(
            ClaudeTeamMessage(
                teammate_id=attrs.get("teammate_id"),
                color=attrs.get("color"),
                summary=attrs.get("summary"),
                body=body,
                event_type=payload.get("type") if payload else None,
            )
        )
    return messages


def _as_non_empty_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _record_title(record: dict[str, object]) -> str | None:
    for key in ("title", "sessionTitle", "conversationTitle", "threadName"):
        title = _as_non_empty_str(record.get(key))
        if title:
            return title
    return None


def _read_subagent_meta(source: Path) -> dict[str, object]:
    meta_path = source.with_name(f"{source.stem}.meta.json")
    try:
        with meta_path.open(encoding="utf-8") as fh:
            loaded = json.load(fh)
    except OSError:
        return {}
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _subagent_input(source: Path, records: list[dict], raw_session_id: UUID) -> ClaudeSubagentInput:
    first = next((record for record in records if record.get("sessionId")), {})
    title = next((_record_title(record) for record in records if _record_title(record)), None)
    is_subagent_file = source.parent.name == "subagents"
    parent_session_id: UUID | None = None
    if is_subagent_file:
        try:
            parent_session_id = UUID(source.parent.parent.name)
        except ValueError:
            parent_session_id = None
    meta = _read_subagent_meta(source) if is_subagent_file else {}
    return ClaudeSubagentInput(
        source_path=str(source.resolve()),
        is_subagent_file=is_subagent_file,
        parent_session_id=parent_session_id,
        raw_session_id=raw_session_id,
        team_name=first.get("teamName"),
        is_sidechain=first.get("isSidechain"),
        permission_mode=first.get("permissionMode"),
        parent_uuid=first.get("parentUuid"),
        request_id=first.get("uuid"),
        agent_name=first.get("agentId") or first.get("agentName") or first.get("slug"),
        agent_role=meta.get("agentType") if isinstance(meta.get("agentType"), str) else None,
        description=meta.get("description") if isinstance(meta.get("description"), str) else None,
        title=title or _as_non_empty_str(meta.get("title")),
    )


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

    def _build_session(self, source: Path, records: list[dict]) -> Session:
        raw_session_id = self._first_session_id(records)
        if raw_session_id is None:
            raise ValueError(f"ClaudeCodeAdapter: no session id parsed from {source}")

        mechanism = _subagent_input(source, records, raw_session_id)
        extensions = claude_extensions(mechanism)
        session_id, parent_session_id = canonical_session_ids(mechanism)
        transcript, team_inputs = self._build_transcript(records)
        if not transcript:
            raise ValueError(f"ClaudeCodeAdapter: no transcript records parsed from {source}")
        events = events_from_transcript(session_id=session_id, records=transcript)
        turns = project_transcript(
            session_id=session_id,
            vendor=Vendor.CLAUDE_CODE,
            records=transcript,
        )
        for turn, team_input in zip(turns, team_inputs, strict=False):
            turn.team_state = build_turn_team_state(turn, team_input=team_input)
        started_at = min(record.timestamp for record in transcript)
        ended_at = max(record.timestamp for record in transcript)

        return Session(
            session_id=session_id,
            vendor=self.vendor,
            agent_name=extensions.claude_code.agent_name if extensions and extensions.claude_code else None,
            account=_infer_claude_account(records),
            started_at=started_at,
            ended_at=ended_at,
            parent_session_id=parent_session_id,
            events=events,
            turns=turns,
            extensions=extensions,
        )

    def _first_session_id(self, records: list[dict]) -> UUID | None:
        for record in records:
            session_id_str = record.get("sessionId")
            if not session_id_str:
                continue
            try:
                return UUID(session_id_str)
            except (ValueError, AttributeError):
                continue
        return None


def _infer_claude_account(records: list[dict]) -> object:
    for record in records[:8]:
        account = infer_account_identity(record, vendor=Vendor.CLAUDE_CODE)
        if account is not None:
            return account
    return None

    def _build_transcript(self, records: list[dict]) -> tuple[list[TranscriptRecord], list[ClaudeTeamStateInput]]:
        """Extract only CT-useful transcript facts from Claude Code JSONL records."""
        transcript: list[TranscriptRecord] = []
        team_inputs: list[ClaudeTeamStateInput] = []
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
                base = _base_payload(record)

                if _is_real_user_prompt(record):
                    text = _extract_text(content)
                    team_input = ClaudeTeamStateInput(messages=_parse_team_messages(text))
                    team_inputs.append(team_input)
                    team_request_summary = high_value_teammate_request(team_input.messages)
                    transcript.append(
                        TranscriptRecord(
                            sequence=len(transcript),
                            timestamp=ts,
                            vendor=Vendor.CLAUDE_CODE,
                            role="user",
                            kind="user_message",
                            data={
                                **base,
                                "text": text,
                                "team_request_summary": team_request_summary,
                            },
                        )
                    )
                    collecting_results = False
                else:
                    for block in _tool_result_blocks(content):
                        tool_use_result = record.get("toolUseResult")
                        success = infer_tool_success(tool_use_result)
                        transcript.append(
                            TranscriptRecord(
                                sequence=len(transcript),
                                timestamp=ts,
                                vendor=Vendor.CLAUDE_CODE,
                                role="tool",
                                kind="tool_result",
                                data={
                                    **base,
                                    "tool_call_id": block.get("tool_use_id") or block.get("toolUseID"),
                                    "output": tool_use_result if tool_use_result is not None else block.get("content"),
                                    "source_tool_assistant_uuid": record.get("sourceToolAssistantUUID"),
                                    "status": (
                                        ToolStatus.FAILED.value
                                        if block.get("is_error") or success is False
                                        else ToolStatus.COMPLETED.value
                                    ),
                                },
                                fidelity="synthetic",
                            )
                        )
                    if _tool_result_blocks(content):
                        collecting_results = True

            elif raw_type == "assistant":
                message = record.get("message", {})
                content = message.get("content", [])
                stop_reason = message.get("stop_reason")
                usage = message.get("usage")
                base = _base_payload(record)
                tool_uses = _tool_use_blocks(content)
                text = _extract_text(content)
                normalized_metrics = normalize_claude_usage(model=message.get("model"), usage=usage)
                vendor_data = compact_dict(
                    {
                        "thinking": _extract_thinking(content) or None,
                        **normalized_metrics,
                        "stop_reason": stop_reason,
                    }
                )

                emitted_step_anchor = False
                if text or vendor_data or not tool_uses:
                    transcript.append(
                        TranscriptRecord(
                            sequence=len(transcript),
                            timestamp=ts,
                            vendor=Vendor.CLAUDE_CODE,
                            role="assistant",
                            kind="assistant_message",
                            data={
                                **base,
                                "text": text,
                                "vendor_data": vendor_data,
                                "flush_before": collecting_results,
                                "flush_after": stop_reason is not None and not tool_uses,
                            },
                        )
                    )
                    emitted_step_anchor = True
                    collecting_results = False

                for index, block in enumerate(tool_uses):
                    tool_id = block.get("id")
                    transcript.append(
                        TranscriptRecord(
                            sequence=len(transcript),
                            timestamp=ts,
                            vendor=Vendor.CLAUDE_CODE,
                            role="assistant",
                            kind="tool_call",
                            data={
                                **base,
                                "tool_name": block.get("name"),
                                "tool_call_id": tool_id,
                                "input": block.get("input"),
                                "vendor_data": vendor_data if not emitted_step_anchor and index == 0 else {},
                                "flush_before": collecting_results and not emitted_step_anchor and index == 0,
                            },
                        )
                    )
                    collecting_results = False

            elif raw_type == "system":
                continue

        return transcript, team_inputs

    def ingest_default(self) -> list[Session]:
        sessions: list[Session] = []
        for jsonl_path in sorted(_DEFAULT_CLAUDE_DIR.rglob("*.jsonl")):
            try:
                sessions.append(self.ingest_file(jsonl_path))
            except Exception as exc:
                logger.warning("ClaudeCodeAdapter: failed to ingest %s: %s", jsonl_path, exc)
        return sessions
