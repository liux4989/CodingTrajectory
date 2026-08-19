"""Shared numeric helpers for dashboard projection modules."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def safe_div(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return round(float(numerator) / float(denominator), 8)


def percentile(ordered_values: list[float], percentile: float) -> float:
    """Interpolate one percentile (0..1 scale) over pre-sorted values."""

    if not ordered_values:
        return 0.0
    if len(ordered_values) == 1:
        return round(ordered_values[0], 8)
    position = (len(ordered_values) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered_values) - 1)
    if lower == upper:
        return round(ordered_values[lower], 8)
    weight = position - lower
    return round(
        ordered_values[lower] * (1 - weight) + ordered_values[upper] * weight,
        8,
    )


def parse_datetime(value: Any, *, tz: Any = None) -> datetime | None:
    """Parse an ISO-8601 timestamp; naive values are assumed UTC.

    Aware values keep their own offset unless ``tz`` is given, in which case
    they are converted with ``astimezone``.
    """

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if tz is None:
        return parsed
    return parsed.astimezone(tz)
