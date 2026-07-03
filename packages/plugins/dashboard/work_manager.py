"""Shared caching and request coalescing for ordinary dashboard work."""

from __future__ import annotations

import threading
import time
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


class DashboardWorkManager:
    """Caches dashboard projections and deduplicates concurrent cache misses.

    This is for ordinary dashboard read-model work that should complete during
    the request. Long-running agent/session-analysis operations should still use
    the explicit async job runner.
    """

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl_seconds = ttl_seconds
        self._entries: dict[tuple[Any, ...], CacheEntry] = {}
        self._in_flight: dict[tuple[Any, ...], _InFlightWork] = {}
        self._lock = threading.Lock()

    def get_or_compute(
        self, key: tuple[Any, ...], factory: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        while True:
            now = time.monotonic()
            with self._lock:
                entry = self._entries.get(key)
                if entry and now - entry.created_at < self._ttl_seconds:
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
            if work.error is not None:
                raise work.error
            if work.result is not None:
                return work.result

    def clear_prefix(self, prefix: tuple[Any, ...]) -> None:
        with self._lock:
            for key in list(self._entries):
                if key[: len(prefix)] == prefix:
                    del self._entries[key]

    def clear_all(self) -> None:
        with self._lock:
            self._entries.clear()

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
                self._in_flight.pop(key, None)
                work.event.set()
            raise
        with self._lock:
            self._entries[key] = CacheEntry(time.monotonic(), value)
            work.result = value
            self._in_flight.pop(key, None)
            work.event.set()
        return value
