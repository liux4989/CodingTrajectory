"""Common model-throughput calculations.

The processed-token numerator follows the canonical accounting contract. The
denominator removes observed tool intervals from the turn boundary, so the
result is a model-active rate rather than an end-to-end rate that includes
tool execution time.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
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
    """Tokens generated over an interval that excludes time to first token."""

    tokens: int
    seconds: float


def model_active_seconds(turn: Turn) -> float | None:
    """Return turn time outside completed, observed tool intervals.

    This is a derived wall-clock denominator. Provider logs do not generally
    expose exact decoder-busy time, so a turn with an unclosed tool interval is
    deliberately ineligible instead of silently assigning that interval to
    the model.
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
    """Return a complete model-active denominator when every turn is known."""
    values = [model_active_seconds(turn) for turn in turns]
    if not values or any(value is None for value in values):
        return None
    return round(sum(value for value in values if value is not None), 3)


def processed_tokens_per_second(
    processed_tokens: int,
    active_seconds: float | None,
) -> float | None:
    """Return processed tokens per model-active second."""
    if processed_tokens <= 0 or active_seconds is None or active_seconds <= 0:
        return None
    return round(processed_tokens / active_seconds, 3)


def output_tokens_per_second(
    output_tokens: int,
    active_seconds: float | None,
) -> float | None:
    """Return generated tokens per model-active second.

    Reasoning tokens are part of ``output_tokens`` for every supported provider,
    so this is the model's generation rate. It is still an end-to-end rate over
    the model-active window: time to first token and prefill are included.
    """
    return processed_tokens_per_second(output_tokens, active_seconds)


def estimated_output_tokens(turn: Turn, vendor: Vendor) -> int | None:
    """Captured Amp generation content, never provider-reported consumption."""
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
    """Weight complete live-observed turns by time, not by their individual rates.

    A partial selection is not silently presented as whole-session throughput.
    Hook time minus the union of tool windows still includes latency, prefill,
    plugin overhead, and other non-tool waiting; this is not decoder-busy time.
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
    """Return first-token-free generation samples for one turn.

    Codex timestamps each response item when it completes, so the gap between a
    completed message (or earlier parallel call) and the tool call that follows
    it in the same response is pure decoding of that call's arguments. Time to
    first token and prefill fall before the first item and are never included.
    A tool completion between the two items means a new request began, so the
    pair is not a single response and is skipped. Other providers do not log
    item boundaries with these semantics and yield no samples.
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
    for previous, current in zip(calls, calls[1:]):
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
    """Return the aggregate decode rate when enough samples back it."""
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
