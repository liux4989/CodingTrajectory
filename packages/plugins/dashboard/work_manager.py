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
                entry = self._entries.get(key)
                if entry:
                    age = now - entry.created_at
                    if age < ttl:
                        return entry.value
                    if age < stale_ttl:
                        self._start_background_refresh(key, factory, now)
                        return entry.value
                work = self._in_flight.get(key)
                if work is None:
                    work = _InFlightWork(event=threading.Event())
                    self._in_flight[key] = work
                    owner = True
                else:
                    owner = False
            if owner:
                return self._compute(key, factory, work)
            work.event.wait()
            if work.invalidated:
                continue
            if work.error is not None:
                raise work.error
            if work.result is not None:
                return work.result

    def clear_prefix(self, prefix: tuple[Any, ...]) -> None:
        with self._lock:
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
                work.error = exc
                if work.background and not work.invalidated:
                    self._retry_after[key] = time.monotonic() + self._retry_seconds
                if self._in_flight.get(key) is work:
                    del self._in_flight[key]
                work.event.set()
            raise
        with self._lock:
            if not work.invalidated:
                self._entries[key] = CacheEntry(time.monotonic(), value)
                self._retry_after.pop(key, None)
                self._evict_oldest()
            work.result = value
            if self._in_flight.get(key) is work:
                del self._in_flight[key]
            work.event.set()
        return value

    def _evict_oldest(self) -> None:
        while len(self._entries) > self._max_entries:
            oldest = min(
                self._entries,
                key=lambda key: self._entries[key].created_at,
            )
            del self._entries[oldest]
            self._retry_after.pop(oldest, None)
