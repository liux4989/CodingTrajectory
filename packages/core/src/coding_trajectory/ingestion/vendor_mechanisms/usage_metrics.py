"""Normalize vendor usage payloads at the raw log boundary."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from coding_trajectory.ingestion.common import compact_dict


class NormalizedUsageMetrics(BaseModel):
    model: str | None = None
    usage: dict[str, Any] | None = None
    last_token_usage: dict[str, Any] | None = None
    total_token_usage: dict[str, Any] | None = None
    model_context_window: int | None = None


class NormalizedQuotaWindow(BaseModel):
    used_percent: float | None = None
    window_minutes: int | None = None
    resets_at: int | None = None


class NormalizedQuotaSnapshot(BaseModel):
    limit_id: str | None = None
    plan_type: str | None = None
    primary: NormalizedQuotaWindow | None = None
    secondary: NormalizedQuotaWindow | None = None
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
    return _normalized_step_usage(model=model, usage=usage)


def normalize_pi_usage(*, model: Any, usage: Any) -> dict[str, Any]:
    usage_map = usage if isinstance(usage, dict) else {}
    return _normalized_step_usage(
        model=model,
        usage={
            "input_tokens": usage_map.get("input"),
            "output_tokens": usage_map.get("output"),
            "total_tokens": usage_map.get("totalTokens"),
        },
    )


def normalize_amp_usage(*, model: Any, usage: Any) -> dict[str, Any]:
    usage_map = usage if isinstance(usage, dict) else {}
    return _normalized_step_usage(
        model=model or usage_map.get("model"),
        usage={
            "input_tokens": usage_map.get("inputTokens"),
            "output_tokens": usage_map.get("outputTokens"),
        },
    )


def normalize_gemini_usage(*, model: Any, tokens: Any) -> dict[str, Any]:
    return _normalized_step_usage(model=model, usage=tokens)


def normalize_quota_snapshot(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    snapshot = NormalizedQuotaSnapshot(
        limit_id=_as_str(value.get("limit_id")),
        plan_type=_as_str(value.get("plan_type")),
        primary=_quota_window(value.get("primary")),
        secondary=_quota_window(value.get("secondary")),
        rate_limit_reached_type=_as_str(value.get("rate_limit_reached_type")),
    )
    dumped = snapshot.model_dump(exclude_none=True)
    return dumped or None


def _normalized_step_usage(*, model: Any, usage: Any) -> dict[str, Any]:
    usage_map = usage if isinstance(usage, dict) else {}
    metrics = NormalizedUsageMetrics(
        model=_as_str(model) or _as_str(usage_map.get("model")),
        usage=usage_map or None,
    )
    dumped = metrics.model_dump(exclude_none=True)
    return {"metrics": dumped} if dumped else {}


def _quota_window(value: Any) -> NormalizedQuotaWindow | None:
    if not isinstance(value, dict):
        return None
    return NormalizedQuotaWindow(
        used_percent=_as_float_or_none(value.get("used_percent")),
        window_minutes=_as_int_or_none(value.get("window_minutes")),
        resets_at=_as_int_or_none(value.get("resets_at")),
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
