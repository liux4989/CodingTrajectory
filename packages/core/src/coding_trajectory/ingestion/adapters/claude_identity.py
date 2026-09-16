"""Claude Code identity scanning: session id, title, and subagent linkage.

Owns the single-pass record scan (`_ClaudeRecordScan`) that collects identity
facts while the transcript streams by, and the subagent-mechanism input that
resolves parentage from the file layout and sidecar meta. Consumed by
``claude_code.py`` for ``scan_header`` and session assembly; imports nothing
from the adapter module.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import UUID

from coding_trajectory.ingestion.adapters._shared import (
    content_block_texts,
    int_or_none,
    non_empty_str,
)
from coding_trajectory.ingestion.common import parse_timestamp
from coding_trajectory.ingestion.models import RuntimeObservation
from coding_trajectory.ingestion.vendor_mechanisms.claude_subagent import (
    ClaudeSubagentInput,
)

_as_non_empty_str = non_empty_str
_as_int_or_none = int_or_none
_extract_text = content_block_texts

# Claude Code logs an ``/effort`` switch as a ``<local-command-stdout>Set effort
# level to <LEVEL> ...`` user record. The level word (``max``, ``ultracode``,
# ``high`` ...) is the resolved effort in effect from that turn onward.
_CLAUDE_EFFORT_STDOUT_RE = re.compile(r"Set effort level to (\w+)")


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
