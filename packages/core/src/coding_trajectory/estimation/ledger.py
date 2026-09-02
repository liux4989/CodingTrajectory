"""Durable append-only forecast and backfill job ledger.

The ledger is derived evidence, not canonical CT data. Successful model
outputs are append-only and immutable: bindings and comparisons are recorded
as later events, never edits. Read models (calibration tables, UI
projections) are rebuilt by folding the event log and may be deleted freely.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LEDGER_FORMAT_VERSION = "ct.estimation.ledger.v1"

_FORECAST_EVENTS = {"forecast_created", "forecast_bound", "forecast_compared"}


def default_ledger_directory() -> Path:
    override = os.environ.get("CT_ESTIMATION_DIR")
    directory = (
        Path(override).expanduser()
        if override
        else Path.home() / ".coding-trajectory" / "estimation"
    )
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


class ForecastLedger:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or default_ledger_directory()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._forecasts_path = self.directory / "forecasts.jsonl"
        self._jobs_path = self.directory / "jobs.jsonl"
        self._lock_path = self.directory / ".ledger.lock"
        self._lock = threading.RLock()
        self._transaction_depth = 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Serialize a ledger operation across threads and processes."""

        with self._lock:
            if self._transaction_depth:
                self._transaction_depth += 1
                try:
                    yield
                finally:
                    self._transaction_depth -= 1
                return

            with self._lock_path.open("a", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                self._transaction_depth = 1
                try:
                    yield
                finally:
                    self._transaction_depth = 0
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    # -- writes -------------------------------------------------------------

    def append_forecast(self, record: dict[str, Any]) -> None:
        self._append(self._forecasts_path, "forecast_created", record)

    def append_attempt_failure(self, record: dict[str, Any]) -> None:
        self._append(self._forecasts_path, "attempt_failed", record)

    def append_binding(self, prediction_id: str, receipt: dict[str, Any]) -> None:
        self._append(
            self._forecasts_path,
            "forecast_bound",
            {"prediction_id": prediction_id, **receipt},
        )

    def append_comparison(self, prediction_id: str, comparison: dict[str, Any]) -> None:
        self._append(
            self._forecasts_path,
            "forecast_compared",
            {"prediction_id": prediction_id, "comparison": comparison},
        )

    def append_job_event(self, event_type: str, payload: dict[str, Any]) -> None:
        self._append(self._jobs_path, event_type, payload)

    def _append(self, path: Path, event_type: str, payload: dict[str, Any]) -> None:
        event = {
            "format": LEDGER_FORMAT_VERSION,
            "type": event_type,
            "recorded_at": _utcnow(),
            "payload": payload,
        }
        line = json.dumps(event, separators=(",", ":"), default=str)
        with self.transaction(), path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    # -- reads --------------------------------------------------------------

    def forecasts(self) -> list[dict[str, Any]]:
        """Fold the event log into current forecast records.

        ``role`` and ``status`` are derived deterministically at fold time:
        the earliest forecast per (turn, estimator cohort) is the primary
        calibration observation; later repeats are diagnostic trials.
        """

        records: dict[str, dict[str, Any]] = {}
        for event in self._read_events(self._forecasts_path):
            event_type = event.get("type")
            payload = event.get("payload") or {}
            if event_type == "forecast_created":
                prediction_id = payload.get("prediction_id")
                if prediction_id:
                    records[prediction_id] = dict(payload)
            elif event_type == "forecast_bound":
                record = records.get(payload.get("prediction_id"))
                if record is not None:
                    record["turn_id"] = payload.get("turn_id", record.get("turn_id"))
                    record["root_session_id"] = payload.get(
                        "root_session_id", record.get("root_session_id")
                    )
                    record["session_id"] = payload.get(
                        "session_id", record.get("session_id")
                    )
                    record["bound_at"] = payload.get("bound_at")
                    record["binding_receipt"] = payload
            elif event_type == "forecast_compared":
                record = records.get(payload.get("prediction_id"))
                if record is not None:
                    record["comparison"] = payload.get("comparison")

        folded = list(records.values())
        for record in folded:
            record["status"] = _status_for(record)
        _assign_roles(folded)
        folded.sort(
            key=lambda item: (str(item.get("issued_at")), item["prediction_id"])
        )
        return folded

    def attempt_failures(self) -> list[dict[str, Any]]:
        return [
            event.get("payload") or {}
            for event in self._read_events(self._forecasts_path)
            if event.get("type") == "attempt_failed"
        ]

    def find_forecast(self, prediction_id: str) -> dict[str, Any] | None:
        for record in self.forecasts():
            if record.get("prediction_id") == prediction_id:
                return record
        return None

    def find_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        for record in self.forecasts():
            if record.get("idempotency_key") == key:
                return record
        return None

    def jobs(self) -> dict[str, dict[str, Any]]:
        """Fold job events into current per-job state."""

        jobs: dict[str, dict[str, Any]] = {}
        for event in self._read_events(self._jobs_path):
            event_type = event.get("type")
            payload = event.get("payload") or {}
            job_id = payload.get("job_id")
            if not job_id:
                continue
            if event_type == "job_created":
                jobs[job_id] = {
                    "job_id": job_id,
                    "status": "running",
                    "created_at": payload.get("created_at"),
                    "finished_at": None,
                    "spec": payload.get("spec") or {},
                    "counts": _empty_job_counts(),
                    "stop_reason": None,
                    "processed_turn_ids": [],
                }
                continue
            job = jobs.get(job_id)
            if job is None:
                continue
            if event_type == "candidate_processed":
                outcome = payload.get("outcome") or "unknown"
                counts = job["counts"]
                if outcome.startswith("excluded:"):
                    reason = outcome.split(":", 1)[1]
                    counts["excluded"][reason] = counts["excluded"].get(reason, 0) + 1
                elif outcome in {
                    "succeeded",
                    "skipped_existing",
                    "retryable_failed",
                    "permanent_failed",
                }:
                    counts["eligible"] += 1
                    counts[outcome] = counts.get(outcome, 0) + 1
                else:
                    counts[outcome] = counts.get(outcome, 0) + 1
                turn_id = payload.get("turn_id")
                if turn_id:
                    job["processed_turn_ids"].append(turn_id)
            elif event_type == "job_finished":
                job["status"] = payload.get("status") or "completed"
                job["finished_at"] = payload.get("finished_at")
                job["stop_reason"] = payload.get("stop_reason")
        for job in jobs.values():
            job["counts"]["processed"] = len(job["processed_turn_ids"])
        return jobs

    def find_job(self, job_id: str) -> dict[str, Any] | None:
        return self.jobs().get(job_id)

    def _read_events(self, path: Path) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        with self.transaction():
            if not path.exists():
                return []
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        event = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        events.append(event)
        return events


def _empty_job_counts() -> dict[str, Any]:
    return {
        "eligible": 0,
        "succeeded": 0,
        "skipped_existing": 0,
        "retryable_failed": 0,
        "permanent_failed": 0,
        "uncompared": 0,
        "excluded": {},
        "processed": 0,
    }


def _status_for(record: dict[str, Any]) -> str:
    comparison = record.get("comparison")
    if comparison is not None and comparison.get("exclusion") != "missing_terminal_time":
        return "compared"
    if record.get("forecast_kind") == "prospective_unbound" and not record.get(
        "bound_at"
    ):
        return "unbound"
    return "uncompared"


def _assign_roles(records: list[dict[str, Any]]) -> None:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for record in records:
        cohort = _role_group_key(record)
        groups.setdefault(cohort, []).append(record)
    for group in groups.values():
        group.sort(key=lambda item: (str(item.get("issued_at")), item["prediction_id"]))
        for index, record in enumerate(group):
            record["role"] = "primary" if index == 0 else "diagnostic"


def _role_group_key(record: dict[str, Any]) -> tuple[Any, ...]:
    estimator = record.get("estimator") or {}
    return (
        record.get("turn_id"),
        estimator.get("provider"),
        estimator.get("model"),
        estimator.get("effort"),
        estimator.get("prompt_version"),
        estimator.get("schema_version"),
    )
