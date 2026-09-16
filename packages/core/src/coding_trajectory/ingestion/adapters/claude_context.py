"""Claude Code usage/context evidence for session assembly hooks.

Owns the ``build_context_usage``/``build_context_sources`` evidence builders:
usage observations deduplicated by provider response id, and the synthetic
starting-context source isolated from the first API call's full input.
Consumed only by ``claude_code.py`` assembly hooks; imports nothing from the
adapter module.
"""

from __future__ import annotations

from datetime import datetime

from coding_trajectory.ingestion.adapters._shared import int_or_none
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

_as_int_or_none = int_or_none


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


def _claude_context_usage(
    transcript: list[TranscriptRecord],
) -> list[ContextUsageObservation]:
    """Build usage observations, deduplicated by provider response id.

    A Claude Code ``uuid`` identifies one local stream event, whereas
    ``message.id`` identifies the provider response.  One response is recorded
    as several stream events (thinking, text, tool-use, final state), each
    repeating the same final usage block.  Preserve every event in the
    transcript, but retain usage once per provider response so billed
    accounting does not charge the same request repeatedly.
    """
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

    return [
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
