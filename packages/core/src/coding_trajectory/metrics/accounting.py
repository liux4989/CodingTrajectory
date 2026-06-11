"""Shared helpers for canonical usage and reported-cost accounting."""

from __future__ import annotations

from typing import Any, Protocol


class CostLike(Protocol):
    amount_usd: float
    complete: bool


TOKEN_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def reported_cost_amount(cost: CostLike) -> float | None:
    """Return the USD amount only when the source-reported cost is complete."""
    return cost.amount_usd if cost.complete else None


def usage_accounting_payload(
    usage: dict[str, int], *, cost_usd: float | None
) -> dict[str, int | float]:
    """Add a normalized total and optional reported cost to a usage payload."""
    total_tokens = int(usage.get("total_tokens") or 0)
    if total_tokens == 0:
        total_tokens = sum(int(usage.get(key) or 0) for key in TOKEN_KEYS)
    payload: dict[str, Any] = {**usage, "total_tokens": total_tokens}
    if cost_usd is not None:
        payload["cost_usd"] = cost_usd
    return payload
