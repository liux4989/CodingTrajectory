"""Typed models for derived execution metrics."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_serializer


class TokenUsage(BaseModel):
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0

    def plus(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_output_tokens=self.reasoning_output_tokens + other.reasoning_output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


class MetricSource(BaseModel):
    vendor: str
    source_type: Literal["step.vendor_data", "event.payload"]
    event_id: UUID | None = None
    confidence: Literal["exact", "derived", "estimated", "unknown"] = "exact"


class TokenUsageObservation(BaseModel):
    scope_type: Literal["step"]
    scope_id: UUID
    timestamp: datetime
    usage: TokenUsage
    provider: str | None = None
    model: str | None = None
    source: MetricSource


class CostBreakdown(BaseModel):
    input_usd: float = 0.0
    cached_input_usd: float = 0.0
    output_usd: float = 0.0
    reasoning_output_usd: float = 0.0

    def plus(self, other: "CostBreakdown") -> "CostBreakdown":
        return CostBreakdown(
            input_usd=self.input_usd + other.input_usd,
            cached_input_usd=self.cached_input_usd + other.cached_input_usd,
            output_usd=self.output_usd + other.output_usd,
            reasoning_output_usd=self.reasoning_output_usd + other.reasoning_output_usd,
        )


class CostEstimate(BaseModel):
    amount_usd: float = 0.0
    currency: Literal["USD"] = "USD"
    extra_billing: bool = False
    pricing_source: str | None = None
    pricing_effective_date: str | None = None
    model: str | None = None
    complete: bool = True
    missing_reasons: list[str] = Field(default_factory=list)
    breakdown: CostBreakdown = Field(default_factory=CostBreakdown)

    def plus(self, other: "CostEstimate") -> "CostEstimate":
        return CostEstimate(
            amount_usd=self.amount_usd + other.amount_usd,
            extra_billing=self.extra_billing or other.extra_billing,
            pricing_source=self.pricing_source or other.pricing_source,
            pricing_effective_date=self.pricing_effective_date or other.pricing_effective_date,
            model=_merge_optional_equal(self.model, other.model),
            complete=self.complete and other.complete,
            missing_reasons=[*self.missing_reasons, *other.missing_reasons],
            breakdown=self.breakdown.plus(other.breakdown),
        )


def _merge_optional_equal(left: str | None, right: str | None) -> str | None:
    if left is None:
        return right
    if right is None:
        return left
    return left if left == right else None


class QuotaWindow(BaseModel):
    used_percent: float | None = None
    window_minutes: int | None = None
    resets_at: int | None = None


class QuotaSnapshot(BaseModel):
    timestamp: datetime
    source_event_id: UUID
    limit_id: str | None = None
    plan_type: str | None = None
    primary: QuotaWindow | None = None
    secondary: QuotaWindow | None = None
    rate_limit_reached_type: str | None = None


class StepMetrics(BaseModel):
    step_id: UUID
    sequence: int
    kind: str | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_estimate: CostEstimate = Field(default_factory=CostEstimate)
    observations: list[TokenUsageObservation] = Field(default_factory=list)
    tool_count: int = 0
    tool_duration_ms: int | None = None


class TurnMetrics(BaseModel):
    turn_id: UUID
    sequence: int
    status: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_estimate: CostEstimate = Field(default_factory=CostEstimate)
    steps: list[StepMetrics] = Field(default_factory=list)
    quota_snapshots: list[QuotaSnapshot] = Field(default_factory=list)


class SessionMetrics(BaseModel):
    session_id: UUID
    vendor: str
    status: str | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_estimate: CostEstimate = Field(default_factory=CostEstimate)
    turns: list[TurnMetrics] = Field(default_factory=list)
    quota_snapshot: QuotaSnapshot | None = None


class SessionGraphMetrics(BaseModel):
    root_session_id: UUID
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_estimate: CostEstimate = Field(default_factory=CostEstimate)
    sessions: list[SessionMetrics] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Simplified flat output for ct graph metrics
# ---------------------------------------------------------------------------

class StepToolMetricsFlat(BaseModel):
    tool_count: int = 0
    duration_ms: int | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if data.get("duration_ms") is None:
            data.pop("duration_ms", None)
        return data


class StepMetricsFlat(BaseModel):
    step_id: UUID
    sequence: int
    kind: str | None = None
    token_usage: TokenUsage | None = None
    tool_metrics: StepToolMetricsFlat | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if data.get("token_usage") is None:
            data.pop("token_usage", None)
        if data.get("tool_metrics") is None:
            data.pop("tool_metrics", None)
        return data


class TurnMetricsFlat(BaseModel):
    turn_id: UUID
    sequence: int
    status: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    model: str | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    cost: float = 0.0
    currency: Literal["USD"] = "USD"
    extra_billing: bool = False
    steps: list[StepMetricsFlat] = Field(default_factory=list)


class SessionMetricsFlat(BaseModel):
    session_id: UUID
    vendor: str
    status: str | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    cost: float = 0.0
    currency: Literal["USD"] = "USD"
    extra_billing: bool = False
    turns: list[TurnMetricsFlat] = Field(default_factory=list)


class SessionGraphMetricsFlat(BaseModel):
    root_session_id: UUID
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    cost: float = 0.0
    currency: Literal["USD"] = "USD"
    extra_billing: bool = False
    sessions: list[SessionMetricsFlat] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
