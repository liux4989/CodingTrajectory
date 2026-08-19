"""Token Efficiency projection models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


Grain = Literal["daily", "weekly"]


class Distribution(BaseModel):
    count: int = 0
    avg: float = 0
    median: float = 0
    p90: float = 0
    p95: float = 0
    max: float = 0


class PeriodSummary(BaseModel):
    bucket: str
    label: str
    is_complete: bool = True
    started_at: datetime
    ended_at: datetime
    session_count: int = 0
    turn_count: int = 0
    total_prompt_tokens: int = 0
    session_prompt: Distribution = Field(default_factory=Distribution)
    turn_prompt: Distribution = Field(default_factory=Distribution)
    pattern_prompt_tokens: int = 0
    pattern_share: float = 0


class ComparisonDelta(BaseModel):
    total_prompt_tokens_pct: float | None = None
    session_median_pct: float | None = None
    session_p90_pct: float | None = None
    turn_median_pct: float | None = None
    turn_p90_pct: float | None = None


class PeriodComparison(BaseModel):
    grain: Grain
    current: PeriodSummary
    previous: PeriodSummary | None = None
    deltas: ComparisonDelta = Field(default_factory=ComparisonDelta)


class PatternIndicators(BaseModel):
    repeated_read: int = 0
    parallel_fanout: int = 0
    truncated_output: int = 0


class UnitDistributions(BaseModel):
    session: Distribution = Field(default_factory=Distribution)
    turn: Distribution = Field(default_factory=Distribution)


class Contributor(BaseModel):
    session_id: str
    turn_id: str | None = None
    title: str | None = None
    prompt_tokens: int = 0
    calls: int = 0
    repeated_calls: int = 0
    pattern: str | None = None


class PatternMetrics(BaseModel):
    incidence_count: int = 0
    incidence_rate: float = 0
    calls: int = 0
    total_prompt_tokens: int = 0
    token_share: float = 0
    zero_inclusive: UnitDistributions = Field(default_factory=UnitDistributions)
    conditional: UnitDistributions = Field(default_factory=UnitDistributions)
    indicators: PatternIndicators = Field(default_factory=PatternIndicators)


class PatternDelta(BaseModel):
    prompt_tokens_pct: float | None = None
    incidence_rate_points: float = 0
    calls_pct: float | None = None
    session_median_pct: float | None = None
    session_p90_pct: float | None = None
    turn_median_pct: float | None = None
    turn_p90_pct: float | None = None


class PatternRow(BaseModel):
    key: str
    label: str
    kind: Literal["exclusive", "indicator"]
    current: PatternMetrics
    previous: PatternMetrics | None = None
    deltas: PatternDelta = Field(default_factory=PatternDelta)
    contributors: list[Contributor] = Field(default_factory=list)


class HotspotRow(BaseModel):
    key: str
    resource: str
    status: Literal["persistent", "phase", "outlier_dominated", "emerging"]
    sessions: int = 0
    turns: int = 0
    calls: int = 0
    repeat_count: int = 0
    enclosing_prompt_tokens: int = 0
    largest_call_tokens: int = 0
    largest_call_share: float = 0
    broad_calls: int = 0
    targeted_calls: int = 0
    previous_enclosing_prompt_tokens: int = 0
    delta_pct: float | None = None
    session: Distribution = Field(default_factory=Distribution)
    turn: Distribution = Field(default_factory=Distribution)
    contributors: list[Contributor] = Field(default_factory=list)


class OutlierRow(BaseModel):
    session_id: str
    turn_id: str
    title: str | None = None
    completed_at: datetime | None = None
    prompt_tokens: int = 0
    session_share: float = 0
    max_context_tokens: int | None = None
    primary_pattern: str | None = None
    reason_codes: list[str] = Field(default_factory=list)


class Coverage(BaseModel):
    root_graphs: int = 0
    sessions: int = 0
    turns: int = 0
    tool_items: int = 0
    attributed_tool_items: int = 0
    undated_tool_items: int = 0
    truncated_input_summaries: int = 0


class ProjectOption(BaseModel):
    name: str
    path: str | None = None
    vendors: list[str] = Field(default_factory=list)


class ProjectProjection(BaseModel):
    schema_version: Literal[1] = 1
    generated_at: datetime
    filters: dict[str, Any]
    attribution: dict[str, Any]
    coverage: Coverage
    warnings: list[str] = Field(default_factory=list)
    project: dict[str, Any]
    comparisons: dict[str, PeriodComparison | None]
    trends: dict[str, list[PeriodSummary]]
    patterns: dict[str, list[PatternRow]] = Field(default_factory=dict)
    hotspots: dict[str, list[HotspotRow]] = Field(default_factory=dict)
    outliers: dict[str, list[OutlierRow]] = Field(default_factory=dict)


