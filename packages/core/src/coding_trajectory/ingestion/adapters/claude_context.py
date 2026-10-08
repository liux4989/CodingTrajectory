"""Claude Code usage/context evidence for session assembly hooks.

Owns the ``build_context_usage``/``build_context_sources`` evidence builders:
recorded usage observations, initial context attachments, and the estimated
starting-context remainder isolated from the first API call's full input.
Consumed only by ``claude_code.py`` assembly hooks; imports nothing from the
adapter module.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import ClassVar

from coding_trajectory.ingestion.adapters._shared import (
    content_block_texts,
    int_or_none,
)
from coding_trajectory.ingestion.common import parse_timestamp
from coding_trajectory.ingestion.models import (
    ContextSourceObservation,
    ContextUsageObservation,
    Event,
    Turn,
)
from coding_trajectory.ingestion.transcript import TranscriptRecord
from coding_trajectory.ingestion.vendor_mechanisms.usage_metrics import (
    context_usage_observation,
)
from coding_trajectory.token_counter import token_counter_for

_as_int_or_none = int_or_none


class _ClaudeStartingContextScan:
    """Capture initial request attachments before the first usage-bearing response.

    Read the raw records before retention drops bodies. A partial initial
    snapshot may omit tool schemas; the next matching snapshot in the same
    turn can recover non-deferred tools. Later injections are excluded.
    """

    _LABELS: ClassVar[dict[str, str]] = {
        "base_system": "Base instructions",
        "developer_instructions": "Developer instructions",
        "agents_md": "AGENTS.md",
        "memory": "Memory",
        "skills": "Skills",
        "mcp": "Tools / MCP",
        "system_tools": "System tools",
    }
    _CATEGORIES: ClassVar[dict[str, str]] = {
        "skill_listing": "skills",
        "mcp_instructions_delta": "mcp",
        "deferred_tools_delta": "mcp",
        "agent_listing_delta": "developer_instructions",
        "environment": "developer_instructions",
        "model": "developer_instructions",
        "auto_mode": "developer_instructions",
        "session_context": "developer_instructions",
        "date": "developer_instructions",
        "remote_session_change": "developer_instructions",
    }

    def __init__(self) -> None:
        self.sources: dict[tuple[str, str], ContextSourceObservation] = {}
        self.finished = False
        self._initial_prompt: str | None = None
        self._initial_snapshot_at: datetime | None = None
        self._deferred_names: set[str] = set()
        self._recover_tools = False
        self._first_response_id: str | None = None

    def _record(
        self,
        timestamp: datetime,
        key: str,
        identity: str,
        text: str,
        *,
        source: str = "claude_initial_attachment",
    ) -> None:
        if not text:
            return
        self.sources[(key, identity)] = ContextSourceObservation(
            timestamp=timestamp,
            key=key,
            label=self._LABELS[key],
            text=text,
            source=source,
        )

    def _record_tools(
        self, timestamp: datetime, tools: object, *, recovered: bool = False
    ) -> None:
        for tool in tools if isinstance(tools, list) else []:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                continue
            name = tool["name"]
            if recovered and (name in self._deferred_names or name.startswith("mcp__")):
                continue
            definition = tool.get("definition", tool)
            if not isinstance(definition, dict):
                continue
            # Snapshots call the API's input_schema field "schema". Count a
            # compact request definition, not arbitrary JSON display spacing.
            definition = dict(definition)
            if "schema" in definition:
                definition["input_schema"] = definition.pop("schema")
            self._record(
                timestamp,
                "mcp" if name.startswith("mcp__") else "system_tools",
                f"tool:{name}",
                json.dumps(
                    definition,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                source=(
                    "claude_matching_tool_snapshot"
                    if recovered
                    else "claude_initial_attachment"
                ),
            )

    def _recover_tool_snapshot(self, record: dict) -> None:
        if not self._recover_tools:
            return
        message = record.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if (
            (
                record.get("type") == "user"
                and not record.get("isMeta")
                and not (
                    isinstance(content, list)
                    and any(
                        isinstance(block, dict) and block.get("type") == "tool_result"
                        for block in content
                    )
                )
            )
            or (
                record.get("type") == "assistant"
                and isinstance(message, dict)
                and message.get("usage")
                and (
                    self._first_response_id is None
                    or message.get("id") != self._first_response_id
                )
            )
            or record.get("subtype") == "compact_boundary"
        ):
            self._recover_tools = False
            return
        attachment = record.get("attachment")
        if isinstance(attachment, dict) and attachment.get("type") in {
            "deferred_tools_delta",
            "instructions",
            "skill_listing",
            "mcp_instructions_delta",
            "agent_listing_delta",
            "model",
            "environment",
        }:
            self._recover_tools = False
            return
        if (
            not isinstance(attachment, dict)
            or attachment.get("type") != "prompt_snapshot"
        ):
            return
        if (
            json.dumps(attachment.get("systemPrompt"), sort_keys=True)
            != self._initial_prompt
        ):
            self._recover_tools = False
            return
        tools = attachment.get("tools")
        if isinstance(tools, list) and tools and self._initial_snapshot_at is not None:
            self._record_tools(self._initial_snapshot_at, tools, recovered=True)
            self._recover_tools = False

    def observe(self, record: dict) -> None:
        if self.finished:
            self._recover_tool_snapshot(record)
            return
        message = record.get("message")
        if (
            record.get("type") == "assistant"
            and isinstance(message, dict)
            and isinstance(message.get("usage"), dict)
            and message["usage"]
        ):
            self.finished = True
            self._first_response_id = message.get("id")
            return
        if record.get("type") != "attachment":
            return
        attachment = record.get("attachment")
        timestamp = parse_timestamp(record.get("timestamp"))
        if not isinstance(attachment, dict) or timestamp is None:
            return
        kind = attachment.get("type")
        if not isinstance(kind, str):
            return
        if kind == "instructions":
            files = attachment.get("files")
            for file in files if isinstance(files, list) else []:
                if not isinstance(file, dict):
                    continue
                path, text = file.get("path"), file.get("content")
                if not isinstance(path, str) or not isinstance(text, str):
                    continue
                key = (
                    "memory"
                    if file.get("type") == "AutoMem"
                    else "agents_md"
                    if path.rsplit("/", 1)[-1].lower() == "agents.md"
                    else "developer_instructions"
                )
                self._record(timestamp, key, path, text)
            return
        if kind == "prompt_snapshot":
            prompt = attachment.get("systemPrompt")
            self._initial_prompt = json.dumps(prompt, sort_keys=True)
            self._initial_snapshot_at = timestamp
            if isinstance(prompt, list):
                prompt = "\n\n".join(
                    block
                    for block in prompt
                    if isinstance(block, str)
                    and not block.startswith("__SYSTEM_PROMPT_")
                )
            if isinstance(prompt, str):
                self._record(timestamp, "base_system", kind, prompt)
            tools = attachment.get("tools")
            self._recover_tools = (
                isinstance(prompt, str)
                and bool(prompt)
                and (not isinstance(tools, list) or not tools)
            )
        else:
            if kind == "deferred_tools_delta":
                names = attachment.get("addedNames")
                if isinstance(names, list):
                    self._deferred_names.update(
                        name for name in names if isinstance(name, str)
                    )
            key = self._CATEGORIES.get(kind)
            if key is not None:
                rendered = record.get("rendered")
                text = (
                    "\n\n".join(
                        content_block_texts(block.get("content")) or ""
                        for block in rendered
                        if isinstance(block, dict)
                    )
                    if isinstance(rendered, list)
                    else ""
                )
                if not text:
                    text = (
                        content_block_texts(attachment.get("content"))
                        or content_block_texts(attachment.get("text"))
                        or ""
                    )
                if not text:
                    blocks = attachment.get("addedBlocks") or attachment.get(
                        "addedLines"
                    )
                    if isinstance(blocks, list):
                        text = "\n\n".join(
                            block for block in blocks if isinstance(block, str)
                        )
                self._record(timestamp, key, kind, text)
            # Tool schemas can be surfaced before the first prompt snapshot.
            tools = attachment.get("surfacedDefinitions")
        self._record_tools(timestamp, tools)


def _estimate_prompt_tokens(text: str | None) -> int:
    """Keep the legacy first-prompt char/4 estimate for aggregate compatibility.

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
    captured_sources: list[ContextSourceObservation] | None = None,
) -> list[ContextSourceObservation]:
    """Combine visible initial attachments with the first-input remainder.

    Older Claude Code logs omit injected context. Newer logs record initial
    attachments and prompt snapshots, which provide visible source estimates.
    Subtract those estimates from the aggregate instead of adding them on top.
    The first assistant turn's full input (``used_input_tokens``) is
    the stable system-prompt + tools prefix plus that turn's user message, so
    subtract the visible-text estimate of the first prompt to isolate the
    prefix. ``used_input_tokens`` is robust to a partially-warm cache: when
    only part of the system prompt was already cached, ``cache_read`` +
    ``cache_creation`` undercounts the prefix, but the full request total never
    does. The cached-prefix sum is used only as a fallback when the used-input
    total is unavailable.
    """
    sources = list(captured_sources or [])
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
        if sources:
            counter = token_counter_for(model=observation.model, provider="anthropic")
            estimate = max(
                estimate
                - sum(max(counter.count(source.text), 1) for source in sources),
                0,
            )
            if estimate == 0:
                return sources
        return sources + [
            ContextSourceObservation(
                timestamp=started_at,
                key="unattributed_context" if sources else "base_system",
                label="Unattributed context" if sources else "System prompt & tools",
                text="",
                source="claude_first_input_estimate",
                reported_tokens=estimate,
            )
        ]
    return sources


def _claude_context_usage(
    transcript: list[TranscriptRecord],
) -> list[ContextUsageObservation]:
    """Preserve every recorded usage block in transcript order.

    Repeated provider response IDs do not collapse stream observations. These
    are log-record totals, not a deduplicated API billing ledger: repeated
    usage blocks contribute repeatedly. Synthetic tool/reasoning records
    without usage do not contribute.
    """
    return [
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
