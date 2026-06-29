"""Shared helpers for canonical usage accounting."""

from __future__ import annotations

TOKEN_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def usage_accounting_payload(usage: dict[str, int]) -> dict[str, int]:
    """Add a normalized total to a usage payload."""
    total_tokens = int(usage.get("total_tokens") or 0)
    if total_tokens == 0:
        total_tokens = sum(int(usage.get(key) or 0) for key in TOKEN_KEYS)
    return {**usage, "total_tokens": total_tokens}
