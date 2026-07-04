"""Claude Code adapter — reads ~/.claude/projects/**/*.jsonl and normalises to canonical models."""

from __future__ import annotations

import logging
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from coding_trajectory.ingestion.adapters.base import BaseAdapter, SessionHeader
from coding_trajectory.ingestion.common import compact_dict, infer_tool_success, parse_timestamp
from coding_trajectory.ingestion.models import (
    ContextSourceObservation,
    ContextUsageObservation,
    Event,
    RuntimeObservation,
    Session,
    ToolStatus,
    Turn,
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
from coding_trajectory.ingestion.vendor_mechanisms.usage_metrics import (
    context_usage_observation,
    normalize_claude_usage,
)

logger = logging.getLogger(__name__)

_DEFAULT_CLAUDE_DIR = Path.home() / ".claude" / "projects"
_TEAMMATE_MESSAGE_RE = re.compile(r"<teammate-message(?P<attrs>[^>]*)>(?P<body>.*?)</teammate-message>", re.DOTALL)
_TEAMMATE_ATTR_RE = re.compile(r'(\w+)="(.*?)"')

_CLAUDE_FILE_TOOL_NAMES: frozenset[str] = frozenset({
    "Read", "Edit", "MultiEdit", "Write", "View", "NotebookEdit",
})
_CLAUDE_PLAN_TOOL_NAMES: frozenset[str] = frozenset({
    "TaskCreate", "TaskUpdate",
})


def _claude_item_kind(tool_name: str | None) -> str:
    if tool_name in _CLAUDE_PLAN_TOOL_NAMES:
        return "plan"
    if tool_name in _CLAUDE_FILE_TOOL_NAMES:
        return "file_change"
    return "tool_call"


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


def _as_int_or_none(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _compact_meta(record: TranscriptRecord, key: str) -> Any:
    """Read a field from a runtime transcript record's ``compact_metadata``.

    Returns ``None`` when the record carries no compaction metadata (non-
    ``compact_boundary`` records) or the field is absent, so non-compaction
    runtime observations are unaffected.
    """
    metadata = record.data.get("compact_metadata")
    if not isinstance(metadata, dict):
        return None
    return metadata.get(key)


def _estimate_prompt_tokens(text: str | None) -> int:
    """Rough char->token estimate mirroring ``visible_text_size`` for non-empty text.

    Inlined here (rather than importing ``analysis.content_size``) to keep
    ingestion from depending on the analysis layer.
    """
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _first_api_prompt_text(
    *,
    turns: list[Turn],
    events: list[Event],
    context_usage: list[ContextUsageObservation],
) -> str | None:
    """Return the user prompt text of the first turn that produced a usage observation.

    The first API call's full input is the stable system-prompt + tools prefix
    plus that call's user message, so isolating the prefix requires subtracting
    the prompt that was actually sent. Local commands (e.g. ``/model``) emit
    user-prompt events but never reach the API, so the prompt is resolved
    through the turn that owns the first usage observation rather than by
    timestamp order alone.
    """
    usage_event_ids = {
        observation.source_event_id
        for observation in context_usage
        if observation.source_event_id is not None
    }
    if not usage_event_ids:
        return None
    event_by_id = {event.event_id: event for event in events}
    for turn in sorted(turns, key=lambda item: item.sequence):
        if not any(event_id in usage_event_ids for event_id in turn.event_ids):
            continue
        if turn.user_request_event_id is None:
            return None
        event = event_by_id.get(turn.user_request_event_id)
        if event is None:
            return None
        text = event.payload.get("text")
        return text if isinstance(text, str) else None
    return None


def _starting_context_sources(
    *,
    started_at: datetime,
    context_usage: list[ContextUsageObservation],
    first_prompt_text: str | None = None,
) -> list[ContextSourceObservation]:
    """Synthesize a starting-context source from the first API call's input.

    Claude Code JSONL never records the system prompt, tool definitions,
    AGENTS.md, skills, or MCP text — they are injected client-side at request
    time, so the observed context composition cannot measure them from visible
    content. The first assistant turn's full input (``used_input_tokens``) is
    the stable system-prompt + tools prefix plus that turn's user message, so
    subtract the visible-text estimate of the first prompt to isolate the
    prefix. ``used_input_tokens`` is robust to a partially-warm cache: when
    only part of the system prompt was already cached, ``cache_read`` +
    ``cache_creation`` undercounts the prefix, but the full request total never
    does. The cached-prefix sum is used only as a fallback when the used-input
    total is unavailable.
    """
    prompt_tokens = _estimate_prompt_tokens(first_prompt_text)
    for observation in context_usage:
        usage = observation.usage or {}
        used_input = max(observation.used_input_tokens, 0)
        cached = _as_int_or_none(usage.get("cached_input_tokens")) or 0
        cache_creation = _as_int_or_none(usage.get("cache_creation_input_tokens")) or 0
        estimate = (
            max(used_input - prompt_tokens, 0)
            if used_input > 0
            else cached + cache_creation
        )
        if estimate <= 0:
            continue
        return [
            ContextSourceObservation(
                timestamp=started_at,
                key="base_system",
                label="System prompt & tools",
                text="",
                source="claude_first_input_estimate",
                reported_tokens=estimate,
            )
        ]
    return []


def _record_title(record: dict[str, object]) -> str | None:
    for key in ("title", "sessionTitle", "conversationTitle", "threadName", "aiTitle"):
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

    mode: str | None = None
    permission_mode: str | None = None
    last_prompt: str | None = None
    for record in records:
        raw_type = record.get("type")
        if raw_type == "mode":
            mode = _as_non_empty_str(record.get("mode")) or mode
        elif raw_type == "permission-mode":
            permission_mode = _as_non_empty_str(record.get("permissionMode")) or permission_mode
        elif raw_type == "last-prompt":
            last_prompt = _as_non_empty_str(record.get("lastPrompt")) or last_prompt
    if permission_mode is None:
        permission_mode = _as_non_empty_str(first.get("permissionMode"))

    return ClaudeSubagentInput(
        source_path=str(source.resolve()),
        is_subagent_file=is_subagent_file,
        parent_session_id=parent_session_id,
        raw_session_id=raw_session_id,
        team_name=first.get("teamName"),
        is_sidechain=first.get("isSidechain"),
        permission_mode=permission_mode,
        mode=mode,
        last_prompt=last_prompt,
        parent_uuid=first.get("parentUuid"),
        request_id=first.get("uuid"),
        agent_name=first.get("agentId") or first.get("agentName") or first.get("slug"),
        agent_role=meta.get("agentType") if isinstance(meta.get("agentType"), str) else None,
        description=meta.get("description") if isinstance(meta.get("description"), str) else None,
        title=title or _as_non_empty_str(meta.get("title")),
        tool_use_id=_as_non_empty_str(meta.get("toolUseId")),
        spawn_depth=_as_int_or_none(meta.get("spawnDepth")),
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


def _extract_image_blocks(content: str | list | None) -> list[dict]:
    if not isinstance(content, list):
        return []
    return [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "image"
    ]


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
            "prompt_id": obj.get("promptId"),
            "prompt_source": obj.get("promptSource"),
            "origin": obj.get("origin"),
            "image_paste_ids": obj.get("imagePasteIds"),
            "is_sidechain": obj.get("isSidechain"),
            "team_name": obj.get("teamName"),
            "agent_id": obj.get("agentId"),
            "agent_name": obj.get("agentName") or obj.get("slug"),
            "attribution_agent": obj.get("attributionAgent"),
            "version": obj.get("version"),
            "cwd": obj.get("cwd"),
            "git_branch": obj.get("gitBranch"),
            "permission_mode": obj.get("permissionMode"),
        }
    )


class ClaudeCodeAdapter(BaseAdapter):
    """Normalise Claude Code JSONL session files into canonical Session objects."""

    vendor = Vendor.CLAUDE_CODE

    _TITLE_LOOKAHEAD = 50

    def scan_header(self, source: Path) -> SessionHeader | None:
        scanned: list[dict] = []
        raw_session_id: UUID | None = None
        title: str | None = None
        cwd: str | None = None
        since_session_id = 0
        for record in self._iter_records(source):
            scanned.append(record)
            if raw_session_id is None:
                session_id_str = record.get("sessionId")
                if session_id_str:
                    try:
                        raw_session_id = UUID(session_id_str)
                    except (ValueError, AttributeError):
                        raw_session_id = None
                    else:
                        cwd = _as_non_empty_str(record.get("cwd"))
            if title is None:
                title = _record_title(record)
            if raw_session_id is not None and title is not None:
                break
            if raw_session_id is not None:
                since_session_id += 1
                if since_session_id >= self._TITLE_LOOKAHEAD:
                    break
        if raw_session_id is None:
            return None
        mechanism = _subagent_input(source, scanned, raw_session_id)
        session_id, parent_session_id = canonical_session_ids(mechanism)
        return SessionHeader(
            session_id=session_id,
            vendor=Vendor.CLAUDE_CODE,
            parent_session_id=parent_session_id,
            title=mechanism.title,
            cwd=cwd,
        )

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
        runtime_observations = [
            RuntimeObservation(
                timestamp=record.timestamp,
                kind=f"claude_{record.data.get('raw_type')}",
                duration_ms=record.data.get("duration_ms"),
                reason=record.data.get("content") or record.data.get("subtype"),
                # ``compact_metadata`` is only present on ``compact_boundary``
                # records; pass its pre/post/dropped/trigger through so stats can
                # surface how much context the compaction reclaimed. The fields
                # are otherwise discarded here.
                pre_tokens=_compact_meta(record, "pre_tokens"),
                post_tokens=_compact_meta(record, "post_tokens"),
                cumulative_dropped_tokens=_compact_meta(
                    record, "cumulative_dropped_tokens"
                ),
                trigger=_compact_meta(record, "trigger"),
            )
            for record in transcript
            if record.kind == "runtime"
        ]
        context_usage = [
            observation
            for record in transcript
            if (
                observation := context_usage_observation(
                    timestamp=record.timestamp,
                    source="claude_usage_block",
                    normalized=record.data.get("vendor_data", {}),
                    source_event_id=record.record_id,
                    # Claude Code emits Anthropic-schema usage (input_tokens is
                    # uncached) regardless of the underlying routed model, so the
                    # net-input convention applies to every observation.
                    provider="anthropic",
                    category_source="claude_usage_block",
                )
            )
            is not None
        ]
        context_sources = _starting_context_sources(
            started_at=started_at,
            context_usage=context_usage,
            first_prompt_text=_first_api_prompt_text(
                turns=turns, events=events, context_usage=context_usage
            ),
        )

        return Session(
            session_id=session_id,
            vendor=self.vendor,
            agent_name=extensions.claude_code.agent_name if extensions and extensions.claude_code else None,
            started_at=started_at,
            ended_at=ended_at,
            parent_session_id=parent_session_id,
            events=events,
            turns=turns,
            context_usage=context_usage,
            context_sources=context_sources,
            runtime_observations=runtime_observations,
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

    def _build_transcript(self, records: list[dict]) -> tuple[list[TranscriptRecord], list[ClaudeTeamStateInput]]:
        """Extract only CT-useful transcript facts from Claude Code JSONL records."""
        transcript: list[TranscriptRecord] = []
        team_inputs: list[ClaudeTeamStateInput] = []

        for record in records:
            raw_type = record.get("type")

            # File-history-snapshot carries its timestamp inside snapshot.timestamp.
            if raw_type == "file-history-snapshot":
                snapshot = record.get("snapshot") or {}
                ts = parse_timestamp(snapshot.get("timestamp"))
                if ts is None:
                    continue
                base = _base_payload(record)
                transcript.append(
                    TranscriptRecord(
                        sequence=len(transcript),
                        timestamp=ts,
                        vendor=Vendor.CLAUDE_CODE,
                        role="runtime",
                        kind="runtime",
                        data={
                            **base,
                            "raw_type": "file-history-snapshot",
                            "snapshot": snapshot,
                        },
                    )
                )
                continue

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
                    image_blocks = _extract_image_blocks(content)
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
                                "image_count": len(image_blocks),
                                "team_request_summary": team_request_summary,
                            },
                        )
                    )
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

            elif raw_type == "assistant":
                message = record.get("message", {})
                content = message.get("content", [])
                stop_reason = message.get("stop_reason")
                usage = message.get("usage")
                base = _base_payload(record)
                tool_uses = _tool_use_blocks(content)
                text = _extract_text(content)
                normalized_metrics = normalize_claude_usage(model=message.get("model"), usage=usage)
                thinking_blocks = _extract_thinking(content)
                vendor_data = compact_dict(
                    {
                        **normalized_metrics,
                        "stop_reason": stop_reason,
                    }
                )

                # Promote each thinking block to a first-class reasoning item.
                # Thinking content is real resident context — it accumulates in
                # the prompt cache and is counted in used_input_tokens — but
                # stashing it only in vendor_data left it invisible to context
                # composition sizing, so the observed composition undercounted
                # the context window by roughly the accumulated thinking. Mirror
                # the codex adapter's reasoning handling by emitting one
                # reasoning transcript record per thinking block.
                for thinking_text in thinking_blocks:
                    transcript.append(
                        TranscriptRecord(
                            sequence=len(transcript),
                            timestamp=ts,
                            vendor=Vendor.CLAUDE_CODE,
                            role="assistant",
                            kind="tool_call",
                            data={
                                **base,
                                "tool_name": "reasoning",
                                "tool_call_id": f"thinking:{len(transcript)}",
                                "text": thinking_text,
                                "item_kind": "reasoning",
                            },
                        )
                    )

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
                            },
                        )
                    )

                for block in tool_uses:
                    tool_id = block.get("id")
                    tool_name = block.get("name")
                    transcript.append(
                        TranscriptRecord(
                            sequence=len(transcript),
                            timestamp=ts,
                            vendor=Vendor.CLAUDE_CODE,
                            role="assistant",
                            kind="tool_call",
                            data={
                                **base,
                                "tool_name": tool_name,
                                "tool_call_id": tool_id,
                                "input": block.get("input"),
                                "item_kind": _claude_item_kind(tool_name),
                            },
                        )
                    )

            elif raw_type == "system":
                base = _base_payload(record)
                subtype = record.get("subtype")
                if subtype == "compact_boundary":
                    # Compaction evicts (almost) all pre-boundary conversation,
                    # preserving only the few messages named in compactMetadata.
                    # The boundary timestamp is the signal the composition layer
                    # uses to exclude evicted (non-resident) items; the preserved
                    # UUIDs and dropped-token counts are carried for future
                    # per-item preserved-segment attribution.
                    compact_meta = record.get("compactMetadata") or {}
                    preserved = compact_meta.get("preservedMessages") or {}
                    preserved_uuids = preserved.get("allUuids") or preserved.get("uuids") or []
                    transcript.append(
                        TranscriptRecord(
                            sequence=len(transcript),
                            timestamp=ts,
                            vendor=Vendor.CLAUDE_CODE,
                            role="runtime",
                            kind="runtime",
                            data={
                                **base,
                                "raw_type": "compact_boundary",
                                "content": _as_non_empty_str(record.get("content")),
                                "compact_metadata": compact_dict(
                                    {
                                        "trigger": _as_non_empty_str(compact_meta.get("trigger")),
                                        "pre_tokens": _as_int_or_none(compact_meta.get("preTokens")),
                                        "post_tokens": _as_int_or_none(compact_meta.get("postTokens")),
                                        "cumulative_dropped_tokens": _as_int_or_none(
                                            compact_meta.get("cumulativeDroppedTokens")
                                        ),
                                        "preserved_uuids": (
                                            list(preserved_uuids)
                                            if isinstance(preserved_uuids, list)
                                            else []
                                        ),
                                    }
                                ),
                            },
                            fidelity="synthetic",
                        )
                    )
                    continue
                if subtype in {"turn_duration", "local_command"}:
                    transcript.append(
                        TranscriptRecord(
                            sequence=len(transcript),
                            timestamp=ts,
                            vendor=Vendor.CLAUDE_CODE,
                            role="runtime",
                            kind="runtime",
                            data={
                                **base,
                                "raw_type": "system",
                                "subtype": subtype,
                                "duration_ms": _as_int_or_none(record.get("durationMs")),
                                "message_count": _as_int_or_none(record.get("messageCount")),
                                "pending_background_agent_count": _as_int_or_none(
                                    record.get("pendingBackgroundAgentCount")
                                ),
                                "content": _as_non_empty_str(record.get("content")),
                            },
                        )
                    )
                continue

            elif raw_type == "attachment":
                base = _base_payload(record)
                transcript.append(
                    TranscriptRecord(
                        sequence=len(transcript),
                        timestamp=ts,
                        vendor=Vendor.CLAUDE_CODE,
                        role="runtime",
                        kind="runtime",
                        data={
                            **base,
                            "raw_type": "attachment",
                            "attachment_type": record.get("attachmentType") or record.get("subtype"),
                            "name": _as_non_empty_str(record.get("name")),
                            "path": _as_non_empty_str(record.get("path")),
                            "content": record.get("content"),
                        },
                    )
                )
                continue

            elif raw_type == "queue-operation":
                base = _base_payload(record)
                transcript.append(
                    TranscriptRecord(
                        sequence=len(transcript),
                        timestamp=ts,
                        vendor=Vendor.CLAUDE_CODE,
                        role="runtime",
                        kind="runtime",
                        data={
                            **base,
                            "raw_type": "queue-operation",
                            "operation": record.get("operation"),
                            "task": record.get("task"),
                        },
                    )
                )
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
