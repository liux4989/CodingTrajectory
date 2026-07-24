"""Common model-throughput calculations.

The processed-token numerator follows the canonical accounting contract. The
denominator removes observed tool intervals from the turn boundary, so the
result is a model-active rate rather than an end-to-end rate that includes
tool execution time.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from coding_trajectory.ingestion.models import Turn, is_tool_shaped_item


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
