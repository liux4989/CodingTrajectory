"""Deterministic joins of forecasts to observed actual durations."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from coding_trajectory.estimation.task import TaskExclusion, candidate_for_turn
from coding_trajectory.ingestion.common import canonical_json
from coding_trajectory.query import DocumentStore

# Stable, declared wall-clock bins. Versioned with the calibration response;
# buckets are outcome diagnostics, not task-difficulty labels.
DURATION_BUCKETS: list[tuple[str, float | None]] = [
    ("under_5m", 5.0),
    ("5_to_20m", 20.0),
    ("20_to_60m", 60.0),
    ("1_to_3h", 180.0),
    ("over_3h", None),
]

OUTCOME_UNKNOWN = "unknown"


def duration_bucket(minutes: float) -> str:
    for label, upper in DURATION_BUCKETS:
        if upper is None or minutes < upper:
            return label
    return DURATION_BUCKETS[-1][0]


def join_actual(
    store: DocumentStore,
    *,
    turn_id: UUID,
    source_paths: list[Path] | None = None,
) -> dict[str, Any]:
    """Join one bound forecast to the turn's observed wall-clock duration.

    Excluded records keep a counted reason instead of a duration; CT does not
    infer a duration from model prose or partial evidence.
    """

    compared_at = datetime.now(UTC).isoformat()
    fingerprint = source_fingerprint(source_paths or [])

    candidate = candidate_for_turn(store, turn_id)
    if isinstance(candidate, TaskExclusion):
        return {
            "compared_at": compared_at,
            "actual_execution_seconds": None,
            "duration_bucket": None,
            "outcome": OUTCOME_UNKNOWN,
            "exclusion": candidate.reason,
            "source_fingerprint": fingerprint,
        }

    turn = candidate.turn
    exclusion: str | None = None
    actual_seconds: int | None = None
    if turn.status.value == "interrupted":
        exclusion = "interrupted"
    elif turn.ended_at is None:
        exclusion = "missing_terminal_time"
    else:
        seconds = round((turn.ended_at - turn.started_at).total_seconds())
        if seconds <= 0:
            exclusion = "zero_duration"
        else:
            actual_seconds = seconds

    return {
        "compared_at": compared_at,
        "actual_execution_seconds": actual_seconds,
        "duration_bucket": (
            duration_bucket(actual_seconds / 60.0)
            if actual_seconds is not None
            else None
        ),
        "outcome": OUTCOME_UNKNOWN,
        "exclusion": exclusion,
        "source_fingerprint": fingerprint,
    }


def source_fingerprint(paths: list[Path]) -> str | None:
    """Fingerprint the canonical sources behind an actual join (path+stat)."""

    entries: list[str] = []
    for path in sorted(paths, key=lambda item: str(item)):
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append(f"{path}:{stat.st_size}:{stat.st_mtime_ns}")
    if not entries:
        return None
    return hashlib.sha256(canonical_json(entries).encode("utf-8")).hexdigest()
