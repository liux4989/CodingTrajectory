"""Shared token-usage arithmetic for the metrics layer."""

from __future__ import annotations

from collections.abc import Iterable


def sum_usage_dicts(items: Iterable[dict[str, int]]) -> dict[str, int]:
    """Sum usage mappings key-wise, clamping negatives and dropping zeros."""
    total: dict[str, int] = {}
    for item in items:
        for key, value in item.items():
            total[key] = total.get(key, 0) + max(value, 0)
    return {key: value for key, value in total.items() if value > 0}
