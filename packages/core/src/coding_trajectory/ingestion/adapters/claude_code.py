"""Claude Code adapter — reads ~/.claude/projects/**/*.jsonl and normalises to canonical models."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from coding_trajectory.ingestion.adapters._shared import (
    SHARED_FILE_TOOL_NAMES,
    HeaderFacts,
    ToolTaxonomy,
    content_block_field_texts,
    content_block_texts,
    content_blocks,
    int_or_none,
    non_empty_str,
    scan_header_records,
)
from coding_trajectory.ingestion.adapters.base import BaseAdapter, SessionHeader
from coding_trajectory.ingestion.adapters.claude_context import (
    _claude_context_usage,
    _first_api_prompt_text,
    _starting_context_sources,
)
from coding_trajectory.ingestion.adapters.claude_identity import (
    _ClaudeRecordScan,
    _record_title,
    _subagent_input,
    _subagent_input_from_scan,
)
from coding_trajectory.ingestion.assembly import AssemblyHooks, assemble_session
from coding_trajectory.ingestion.common import (
    compact_dict,
    infer_tool_success,
    parse_timestamp,
)
from coding_trajectory.ingestion.models import (
    RuntimeObservation,
    Session,
    ToolStatus,
    Turn,
    Vendor,
)
from coding_trajectory.ingestion.provenance import RecordSpan
from coding_trajectory.ingestion.retention import CanonicalRetention
from coding_trajectory.ingestion.transcript import TranscriptRecord
from coding_trajectory.ingestion.vendor_mechanisms.claude_subagent import (
    canonical_session_ids,
)
from coding_trajectory.ingestion.vendor_mechanisms.claude_subagent import (
    extensions as claude_extensions,
)
from coding_trajectory.ingestion.vendor_mechanisms.claude_team import (
    ClaudeTeamMessage,
    ClaudeTeamStateInput,
    build_turn_team_state,
    high_value_teammate_request,
)
from coding_trajectory.ingestion.vendor_mechanisms.usage_metrics import (
    normalize_claude_usage,
)

logger = logging.getLogger(__name__)

_TEAMMATE_MESSAGE_RE = re.compile(
    r"<teammate-message(?P<attrs>[^>]*)>(?P<body>.*?)</teammate-message>", re.DOTALL
)
_TEAMMATE_ATTR_RE = re.compile(r'(\w+)="(.*?)"')

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


_TEAM_TOOL_NAMES: frozenset[str] = frozenset({"Agent", "TaskCreate", "TaskUpdate"})


def _team_tool_calls_from_transcript(
    transcript: list[TranscriptRecord],
) -> dict[str, dict[str, Any]]:
    """Capture the merged input/output bodies of team-management tool calls.

    Derived from the pre-retention transcript (tool_call input + tool_result
    output, keyed by tool_call_id), replacing per-item body reads so compact
    sessions - whose item bodies were dropped at translation - rebuild the
    identical team state.
    """
    calls: dict[str, dict[str, Any]] = {}
    for record in transcript:
        tool_call_id = record.data.get("tool_call_id")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            continue
        if record.kind == "tool_call":
            tool_name = record.data.get("tool_name")
            if tool_name in _TEAM_TOOL_NAMES:
                calls[tool_call_id] = {
                    "tool_name": tool_name,
                    "input": record.data.get("input"),
                    "output": None,
                }
        elif record.kind == "tool_result":
            entry = calls.get(tool_call_id)
            output = record.data.get("output")
            if entry is not None and output is not None:
                entry["output"] = output
    return calls


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
        id_resolved = False

        def extract(record: dict) -> HeaderFacts:
            nonlocal id_resolved
            scanned.append(record)
            session_id: UUID | None = None
            cwd: str | None = None
            if not id_resolved:
                session_id_str = record.get("sessionId")
                if session_id_str:
                    try:
                        session_id = UUID(session_id_str)
                    except (ValueError, AttributeError):
                        session_id = None
                    else:
                        cwd = _as_non_empty_str(record.get("cwd"))
                        id_resolved = True
            return HeaderFacts(
                session_id=session_id, title=_record_title(record), cwd=cwd
            )

        facts = scan_header_records(
            self._iter_records(source),
            extract=extract,
            lookahead=self._TITLE_LOOKAHEAD,
        )
        if facts.session_id is None:
            return None
        mechanism = _subagent_input(source, scanned, facts.session_id)
        session_id, parent_session_id = canonical_session_ids(mechanism)
        return SessionHeader(
            session_id=session_id,
            vendor=Vendor.CLAUDE_CODE,
            parent_session_id=parent_session_id,
            title=mechanism.title,
            cwd=facts.cwd,
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
        # Pre-retention team-tool bodies: compact items drop input/output at
        # translation, so team state is rebuilt from these captured dicts
        # through the same ``build_turn_team_state`` code path in both modes.
        team_tool_calls = _team_tool_calls_from_transcript(transcript)

        def _attach_team_states(turns: list[Turn]) -> None:
            for turn, team_input in zip(turns, team_inputs, strict=False):
                turn.team_state = build_turn_team_state(
                    turn,
                    team_input=team_input,
                    team_tool_calls=team_tool_calls,
                )

        hooks = AssemblyHooks(
            extensions=extensions,
            parent_session_id=parent_session_id,
            runtime_observations=self._build_runtime_observations(
                transcript, scan.effort_observations
            ),
            session_fields={
                "agent_name": extensions.claude_code.agent_name
                if extensions and extensions.claude_code
                else None,
            },
            build_context_usage=_claude_context_usage,
            build_context_sources=lambda context: _starting_context_sources(
                started_at=context.started_at,
                context_usage=context.context_usage,
                first_prompt_text=_first_api_prompt_text(
                    turns=context.turns,
                    events=context.events,
                    context_usage=context.context_usage,
                ),
            ),
            decorate_turns=_attach_team_states,
            provenance_sink=lambda provenance: setattr(
                self, "last_provenance", provenance
            ),
        )
        return assemble_session(
            vendor=Vendor.CLAUDE_CODE,
            source=source,
            session_id=session_id,
            transcript=transcript,
            retention=retention,
            hooks=hooks,
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
