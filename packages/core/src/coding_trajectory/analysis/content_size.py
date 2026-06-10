"""Shared visible-content sizing for item and context analysis."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from coding_trajectory.ingestion.models import Item


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


def visible_text_size(text: str, *, reported_tokens: int | None = None) -> ContentSize:
    if reported_tokens is not None:
        return ContentSize(
            chars=len(text),
            tokens=max(reported_tokens, 0),
            confidence="observed_token_count",
        )
    if not text:
        return ContentSize(chars=0, tokens=0, confidence="no_visible_content")
    return ContentSize(
        chars=len(text),
        tokens=max(1, (len(text) + 3) // 4),
        confidence="visible_content_estimate",
    )


def item_input_text(item: Item) -> str:
    value = item.command if item.kind == "command_execution" else getattr(item, "input", None)
    return _stringify(value)


def item_output_text(item: Item) -> str:
    return _stringify(getattr(item, "output", None))


def item_input_size(item: Item) -> ContentSize:
    return visible_text_size(item_input_text(item))


def item_output_size(item: Item) -> ContentSize:
    text = item_output_text(item)
    return visible_text_size(text, reported_tokens=reported_token_count(text))


def reported_token_count(text: str) -> int | None:
    match = _ORIGINAL_TOKEN_COUNT.search(text)
    return int(match.group(1)) if match else None


def output_is_truncated(text: str) -> bool:
    return "chars → event.detail" in text or "tokens truncated" in text


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
