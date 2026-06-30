"""Typed models for derived execution metrics."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_serializer

from coding_trajectory.metrics.accounting import usage_accounting_payload as _usage_accounting_payload


class TokenUsage(BaseModel):
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0

    def compute_total(self) -> int:
        return (
            self.input_tokens
            + self.cached_input_tokens
            + self.cache_creation_input_tokens
            + self.output_tokens
            + self.reasoning_output_tokens
        )

    def plus(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens + other.cache_creation_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_output_tokens=self.reasoning_output_tokens + other.reasoning_output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


class MetricSource(BaseModel):
    vendor: str
    source_type: Literal["event.payload", "session.context_usage"]
    event_id: UUID | None = None
    confidence: Literal["exact", "derived", "estimated", "unknown"] = "exact"


class TokenUsageObservation(BaseModel):
    scope_type: Literal["turn"]
    scope_id: UUID
    timestamp: datetime
    usage: TokenUsage
    provider: str | None = None
    model: str | None = None
    source: MetricSource


class QuotaWindow(BaseModel):
    used_percent: float | None = None
    window_minutes: int | None = None
    resets_at: int | None = None


class QuotaSnapshot(BaseModel):
    timestamp: datetime
    source_event_id: UUID
    limit_id: str | None = None
    limit_name: str | None = None
    plan_type: str | None = None
    primary: QuotaWindow | None = None
    secondary: QuotaWindow | None = None
    credits: dict[str, bool | str | None] | None = None
    individual_limit: dict[str, str | int | None] | None = None
    rate_limit_reached_type: str | None = None


class TurnMetrics(BaseModel):
    turn_id: UUID
    sequence: int
    status: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    observations: list[TokenUsageObservation] = Field(default_factory=list)
    quota_snapshots: list[QuotaSnapshot] = Field(default_factory=list)


class SessionMetrics(BaseModel):
    session_id: UUID
    vendor: str
    status: str | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    turns: list[TurnMetrics] = Field(default_factory=list)
    quota_snapshot: QuotaSnapshot | None = None


class SessionGraphMetrics(BaseModel):
    root_session_id: UUID
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    sessions: list[SessionMetrics] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Simplified flat output for ct session stats scopes
# ---------------------------------------------------------------------------

class TurnMetricsFlat(BaseModel):
    turn_id: UUID
    sequence: int
    status: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    model: str | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)


class SessionMetricsFlat(BaseModel):
    session_id: UUID
    vendor: str
    status: str | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    turns: list[TurnMetricsFlat] = Field(default_factory=list)


class SessionGraphMetricsFlat(BaseModel):
    root_session_id: UUID
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    sessions: list[SessionMetricsFlat] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ContextCategoryFlat(BaseModel):
    key: str
    label: str
    tokens: int = 0
    observed_chars: int | None = None
    items: int | None = None
    percent: float | None = None
    confidence: Literal["exact_usage", "exact_text", "estimated_tokens", "text_chars", "structural"] = (
        "estimated_tokens"
    )
    source: str | None = None
    children: list["ContextCategoryFlat"] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if data.get("observed_chars") is None:
            data.pop("observed_chars", None)
        if data.get("items") is None:
            data.pop("items", None)
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
    execution_seconds: int | None = None
    wait_seconds: int | None = None
    turns: int = 0
    items: int = 0
    tool_calls: int = 0
    failed_tool_calls: int = 0
    subagent_sessions: int = 0
    compactions: int = 0
    interrupted_turns: int = 0
    rollbacks: int = 0
    average_time_to_first_token_ms: int | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        for key in ("status", "started_at", "ended_at", "execution_seconds", "wait_seconds"):
            if data.get(key) is None:
                data.pop(key, None)
        return data


class TurnRuntimeFlat(BaseModel):
    started_at: datetime | None = None
    ended_at: datetime | None = None
    execution_seconds: int | None = None
    wait_before_seconds: int | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        return {key: value for key, value in data.items() if value is not None}


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
    limit_id: str | None = None
    limit_name: str | None = None
    plan_type: str | None = None
    primary_used_percent: float | None = None
    primary_window_minutes: int | None = None
    primary_resets_at: int | None = None
    secondary_used_percent: float | None = None
    secondary_window_minutes: int | None = None
    secondary_resets_at: int | None = None
    credits_has_credits: bool | None = None
    credits_unlimited: bool | None = None
    credits_balance: str | None = None
    individual_limit: str | None = None
    individual_used: str | None = None
    individual_remaining_percent: int | None = None
    rate_limit_reached_type: str | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        return {key: value for key, value in data.items() if value is not None}


class SessionContextStatsFlat(BaseModel):
    root_session_id: UUID
    vendor: str
    model: ContextModelStatsFlat = Field(default_factory=ContextModelStatsFlat)
    context_window: ContextWindowStatsFlat = Field(default_factory=ContextWindowStatsFlat)
    provider_usage_buckets: list[ContextCategoryFlat] = Field(default_factory=list)
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


class TurnUsageCompactFlat(BaseModel):
    turn_id: UUID
    session_id: UUID | None = None
    runtime: TurnRuntimeFlat | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if data.get("session_id") is None:
            data.pop("session_id", None)
        if data.get("runtime") == {}:
            data.pop("runtime", None)
        if data.get("usage"):
            data["usage"] = _usage_accounting_payload(data["usage"])
        return data


class SessionUsageCompactFlat(BaseModel):
    session_id: UUID
    runtime: RuntimeStatsFlat | None = None
    turns: list[TurnUsageCompactFlat] = Field(default_factory=list)
    total_usage: TokenUsage = Field(default_factory=TokenUsage)
    warnings: list[str] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if data.get("total_usage"):
            data["total_usage"] = _usage_accounting_payload(data["total_usage"])
        return data


class UsageSpan(BaseModel):
    usage_observation_id: UUID
    turn_id: UUID
    related_item_ids: list[UUID] = Field(default_factory=list)
    attribution: Literal["exact", "shared", "unknown"] = "shared"


class ToolTokenAttribution(BaseModel):
    tool_input_tokens: int = 0
    tool_output_tokens: int = 0
    content_confidence: Literal[
        "observed_tool_output_token_count",
        "visible_content_estimate",
        "no_visible_content",
    ] = "visible_content_estimate"
    method: Literal["visible_content_estimate"] = "visible_content_estimate"

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        return data


class InvokeResponseTokens(BaseModel):
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    attribution: Literal[
        "single_tool_response",
        "shared_model_response",
        "unknown",
    ] = "unknown"


class ReadAfterResult(BaseModel):
    included_in_turn_usage: bool = False
    attribution: Literal[
        "causal_next_model_request",
        "turn_completed_without_reuse",
        "unknown",
    ] = "unknown"


class AllocatedRealTokenCost(BaseModel):
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    allocation_method: Literal[
        "usage_observation_weighted_by_visible_item_tokens",
        "usage_observation_even_split",
    ] = "usage_observation_weighted_by_visible_item_tokens"
    confidence: Literal["allocated_from_exact_usage"] = "allocated_from_exact_usage"
    usage_authority: Literal["session.usage"] = "session.usage"


class AttributionPolicy(BaseModel):
    scope: Literal["tool_items"] = "tool_items"
    cache: Literal["allocated_from_exact_usage"] = "allocated_from_exact_usage"
    usage_authority: Literal["session.usage"] = "session.usage"
    method: Literal["visible_content_plus_event_order"] = "visible_content_plus_event_order"
    real_token_cost: Literal[
        "allocated_from_usage_observation_weighted_by_visible_item_tokens"
    ] = "allocated_from_usage_observation_weighted_by_visible_item_tokens"


class ToolItemFlat(BaseModel):
    item_id: UUID
    session_id: UUID
    turn_id: UUID
    tool_name: str | None = None
    status: str | None = None
    input_summary: str | None = None
    output_chars: int = 0
    output_original_tokens: int | None = None
    output_truncated: bool = False
    token_attribution: ToolTokenAttribution | None = None
    allocated_real_token_cost: AllocatedRealTokenCost | None = None
    invoke_response_tokens: InvokeResponseTokens | None = None
    read_after_result: ReadAfterResult | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        for key in ("tool_name", "status", "input_summary", "output_original_tokens"):
            if data.get(key) is None:
                data.pop(key, None)
        if data.get("output_truncated") is False:
            data.pop("output_truncated", None)
        for key in (
            "token_attribution",
            "allocated_real_token_cost",
            "invoke_response_tokens",
            "read_after_result",
        ):
            if data.get(key) is None:
                data.pop(key, None)
        return data


class SessionGraphToolUsageFlat(BaseModel):
    root_session_id: UUID
    tool_item_count: int = 0
    tool_call_count: int = 0
    tool_output_chars: int = 0
    tool_output_original_tokens: int = 0
    allocated_real_token_cost: AllocatedRealTokenCost | None = None
    tool_items: list[ToolItemFlat] = Field(default_factory=list)
    attribution_policy: AttributionPolicy = Field(default_factory=AttributionPolicy)
    warnings: list[str] = Field(default_factory=list)
