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


def _usage_accounting_payload(usage: dict[str, int], *, cost_usd: float) -> dict[str, int | float]:
    total_tokens = int(usage.get("total_tokens") or 0)
    if total_tokens == 0:
        total_tokens = (
            int(usage.get("input_tokens") or 0)
            + int(usage.get("output_tokens") or 0)
            + int(usage.get("reasoning_output_tokens") or 0)
        )
    return {**usage, "total_tokens": total_tokens, "cost_usd": cost_usd}


class MetricSource(BaseModel):
    vendor: str
    source_type: Literal["step.vendor_data", "event.payload", "session.context_usage"]
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
# Simplified flat output for ct session stats scopes
# ---------------------------------------------------------------------------

class StepMetricsFlat(BaseModel):
    step_id: UUID
    sequence: int
    kind: str | None = None
    token_usage: TokenUsage | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if data.get("token_usage") is None:
            data.pop("token_usage", None)
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
    steps: list[StepMetricsFlat] | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if data.get("steps") is None:
            data.pop("steps", None)
        return data


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


class ContextCategoryFlat(BaseModel):
    key: str
    label: str
    tokens: int = 0
    percent: float | None = None
    confidence: Literal["exact_usage", "exact_text", "estimated_tokens", "text_chars", "structural"] = (
        "estimated_tokens"
    )
    source: str | None = None
    children: list["ContextCategoryFlat"] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        data.pop("confidence", None)
        data.pop("source", None)
        if data.get("percent") is None:
            data.pop("percent", None)
        if not data.get("children"):
            data.pop("children", None)
        return data


class ContextWindowStatsFlat(BaseModel):
    used_tokens: int = 0
    used_percent: float | None = None
    source: str | None = None
    categories: list[ContextCategoryFlat] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        data.pop("source", None)
        if data.get("used_percent") is None:
            data.pop("used_percent", None)
        return data


class RuntimeStatsFlat(BaseModel):
    status: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_seconds: int | None = None
    turns: int = 0
    model_steps: int = 0
    tool_calls: int = 0
    failed_tool_calls: int = 0
    subagent_sessions: int = 0
    compactions: int = 0

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        for key in ("status", "started_at", "ended_at", "duration_seconds"):
            if data.get(key) is None:
                data.pop(key, None)
        return data


class MessageStatsFlat(BaseModel):
    user: int = 0
    assistant: int = 0
    developer: int = 0
    tool_outputs: int = 0
    reasoning_items: int = 0
    compacted_contexts: int = 0


class ContextModelStatsFlat(BaseModel):
    name: str | None = None
    context_window_tokens: int | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if data.get("name") is None:
            data.pop("name", None)
        if data.get("context_window_tokens") is None:
            data.pop("context_window_tokens", None)
        return data


class QuotaStatsFlat(BaseModel):
    plan_type: str | None = None
    primary_used_percent: float | None = None
    secondary_used_percent: float | None = None
    resets_at: int | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        return {key: value for key, value in data.items() if value is not None}


class SessionContextStatsFlat(BaseModel):
    root_session_id: UUID
    vendor: str
    model: ContextModelStatsFlat = Field(default_factory=ContextModelStatsFlat)
    context_window: ContextWindowStatsFlat = Field(default_factory=ContextWindowStatsFlat)
    runtime: RuntimeStatsFlat = Field(default_factory=RuntimeStatsFlat)
    messages: MessageStatsFlat = Field(default_factory=MessageStatsFlat)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    quota: QuotaStatsFlat | None = None
    warnings: list[str] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if data.get("quota") is None:
            data.pop("quota", None)
        return data


class ActivityUsageBreakdownFlat(BaseModel):
    category: Literal["tool_steps", "response_steps", "mixed_steps", "other_steps"]
    usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float = 0.0

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if data.get("usage"):
            data["usage"] = _usage_accounting_payload(data["usage"], cost_usd=self.cost_usd)
        data.pop("cost_usd", None)
        return data


class TurnUsageCompactFlat(BaseModel):
    turn_id: UUID
    session_id: UUID | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float = 0.0
    activity_usage: list[ActivityUsageBreakdownFlat] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if data.get("session_id") is None:
            data.pop("session_id", None)
        if data.get("usage"):
            data["usage"] = _usage_accounting_payload(data["usage"], cost_usd=self.cost_usd)
        data.pop("cost_usd", None)
        if not data.get("activity_usage"):
            data.pop("activity_usage", None)
        return data


class SessionUsageCompactFlat(BaseModel):
    session_id: UUID
    extra_billing: bool = False
    turns: list[TurnUsageCompactFlat] = Field(default_factory=list)
    total_usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float = 0.0
    warnings: list[str] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if data.get("total_usage"):
            data["total_usage"] = _usage_accounting_payload(data["total_usage"], cost_usd=self.cost_usd)
        data.pop("cost_usd", None)
        return data


class ToolCostSemantics(BaseModel):
    observed_cost_scope: Literal["tool_step"] = "tool_step"
    per_tool_cost: Literal["not_measured"] = "not_measured"
    output_metrics: Literal["causal_signal_only"] = "causal_signal_only"


class ToolOutputUsageFlat(BaseModel):
    tool_index: int
    tool_name: str | None = None
    status: str | None = None
    input_summary: str | None = None
    output_chars: int = 0
    output_original_tokens: int | None = None
    output_truncated: bool = False

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        for key in ("tool_name", "status", "input_summary", "output_original_tokens"):
            if data.get(key) is None:
                data.pop(key, None)
        if data.get("output_truncated") is False:
            data.pop("output_truncated", None)
        return data


class ToolStepUsageFlat(BaseModel):
    session_id: UUID
    turn_id: UUID
    turn_sequence: int
    step_id: UUID
    step_sequence: int
    kind: str | None = None
    observed_step_cost: float = 0.0
    currency: Literal["USD"] = "USD"
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    tool_count: int = 0
    duration_ms: int | None = None
    tool_output_chars: int = 0
    tool_output_original_tokens: int = 0
    tools: list[ToolOutputUsageFlat] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if data.get("duration_ms") is None:
            data.pop("duration_ms", None)
        return data


class SessionGraphToolUsageFlat(BaseModel):
    root_session_id: UUID
    cost_semantics: ToolCostSemantics = Field(default_factory=ToolCostSemantics)
    observed_tool_step_cost: float = 0.0
    currency: Literal["USD"] = "USD"
    extra_billing: bool = False
    tool_step_count: int = 0
    tool_call_count: int = 0
    tool_output_chars: int = 0
    tool_output_original_tokens: int = 0
    tool_steps: list[ToolStepUsageFlat] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
