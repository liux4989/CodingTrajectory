"""Normalize vendor usage payloads at the raw log boundary."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from coding_trajectory.ingestion.adapters._shared import int_or_none
from coding_trajectory.ingestion.common import compact_dict
from coding_trajectory.ingestion.models import (
    ContextCategoryObservation,
    ContextUsageObservation,
)


class NormalizedUsageMetrics(BaseModel):
    model: str | None = None
    usage: dict[str, Any] | None = None
    last_token_usage: dict[str, Any] | None = None
    total_token_usage: dict[str, Any] | None = None
    model_context_window: int | None = None
    cumulative_input_tokens: int | None = None


class UsageSchema(StrEnum):
    """Which token-counting convention a vendor's raw usage payload follows.

    The schema owns the ``uncached_input_tokens`` derivation rule:

    - ``OPENAI_TOTAL``: ``input_tokens`` is the TOTAL prompt (cached +
      uncached), so the uncached subset is derived as ``input - cached``.
      Without the derivation, the ``or input_tokens`` fallback in
      ``usage_accounting_payload`` would label the whole prompt as uncached,
      overstating cache-break re-reads.
    - ``ANTHROPIC_UNCACHED``: ``input_tokens`` is already the uncached portion
      of the individual provider call, so it is mirrored directly. Preserving
      it per call lets a multi-call turn charge a cache break to its first
      call rather than to the turn-wide input sum (and lets per-turn re-read
      evidence such as ``cache_break_re_read_tokens`` populate instead of
      staying ``None``).
    """

    OPENAI_TOTAL = "openai_total"
    ANTHROPIC_UNCACHED = "anthropic_uncached"

    def derive_uncached_input_tokens(
        self,
        *,
        input_tokens: int | None,
        cached_input_tokens: int | None,
    ) -> int | None:
        """Return the ``uncached_input_tokens`` value for this schema."""
        if self is UsageSchema.OPENAI_TOTAL:
            if input_tokens is None or cached_input_tokens is None:
                return None
            return max(input_tokens - cached_input_tokens, 0)
        return input_tokens

    def apply(self, usage: dict[str, Any] | None) -> dict[str, Any] | None:
        """Return ``usage`` with ``uncached_input_tokens`` set per the schema.

        Non-dict payloads and payloads that already carry an uncached value
        pass through untouched, as do OpenAI-schema dicts lacking either side
        of the derivation.
        """
        if not isinstance(usage, dict):
            return usage
        if usage.get("uncached_input_tokens") is not None:
            return usage
        uncached = self.derive_uncached_input_tokens(
            input_tokens=_as_int_or_none(usage.get("input_tokens")),
            cached_input_tokens=_as_int_or_none(usage.get("cached_input_tokens")),
        )
        if uncached is None:
            return usage
        enriched = dict(usage)
        enriched["uncached_input_tokens"] = uncached
        return enriched


def normalize_codex_token_count(
    *,
    model: Any,
    info: Any,
) -> dict[str, Any]:
    """Return normalized usage facts from a Codex token_count event."""
    info_map = info if isinstance(info, dict) else {}
    return _normalized_usage_metrics(
        model=_as_str(info_map.get("model"))
        or _as_str(info_map.get("model_name"))
        or _as_str(model),
        usage_schema=UsageSchema.OPENAI_TOTAL,
        last_token_usage=info_map.get("last_token_usage"),
        total_token_usage=info_map.get("total_token_usage"),
        model_context_window=info_map.get("model_context_window"),
    )


def normalize_claude_usage(*, model: Any, usage: Any) -> dict[str, Any]:
    usage_map = usage if isinstance(usage, dict) else {}
    input_tokens = _as_int_or_none(usage_map.get("input_tokens")) or 0
    cache_read = _as_int_or_none(usage_map.get("cache_read_input_tokens")) or 0
    cache_creation = _as_int_or_none(usage_map.get("cache_creation_input_tokens")) or 0
    output_tokens = _as_int_or_none(usage_map.get("output_tokens")) or 0
    total = input_tokens + cache_read + cache_creation + output_tokens

    cache_creation_breakdown = _dict_or_none(usage_map.get("cache_creation")) or {}
    server_tool_use = _dict_or_none(usage_map.get("server_tool_use")) or {}

    usage_payload = compact_dict(
        {
            "input_tokens": input_tokens,
            "uncached_input_tokens": UsageSchema.ANTHROPIC_UNCACHED.derive_uncached_input_tokens(
                input_tokens=input_tokens,
                cached_input_tokens=None,
            ),
            "cached_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
            "cache_creation_1h_input_tokens": _as_int_or_none(
                cache_creation_breakdown.get("ephemeral_1h_input_tokens")
            ),
            "cache_creation_5m_input_tokens": _as_int_or_none(
                cache_creation_breakdown.get("ephemeral_5m_input_tokens")
            ),
            "output_tokens": output_tokens,
            "total_tokens": total,
            "server_tool_use": server_tool_use if server_tool_use else None,
            "service_tier": _as_str(usage_map.get("service_tier")),
            "speed": _as_str(usage_map.get("speed")),
            "inference_geo": _as_str(usage_map.get("inference_geo")),
            "iterations": usage_map.get("iterations")
            if isinstance(usage_map.get("iterations"), list)
            else None,
        }
    )
    return _normalized_usage_metrics(
        model=model,
        usage_schema=UsageSchema.ANTHROPIC_UNCACHED,
        usage=usage_payload,
        cumulative_input_tokens=input_tokens + cache_read + cache_creation,
    )


def normalize_pi_usage(
    *, provider: Any = None, model: Any, usage: Any
) -> dict[str, Any]:
    usage_map = usage if isinstance(usage, dict) else {}
    input_tokens = _as_int_or_none(usage_map.get("input")) or 0
    cache_read = _as_int_or_none(usage_map.get("cacheRead")) or 0
    cache_write = _as_int_or_none(usage_map.get("cacheWrite")) or 0
    output_tokens = _as_int_or_none(usage_map.get("output")) or 0
    normalized_usage = {
        "input_tokens": input_tokens,
        "uncached_input_tokens": UsageSchema.ANTHROPIC_UNCACHED.derive_uncached_input_tokens(
            input_tokens=input_tokens,
            cached_input_tokens=None,
        ),
        "cached_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
        "output_tokens": output_tokens,
        "total_tokens": _as_int_or_none(usage_map.get("totalTokens"))
        or (input_tokens + cache_read + cache_write + output_tokens),
    }
    # Pi's jsonl logs report the real per-call USD cost (``cost.total``); carry
    # it through so downstream consumers prefer it over the pricing SoT's
    # estimate. Vendors that don't report cost stay ``None`` (estimated).
    if (cost_usd := _pi_cost_usd(usage_map.get("cost"))) is not None:
        normalized_usage["cost_usd"] = cost_usd
    return _normalized_usage_metrics(
        model=model,
        provider=provider,
        usage_schema=UsageSchema.ANTHROPIC_UNCACHED,
        usage=normalized_usage,
        cumulative_input_tokens=input_tokens + cache_read + cache_write,
    )


def context_usage_observation(
    *,
    timestamp: datetime,
    source: str,
    normalized: dict[str, Any],
    source_event_id: UUID | None = None,
    provider: str | None = None,
    category_source: str | None = None,
) -> ContextUsageObservation | None:
    metrics = normalized.get("metrics")
    if not isinstance(metrics, dict):
        return None

    usage = metrics.get("usage")
    if not isinstance(usage, dict):
        usage = metrics.get("last_token_usage")
    usage = usage if isinstance(usage, dict) else {}
    used_input_tokens = _as_int_or_none(metrics.get("cumulative_input_tokens"))
    if used_input_tokens is None:
        used_input_tokens = _as_int_or_none(usage.get("input_tokens")) or 0

    categories: list[ContextCategoryObservation] = []
    if category_source is not None:
        category_specs = (
            (
                "cached_context",
                "Cached prefix (system + tools + prior turns)",
                "cached_input_tokens",
            ),
            (
                "new_cached_prefix",
                "Newly cached this turn",
                "cache_creation_input_tokens",
            ),
            ("messages", "Messages (uncached input)", "input_tokens"),
        )
        categories = [
            ContextCategoryObservation(
                key=key,
                label=label,
                tokens=tokens,
                confidence="exact_usage",
                source=category_source,
            )
            for key, label, usage_key in category_specs
            if (tokens := _as_int_or_none(usage.get(usage_key)) or 0) > 0
        ]

    if used_input_tokens == 0 and _is_zero_usage(usage):
        return None

    return ContextUsageObservation(
        source_event_id=source_event_id,
        timestamp=timestamp,
        source=source,
        model=_as_str(metrics.get("model")),
        provider=_as_str(metrics.get("provider")) or provider,
        context_window_tokens=_as_int_or_none(metrics.get("model_context_window")),
        used_input_tokens=used_input_tokens,
        usage=usage,
        cumulative_usage=(
            metrics.get("total_token_usage")
            if isinstance(metrics.get("total_token_usage"), dict)
            else None
        ),
        categories=categories,
    )


def _is_zero_usage(usage: dict[str, Any]) -> bool:
    return not any(_as_int_or_none(value) for value in usage.values())


def _normalized_usage_metrics(
    *,
    model: Any,
    usage_schema: UsageSchema,
    usage: Any = None,
    provider: Any = None,
    cumulative_input_tokens: int | None = None,
    last_token_usage: Any = None,
    total_token_usage: Any = None,
    model_context_window: Any = None,
) -> dict[str, Any]:
    """Build the ``{"metrics": {...}}`` payload for one vendor entry point.

    ``usage_schema`` derives ``uncached_input_tokens`` for the OpenAI-schema
    ``last_token_usage``/``total_token_usage`` dicts; per-call ``usage``
    payloads derive it at construction time to preserve key order.
    """
    usage_map = usage if isinstance(usage, dict) else {}
    metrics = NormalizedUsageMetrics(
        model=_as_str(model) or _as_str(usage_map.get("model")),
        usage=usage_map or None,
        last_token_usage=usage_schema.apply(_dict_or_none(last_token_usage)),
        total_token_usage=usage_schema.apply(_dict_or_none(total_token_usage)),
        model_context_window=_as_int_or_none(model_context_window),
        cumulative_input_tokens=cumulative_input_tokens,
    )
    dumped = metrics.model_dump(exclude_none=True)
    provider_value = _as_str(provider)
    if provider_value:
        dumped["provider"] = provider_value
    return {"metrics": dumped}


def _dict_or_none(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) and value else None


def _as_str(value: Any) -> str | None:
    # Deliberately non-stripping, unlike the shared adapters' ``non_empty_str``:
    # usage values pass through verbatim.
    return value if isinstance(value, str) and value else None


_as_int_or_none = int_or_none


def _pi_cost_usd(value: Any) -> float | None:
    """Pi reports per-call USD cost as ``{"input":…, "output":…, "total":…}``;
    ``total`` is the billable amount for the call."""
    if not isinstance(value, dict):
        return None
    total = value.get("total")
    if isinstance(total, int | float) and not isinstance(total, bool):
        return float(total)
    return None
