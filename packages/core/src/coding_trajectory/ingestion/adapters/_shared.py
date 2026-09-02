"""Shared adapter toolkit (module-private to the adapters layer).

Single implementations of small coercion, UUID, and content-block helpers
that were previously duplicated across vendor adapters. Each adapter keeps
its historical private names as thin aliases over these, so behavior is
unchanged by construction.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

_UUID_TEXT_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def non_empty_str(value: Any) -> str | None:
    """Return ``value.strip()`` when it is a non-empty string, else ``None``."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def int_or_none(value: Any) -> int | None:
    """Return ``value`` when it is an int (excluding bool), else ``None``."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def parse_uuid_text(value: str | None) -> UUID | None:
    """Strict UUID parse: accept raw or ``T-``-prefixed text, else ``None``."""
    if not value:
        return None
    for candidate in (value, value.removeprefix("T-")):
        try:
            return UUID(candidate)
        except ValueError:
            continue
    return None


def extract_uuid_text(value: Any) -> str | None:
    """Lenient UUID extraction returning text, not a ``UUID``.

    Tries the raw value and its ``T-``-stripped form, then regex-extracts an
    embedded UUID, and finally falls back to the raw non-empty text.
    """
    raw = non_empty_str(value)
    if raw is None:
        return None
    for candidate in (raw, raw.removeprefix("T-")):
        try:
            UUID(candidate)
            return candidate
        except ValueError:
            continue
    match = _UUID_TEXT_RE.search(raw)
    return match.group(0) if match else raw


def content_blocks(content: Any, block_type: str) -> list[dict]:
    """Return the dict blocks whose ``type`` equals ``block_type``."""
    if not isinstance(content, list):
        return []
    return [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == block_type
    ]


def content_block_texts(
    content: Any, *, text_type: str = "text", text_key: str = "text"
) -> str | None:
    """Join the ``text_key`` payloads of ``text_type`` blocks.

    String content passes through (empty becomes ``None``); block texts are
    joined with a single space and stripped, empty becomes ``None``.
    """
    if content is None:
        return None
    if isinstance(content, str):
        return content or None
    texts = [
        block.get(text_key, "")
        for block in content
        if isinstance(block, dict) and block.get("type") == text_type
    ]
    joined = " ".join(text for text in texts if text).strip()
    return joined or None


def content_block_field_texts(content: Any, block_type: str, field: str) -> list[str]:
    """Return the non-empty ``field`` texts of ``block_type`` blocks."""
    if not isinstance(content, list):
        return []
    return [
        block.get(field, "")
        for block in content
        if isinstance(block, dict)
        and block.get("type") == block_type
        and block.get(field)
    ]


def collapse_whitespace(text: str) -> str:
    """Collapse every run of whitespace into a single space."""
    return " ".join(text.split())


def preview_text(value: Any, *, max_len: int = 96) -> str | None:
    """Whitespace-collapsed preview truncated to ``max_len`` with an ellipsis."""
    text = non_empty_str(value)
    if text is None:
        return None
    text = collapse_whitespace(text)
    if len(text) <= max_len:
        return text
    return f"{text[: max_len - 3].rstrip()}..."


@dataclass
class HeaderFacts:
    """Header facts accumulated by ``scan_header_records``."""

    session_id: UUID | None = None
    title: str | None = None
    cwd: str | None = None


def scan_header_records(
    records: Iterable[dict],
    *,
    extract: Callable[[dict], HeaderFacts | None],
    lookahead: int,
) -> HeaderFacts:
    """Bounded header scan shared by the vendor adapters.

    Merges each record's extracted facts into the running header (first
    non-None session id and title win; a reported cwd replaces the previous
    one). Stops once both id and title are found, or ``lookahead`` records
    after the record that yielded the id.
    """
    facts = HeaderFacts()
    since_id = 0
    for record in records:
        update = extract(record)
        if update is not None:
            if facts.session_id is None and update.session_id is not None:
                facts.session_id = update.session_id
            if facts.title is None and update.title is not None:
                facts.title = update.title
            if update.cwd is not None:
                facts.cwd = update.cwd
        if facts.session_id is not None and facts.title is not None:
            break
        if facts.session_id is not None:
            since_id += 1
            if since_id >= lookahead:
                break
    return facts


# Tool names shared by more than one vendor taxonomy. Per-vendor taxonomies
# extend these with vendor-specific extras; each vendor's effective set is
# unchanged from its pre-extraction definition.
SHARED_FILE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "Read",
        "Edit",
        "MultiEdit",
        "Write",
        "View",
    }
)
SHARED_PLAN_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "TodoWrite",
        "TodoRead",
        "update_plan",
    }
)


@dataclass(frozen=True)
class ToolTaxonomy:
    """Classify tool names into canonical item kinds.

    The check order (command, plan, file change) matches every vendor
    classifier this replaces; each vendor's name sets are mutually exclusive
    across kinds, so the fixed order never changes an outcome.
    """

    command_names: frozenset[str] = frozenset()
    plan_names: frozenset[str] = frozenset()
    file_change_names: frozenset[str] = frozenset()

    def classify(self, tool_name: str | None) -> str:
        if tool_name in self.command_names:
            return "command_execution"
        if tool_name in self.plan_names:
            return "plan"
        if tool_name in self.file_change_names:
            return "file_change"
        return "tool_call"
