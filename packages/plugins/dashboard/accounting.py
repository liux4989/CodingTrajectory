"""Dashboard-owned cost accounting helpers.

Moved from core's metrics layer so the dashboard can compute reported costs
and build usage payloads without re-implementing the logic.
"""

from __future__ import annotations

from typing import Any, Protocol


class _CostLike(Protocol):
    amount_usd: float
    complete: bool


_TOKEN_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def reported_cost_amount(cost: _CostLike) -> float | None:
    """Return the USD amount only when the cost estimate is complete."""
    return cost.amount_usd if cost.complete else None


def usage_accounting_payload(
    usage: dict[str, int], *, cost_usd: float | None
) -> dict[str, int | float]:
    """Augment a usage dict with ``total_tokens`` and an optional ``cost_usd``."""
    total_tokens = int(usage.get("total_tokens") or 0)
    if total_tokens == 0:
        total_tokens = sum(int(usage.get(key) or 0) for key in _TOKEN_KEYS)
    payload: dict[str, Any] = {**usage, "total_tokens": total_tokens}
    if cost_usd is not None:
        payload["cost_usd"] = cost_usd
    return payload
