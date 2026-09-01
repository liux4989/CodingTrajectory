"""Claude Code adapter — reads ~/.claude/projects/**/*.jsonl and normalises to canonical models."""

from __future__ import annotations

import logging
import json
import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from coding_trajectory.ingestion.adapters._shared import (
    SHARED_FILE_TOOL_NAMES,
    ToolTaxonomy,
    content_block_field_texts,
    content_block_texts,
    content_blocks,
    int_or_none,
    non_empty_str,
)
from coding_trajectory.ingestion.adapters.base import BaseAdapter, SessionHeader
from coding_trajectory.ingestion.common import (
    compact_dict,
    infer_tool_success,
    parse_timestamp,
)
from coding_trajectory.ingestion.models import (
    ContextSourceObservation,
    ContextUsageObservation,
    Event,
    Item,
    RuntimeObservation,
    Session,
    TeamTurnState,
    ToolCallItem,
    ToolStatus,
    Turn,
    Vendor,
)
from coding_trajectory.ingestion.provenance import RecordSpan
from coding_trajectory.ingestion.retention import (
    CanonicalRetention,
    compact_context_usage_observation,
)
from coding_trajectory.ingestion.transcript import (
    TranscriptRecord,
    TranscriptStabilizer,
    build_session_provenance,
    compact_session_cwd,
    events_from_transcript,
    project_transcript,
)
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

_TEAMMATE_MESSAGE_RE = re.compile(
    r"<teammate-message(?P<attrs>[^>]*)>(?P<body>.*?)</teammate-message>", re.DOTALL
)
_TEAMMATE_ATTR_RE = re.compile(r'(\w+)="(.*?)"')
# Claude Code logs an ``/effort`` switch as a ``<local-command-stdout>Set effort
# level to <LEVEL> ...`` user record. The level word (``max``, ``ultracode``,
# ``high`` ...) is the resolved effort in effect from that turn onward.
_CLAUDE_EFFORT_STDOUT_RE = re.compile(r"Set effort level to (\w+)")

_CLAUDE_TOOL_TAXONOMY = ToolTaxonomy(
    command_names=frozenset({"Bash", "bash"}),
    plan_names=frozenset({"TaskCreate", "TaskUpdate"}),
    file_change_names=SHARED_FILE_TOOL_NAMES | frozenset({"NotebookEdit"}),
)


def _claude_item_kind(tool_name: str | None) -> str:
    return _CLAUDE_TOOL_TAXONOMY.classify(tool_name)


def _parse_team_messages(raw: str | None) -> list[ClaudeTeamMessage]:
    if not raw:
        return []

    messages: list[ClaudeTeamMessage] = []
    for match in _TEAMMATE_MESSAGE_RE.finditer(raw):
        attrs = {
            key: value
            for key, value in _TEAMMATE_ATTR_RE.findall(match.group("attrs") or "")
        }
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


_as_non_empty_str = non_empty_str
_as_int_or_none = int_or_none


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


_TEAM_TOOL_NAMES: frozenset[str] = frozenset({"Agent", "TaskCreate", "TaskUpdate"})


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


def _subagent_input(
    source: Path, records: list[dict], raw_session_id: UUID
) -> ClaudeSubagentInput:
    scan = _ClaudeRecordScan()
    for record in records:
        scan.observe_meta(record)
    return _subagent_input_from_scan(source, scan, raw_session_id)


def _subagent_input_from_scan(
    source: Path, scan: "_ClaudeRecordScan", raw_session_id: UUID
) -> ClaudeSubagentInput:
    first = scan.first_session_record or {}
    title = scan.title
    is_subagent_file = source.parent.name == "subagents"
    parent_session_id: UUID | None = None
    if is_subagent_file:
        try:
            parent_session_id = UUID(source.parent.parent.name)
        except ValueError:
            parent_session_id = None
    meta = _read_subagent_meta(source) if is_subagent_file else {}

    permission_mode = scan.permission_mode
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
        mode=scan.mode,
        last_prompt=scan.last_prompt,
        parent_uuid=first.get("parentUuid"),
        request_id=first.get("uuid"),
        agent_name=first.get("agentId") or first.get("agentName") or first.get("slug"),
        agent_role=meta.get("agentType")
        if isinstance(meta.get("agentType"), str)
        else None,
        description=meta.get("description")
        if isinstance(meta.get("description"), str)
        else None,
        title=title or _as_non_empty_str(meta.get("title")),
        tool_use_id=_as_non_empty_str(meta.get("toolUseId")),
        spawn_depth=_as_int_or_none(meta.get("spawnDepth")),
    )


class _ClaudeRecordScan:
    """Single-pass collector for record facts used outside the transcript.

    Replaces repeated full-list scans of record facts so ingestion can
    stream records instead of materializing them.
    """

    def __init__(self) -> None:
        self.first_session_record: dict | None = None
        self.raw_session_id: UUID | None = None
        self.title: str | None = None
        self.mode: str | None = None
        self.permission_mode: str | None = None
        self.last_prompt: str | None = None
        self._effort_prev: str | None = None
        self.effort_observations: list[RuntimeObservation] = []

    def observe_meta(self, record: dict) -> None:
        """Capture session-id/title/mode scalars (no effort scan)."""
        sid_str = record.get("sessionId")
        if self.first_session_record is None and sid_str:
            self.first_session_record = record
        if self.raw_session_id is None and sid_str:
            try:
                self.raw_session_id = UUID(str(sid_str))
            except (ValueError, AttributeError, TypeError):
                pass
        if self.title is None:
            title = _record_title(record)
            if title:
                self.title = title
        raw_type = record.get("type")
        if raw_type == "mode":
            self.mode = _as_non_empty_str(record.get("mode")) or self.mode
        elif raw_type == "permission-mode":
            self.permission_mode = (
                _as_non_empty_str(record.get("permissionMode")) or self.permission_mode
            )
        elif raw_type == "last-prompt":
            self.last_prompt = (
                _as_non_empty_str(record.get("lastPrompt")) or self.last_prompt
            )

    def observe(self, record: dict) -> None:
        """Capture all scan facts, including effort change-points."""
        self.observe_meta(record)
        if record.get("type") != "user":
            return
        message = record.get("message")
        if not isinstance(message, dict):
            return
        text = _extract_text(message.get("content"))
        if not text:
            return
        match = _CLAUDE_EFFORT_STDOUT_RE.search(text)
        if match is None:
            return
        level = match.group(1)
        if self._effort_prev is not None and level == self._effort_prev:
            return
        ts = parse_timestamp(record.get("timestamp"))
        if ts is None:
            return
        self.effort_observations.append(
            RuntimeObservation(
                timestamp=ts,
                kind="effort_changed",
                effort_from=self._effort_prev,
                effort_to=level,
            )
        )
        self._effort_prev = level


_extract_text = content_block_texts


def _extract_image_blocks(content: str | list | None) -> list[dict]:
    return content_blocks(content, "image")


def _extract_thinking(content: list | None) -> list[str]:
    return content_block_field_texts(content, "thinking", "thinking")


def _tool_result_blocks(content: list | None) -> list[dict]:
    return content_blocks(content, "tool_result")


def _tool_use_blocks(content: list | None) -> list[dict]:
    return content_blocks(content, "tool_use")


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

    def _build_session(
        self,
        source: Path,
        records: Iterable[tuple[dict, RecordSpan | None]],
        *,
        retention: CanonicalRetention = "trajectory",
    ) -> Session:
        scan = _ClaudeRecordScan()
        transcript, team_inputs = self._build_transcript(records, scan=scan)
        raw_session_id = scan.raw_session_id
        if raw_session_id is None:
            raise ValueError(f"ClaudeCodeAdapter: no session id parsed from {source}")

        mechanism = _subagent_input_from_scan(source, scan, raw_session_id)
        extensions = claude_extensions(mechanism)
        session_id, parent_session_id = canonical_session_ids(mechanism)
        if not transcript:
            raise ValueError(
                f"ClaudeCodeAdapter: no transcript records parsed from {source}"
            )
        compact = (
            TranscriptStabilizer(vendor=Vendor.CLAUDE_CODE, source=source)
            if retention == "measurements"
            else None
        )
        events = events_from_transcript(
            session_id=session_id, records=transcript, stabilizer=compact
        )
        turns = project_transcript(
            session_id=session_id,
            vendor=Vendor.CLAUDE_CODE,
            records=transcript,
            compact=compact,
        )
        if compact is not None:
            self.last_provenance = build_session_provenance(
                session_id=session_id,
                vendor=Vendor.CLAUDE_CODE,
                source=source,
                stabilizer=compact,
                turns=turns,
            )
        if compact is not None:
            for turn, team_state in zip(
                turns, self._compact_team_states(turns, team_inputs), strict=False
            ):
                turn.team_state = team_state
        else:
            for turn, team_input in zip(turns, team_inputs, strict=False):
                turn.team_state = build_turn_team_state(turn, team_input=team_input)
        started_at = min(record.timestamp for record in transcript)
        ended_at = max(record.timestamp for record in transcript)
        runtime_observations = self._build_runtime_observations(
            transcript, scan.effort_observations
        )
        # A Claude Code ``uuid`` identifies one local stream event, whereas
        # ``message.id`` identifies the provider response.  One response is
        # recorded as several stream events (thinking, text, tool-use, final
        # state), each repeating the same final usage block.  Preserve every
        # event in the transcript, but retain usage once per provider response
        # so billed accounting does not charge the same request repeatedly.
        usage_records_by_response_id: dict[str, TranscriptRecord] = {}
        usage_records_without_response_id: list[TranscriptRecord] = []
        for record in transcript:
            vendor_data = record.data.get("vendor_data", {})
            if not isinstance(vendor_data, dict):
                continue
            response_id = vendor_data.get("provider_response_id")
            if isinstance(response_id, str) and response_id:
                # The final stream event is the most complete observation and
                # remains associated with the turn that owns the response.
                usage_records_by_response_id[response_id] = record
            else:
                usage_records_without_response_id.append(record)

        context_usage = [
            observation
            for record in [
                *usage_records_by_response_id.values(),
                *usage_records_without_response_id,
            ]
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
        context_sources = (
            []
            if compact is not None
            else _starting_context_sources(
                started_at=started_at,
                context_usage=context_usage,
                first_prompt_text=_first_api_prompt_text(
                    turns=turns, events=events, context_usage=context_usage
                ),
            )
        )
        if compact is not None:
            context_usage = [
                compact_context_usage_observation(observation, compact.event_ids)
                for observation in context_usage
            ]

        return Session(
            session_id=session_id,
            vendor=self.vendor,
            agent_name=extensions.claude_code.agent_name
            if extensions and extensions.claude_code
            else None,
            started_at=started_at,
            ended_at=ended_at,
            parent_session_id=parent_session_id,
            events=events,
            turns=turns,
            context_usage=context_usage,
            context_sources=context_sources,
            runtime_observations=runtime_observations,
            extensions=extensions,
            cwd=(
                compact_session_cwd(
                    vendor=Vendor.CLAUDE_CODE,
                    source=source,
                    extensions=extensions,
                    payload_cwd=compact.cwd,
                )
                if compact is not None
                else None
            ),
        )

    def _build_runtime_observations(
        self,
        transcript: list[TranscriptRecord],
        effort_observations: list[RuntimeObservation],
    ) -> list[RuntimeObservation]:
        """Build runtime observations from runtime-kind transcript records,
        plus effort change-points collected during the transcript pass.
        """
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
        # Effort change-points are not in the transcript (the ``/effort`` command
        # and its ``<local-command-stdout>`` echo are user records, not runtime
        # records); they were collected during the transcript pass.
        runtime_observations.extend(effort_observations)
        return runtime_observations

    def _build_transcript(
        self,
        records: Iterable[tuple[dict, RecordSpan | None]],
        *,
        scan: _ClaudeRecordScan | None = None,
    ) -> tuple[list[TranscriptRecord], list[ClaudeTeamStateInput]]:
        """Extract only CT-useful transcript facts from Claude Code JSONL records."""
        transcript: list[TranscriptRecord] = []
        team_inputs: list[ClaudeTeamStateInput] = []
        # Small merged input/output dicts for team-management tools
        # (Agent/TaskCreate/TaskUpdate), keyed by tool_call_id.  Compact
        # sessions drop item bodies at translation; team-state reconstruction
        # re-reads only these bounded dicts instead of resident bodies.
        self._team_tool_calls: dict[str, dict[str, Any]] = {}

        for record, span in records:
            if scan is not None:
                scan.observe(record)
            before = len(transcript)
            self._translate_record(record, transcript, team_inputs)
            if span is not None:
                for entry in transcript[before:]:
                    entry.origin = span
        return transcript, team_inputs

    def _translate_record(
        self,
        record: dict,
        transcript: list[TranscriptRecord],
        team_inputs: list[ClaudeTeamStateInput],
    ) -> None:
        raw_type = record.get("type")

        # File-history-snapshot carries its timestamp inside snapshot.timestamp.
        if raw_type == "file-history-snapshot":
            snapshot = record.get("snapshot") or {}
            ts = parse_timestamp(snapshot.get("timestamp"))
            if ts is None:
                return
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
            return

        ts = parse_timestamp(record.get("timestamp"))
        if ts is None:
            return

        sid_str = record.get("sessionId")
        if not sid_str:
            return

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
                    result_call_id = block.get("tool_use_id") or block.get("toolUseID")
                    result_output = (
                        tool_use_result
                        if tool_use_result is not None
                        else block.get("content")
                    )
                    self._merge_team_tool_result(result_call_id, result_output)
                    transcript.append(
                        TranscriptRecord(
                            sequence=len(transcript),
                            timestamp=ts,
                            vendor=Vendor.CLAUDE_CODE,
                            role="tool",
                            kind="tool_result",
                            data={
                                **base,
                                "tool_call_id": block.get("tool_use_id")
                                or block.get("toolUseID"),
                                "output": tool_use_result
                                if tool_use_result is not None
                                else block.get("content"),
                                "source_tool_assistant_uuid": record.get(
                                    "sourceToolAssistantUUID"
                                ),
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
            self._handle_assistant_record(record, ts, transcript)

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
                preserved_uuids = (
                    preserved.get("allUuids") or preserved.get("uuids") or []
                )
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
                                    "trigger": _as_non_empty_str(
                                        compact_meta.get("trigger")
                                    ),
                                    "pre_tokens": _as_int_or_none(
                                        compact_meta.get("preTokens")
                                    ),
                                    "post_tokens": _as_int_or_none(
                                        compact_meta.get("postTokens")
                                    ),
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
                return
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
                            "message_count": _as_int_or_none(
                                record.get("messageCount")
                            ),
                            "pending_background_agent_count": _as_int_or_none(
                                record.get("pendingBackgroundAgentCount")
                            ),
                            "content": _as_non_empty_str(record.get("content")),
                        },
                    )
                )
            return

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
                        "attachment_type": record.get("attachmentType")
                        or record.get("subtype"),
                        "name": _as_non_empty_str(record.get("name")),
                        "path": _as_non_empty_str(record.get("path")),
                        "content": record.get("content"),
                    },
                )
            )
            return

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
            return

    def _merge_team_tool_result(self, tool_call_id: Any, output: Any) -> None:
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return
        entry = self._team_tool_calls.get(tool_call_id)
        if entry is not None and output is not None:
            entry["output"] = output

    def _compact_team_states(
        self,
        turns: list[Turn],
        team_inputs: list[ClaudeTeamStateInput],
    ) -> list[TeamTurnState | None]:
        """Rebuild team state for compact turns from captured team-tool dicts.

        ``build_turn_team_state`` reads merged item input/output bodies, which
        compact items no longer carry.  The captured per-call dicts are exactly
        the merged bodies for the three team-management tools, so transient
        full-fidelity proxy items reproduce the same state.
        """
        results: list[TeamTurnState | None] = []
        for turn, team_input in zip(turns, team_inputs, strict=False):
            pseudo_items: list[Item] = []
            for item in turn.items:
                if item.kind not in {
                    "tool_call",
                    "command_execution",
                    "file_change",
                    "plan",
                }:
                    continue
                call_id = getattr(item, "tool_call_id", None)
                entry = self._team_tool_calls.get(call_id) if call_id else None
                if entry is None:
                    continue
                pseudo_items.append(
                    ToolCallItem(
                        session_id=turn.session_id,
                        turn_id=turn.turn_id,
                        sequence=item.sequence,
                        started_at=item.started_at,
                        tool_name=entry.get("tool_name")
                        if isinstance(entry.get("tool_name"), str)
                        else None,
                        tool_call_id=call_id,
                        input=entry.get("input"),
                        output=entry.get("output"),
                    )
                )
            proxy = Turn(
                session_id=turn.session_id,
                sequence=turn.sequence,
                started_at=turn.started_at,
                items=pseudo_items,
            )
            results.append(build_turn_team_state(proxy, team_input=team_input))
        return results

    def _handle_assistant_record(
        self,
        record: dict,
        ts: datetime,
        transcript: list[TranscriptRecord],
    ) -> None:
        """Project a Claude Code ``assistant`` record into transcript facts.

        Promotes each thinking block to a first-class reasoning item, emits
        the assistant message (text + usage vendor_data), and one tool_call
        record per tool_use block.
        """
        message = record.get("message", {})
        content = message.get("content", [])
        stop_reason = message.get("stop_reason")
        usage = message.get("usage")
        base = _base_payload(record)
        tool_uses = _tool_use_blocks(content)
        text = _extract_text(content)
        normalized_metrics = normalize_claude_usage(
            model=message.get("model"), usage=usage
        )
        thinking_blocks = _extract_thinking(content)
        vendor_data = compact_dict(
            {
                **normalized_metrics,
                "provider_response_id": message.get("id"),
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
            if tool_name in _TEAM_TOOL_NAMES and isinstance(tool_id, str) and tool_id:
                self._team_tool_calls[tool_id] = {
                    "tool_name": tool_name,
                    "input": block.get("input"),
                    "output": None,
                }
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
