"""Normalize vendor usage payloads at the raw log boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

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


class NormalizedQuotaWindow(BaseModel):
    used_percent: float | None = None
    window_minutes: int | None = None
    resets_at: int | None = None


class NormalizedCreditsSnapshot(BaseModel):
    has_credits: bool | None = None
    unlimited: bool | None = None
    balance: str | None = None


class NormalizedSpendControlLimit(BaseModel):
    limit: str | None = None
    used: str | None = None
    remaining_percent: int | None = None


class NormalizedQuotaSnapshot(BaseModel):
    limit_id: str | None = None
    limit_name: str | None = None
    plan_type: str | None = None
    primary: NormalizedQuotaWindow | None = None
    secondary: NormalizedQuotaWindow | None = None
    credits: NormalizedCreditsSnapshot | None = None
    individual_limit: NormalizedSpendControlLimit | None = None
    rate_limit_reached_type: str | None = None


def normalize_codex_token_count(
    *,
    model: Any,
    info: Any,
    rate_limits: Any,
) -> dict[str, Any]:
    """Return normalized usage/quota facts from a Codex token_count event."""
    info_map = info if isinstance(info, dict) else {}
    metrics = NormalizedUsageMetrics(
        model=_as_str(info_map.get("model")) or _as_str(info_map.get("model_name")) or _as_str(model),
        last_token_usage=_dict_or_none(info_map.get("last_token_usage")),
        total_token_usage=_dict_or_none(info_map.get("total_token_usage")),
        model_context_window=_as_int_or_none(info_map.get("model_context_window")),
    )
    return compact_dict(
        {
            "metrics": metrics.model_dump(exclude_none=True),
            "quota": normalize_quota_snapshot(rate_limits),
        }
    )


def normalize_claude_usage(*, model: Any, usage: Any) -> dict[str, Any]:
    usage_map = usage if isinstance(usage, dict) else {}
    input_tokens = _as_int_or_none(usage_map.get("input_tokens")) or 0
    cache_read = _as_int_or_none(usage_map.get("cache_read_input_tokens")) or 0
    cache_creation = _as_int_or_none(usage_map.get("cache_creation_input_tokens")) or 0
    output_tokens = _as_int_or_none(usage_map.get("output_tokens")) or 0
    total = input_tokens + cache_read + cache_creation + output_tokens
    return _normalized_step_usage(
        model=model,
        usage={
            "input_tokens": input_tokens,
            "cached_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
            "output_tokens": output_tokens,
            "total_tokens": total,
        },
        cumulative_input_tokens=input_tokens + cache_read + cache_creation,
    )


def normalize_pi_usage(*, provider: Any = None, model: Any, usage: Any) -> dict[str, Any]:
    usage_map = usage if isinstance(usage, dict) else {}
    input_tokens = _as_int_or_none(usage_map.get("input")) or 0
    cache_read = _as_int_or_none(usage_map.get("cacheRead")) or 0
    cache_write = _as_int_or_none(usage_map.get("cacheWrite")) or 0
    output_tokens = _as_int_or_none(usage_map.get("output")) or 0
    return _normalized_step_usage(
        model=model,
        provider=provider,
        usage={
            "input_tokens": input_tokens,
            "cached_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write,
            "output_tokens": output_tokens,
            "total_tokens": _as_int_or_none(usage_map.get("totalTokens"))
            or (input_tokens + cache_read + cache_write + output_tokens),
            "cost_usd": _pi_cost_usd(usage_map.get("cost")),
        },
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
            ("cached_context", "Cached prefix (system + tools + prior turns)", "cached_input_tokens"),
            ("new_cached_prefix", "Newly cached this turn", "cache_creation_input_tokens"),
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
        quota=normalized.get("quota") if isinstance(normalized.get("quota"), dict) else None,
        categories=categories,
    )



def normalize_quota_snapshot(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    snapshot = NormalizedQuotaSnapshot(
        limit_id=_as_str(value.get("limit_id")),
        limit_name=_as_str(value.get("limit_name")),
        plan_type=_as_str(value.get("plan_type")),
        primary=_quota_window(value.get("primary")),
        secondary=_quota_window(value.get("secondary")),
        credits=_credits_snapshot(value.get("credits")),
        individual_limit=_spend_control_limit(value.get("individual_limit")),
        rate_limit_reached_type=_as_str(value.get("rate_limit_reached_type")),
    )
    dumped = snapshot.model_dump(exclude_none=True)
    return dumped or None


def _normalized_step_usage(
    *,
    model: Any,
    usage: Any,
    provider: Any = None,
    cumulative_input_tokens: int | None = None,
) -> dict[str, Any]:
    usage_map = usage if isinstance(usage, dict) else {}
    metrics = NormalizedUsageMetrics(
        model=_as_str(model) or _as_str(usage_map.get("model")),
        usage=usage_map or None,
        cumulative_input_tokens=cumulative_input_tokens,
    )
    dumped = metrics.model_dump(exclude_none=True)
    provider_value = _as_str(provider)
    if provider_value:
        dumped["provider"] = provider_value
    return {"metrics": dumped} if dumped else {}


def _quota_window(value: Any) -> NormalizedQuotaWindow | None:
    if not isinstance(value, dict):
        return None
    return NormalizedQuotaWindow(
        used_percent=_as_float_or_none(value.get("used_percent")),
        window_minutes=_as_int_or_none(value.get("window_minutes")),
        resets_at=_as_int_or_none(value.get("resets_at")),
    )


def _credits_snapshot(value: Any) -> NormalizedCreditsSnapshot | None:
    if not isinstance(value, dict):
        return None
    return NormalizedCreditsSnapshot(
        has_credits=_as_bool_or_none(value.get("has_credits")),
        unlimited=_as_bool_or_none(value.get("unlimited")),
        balance=_as_str(value.get("balance")),
    )


def _spend_control_limit(value: Any) -> NormalizedSpendControlLimit | None:
    if not isinstance(value, dict):
        return None
    return NormalizedSpendControlLimit(
        limit=_as_str(value.get("limit")),
        used=_as_str(value.get("used")),
        remaining_percent=_as_int_or_none(value.get("remaining_percent")),
    )


def _dict_or_none(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) and value else None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _as_int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_float_or_none(value: Any) -> float | None:
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return float(value)
    return None


def _as_bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _pi_cost_usd(value: Any) -> float | None:
    if isinstance(value, dict):
        total = value.get("total")
        return _as_float_or_none(total)
    return _as_float_or_none(value)
