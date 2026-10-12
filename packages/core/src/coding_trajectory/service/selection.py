"""Deterministic local collection selection, without transport pagination."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def select_rows(
    rows: list[Any],
    *,
    limit: int | None,
    key: Callable[[Any], tuple],
    latest: bool = False,
) -> list[Any]:
    """Return canonical order, optionally selecting the first or latest rows."""
    ordered = sorted(rows, key=key)
    if limit is None:
        return ordered
    return ordered[-limit:] if latest else ordered[:limit]
