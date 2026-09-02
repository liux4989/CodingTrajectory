"""Strict serialization contracts for Datahub-owned HTTP response payloads.

These models describe final route payloads, not persistence rows.  Canonical
service responses (graph and code-time APIs) deliberately remain outside this
module because their upstream contracts own those shapes.
"""

from __future__ import annotations

from typing import Any, Literal

from coding_trajectory.contracts.estimate import EstimateListResponse
from coding_trajectory.contracts.session import (
    GraphOverviewResponse,
    SessionEventsResponse,
    SessionStatsResponse,
    SessionTreeResponse,
    SessionUsageResponse,
)
from pydantic import BaseModel, ConfigDict, Field, RootModel

from datahub_plugin.projections.context_window.models import ContextWindowProjection
from datahub_plugin.projections.read_models_contracts import (
    ProjectDetailPayload as ReadModelProjectDetailPayload,
    SessionTimelinePayload as ReadModelSessionTimelinePayload,
)
from datahub_plugin.projections.session_timeline import SessionEvidenceTimeline
from datahub_plugin.projections.token_efficiency_models import ProjectProjection

DELIVERY_FAMILIES = (
    "overview",
    "projects",
    "sessions",
    "model-usage",
    "token-efficiency",
    "context-window",
    "session-timeline",
    "session-tree",
    "session-graph",
)
DeliveryFamily = Literal[*DELIVERY_FAMILIES]


class StrictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExtensibleResponse(BaseModel):
    """Typed stable fields on an upstream-owned extensible payload."""

    model_config = ConfigDict(extra="allow")


class RuntimeTotals(StrictResponse):
    execution_seconds: float
    wait_seconds: float
    turns: float
    tool_calls: float
    failed_tool_calls: float


class UsageTotals(StrictResponse):
    processed_tokens: int
    cost_usd: float
    known_cost_count: int
    missing_cost_count: int


class ProjectSummary(StrictResponse):
    project: str
    count: int
    vendors: dict[str, int]
    execution_seconds: float
    processed_tokens: int
    cost_usd: float
    known_cost_count: int


class SessionSummary(StrictResponse):
    id: str | None = None
    title: str | None = None
    project: str | None = None
    vendor: str
    vendors: list[str]
    started_at: str | None = None
    execution_seconds: int
    wait_seconds: int
    turns: int
    tool_calls: int
    failed_tool_calls: int
    processed_tokens: int


class WarningSummary(StrictResponse):
    session_id: str | None = None
    project: str
    message: str


class SessionOverview(StrictResponse):
    count: int
    window_days: int
    runtime: RuntimeTotals
    usage: UsageTotals
    top_projects: list[ProjectSummary]
    top_sessions: list[SessionSummary]
    warnings: list[WarningSummary]
    errors: list[Any]


class ProjectsOverview(StrictResponse):
    count: int
    vendors: dict[str, int]


class OverviewPayload(StrictResponse):
    schema_version: Literal[1]
    revision: int
    generated_at: str
    cohort: dict[str, int]
    coverage: dict[str, int]
    projects: ProjectsOverview
    sessions: SessionOverview
    warnings: list[WarningSummary]


class ActivityBucket(StrictResponse):
    bucket: str
    sessions: int
    execution_seconds: int
    processed_tokens: int
    cost_usd: float


class TodayPayload(OverviewPayload):
    hourly: list[ActivityBucket]
    daily: list[ActivityBucket]
    warnings: list[WarningSummary]


class ProjectItem(StrictResponse):
    name: str
    path: str | None = None
    vendors: list[str]


class ProjectsPayload(StrictResponse):
    items: list[ProjectItem]
    page: CursorPageMetadata


class SessionItem(StrictResponse):
    root_session_id: str
    lineage_root_session_id: str | None = None
    graph_id: str | None = None
    vendors: list[str]
    session_ids: list[str]
    title: str | None = None
    preview: str | None = None
    project: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    status: str | None = None
    turns: int | None = None
    execution_seconds: float | None = None
    failed_tool_calls: int | None = None
    processed_tokens: int | None = None
    cost_usd: float | None = None
    pricing_confidence: str | None = None


class CursorPageMetadata(StrictResponse):
    revision: int
    next_cursor: str | None
    has_more: bool


class SessionTimelinePayload(ReadModelSessionTimelinePayload):
    """Timeline route payload, including its cursor metadata."""

    page: CursorPageMetadata


class ProjectDetailPayload(ReadModelProjectDetailPayload):
    """Project-detail route payload, including its cursor metadata."""

    page: CursorPageMetadata


class SessionPage(StrictResponse):
    items: list[SessionItem]
    page: CursorPageMetadata


class DatahubFreshness(StrictResponse):
    last_refresh_at: str | None
    lag_seconds: float | None


class DatahubSourceStatus(StrictResponse):
    ready: int
    ingesting: int
    failed: int
    incomplete: int


class BootstrapStatus(StrictResponse):
    ready: bool
    scan_started_at: str | None
    scan_finished_at: str | None
    error: str | None
    last_result: dict[str, Any] | None = None
    coverage: dict[str, Any] | None = None


class DatahubSnapshot(StrictResponse):
    revision: int
    generated_at: str
    freshness: DatahubFreshness
    catching_up: bool
    source_status: DatahubSourceStatus
    minimum_available_revision: int
    bootstrap: BootstrapStatus
    horizon_days: int


class DatahubUpsert(StrictResponse):
    entity_type: str
    entity_id: str
    revision: int
    payload: Any


class DatahubDeletion(StrictResponse):
    entity_type: str
    entity_id: str
    revision: int


class DatahubChanges(StrictResponse):
    from_revision: int
    to_revision: int
    reset_required: bool
    upserts: list[DatahubUpsert]
    deletions: list[DatahubDeletion]
    invalidations: list[str]
    freshness: DatahubFreshness
    catching_up: bool
    source_status: DatahubSourceStatus


class ContextWindowPayload(ContextWindowProjection):
    pass


class SessionEvidenceTimelinePayload(SessionEvidenceTimeline):
    pass


class TokenEfficiencyProjectPayload(ProjectProjection):
    pass


class GraphOrchestration(ExtensibleResponse):
    kind: str | None = None
    vendors: list[str] | None = None
    session_count: int | None = None
    spawned_agent_count: int | None = None
    multi_agent_versions: list[str] | None = None
    multi_agent_modes: list[str] | None = None
    edge_counts: dict[str, int] | None = None
    agent_paths: list[str] | None = None


class GraphSessionNode(ExtensibleResponse):
    session_id: str
    parent_session_id: str | None = None
    edge_type: str | None = None
    vendor: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    status: str | None = None
    title: str | None = None
    agent_name: str | None = None
    agent_path: str | None = None
    cwd: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    multi_agent_version: str | None = None
    multi_agent_mode: str | None = None


class GraphEdge(ExtensibleResponse):
    type: str | None = None
    source_session_id: str | None = None
    target_session_id: str | None = None
    provenance: str | None = None
    confidence: str | None = None


class GraphOverviewGraph(ExtensibleResponse):
    orchestration: GraphOrchestration | None = None


class GraphOverviewSummary(ExtensibleResponse):
    session_count: int | None = None
    turn_count: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    vendors: list[str] | None = None


class GraphOverviewPayload(GraphOverviewResponse):
    graph: GraphOverviewGraph = Field(default_factory=GraphOverviewGraph)
    summary: GraphOverviewSummary | None = None
    sessions: list[GraphSessionNode]
    edges: list[GraphEdge]


class GraphContextWindow(ExtensibleResponse):
    used_tokens: int | None = None
    used_percent: float | None = None


class GraphRuntime(ExtensibleResponse):
    turns: int | None = None
    execution_seconds: float | None = None
    tool_calls: int | None = None


class GraphUsageBuckets(ExtensibleResponse):
    processed_tokens: int | None = None
    prompt_tokens: int | None = None
    cached_prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None


class GraphCostEvidence(ExtensibleResponse):
    value_usd: float
    confidence: Literal["reported", "estimated"]
    source: str | None = None
    effective_date: str | None = None


class GraphStatsSession(ExtensibleResponse):
    session_id: str
    role: str | None = None
    vendor: str | None = None
    context_window: GraphContextWindow | None = None
    runtime: GraphRuntime | None = None
    usage: GraphUsageBuckets | None = None


class GraphStatsPayload(SessionStatsResponse):
    context_window: GraphContextWindow = Field(default_factory=GraphContextWindow)
    usage: GraphUsageBuckets = Field(default_factory=GraphUsageBuckets)
    runtime: GraphRuntime = Field(default_factory=GraphRuntime)
    sessions: list[GraphStatsSession] | None = None


class GraphUsageSession(ExtensibleResponse):
    session_id: str
    parent_session_id: str | None = None
    role: str | None = None
    relationship: str | None = None
    title: str | None = None
    agent_name: str | None = None
    total_usage: GraphUsageBuckets | None = None
    estimated_cost: GraphCostEvidence | None = None
    runtime: GraphRuntime | None = None


class GraphModelUsage(ExtensibleResponse):
    provider: str | None = None
    model: str | None = None
    turns: int | None = None
    usage: GraphUsageBuckets | None = None


class GraphUsagePayload(SessionUsageResponse):
    total_usage: GraphUsageBuckets = Field(default_factory=GraphUsageBuckets)
    runtime: GraphRuntime = Field(default_factory=GraphRuntime)
    models: list[GraphModelUsage] = Field(default_factory=list)
    sessions: list[GraphUsageSession] | None = None
    estimated_cost: GraphCostEvidence | None = None


class SessionGraphPayload(StrictResponse):
    root_session_id: str
    overview: GraphOverviewPayload
    stats: GraphStatsPayload
    usage: GraphUsagePayload


class ConversationBranch(ExtensibleResponse):
    session_id: str
    parent_session_id: str | None = None
    source_turn_id: str | None = None
    vendor: str | None = None
    status: str | None = None
    title: str | None = None
    agent_name: str | None = None
    cwd: str | None = None
    started_at: str | None = None
    turn_count: int | None = None
    graph_session_count: int | None = None
    spawned_agent_count: int | None = None


class SessionTreePayload(SessionTreeResponse):
    selected_branch_id: str | None = None
    branches: list[ConversationBranch]


class SessionEventDetail(ExtensibleResponse):
    event_id: str
    session_id: str
    timestamp: str
    type: str
    tool_call: dict[str, Any] | None = None
    llm: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    text: dict[str, str] | None = None


class SessionEventDetailsPayload(SessionEventsResponse):
    matches: list[SessionEventDetail]


class SessionItemDetail(ExtensibleResponse):
    item_id: str
    session_id: str
    turn_id: str
    kind: str
    type: str
    operations: list[str] | None = None
    shape: dict[str, Any] | None = None
    event_ids: list[str] | None = None


class SessionItemDetailsPayload(RootModel[list[SessionItemDetail]]):
    pass


class UsageBuckets(StrictResponse):
    prompt_tokens: int = 0
    uncached_prompt_tokens: int = 0
    cached_prompt_tokens: int = 0
    cache_write_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    processed_tokens: int = 0
    prompt_completion_tokens: int = 0
    reported_total_tokens: int | None = None
    total_confidence: Literal[
        "reported_consistent", "reported_missing", "reported_inconsistent"
    ] = "reported_missing"


class ModelUsageContext(StrictResponse):
    final_used_tokens: int | None = None
    max_used_tokens: int | None = None
    context_window_tokens: int | None = None
    final_used_percent: float | None = None
    max_used_percent: float | None = None
    source: str | None = None
    confidence: str = "unknown"


class ModelUsagePricing(StrictResponse):
    confidence: Literal["reported", "estimated", "missing_price"]
    source: str | None
    effective_date: str | None
    breakdown: dict[str, float] = Field(default_factory=dict)


class DistributionStats(StrictResponse):
    count: int
    avg: float
    median: float
    p90: float
    p95: float
    max: float


class SessionTurnDistributionStats(StrictResponse):
    session: DistributionStats
    turn: DistributionStats


class ModelUsageTokenStats(SessionTurnDistributionStats):
    buckets: dict[str, SessionTurnDistributionStats]


class ModelUsageModel(StrictResponse):
    provider: str | None
    model: str | None
    model_key: str
    sessions: int
    turns: int
    usage: UsageBuckets
    estimated_cost_usd: float
    elapsed_seconds: float
    avg_session_cost_usd: float
    avg_turn_cost_usd: float
    avg_session_elapsed_seconds: float
    avg_turn_elapsed_seconds: float
    token_stats: SessionTurnDistributionStats
    cost_stats: SessionTurnDistributionStats
    pricing: ModelUsagePricing


class ModelUsageOption(StrictResponse):
    provider: str | None
    model: str | None
    model_key: str
    sessions: int
    turns: int
    usage: UsageBuckets
    estimated_cost_usd: float
    elapsed_seconds: float


class ModelUsageSessionModel(StrictResponse):
    provider: str | None
    model: str | None
    model_key: str
    turns: int
    usage: UsageBuckets
    estimated_cost_usd: float
    pricing: ModelUsagePricing


class ModelUsageDominantModel(StrictResponse):
    provider: str | None
    model: str | None
    basis: str


class ModelUsageSession(StrictResponse):
    id: str
    project: str | None
    title: str | None
    vendor: str | None
    started_at: str | None
    completed_at: str | None
    elapsed_seconds: float
    execution_seconds: float
    wait_seconds: float
    runtime_available: bool
    mixed_models: bool
    usage: UsageBuckets
    context: ModelUsageContext | None
    dominant_model: ModelUsageDominantModel | None
    estimated_cost_usd: float
    models: list[ModelUsageSessionModel]
    turns: list[ModelUsageTurn] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ModelUsageTurn(StrictResponse):
    session_id: str
    turn_id: str
    sequence: int
    started_at: str | None
    provider: str | None
    model: str | None
    model_key: str
    project: str | None = None
    session_title: str | None = None
    vendor: str | None = None
    usage: UsageBuckets
    context: ModelUsageContext | None
    estimated_cost_usd: float
    pricing: ModelUsagePricing


class ModelUsageCohort(StrictResponse):
    since_days: int
    session_graph_count: int
    turn_count: int


class ModelUsageCoverage(StrictResponse):
    total_models: int
    missing_pricing: int


class ModelUsageFilters(StrictResponse):
    since_days: int
    project_name: str | None
    model_key: str | None


class ModelUsageSummary(StrictResponse):
    sessions: int
    turns: int
    models: int
    processed_tokens: int
    total_elapsed_seconds: float
    total_execution_seconds: float
    total_wait_seconds: float
    runtime_eligible: int
    avg_tokens_per_session: float
    avg_tokens_per_turn: float
    avg_elapsed_seconds_per_session: float
    token_stats: ModelUsageTokenStats
    cost_stats: SessionTurnDistributionStats
    elapsed_stats: dict[str, DistributionStats]
    estimated_cost_usd: float
    missing_price_count: int
    top_model_by_cost: str | None
    top_model_by_sessions: str | None


class ModelUsageTimeBucket(StrictResponse):
    bucket: str
    model_key: str
    provider: str | None
    model: str | None
    turns: int
    estimated_cost_usd: float
    usage: UsageBuckets


class ModelUsageWarning(StrictResponse):
    session_id: str
    message: str


class ModelUsagePageMetadata(CursorPageMetadata):
    limit: int


class ModelUsagePages(StrictResponse):
    sessions: ModelUsagePageMetadata | None = None
    turns: ModelUsagePageMetadata | None = None


class ModelUsagePayload(StrictResponse):
    schema_version: Literal[1]
    revision: int
    generated_at: str
    cohort: ModelUsageCohort
    coverage: ModelUsageCoverage
    filters: ModelUsageFilters
    project_options: list[ProjectItem]
    model_options: list[ModelUsageOption]
    summary: ModelUsageSummary
    models: list[ModelUsageModel]
    sessions: list[ModelUsageSession]
    turns: list[ModelUsageTurn]
    time_buckets: dict[str, list[ModelUsageTimeBucket]]
    warnings: list[ModelUsageWarning]
    pages: ModelUsagePages | None = None


class CodeTimeTokens(StrictResponse):
    prompt_tokens: int
    cached_prompt_tokens: int
    cache_write_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    processed_tokens: int


class CodeTimeSession(StrictResponse):
    root_session_id: str
    title: str | None
    vendor: str
    execution_seconds: int
    wait_seconds: int
    turns: int
    tool_calls: int
    tokens: CodeTimeTokens
    cost_usd: float | None


class CodeTimeProject(StrictResponse):
    project_name: str
    session_count: int
    execution_seconds: int
    wait_seconds: int
    turns: int
    tool_calls: int
    tokens: CodeTimeTokens
    cost_usd: float | None
    sessions: list[CodeTimeSession]


class CodeTimeTotals(StrictResponse):
    session_count: int
    project_count: int
    execution_seconds: int
    wait_seconds: int
    turns: int
    tool_calls: int
    tokens: CodeTimeTokens
    cost_usd: float | None


class CodeTimeReport(StrictResponse):
    window: Literal["today", "72h", "7d", "30d"]
    generated_at: str
    totals: CodeTimeTotals
    projects: list[CodeTimeProject]


class CodeTimeForecastsPayload(EstimateListResponse):
    pass


class CalibrationPolicy(StrictResponse):
    version: str
    min_samples: int
    min_compression_samples: int
    within_factor: float
    bootstrap_samples: int
    bootstrap_seed: int
    duration_buckets: list[str]


class CalibrationCohortKey(StrictResponse):
    forecast_kind: Literal[
        "prospective",
        "prospective_unbound",
        "historical_backcast",
        "runtime_advisory",
    ]
    estimator_provider: str | None
    estimator_model: str | None
    estimator_effort: str | None
    prompt_version: str | None
    schema_version: str | None
    retrieval_policy_version: str | None


class CalibrationDefinedStatistic(StrictResponse):
    value: float
    interval_95: list[float] | None = None


class CalibrationUndefinedStatistic(StrictResponse):
    value: Literal["undefined"]
    reason: str


class CalibrationStatistics(StrictResponse):
    sample_count: int
    calibration_ratio: CalibrationDefinedStatistic | CalibrationUndefinedStatistic
    median_absolute_log_error: float | Literal["undefined"]
    within_1_5x_share: float | Literal["undefined"]
    p80_coverage: float | Literal["undefined"]
    compression_exponent: CalibrationDefinedStatistic | CalibrationUndefinedStatistic


class CalibrationBucket(StrictResponse):
    bucket: str
    sample_count: int
    calibration_ratio: float | None = None
    within_1_5x_share: float | None = None
    outcome: str | None = None


class CalibrationCohort(StrictResponse):
    cohort: CalibrationCohortKey
    eligible_count: int
    primary_count: int
    exclusions: dict[str, int]
    statistics: CalibrationStatistics
    buckets: list[CalibrationBucket] = Field(default_factory=list)


class CodeTimeCalibrationPayload(StrictResponse):
    policy: CalibrationPolicy
    cohorts: list[CalibrationCohort]


class IncrementalRefresh(StrictResponse):
    status: Literal["catching_up"]
    revision: int
    reused: bool


class RefreshPayload(StrictResponse):
    status: Literal["refreshed"]
    incremental: IncrementalRefresh | None = None


API_RESPONSE_MODELS = (
    OverviewPayload,
    TodayPayload,
    ProjectsPayload,
    ProjectDetailPayload,
    SessionPage,
    SessionTimelinePayload,
    ContextWindowPayload,
    SessionEvidenceTimelinePayload,
    TokenEfficiencyProjectPayload,
    SessionGraphPayload,
    SessionTreePayload,
    SessionEventDetailsPayload,
    SessionItemDetailsPayload,
    ModelUsagePayload,
    CodeTimeReport,
    CodeTimeForecastsPayload,
    CodeTimeCalibrationPayload,
    RefreshPayload,
    DatahubSnapshot,
    DatahubChanges,
)

API_RESPONSE_BY_HANDLER: dict[str, type[BaseModel]] = {
    "overview": OverviewPayload,
    "today": TodayPayload,
    "projects": ProjectsPayload,
    "project_detail": ProjectDetailPayload,
    "sessions": SessionPage,
    "session_timeline": SessionTimelinePayload,
    "context_window": ContextWindowPayload,
    "session_evidence_timeline": SessionEvidenceTimelinePayload,
    "token_efficiency_project": TokenEfficiencyProjectPayload,
    "graph_detail": SessionGraphPayload,
    "session_tree": SessionTreePayload,
    "session_event_details": SessionEventDetailsPayload,
    "session_item_details": SessionItemDetailsPayload,
    "model_usage": ModelUsagePayload,
    "code_time_report": CodeTimeReport,
    "code_time_forecasts": CodeTimeForecastsPayload,
    "code_time_calibration": CodeTimeCalibrationPayload,
    "request_refresh": RefreshPayload,
    "snapshot": DatahubSnapshot,
    "changes": DatahubChanges,
}


def validate_api_response(handler: str, payload: Any) -> None:
    """Check generated route contracts without rewriting the wire payload."""

    model = API_RESPONSE_BY_HANDLER.get(handler)
    if model is not None:
        model.model_validate(payload)
