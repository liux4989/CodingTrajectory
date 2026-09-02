"""Shared helpers for canonical usage accounting."""

from __future__ import annotations

TOKEN_KEYS = (
    "prompt_tokens",
    "cached_prompt_tokens",
    "cache_write_tokens",
    "completion_tokens",
    "reasoning_tokens",
)


def glossary_usage_dict(
    *,
    input_tokens: int,
    cached_input_tokens: int,
    cache_creation_input_tokens: int,
    output_tokens: int,
    reasoning_output_tokens: int,
    processed_tokens: int,
    uncached_input_tokens: int | None = None,
    drop_nonpositive: bool = False,
) -> dict[str, int]:
    """Map canonical token fields to the public glossary names.

    Single source for the canonical→glossary correspondence
    (input→prompt, uncached_input→uncached_prompt, cached_input→cached_prompt,
    cache_creation_input→cache_write, output→completion,
    reasoning_output→reasoning, processed→processed, plus the derived
    prompt_completion sum). ``uncached_prompt_tokens`` is omitted when
    ``uncached_input_tokens`` is ``None``; with ``drop_nonpositive`` only
    positive entries survive.
    """
    payload: dict[str, int] = {"prompt_tokens": input_tokens}
    if uncached_input_tokens is not None:
        payload["uncached_prompt_tokens"] = uncached_input_tokens
    payload.update(
        {
            "cached_prompt_tokens": cached_input_tokens,
            "cache_write_tokens": cache_creation_input_tokens,
            "completion_tokens": output_tokens,
            "reasoning_tokens": reasoning_output_tokens,
            "processed_tokens": processed_tokens,
            "prompt_completion_tokens": input_tokens + output_tokens,
        }
    )
    if drop_nonpositive:
        payload = {key: value for key, value in payload.items() if value > 0}
    return payload


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
    result = glossary_usage_dict(
        input_tokens=prompt_tokens,
        uncached_input_tokens=uncached_prompt_tokens,
        cached_input_tokens=cached_prompt_tokens,
        cache_creation_input_tokens=cache_write_tokens,
        output_tokens=completion_tokens,
        reasoning_output_tokens=reasoning_tokens,
        processed_tokens=processed_tokens,
    )
    reported_total = usage.get("reported_total_tokens") or usage.get("total_tokens")
    if reported_total is not None:
        result["reported_total_tokens"] = int(reported_total)
    return result
