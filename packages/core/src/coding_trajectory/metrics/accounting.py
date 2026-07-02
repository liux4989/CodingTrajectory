"""Shared helpers for canonical usage accounting."""

from __future__ import annotations

TOKEN_KEYS = (
    "prompt_tokens",
    "cached_prompt_tokens",
    "cache_write_tokens",
    "completion_tokens",
    "reasoning_tokens",
)


def usage_accounting_payload(usage: dict[str, int]) -> dict[str, int]:
    """Return usage with glossary names only."""
    prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    uncached_prompt_tokens = int(
        usage.get("uncached_prompt_tokens")
        or usage.get("uncached_input_tokens")
        or prompt_tokens
    )
    cached_prompt_tokens = int(
        usage.get("cached_prompt_tokens") or usage.get("cached_input_tokens") or 0
    )
    cache_write_tokens = int(
        usage.get("cache_write_tokens") or usage.get("cache_creation_input_tokens") or 0
    )
    completion_tokens = int(
        usage.get("completion_tokens") or usage.get("output_tokens") or 0
    )
    reasoning_tokens = int(
        usage.get("reasoning_tokens") or usage.get("reasoning_output_tokens") or 0
    )
    processed_tokens = int(usage.get("processed_tokens") or 0) or (
        uncached_prompt_tokens
        + cached_prompt_tokens
        + cache_write_tokens
        + completion_tokens
        + reasoning_tokens
    )
    result = {
        "prompt_tokens": prompt_tokens,
        "uncached_prompt_tokens": uncached_prompt_tokens,
        "cached_prompt_tokens": cached_prompt_tokens,
        "cache_write_tokens": cache_write_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "processed_tokens": processed_tokens,
        "prompt_completion_tokens": prompt_tokens + completion_tokens,
    }
    reported_total = usage.get("reported_total_tokens") or usage.get("total_tokens")
    if reported_total is not None:
        result["reported_total_tokens"] = int(reported_total)
    return result
