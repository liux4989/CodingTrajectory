"""Long-lived datahub runtime facade over revisioned SQLite read models."""

from __future__ import annotations

import queue
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from datahub_plugin.projections.read_models import DEFAULT_RECENT_HORIZON_DAYS
from datahub_plugin.runtime.evidence import RuntimeEvidenceMixin
from datahub_plugin.runtime.materialize import _default_database_path
from datahub_plugin.runtime.monitor import (
    DEFAULT_REFRESH_SECONDS,
    PARSER_VERSION,
    READ_MODEL_SCHEMA_VERSION,
    RETAINED_CHANGE_REVISIONS,
    RuntimeMonitorMixin,
)
from datahub_plugin.runtime.read_api import RuntimeReadApiMixin, RuntimeSnapshot
from datahub_plugin.store.core import IncrementalStore


class DatahubIncrementalRuntime(
    RuntimeReadApiMixin, RuntimeEvidenceMixin, RuntimeMonitorMixin
):
    """Own one persistent store and one coalesced reconciliation worker."""

    def __init__(
        self,
        *,
        current_dir: Path,
        database_path: Path | None = None,
        since_days: int = DEFAULT_RECENT_HORIZON_DAYS,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
        autostart: bool = True,
    ) -> None:
        if since_days < 1:
            raise ValueError("since_days must be at least 1")
        self.current_dir = current_dir.resolve()
        self.since_days = since_days
        self.refresh_seconds = max(1.0, refresh_seconds)
        self._uses_default_database = database_path is None
        self._subscriber_lock = threading.Lock()
        self._subscribers: set[queue.Queue[int]] = set()
        resolved_database_path = (database_path or _default_database_path()).resolve()
        self.store = IncrementalStore(
            resolved_database_path,
            parser_version=PARSER_VERSION,
            schema_version=READ_MODEL_SCHEMA_VERSION,
            retained_change_revisions=RETAINED_CHANGE_REVISIONS,
            retain_source_messages=False,
            post_commit=self._notify_revision,
        )
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="datahub-ingest"
        )
        self._future: Future[dict[str, Any]] | None = None
        self._lock = threading.Lock()
        self._evidence_lock = threading.Lock()
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._last_scan_started_at: str | None = None
        self._last_scan_finished_at: str | None = None
        self._last_error: str | None = None
        self._last_result: dict[str, Any] | None = None
        if autostart:
            self.request_refresh(force_bootstrap=self.store.current_revision() == 0)
            self._monitor = threading.Thread(
                target=self._monitor_loop,
                name="datahub-reconcile",
                daemon=True,
            )
            self._monitor.start()


__all__ = ["DatahubIncrementalRuntime", "RuntimeSnapshot"]
