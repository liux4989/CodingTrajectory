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
    """Add compatibility totals and glossary aliases to a usage payload."""
    total_tokens = int(usage.get("total_tokens") or 0)
    input_tokens = int(usage.get("input_tokens") or 0)
    uncached_prompt_tokens = int(usage.get("uncached_input_tokens") or input_tokens)
    cached_prompt_tokens = int(usage.get("cached_input_tokens") or 0)
    cache_write_tokens = int(usage.get("cache_creation_input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    reasoning_tokens = int(usage.get("reasoning_output_tokens") or 0)
    processed_tokens = int(usage.get("processed_tokens") or 0) or (
        uncached_prompt_tokens
        + cached_prompt_tokens
        + cache_write_tokens
        + output_tokens
        + reasoning_tokens
    )
    if total_tokens == 0:
        total_tokens = processed_tokens
    return {
        **usage,
        "total_tokens": total_tokens,
        "prompt_tokens": input_tokens,
        "cached_prompt_tokens": cached_prompt_tokens,
        "cache_write_tokens": cache_write_tokens,
        "completion_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "processed_tokens": processed_tokens,
        "prompt_completion_tokens": input_tokens + output_tokens,
        "fresh_io_tokens": input_tokens + output_tokens,
    }
