"""Typed local query contracts with complete collections by default."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from coding_trajectory.contracts.base import ContractModel, RequestModel
from coding_trajectory.contracts.overview import OverviewResponse
from coding_trajectory.ingestion.execution_models import (
    ExecutionComposition,
    TurnExecutionLayer,
)


class SessionScopedRequest(RequestModel):
    """Session entry point: ``session_id``; ``turn_id`` is subordinate only."""

    session_id: str
    turn_id: str | None = None

    @field_validator("turn_id")
    @classmethod
    def canonical_turn_id(cls, value: str | None) -> str | None:
        return str(UUID(value)) if value is not None else None


class GraphScopedRequest(RequestModel):
    """Graph entry point: ``root_session_id`` is required."""

    root_session_id: str


class ProjectListRequest(RequestModel):
    project_id: str | None = Field(default=None, pattern=r"^[0-9a-fA-F-]{36}$")
    project_name: str | None = None
    modified_since: datetime | None = None
    agent_vendor: str | None = None
    limit: int | None = Field(default=None, ge=1)

    @field_validator("project_id")
    @classmethod
    def canonical_project_id(cls, value: str | None) -> str | None:
        return str(UUID(value)) if value is not None else None

    @model_validator(mode="after")
    def one_project_selector(self) -> ProjectListRequest:
        if self.project_id is not None and self.project_name is not None:
            raise ValueError("use project_id or project_name, not both")
        return self


class ProjectSessionsRequest(ProjectListRequest):
    pass


class SessionOverviewRequest(RequestModel):
    session_id: str
    limit: int | None = Field(default=None, ge=1)


class SessionSummaryRequest(SessionScopedRequest):
    pass


SearchKind = Literal[
    "user_message",
    "assistant_message",
    "tool_call",
    "tool_result",
    "file_change",
]

DEFAULT_SEARCH_KINDS: list[SearchKind] = [
    "user_message",
    "assistant_message",
    "tool_call",
    "tool_result",
    "file_change",
]

#: Normalized local content and semantic fields matched by session.search.
SEARCHABLE_FIELDS: list[str] = [
    "text",
    "input",
    "command",
    "output",
    "text_preview",
    "path",
    "tool_name",
    "concept",
    "target",
    "operation",
    "status",
    "outcome",
    "verification_kind",
    "evidence_facts",
]


class SessionSearchRequest(SessionScopedRequest):
    query: str = Field(min_length=1)
    mode: Literal["text", "path"] = "text"
    kinds: list[SearchKind] = Field(default_factory=lambda: list(DEFAULT_SEARCH_KINDS))
    limit: int | None = Field(default=None, ge=1)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("query must contain non-whitespace text")
        return normalized


class SessionTreeRequest(RequestModel):
    session_id: str


class GraphOverviewRequest(GraphScopedRequest):
    limit: int | None = Field(default=None, ge=1)


class SessionStatsRequest(RequestModel):
    session_id: str


class GraphStatsRequest(GraphScopedRequest):
    pass


class SessionUsageRequest(SessionScopedRequest):
    pass


class GraphUsageRequest(GraphScopedRequest):
    pass


class SessionModelUsageRequest(SessionScopedRequest):
    pass


class SessionRequestUsageRequest(SessionScopedRequest):
    limit: int | None = Field(default=None, ge=1)


class SessionToolUsageRequest(SessionScopedRequest):
    limit: int | None = Field(default=None, ge=1)


class SessionEventsRequest(SessionScopedRequest):
    event_ids: list[str] | None = None
    item_id: str | None = None
    types: list[str] | None = None
    status: str | None = None
    tool_name: str | None = None
    limit: int | None = Field(default=None, ge=1)


class SessionItemsRequest(SessionScopedRequest):
    item_ids: list[str] | None = None
    types: list[str] | None = None
    limit: int | None = Field(default=None, ge=1)


class ProjectSummary(ContractModel):
    project_id: str
    display_name: str
    path: str | None = None
    vendors: list[str] = Field(default_factory=list)
    sessions: list[dict[str, Any]] | None = None


class CollectionResponse(ContractModel):
    total: int = Field(ge=0)
    returned: int = Field(ge=0)


class ProjectListResponse(CollectionResponse):
    items: list[ProjectSummary]


class SessionGraphSummary(ContractModel):
    graph_id: str | None = None
    root_session_id: str
    lineage_root_session_id: str | None = None
    project_id: str | None = None
    project: str | None = None
    title: str | None = None
    preview: str | None = None
    vendors: list[str] = Field(default_factory=list)
    session_ids: list[str] = Field(default_factory=list)
    modified: datetime | None = None


class ProjectSessionsResponse(CollectionResponse):
    items: list[SessionGraphSummary]


class SessionOverviewResponse(OverviewResponse):
    pass


class EvidenceReferences(ContractModel):
    script_id: str | None = None
    session_id: str
    turn_id: str | None = None
    item_id: str | None = None
    event_ids: list[str] = Field(default_factory=list)


class ProjectionIdentity(ContractModel):
    name: str
    version: int = Field(ge=1)
    strategy: str


class ProjectionCoverage(ContractModel):
    """Content availability and selection completeness for local projections."""

    retention: Literal["not_applicable", "not_retained", "preview", "complete"]
    searchable: Literal["complete", "preview", "facts_only", "none"] | None = None
    searched_resources: int | None = Field(default=None, ge=0)
    trimmed: bool = False


class SummaryClaim(ContractModel):
    text: str
    references: EvidenceReferences


class SummaryChange(ContractModel):
    path: str
    operations: list[str] = Field(default_factory=list)
    references: EvidenceReferences


class SummaryEvidence(ContractModel):
    label: str
    status: str
    references: EvidenceReferences


class SummaryActivity(ContractModel):
    kind: str
    label: str
    status: str | None = None
    references: EvidenceReferences


class TruncationStatus(ContractModel):
    total: int = Field(ge=0)
    truncated: bool


class SessionSummaryResponse(ContractModel):
    execution_units: list[TurnExecutionLayer] = Field(default_factory=list)
    execution_composition: ExecutionComposition | None = None
    session_id: str
    selected_turn_id: str | None = None
    latest_turn_status: str | None = None
    objective: SummaryClaim | None = None
    decisions: list[SummaryClaim] = Field(default_factory=list)
    changes: list[SummaryChange] = Field(default_factory=list)
    verification: list[SummaryEvidence] = Field(default_factory=list)
    unresolved: list[SummaryEvidence] = Field(default_factory=list)
    next_actions: list[SummaryClaim] = Field(default_factory=list)
    recent_activity: list[SummaryActivity] = Field(default_factory=list)
    truncation: dict[str, TruncationStatus] = Field(default_factory=dict)
    projection: ProjectionIdentity
    coverage: ProjectionCoverage
    warnings: list[str] = Field(default_factory=list)


class SearchQuery(ContractModel):
    text: str
    mode: Literal["text", "path"]
    kinds: list[SearchKind] = Field(default_factory=list)


class SearchMatch(ContractModel):
    rank: int = Field(ge=1)
    score: float
    kind: SearchKind
    timestamp: datetime
    label: str
    snippet: str
    matched_fields: list[str] = Field(default_factory=list)
    references: EvidenceReferences


class SessionSearchResponse(ContractModel):
    execution_units: list[TurnExecutionLayer] = Field(default_factory=list)
    execution_composition: ExecutionComposition | None = None
    session_id: str
    selected_turn_id: str | None = None
    query: SearchQuery
    matches: list[SearchMatch] = Field(default_factory=list)
    total: int = Field(ge=0)
    truncated: bool
    searchable_fields: list[str] = Field(default_factory=list)
    projection: ProjectionIdentity
    coverage: ProjectionCoverage
    warnings: list[str] = Field(default_factory=list)


class SessionTreeResponse(ContractModel):
    root_session_id: str
    branches: list[dict[str, Any]] = Field(default_factory=list)


class GraphOverviewResponse(OverviewResponse):
    pass


class SessionStatsResponse(ContractModel):
    execution_units: list[TurnExecutionLayer] = Field(default_factory=list)
    execution_composition: ExecutionComposition | None = None
    root_session_id: str | None = None
    scope: str | None = None
    vendor: str | None = None
    model: dict[str, Any] = Field(default_factory=dict)
    context_window: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)
    messages: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    billed_token_usage: dict[str, Any] | None = None
    provider_usage_buckets: list[dict[str, Any]] = Field(default_factory=list)
    sessions: list[dict[str, Any]] | None = None
    warnings: list[str] = Field(default_factory=list)


class SessionUsageResponse(ContractModel):
    execution_units: list[TurnExecutionLayer] = Field(default_factory=list)
    execution_composition: ExecutionComposition | None = None
    session_id: str
    scope: str | None = None
    selected_turn_id: str | None = None
    total_usage: dict[str, Any]
    runtime: dict[str, Any] = Field(default_factory=dict)
    turns: list[dict[str, Any]] | None = None
    sessions: list[dict[str, Any]] | None = None
    models: list[dict[str, Any]] = Field(default_factory=list)
    estimated_cost: dict[str, Any] | None = None
    compaction: dict[str, Any] | None = None
    effort_changes: dict[str, Any] | None = None
    cache_attribution: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)


class SessionModelUsageResponse(ContractModel):
    execution_units: list[TurnExecutionLayer] = Field(default_factory=list)
    execution_composition: ExecutionComposition | None = None
    root_session_id: str
    vendor: str | None = None
    project: str | None = None
    title: str | None = None
    started_at: Any | None = None
    completed_at: Any | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    model_active_seconds: float | None = None
    processed_tokens_per_second: float | None = None
    context: dict[str, Any] | None = None
    models: list[dict[str, Any]] = Field(default_factory=list)
    dominant_model: dict[str, Any] | None = None
    turns: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SessionRequestUsageResponse(CollectionResponse):
    execution_units: list[TurnExecutionLayer] = Field(default_factory=list)
    execution_composition: ExecutionComposition | None = None
    root_session_id: str
    request_count: int = 0
    usage: dict[str, Any] = Field(default_factory=dict)
    estimated_cost: dict[str, Any] | None = None
    requests: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SessionToolUsageResponse(CollectionResponse):
    execution_units: list[TurnExecutionLayer] = Field(default_factory=list)
    execution_composition: ExecutionComposition | None = None
    root_session_id: str
    tool_item_count: int = 0
    tool_output_chars: int = 0
    tool_output_original_tokens: int = 0
    allocated_real_token_cost: dict[str, Any] | None = None
    item_real_token_costs: list[dict[str, Any]] | None = None
    tool_items: list[dict[str, Any]] = Field(default_factory=list)
    attribution_policy: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class CanonicalProvenance(ContractModel):
    source: str
    method: str
    confidence: Literal["high", "medium", "low"]


class CanonicalResourceCoverage(ContractModel):
    """Available content and evidence quality for normalized local resources."""

    retention: Literal["not_applicable", "not_retained", "preview", "complete"]
    measurement: Literal["complete", "partial", "none"]
    searchable: Literal["complete", "preview", "facts_only", "none"]
    hierarchy: Literal["complete", "partial"]
    lifecycle: Literal["complete", "partial"]


class ToolOutputEvidence(ContractModel):
    """Public mirror of the retained per-item output evidence fact."""

    processor: str
    processor_version: int = Field(ge=1)
    lifecycle: Literal["completed", "failed", "interrupted", "unknown"]
    outcome: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    output_chars: int = Field(default=0, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    token_method: Literal["provider_reported", "tokenizer_estimate", "not_measured"]
    tokenizer: str | None = None
    provider: str | None = None
    truncated: bool = False
    original_tokens: int | None = Field(default=None, ge=0)
    facts: dict[str, bool | int | str] | None = None
    preview: str | None = None
    source_event_ids: list[str] = Field(default_factory=list)
    retention: Literal["not_applicable", "not_retained", "preview", "complete"]
    searchable: Literal["complete", "preview", "facts_only", "none"]


class CanonicalItemDetail(ContractModel):
    """Normalized item content and semantic facts from local source evidence."""

    text: str | None = None
    input: Any | None = None
    output: Any | None = None
    command: Any | None = None
    tool_name: str | None = None
    concept: str | None = None
    target_kind: (
        Literal["file", "search", "command", "web", "coordination", "tool"] | None
    ) = None
    target: str | None = None
    path: str | None = None
    operation: str | None = None
    exit_code: int | None = None
    verification_kind: str | None = None
    target_session_id: str | None = None
    resolution_key: str | None = None


class ItemContentMeasurements(ContractModel):
    input_chars: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_chars: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    text_chars: int = Field(default=0, ge=0)
    text_tokens: int = Field(default=0, ge=0)


class CanonicalItemRecord(ContractModel):
    script_id: str | None = None
    item_id: str
    session_id: str
    turn_id: str
    event_ids: list[str] = Field(default_factory=list)
    source_sequence: int = Field(ge=0)
    source_order_key: str
    started_at: datetime
    completed_at: datetime | None = None
    kind: Literal[
        "agent_message",
        "tool_call",
        "command_execution",
        "file_change",
        "reasoning",
        "plan",
    ]
    operation: str | None = None
    status: str | None = None
    projection_parent_item_id: str | None = Field(
        default=None,
        description="Reconstructed wrapper parent; null means unknown.",
    )
    nested_index: int | None = Field(
        default=None,
        ge=0,
        description="Reconstructed child position within the wrapper.",
    )
    projection_only: bool | None = Field(
        default=None,
        description="Whether the item is a semantic projection, not content custody.",
    )
    projection_provenance: CanonicalProvenance | None = None
    provenance: CanonicalProvenance
    coverage: CanonicalResourceCoverage
    type: str | None = None
    operations: list[str] | None = None
    preview: str | None = None
    detail: CanonicalItemDetail | None = None
    measurements: ItemContentMeasurements | None = None
    output_evidence: ToolOutputEvidence | None = None


class CanonicalEventUsage(ContractModel):
    """Native numeric usage measurements for one usage-observation event."""

    model: str | None = None
    provider: str | None = None
    source: str | None = None
    context_window_tokens: int | None = Field(default=None, ge=0)
    used_input_tokens: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    uncached_input_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = None


class CanonicalEventRecord(ContractModel):
    script_id: str | None = None
    """Normalized local event content and references with native usage."""

    event_id: str
    session_id: str
    turn_id: str | None = None
    item_id: str | None = None
    timestamp: datetime
    type: str
    status: str | None = None
    source_sequence: int = Field(ge=0)
    source_order_key: str
    provenance: CanonicalProvenance
    coverage: CanonicalResourceCoverage
    content: dict[str, Any] = Field(default_factory=dict)
    output_evidence_id: str | None = None
    usage: CanonicalEventUsage | None = None


class SessionEventsResponse(CollectionResponse):
    execution_units: list[TurnExecutionLayer] = Field(default_factory=list)
    execution_composition: ExecutionComposition | None = None
    root_session_id: str | None = None
    events: list[CanonicalEventRecord] = Field(default_factory=list)
    unresolved_ids: list[str] = Field(default_factory=list)
    coverage: ProjectionCoverage | None = None


class CliSessionEventsResponse(ContractModel):
    execution_units: list[TurnExecutionLayer] = Field(default_factory=list)
    execution_composition: ExecutionComposition | None = None
    id: str | None = None
    matches: list[dict[str, Any]] = Field(default_factory=list)


class SessionItemsResponse(CollectionResponse):
    execution_units: list[TurnExecutionLayer] = Field(default_factory=list)
    execution_composition: ExecutionComposition | None = None
    root_session_id: str | None = None
    items: list[CanonicalItemRecord] = Field(default_factory=list)
    unresolved_ids: list[str] = Field(default_factory=list)
    coverage: ProjectionCoverage | None = None


class CliCanonicalItemRecord(ContractModel):
    script_id: str | None = None
    id: str
    session: str
    turn: str
    kind: str
    operation: str | None = None
    status: str | None = None
    projection_parent_item_id: str | None = Field(
        default=None,
        description="Reconstructed wrapper parent; null means unknown.",
    )
    nested_index: int | None = Field(
        default=None,
        ge=0,
        description="Reconstructed child position within the wrapper.",
    )
    projection_only: bool | None = Field(
        default=None,
        description="Whether the item is a semantic projection, not content custody.",
    )
    projection_provenance: CanonicalProvenance | None = None
    source_sequence: int = Field(ge=0)
    source_order_key: str
    started_at: datetime
    completed_at: datetime | None = None
    provenance: CanonicalProvenance
    coverage: CanonicalResourceCoverage
    type: str | None = None
    operations: list[str] | None = None
    preview: str | None = None
    detail: CanonicalItemDetail | None = None
    measurements: ItemContentMeasurements | None = None
    output_evidence: ToolOutputEvidence | None = None
    events: list[str] = Field(default_factory=list)


class CliSessionItemsResponse(ContractModel):
    execution_units: list[TurnExecutionLayer] = Field(default_factory=list)
    execution_composition: ExecutionComposition | None = None
    id: str | None = None
    items: list[CliCanonicalItemRecord] = Field(default_factory=list)


class CliSessionGraphSummary(ContractModel):
    graph_id: str | None = None
    id: str
    project_id: str | None = None
    project: str | None = None
    title: str | None = None
    vendors: list[str] = Field(default_factory=list)
    sessions: list[str] = Field(default_factory=list)


class CliProjectSessionsResponse(ContractModel):
    items: list[CliSessionGraphSummary]


class CliSessionOverviewResponse(ContractModel):
    id: str
    sessions: list[dict[str, Any]] = Field(default_factory=list)


class CliSessionStatsResponse(ContractModel):
    execution_units: list[TurnExecutionLayer] = Field(default_factory=list)
    execution_composition: ExecutionComposition | None = None
    id: str
    scope: str | None = None
    vendor: str | None = None
    model: dict[str, Any] | None = None
    context_window: dict[str, Any] | None = None
    provider_usage_buckets: list[dict[str, Any]] | None = None
    runtime: dict[str, Any] | None = None
    messages: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    billed_token_usage: dict[str, Any] | None = None
    sessions: list[dict[str, Any]] | None = None
    warnings: list[str] = Field(default_factory=list)


class CliSessionUsageResponse(ContractModel):
    execution_units: list[TurnExecutionLayer] = Field(default_factory=list)
    execution_composition: ExecutionComposition | None = None
    id: str
    scope: str | None = None
    runtime: dict[str, Any] | None = None
    usage: dict[str, Any]
    turns: list[dict[str, Any]] = Field(default_factory=list)
    sessions: list[dict[str, Any]] | None = None
    warnings: list[str] = Field(default_factory=list)
