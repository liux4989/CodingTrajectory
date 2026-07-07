"""Typed models for derived execution metrics."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_serializer

from coding_trajectory.metrics.accounting import (
    usage_accounting_payload as _usage_accounting_payload,
)
from coding_trajectory.metrics.pricing import CostEvidenceFlat


class TokenUsage(BaseModel):
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    processed_tokens: int = 0
    reported_total_tokens: int | None = None
    # Vendor-reported USD cost for this usage bucket (e.g. Pi's ``cost.total``
    # from its jsonl logs). ``None`` when the vendor doesn't report cost, in
    # which case downstream consumers fall back to the pricing SoT's estimate.
    cost_usd: float | None = None
    total_confidence: Literal[
        "reported_consistent",
        "reported_missing",
        "reported_inconsistent",
    ] = "reported_missing"

    def compute_total(self) -> int:
        return (
            self.input_tokens
            + self.cached_input_tokens
            + self.cache_creation_input_tokens
            + self.output_tokens
            + self.reasoning_output_tokens
        )

    def processed_token_total(self) -> int:
        return self.processed_tokens or self.compute_total()

    def prompt_completion_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        _ = handler
        data: dict[str, int | str | float | None] = {
            "prompt_tokens": self.input_tokens,
            "cached_prompt_tokens": self.cached_input_tokens,
            "cache_write_tokens": self.cache_creation_input_tokens,
            "completion_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_output_tokens,
            "processed_tokens": self.processed_token_total(),
            "prompt_completion_tokens": self.prompt_completion_tokens(),
            "total_confidence": self.total_confidence,
        }
        if self.reported_total_tokens is not None:
            data["reported_total_tokens"] = self.reported_total_tokens
        if self.cost_usd is not None:
            data["cost_usd"] = round(self.cost_usd, 8)
        return data

    def plus(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens
            + other.cache_creation_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_output_tokens=self.reasoning_output_tokens
            + other.reasoning_output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            processed_tokens=self.processed_token_total()
            + other.processed_token_total(),
            reported_total_tokens=_optional_sum(
                self.reported_total_tokens,
                other.reported_total_tokens,
            ),
            cost_usd=_optional_sum(self.cost_usd, other.cost_usd),
            total_confidence=_combine_total_confidence(
                self.total_confidence,
                other.total_confidence,
            ),
        )


def _optional_sum(left: int | float | None, right: int | float | None) -> int | float | None:
    if left is None and right is None:
        return None
    return (left or 0) + (right or 0)


def _combine_total_confidence(
    left: Literal["reported_consistent", "reported_missing", "reported_inconsistent"],
    right: Literal["reported_consistent", "reported_missing", "reported_inconsistent"],
) -> Literal["reported_consistent", "reported_missing", "reported_inconsistent"]:
    if "reported_inconsistent" in {left, right}:
        return "reported_inconsistent"
    if "reported_missing" in {left, right}:
        return "reported_missing"
    return "reported_consistent"


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


class TurnMetrics(BaseModel):
    turn_id: UUID
    sequence: int
    status: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    observations: list[TokenUsageObservation] = Field(default_factory=list)


class SessionMetrics(BaseModel):
    session_id: UUID
    vendor: str
    status: str | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    turns: list[TurnMetrics] = Field(default_factory=list)


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
    allocated_usage: dict[str, int] | None = None
    estimated_cost: CostEvidenceFlat | None = None
    observed_chars: int | None = None
    items: int | None = None
    percent: float | None = None
    confidence: Literal[
        "exact_usage", "exact_text", "estimated_tokens", "text_chars", "structural"
    ] = "estimated_tokens"
    source: str | None = None
    children: list["ContextCategoryFlat"] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if data.get("allocated_usage") is None:
            data.pop("allocated_usage", None)
        if data.get("estimated_cost") is None:
            data.pop("estimated_cost", None)
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
        for key in (
            "status",
            "started_at",
            "ended_at",
            "execution_seconds",
            "wait_seconds",
        ):
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


class CompactionEventFlat(BaseModel):
    timestamp: datetime
    # Provider-native compaction mechanism, derived from the observation
    # kind. ``eviction_boundary`` (Claude Code's ``claude_compact_boundary``)
    # is a discrete eviction that carries pre/post/dropped/trigger metadata;
    # ``context_compacted`` (Codex) is a sliding window that exposes none of
    # those, so its event renders without the delta columns rather than as
    # empty pre→post / dropped cells. Drives per-provider rendering.
    mechanism: str
    trigger: str | None = None
    pre_tokens: int | None = None
    post_tokens: int | None = None
    dropped_tokens: int | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        return {key: value for key, value in data.items() if value is not None}


class CompactionStatsFlat(BaseModel):
    count: int = 0
    # Claude Code reports dropped tokens cumulatively across compactions in a
    # session, so the latest observation's value is the running total (not a
    # per-event delta to be summed). ``None`` when no observation carries it
    # (e.g. Codex's sliding-window ``context_compacted``).
    cumulative_dropped_tokens: int | None = None
    last: CompactionEventFlat | None = None
    # Full per-event timeline (oldest first). Empty for sessions that never
    # compacted; populated alongside ``last`` (which mirrors the final entry).
    events: list[CompactionEventFlat] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if data.get("cumulative_dropped_tokens") is None:
            data.pop("cumulative_dropped_tokens", None)
        if data.get("last") is None:
            data.pop("last", None)
        if not data.get("events"):
            data.pop("events", None)
        return data


class SessionContextStatsFlat(BaseModel):
    root_session_id: UUID
    vendor: str
    model: ContextModelStatsFlat = Field(default_factory=ContextModelStatsFlat)
    context_window: ContextWindowStatsFlat = Field(
        default_factory=ContextWindowStatsFlat
    )
    provider_usage_buckets: list[ContextCategoryFlat] = Field(default_factory=list)
    runtime: RuntimeStatsFlat = Field(default_factory=RuntimeStatsFlat)
    compaction: CompactionStatsFlat | None = None
    messages: MessageStatsFlat = Field(default_factory=MessageStatsFlat)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    warnings: list[str] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if data.get("compaction") is None:
            data.pop("compaction", None)
        return data


class TurnUsageCompactFlat(BaseModel):
    turn_id: UUID
    session_id: UUID | None = None
    runtime: TurnRuntimeFlat | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    estimated_cost: CostEvidenceFlat | None = None
    cache_break_waste_usd: float | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if data.get("session_id") is None:
            data.pop("session_id", None)
        if data.get("runtime") == {}:
            data.pop("runtime", None)
        if data.get("usage"):
            data["usage"] = _usage_accounting_payload(data["usage"])
        if data.get("estimated_cost") is None:
            data.pop("estimated_cost", None)
        if data.get("cache_break_waste_usd") is None:
            data.pop("cache_break_waste_usd", None)
        return data


class SessionUsageCompactFlat(BaseModel):
    session_id: UUID
    runtime: RuntimeStatsFlat | None = None
    turns: list[TurnUsageCompactFlat] = Field(default_factory=list)
    total_usage: TokenUsage = Field(default_factory=TokenUsage)
    estimated_cost: CostEvidenceFlat | None = None
    compaction: CompactionStatsFlat | None = None
    warnings: list[str] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if data.get("total_usage"):
            data["total_usage"] = _usage_accounting_payload(data["total_usage"])
        if data.get("estimated_cost") is None:
            data.pop("estimated_cost", None)
        if data.get("compaction") is None:
            data.pop("compaction", None)
        return data


class ModelUsageContextFlat(BaseModel):
    final_used_tokens: int | None = None
    max_used_tokens: int | None = None
    context_window_tokens: int | None = None
    final_used_percent: float | None = None
    max_used_percent: float | None = None
    source: str | None = None
    confidence: Literal["exact_usage", "derived", "unknown"] = "unknown"

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        return {key: value for key, value in data.items() if value is not None}


class ModelUsageModelFlat(BaseModel):
    provider: str | None = None
    model: str | None = None
    turns: int = 0
    usage: TokenUsage = Field(default_factory=TokenUsage)
    estimated_cost: CostEvidenceFlat | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if data.get("usage"):
            data["usage"] = _usage_accounting_payload(data["usage"])
        if data.get("estimated_cost") is None:
            data.pop("estimated_cost", None)
        return data


class DominantModelFlat(BaseModel):
    provider: str | None = None
    model: str | None = None
    basis: Literal["total_tokens", "turns"] = "total_tokens"


class ModelUsageTurnFlat(BaseModel):
    turn_id: UUID
    session_id: UUID
    vendor: str
    sequence: int
    started_at: datetime | None = None
    completed_at: datetime | None = None
    provider: str | None = None
    model: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    models: list[ModelUsageModelFlat] = Field(default_factory=list)
    context: ModelUsageContextFlat | None = None
    estimated_cost: CostEvidenceFlat | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if data.get("usage"):
            data["usage"] = _usage_accounting_payload(data["usage"])
        if data.get("context") == {}:
            data.pop("context", None)
        if data.get("estimated_cost") is None:
            data.pop("estimated_cost", None)
        return data


class SessionGraphModelUsageFlat(BaseModel):
    root_session_id: UUID
    vendor: str | None = None
    project: str | None = None
    title: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    context: ModelUsageContextFlat | None = None
    models: list[ModelUsageModelFlat] = Field(default_factory=list)
    dominant_model: DominantModelFlat | None = None
    turns: list[ModelUsageTurnFlat] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if data.get("usage"):
            data["usage"] = _usage_accounting_payload(data["usage"])
        if data.get("context") == {}:
            data.pop("context", None)
        if data.get("dominant_model") == {}:
            data.pop("dominant_model", None)
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
    uncached_input_tokens: int = 0
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

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        _ = handler
        return {
            "prompt_tokens": self.input_tokens,
            "uncached_prompt_tokens": self.uncached_input_tokens,
            "cached_prompt_tokens": self.cached_input_tokens,
            "cache_write_tokens": self.cache_creation_input_tokens,
            "completion_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_output_tokens,
            "processed_tokens": self.total_tokens,
            "prompt_completion_tokens": self.input_tokens + self.output_tokens,
            "allocation_method": self.allocation_method,
            "confidence": self.confidence,
            "usage_authority": self.usage_authority,
        }


class AttributionPolicy(BaseModel):
    scope: Literal["tool_items", "all_items"] = "tool_items"
    cache: Literal["allocated_from_exact_usage"] = "allocated_from_exact_usage"
    usage_authority: Literal["session.usage"] = "session.usage"
    method: Literal["visible_content_plus_event_order"] = (
        "visible_content_plus_event_order"
    )
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
    estimated_cost: CostEvidenceFlat | None = None
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
            "estimated_cost",
            "invoke_response_tokens",
            "read_after_result",
        ):
            if data.get(key) is None:
                data.pop(key, None)
        return data


class ItemRealTokenCostFlat(BaseModel):
    item_id: UUID
    session_id: UUID
    turn_id: UUID
    sequence: int
    kind: str
    visible_tokens: int = 0
    allocated_real_token_cost: AllocatedRealTokenCost | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if data.get("allocated_real_token_cost") is None:
            data.pop("allocated_real_token_cost", None)
        return data


class SessionGraphToolUsageFlat(BaseModel):
    root_session_id: UUID
    tool_item_count: int = 0
    tool_call_count: int = 0
    tool_output_chars: int = 0
    tool_output_original_tokens: int = 0
    allocated_real_token_cost: AllocatedRealTokenCost | None = None
    item_real_token_costs: list[ItemRealTokenCostFlat] = Field(default_factory=list)
    tool_items: list[ToolItemFlat] = Field(default_factory=list)
    attribution_policy: AttributionPolicy = Field(default_factory=AttributionPolicy)
    warnings: list[str] = Field(default_factory=list)
