"""Shared visible-content sizing for item and context analysis."""

from __future__ import annotations

import contextlib
import contextvars
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

from coding_trajectory.ingestion.models import Item
from coding_trajectory.token_counter import get_current_counter

ContentSizeConfidence = Literal[
    "observed_token_count",
    "visible_content_estimate",
    "no_visible_content",
]

_ORIGINAL_TOKEN_COUNT = re.compile(r"Original token count: (\d+)")


@dataclass(frozen=True)
class ContentSize:
    chars: int
    tokens: int
    confidence: ContentSizeConfidence


_current_size_cache: contextvars.ContextVar[
    dict[tuple[str, str, int | None], ContentSize] | None
] = contextvars.ContextVar("coding_trajectory_content_size_cache", default=None)


@contextlib.contextmanager
def scoped_content_size_cache() -> Iterator[None]:
    """Reuse exact visible-content sizes within one projection request.

    Stats and tool-usage projections intentionally inspect the same canonical
    item more than once (allocation, composition, and presentation). Real BPE
    tokenization is substantially more expensive than the surrounding scans,
    so retain those immutable results only for the duration of the outer
    request. Nested scopes share the existing cache and the cache key includes
    the effective tokenizer name and reported-token override.
    """

    existing = _current_size_cache.get()
    if existing is not None:
        yield
        return
    token = _current_size_cache.set({})
    try:
        yield
    finally:
        _current_size_cache.reset(token)


def visible_text_size(text: str, *, reported_tokens: int | None = None) -> ContentSize:
    cache = _current_size_cache.get()
    counter_name = ""
    counter = None
    if reported_tokens is None and text:
        counter = get_current_counter()
        counter_name = counter.name
    key = (counter_name, text, reported_tokens)
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            return cached
    if reported_tokens is not None:
        result = ContentSize(
            chars=len(text),
            tokens=max(reported_tokens, 0),
            confidence="observed_token_count",
        )
    elif not text:
        result = ContentSize(chars=0, tokens=0, confidence="no_visible_content")
    else:
        assert counter is not None
        result = ContentSize(
            chars=len(text),
            tokens=max(counter.count(text), 1),
            confidence="visible_content_estimate",
        )
    if cache is not None:
        cache[key] = result
    return result


def item_input_text(item: Item) -> str:
    value = (
        item.command
        if item.kind == "command_execution"
        else getattr(item, "input", None)
    )
    return _stringify(value)


def item_output_text(item: Item) -> str:
    return _stringify(getattr(item, "output", None))


def item_input_size(item: Item) -> ContentSize:
    measurements = getattr(item, "measurements", None)
    if measurements is not None:
        return _measured_size(measurements.input_chars, measurements.input_tokens)
    return visible_text_size(item_input_text(item))


def item_output_size(item: Item) -> ContentSize:
    measurements = getattr(item, "measurements", None)
    if measurements is not None:
        return _measured_size(measurements.output_chars, measurements.output_tokens)
    text = item_output_text(item)
    # Size the actual resident text. Do NOT honor "Original token count: N"
    # here: Codex emits that marker only when it truncates the output, and N is
    # the PRE-truncation count, so honoring it overcounts (~6M tokens across
    # 81 Codex sessions). In Claude Code the marker is content coincidence, not
    # a real count. `reported_token_count` is still used for the separate
    # `output_original_tokens` stat in analysis.py.
    return visible_text_size(text)


def item_text_size(item: Item) -> ContentSize:
    """Visible size of an agent/reasoning item's text (or its measurement)."""
    measurements = getattr(item, "measurements", None)
    if measurements is not None:
        return _measured_size(measurements.text_chars, measurements.text_tokens)
    return visible_text_size(getattr(item, "text", None) or "")


def _measured_size(chars: int, tokens: int) -> ContentSize:
    return ContentSize(
        chars=chars,
        tokens=tokens,
        confidence="visible_content_estimate" if chars else "no_visible_content",
    )


def reported_token_count(text: str) -> int | None:
    match = _ORIGINAL_TOKEN_COUNT.search(text)
    return int(match.group(1)) if match else None


def output_is_truncated(text: str) -> bool:
    return (
        "chars → session.events" in text
        or "chars → session.items" in text
        or "chars → event.detail" in text
        or "tokens truncated" in text
    )


def tool_input_summary(value: Any) -> str | None:
    """Bounded one-line summary of a tool input (command, path, pattern...)."""
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("cmd", "command", "path", "pattern", "query"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return compact_text(candidate)
    return compact_text(str(value))


def compact_text(value: str, *, limit: int = 240) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            pass
    return str(value)
