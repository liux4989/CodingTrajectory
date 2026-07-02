"""Async job tracking for long-running dashboard operations.

The dashboard server is a synchronous ``BaseHTTPRequestHandler``, but some
operations (agent tasks, session analysis) drive external coding agents that
can take minutes. Rather than holding the HTTP request open, those operations
are submitted as background jobs: the POST enqueues the job and returns a
``job_id`` (HTTP 202), and the client polls ``GET /api/jobs/<job_id>`` until the
status becomes ``ready`` or ``error``.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict

JobStatus = Literal["pending", "running", "ready", "error"]

# Jobs are evicted once they are older than this (regardless of status).
_JOB_TTL_SECONDS = 3600.0
_MAX_WORKERS = 4


class JobRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    status: JobStatus
    created_at: str
    updated_at: str
    progress: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    def public(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class JobStore:
    """Thread-safe in-memory job registry."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.Lock()

    def create(self, kind: str) -> JobRecord:
        now = _now()
        record = JobRecord(
            id=uuid.uuid4().hex,
            kind=kind,
            status="pending",
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._evict_expired_locked()
            self._jobs[record.id] = record
        return record

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(
        self,
        job_id: str,
        *,
        status: JobStatus | None = None,
        progress: str | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> JobRecord | None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return None
            if status is not None:
                record = record.model_copy(update={"status": status})
            if progress is not None:
                record = record.model_copy(update={"progress": progress})
            if result is not None:
                record = record.model_copy(update={"result": result})
            if error is not None:
                record = record.model_copy(update={"error": error})
            record = record.model_copy(update={"updated_at": _now()})
            self._jobs[job_id] = record
            return record

    def _evict_expired_locked(self) -> None:
        cutoff = _now_dt()
        expired = [
            job_id
            for job_id, record in self._jobs.items()
            if (cutoff - _parse_dt(record.created_at)).total_seconds()
            > _JOB_TTL_SECONDS
        ]
        for job_id in expired:
            del self._jobs[job_id]


class JobRunner:
    """Runs jobs on a thread pool and records their outcome in a ``JobStore``."""

    def __init__(self, store: JobStore, *, max_workers: int = _MAX_WORKERS) -> None:
        self._store = store
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="dashboard-job"
        )

    def submit(
        self,
        kind: str,
        fn: Callable[..., dict[str, Any]],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> str:
        record = self._store.create(kind)
        future = self._executor.submit(self._run, record.id, fn, *args, **kwargs)
        future.add_done_callback(lambda _f: None)
        return record.id

    def _run(
        self,
        job_id: str,
        fn: Callable[..., dict[str, Any]],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self._store.update(job_id, status="running")
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - surface any failure to the client
            self._store.update(job_id, status="error", error=str(exc))
            return
        if hasattr(result, "model_dump"):
            result = result.model_dump(mode="json")
        elif not isinstance(result, dict):
            result = {"value": result}
        self._store.update(job_id, status="ready", result=result)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return _now_dt()
