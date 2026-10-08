"""Common processing and output speed calculations.

Processing speed uses the processed token total. Model time is the recorded
turn duration minus tool runs, with overlapping tool runs counted once.
See docs/token-usage-glossary.md for the common terms and measurement limits.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from itertools import pairwise
from typing import NamedTuple

from coding_trajectory.analysis.content_size import (
    item_input_size,
    item_text_size,
    item_thinking_tokens,
)
from coding_trajectory.ingestion.models import (
    Turn,
    TurnStatus,
    Vendor,
    is_tool_shaped_item,
)
from coding_trajectory.metrics.models import TurnMetrics

# A decode sample shorter than either bound is dominated by log-write jitter.
_MIN_DECODE_SAMPLE_TOKENS = 20
_MIN_DECODE_SAMPLE_SECONDS = 0.5
# Fewer samples than this is not enough evidence for a stable rate.
MIN_DECODE_SAMPLES = 3


class DecodeSample(NamedTuple):
    """Estimated tool-argument tokens and time after the first response item."""

    tokens: int
    seconds: float


def model_active_seconds(turn: Turn) -> float | None:
    """Return model time: recorded turn duration minus completed tool runs.

    This includes prompt processing and waiting for the first token. Provider
    logs usually do not reveal the exact time spent generating tokens. A turn
    with no recorded tool end cannot be used for this measurement.
    """
    if turn.started_at is None or turn.ended_at is None:
        return None

    turn_start = turn.started_at
    turn_end = turn.ended_at
    if turn_end < turn_start:
        return None

    intervals: list[tuple[datetime, datetime]] = []
    for item in turn.items:
        if not is_tool_shaped_item(item):
            continue
        if item.completed_at is None:
            return None
        start = max(item.started_at, turn_start)
        end = min(item.completed_at, turn_end)
        if end > start:
            intervals.append((start, end))

    tool_seconds = 0.0
    for start, end in _merge_intervals(intervals):
        tool_seconds += (end - start).total_seconds()
    total_seconds = (turn_end - turn_start).total_seconds()
    return round(max(total_seconds - tool_seconds, 0.0), 3)


def aggregate_model_active_seconds(turns: Iterable[Turn]) -> float | None:
    """Add up model time only when every selected turn has recorded timing."""
    values = [model_active_seconds(turn) for turn in turns]
    if not values or any(value is None for value in values):
        return None
    return round(sum(value for value in values if value is not None), 3)


def processed_tokens_per_second(
    processed_tokens: int,
    active_seconds: float | None,
) -> float | None:
    """Return processing speed: processed tokens divided by model time."""
    if processed_tokens <= 0 or active_seconds is None or active_seconds <= 0:
        return None
    return round(processed_tokens / active_seconds, 3)


def output_tokens_per_second(
    output_tokens: int,
    active_seconds: float | None,
) -> float | None:
    """Return output speed: recorded output tokens divided by model time.

    Codex includes reasoning in output. Separately reported reasoning is not
    added here. Model time includes prompt processing and waiting for the
    first output token.
    """
    return processed_tokens_per_second(output_tokens, active_seconds)


def estimated_output_tokens(turn: Turn, vendor: Vendor) -> int | None:
    """Estimate Amp output from saved text, thinking, and tool arguments."""
    if (
        vendor != Vendor.AMP
        or turn.status != TurnStatus.COMPLETED
        or turn.timing_source != "live_hooks"
    ):
        return None
    return sum(
        item_input_size(item).tokens
        if is_tool_shaped_item(item)
        else item_text_size(item).tokens + item_thinking_tokens(item)
        for item in turn.items
    )


def estimated_output_tokens_per_second(turns: Iterable[TurnMetrics]) -> float | None:
    """Estimate Amp output speed from total tokens divided by total model time.

    Every selected turn needs complete live timing. Model time still includes
    prompt processing, waiting for the first token, plugin work, and other
    waiting outside tool runs. It is not time spent generating tokens alone.
    """
    selected = list(turns)
    if not selected or any(
        turn.estimated_output_tokens is None
        or turn.model_active_seconds is None
        or turn.model_active_seconds <= 0
        for turn in selected
    ):
        return None
    return processed_tokens_per_second(
        sum(turn.estimated_output_tokens for turn in selected),
        sum(turn.model_active_seconds for turn in selected),
    )


def decode_samples(turn: Turn, vendor: Vendor) -> list[DecodeSample]:
    """Collect Codex tool-argument generation speed samples for one turn.

    Codex records when each response item completes. The gap from a completed
    message or earlier tool call to the next tool call in the same response
    estimates time spent generating that call's arguments. Prompt processing
    and waiting for the first token happen before the first item. If a tool
    finishes between the two items, another request began and the pair is
    skipped. Other providers lack these timestamps and yield no samples.
    """
    if vendor != Vendor.CODEX_CLI:
        return []
    calls = [
        item
        for item in turn.items
        if item.kind in {"agent_message", "tool_call"}
    ]
    completions = [
        item.completed_at
        for item in calls
        if item.kind == "tool_call" and item.completed_at is not None
    ]
    samples: list[DecodeSample] = []
    for previous, current in pairwise(calls):
        if current.kind != "tool_call":
            continue
        # A tool that finished before ``current`` started ends the response.
        if any(previous.started_at < done <= current.started_at for done in completions):
            continue
        seconds = (current.started_at - previous.started_at).total_seconds()
        tokens = item_input_size(current).tokens
        if tokens < _MIN_DECODE_SAMPLE_TOKENS or seconds < _MIN_DECODE_SAMPLE_SECONDS:
            continue
        samples.append(DecodeSample(tokens, seconds))
    return samples


def decode_tokens_per_second(
    tokens: int,
    seconds: float,
    samples: int,
) -> float | None:
    """Estimate tool-argument generation speed when enough samples qualify."""
    if samples < MIN_DECODE_SAMPLES or tokens <= 0 or seconds <= 0:
        return None
    return round(tokens / seconds, 3)


def _merge_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged: list[tuple[datetime, datetime]] = [ordered[0]]
    for start, end in ordered[1:]:
        previous_start, previous_end = merged[-1]
        if start <= previous_end:
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged
