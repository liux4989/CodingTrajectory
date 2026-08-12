"""Shared caching and request coalescing for ordinary dashboard work."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True, slots=True)
class CacheEntry:
    created_at: float
    value: dict[str, Any]


@dataclass(slots=True)
class _InFlightWork:
    event: threading.Event
    result: dict[str, Any] | None = None
    error: BaseException | None = None
    invalidated: bool = False
    background: bool = False


class _WorkInvalidated(Exception):
    """Internal control flow for a foreground load invalidated while running."""


class DashboardWorkManager:
    """Caches dashboard projections and deduplicates concurrent cache misses.

    This is for ordinary dashboard read-model work that should complete during
    the request. Long-running agent/session-analysis operations should still use
    the explicit async job runner.
    """

    def __init__(
        self,
        ttl_seconds: float,
        *,
        stale_ttl_seconds: float | None = None,
        retry_seconds: float = 5,
        max_entries: int = 256,
        refresh_workers: int = 2,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._stale_ttl_seconds = (
            ttl_seconds if stale_ttl_seconds is None else stale_ttl_seconds
        )
        self._retry_seconds = retry_seconds
        self._max_entries = max_entries
        self._entries: dict[tuple[Any, ...], CacheEntry] = {}
        self._in_flight: dict[tuple[Any, ...], _InFlightWork] = {}
        self._retry_after: dict[tuple[Any, ...], float] = {}
        self._metrics = {
            "lookups_total": 0,
            "fresh_hits_total": 0,
            "stale_serves_total": 0,
            "misses_total": 0,
            "loads_started_total": 0,
            "coalesced_waits_total": 0,
            "load_failed_total": 0,
            "refresh_started_total": 0,
            "refresh_succeeded_total": 0,
            "refresh_failed_total": 0,
            "refresh_invalidated_total": 0,
            "invalidated_retries_total": 0,
            "invalidations_total": 0,
            "evictions_total": 0,
        }
        self._lock = threading.Lock()
        self._closed = False
        self._refresh_pool = ThreadPoolExecutor(
            max_workers=refresh_workers,
            thread_name_prefix="dashboard-refresh",
        )

    def get_or_compute(
        self,
        key: tuple[Any, ...],
        factory: Callable[[], dict[str, Any]],
        *,
        ttl_seconds: float | None = None,
        stale_ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        while True:
            now = time.monotonic()
            ttl = self._ttl_seconds if ttl_seconds is None else ttl_seconds
            stale_ttl = (
                self._stale_ttl_seconds
                if stale_ttl_seconds is None
                else stale_ttl_seconds
            )
            stale_ttl = max(ttl, stale_ttl)
            with self._lock:
                if self._closed:
                    raise RuntimeError("dashboard work manager is shut down")
                self._metrics["lookups_total"] += 1
                entry = self._entries.get(key)
                if entry:
                    age = now - entry.created_at
                    if age < ttl:
                        self._metrics["fresh_hits_total"] += 1
                        return entry.value
                    if age < stale_ttl:
                        self._metrics["stale_serves_total"] += 1
                        self._start_background_refresh(key, factory, now)
                        return entry.value
                work = self._in_flight.get(key)
                if work is None:
                    work = _InFlightWork(event=threading.Event())
                    self._in_flight[key] = work
                    self._metrics["misses_total"] += 1
                    self._metrics["loads_started_total"] += 1
                    owner = True
                else:
                    self._metrics["misses_total"] += 1
                    self._metrics["coalesced_waits_total"] += 1
                    owner = False
            if owner:
                try:
                    return self._compute(key, factory, work)
                except _WorkInvalidated:
                    continue
            work.event.wait()
            if work.invalidated:
                with self._lock:
                    self._metrics["invalidated_retries_total"] += 1
                continue
            if work.error is not None:
                raise work.error
            if work.result is not None:
                return work.result

    def clear_prefix(self, prefix: tuple[Any, ...]) -> None:
        with self._lock:
            self._metrics["invalidations_total"] += 1
            for key in list(self._entries):
                if key[: len(prefix)] == prefix:
                    del self._entries[key]
            for key in list(self._retry_after):
                if key[: len(prefix)] == prefix:
                    del self._retry_after[key]
            for key, work in list(self._in_flight.items()):
                if key[: len(prefix)] == prefix:
                    work.invalidated = True
                    work.event.set()
                    del self._in_flight[key]

    def clear_all(self) -> None:
        with self._lock:
            self._metrics["invalidations_total"] += 1
            self._entries.clear()
            self._retry_after.clear()
            for work in self._in_flight.values():
                work.invalidated = True
                work.event.set()
            self._in_flight.clear()

    def shutdown(self, *, wait: bool = False) -> None:
        with self._lock:
            self._closed = True
            self._entries.clear()
            self._retry_after.clear()
            for work in self._in_flight.values():
                work.invalidated = True
                work.event.set()
            self._in_flight.clear()
        self._refresh_pool.shutdown(wait=wait, cancel_futures=True)

    def metrics(self) -> dict[str, int]:
        """Return aggregate operation counters and registry-size gauges.

        ``invalidations_total`` counts clear operations. ``in_flight`` counts
        coalescible registry entries; an invalidated factory may still finish
        after it has been removed from that registry.
        """
        with self._lock:
            return {
                **self._metrics,
                "entries": len(self._entries),
                "in_flight": len(self._in_flight),
                "retry_backoffs": len(self._retry_after),
            }

    def _start_background_refresh(
        self,
        key: tuple[Any, ...],
        factory: Callable[[], dict[str, Any]],
        now: float,
    ) -> None:
        if key in self._in_flight or now < self._retry_after.get(key, 0):
            return
        work = _InFlightWork(event=threading.Event(), background=True)
        self._in_flight[key] = work
        try:
            self._refresh_pool.submit(self._refresh, key, factory, work)
        except RuntimeError:
            work.invalidated = True
            work.event.set()
            if self._in_flight.get(key) is work:
                del self._in_flight[key]
        else:
            self._metrics["refresh_started_total"] += 1

    def _refresh(
        self,
        key: tuple[Any, ...],
        factory: Callable[[], dict[str, Any]],
        work: _InFlightWork,
    ) -> None:
        try:
            self._compute(key, factory, work)
        except BaseException:
            # Stale-while-revalidate keeps serving the last successful value.
            # The retry deadline prevents a failing source from spawning work
            # on every request while still allowing a later recovery.
            return

    def _compute(
        self,
        key: tuple[Any, ...],
        factory: Callable[[], dict[str, Any]],
        work: _InFlightWork,
    ) -> dict[str, Any]:
        try:
            value = factory()
        except BaseException as exc:
            with self._lock:
                invalidated = work.invalidated
                if not invalidated:
                    work.error = exc
                if work.background and not invalidated:
                    self._retry_after[key] = time.monotonic() + self._retry_seconds
                    self._metrics["refresh_failed_total"] += 1
                elif not work.background and not invalidated:
                    self._metrics["load_failed_total"] += 1
                if self._in_flight.get(key) is work:
                    del self._in_flight[key]
                work.event.set()
                if invalidated and not work.background:
                    self._metrics["invalidated_retries_total"] += 1
            if invalidated:
                if work.background:
                    with self._lock:
                        self._metrics["refresh_invalidated_total"] += 1
                raise _WorkInvalidated
            raise
        with self._lock:
            invalidated = work.invalidated
            if not invalidated:
                self._entries[key] = CacheEntry(time.monotonic(), value)
                self._retry_after.pop(key, None)
                self._evict_oldest()
                work.result = value
                if work.background:
                    self._metrics["refresh_succeeded_total"] += 1
            elif not work.background:
                self._metrics["invalidated_retries_total"] += 1
            if self._in_flight.get(key) is work:
                del self._in_flight[key]
            work.event.set()
        if invalidated:
            if work.background:
                with self._lock:
                    self._metrics["refresh_invalidated_total"] += 1
            raise _WorkInvalidated
        return value

    def _evict_oldest(self) -> None:
        while len(self._entries) > self._max_entries:
            oldest = min(
                self._entries,
                key=lambda key: self._entries[key].created_at,
            )
            del self._entries[oldest]
            self._retry_after.pop(oldest, None)
            self._metrics["evictions_total"] += 1
