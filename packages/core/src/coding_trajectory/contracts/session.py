"""Contracts for the project.*, session.*, and graph.* service methods."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, RootModel, field_validator, model_validator

from coding_trajectory.contracts.base import ContractModel, RequestModel


class SessionEntryRequest(RequestModel):
    session_id: str | None = None
    root_session_id: str | None = None
    turn_id: str | None = None

    @model_validator(mode="after")
    def require_entrypoint(self) -> SessionEntryRequest:
        if not (self.session_id or self.root_session_id or self.turn_id):
            raise ValueError("session_id, root_session_id, or turn_id is required")
        return self


class ProjectListRequest(RequestModel):
    project_name: str | None = None
    since_days: int | None = Field(default=None, ge=1)
    modified_since: datetime | None = None
    agent_vendor: str | None = None


class ProjectSessionsRequest(RequestModel):
    project_name: str | None = None
    since_days: int | None = Field(default=None, ge=1)
    modified_since: datetime | None = None
    agent_vendor: str | None = None
    include: list[Literal["runtime", "usage"]] = Field(default_factory=list)


class SessionOverviewRequest(SessionEntryRequest):
    num_turns: int | None = Field(default=None, ge=1)
    drop_turns: int | None = Field(default=None, ge=1)


class CanonicalSessionRequest(RequestModel):
    session_id: str
    turn_id: str | None = None


class SessionSummaryRequest(CanonicalSessionRequest):
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


class SessionSearchRequest(CanonicalSessionRequest):
    query: str = Field(min_length=1, max_length=1000)
    mode: Literal["text", "path"] = "text"
    kinds: list[SearchKind] = Field(
        default_factory=lambda: list(DEFAULT_SEARCH_KINDS)
    )
    limit: int = Field(default=20, ge=1, le=50)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("query must contain non-whitespace text")
        return normalized


class SessionTreeRequest(SessionEntryRequest):
    pass


class GraphOverviewRequest(SessionOverviewRequest):
    include: list[Literal["narrative"]] = Field(default_factory=list)


class SessionStatsRequest(SessionEntryRequest):
    pass


class GraphStatsRequest(SessionStatsRequest):
    include: list[Literal["session_composition"]] = Field(default_factory=list)


class SessionUsageRequest(SessionEntryRequest):
    pass


class GraphUsageRequest(SessionUsageRequest):
    include: list[Literal["flat_turns"]] = Field(default_factory=list)


class SessionModelUsageRequest(SessionEntryRequest):
    pass


class SessionRequestUsageRequest(SessionEntryRequest):
    include: list[Literal["causality", "context"]] = Field(default_factory=list)


class SessionToolUsageRequest(SessionEntryRequest):
    include: list[Literal["causality", "item_costs"]] = Field(default_factory=list)


class SessionEventsRequest(SessionEntryRequest):
    event_ids: list[str] | None = None
    type: str | None = None
    filters: list[str] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def require_entrypoint(self) -> SessionEventsRequest:
        if not (
            self.session_id or self.root_session_id or self.turn_id or self.event_ids
        ):
            raise ValueError(
                "session_id, root_session_id, turn_id, or event_ids is required"
            )
        return self


class SessionItemsRequest(SessionEntryRequest):
    item_ids: list[str] | None = None
    types: list[str] | None = None
    include_content: bool = False

    @model_validator(mode="after")
    def require_entrypoint(self) -> SessionItemsRequest:
        # NOTE: turn_id is accepted as a field but does not satisfy the
        # entrypoint requirement; preserved from the pre-split contract.
        if not (self.session_id or self.root_session_id or self.item_ids):
            raise ValueError("session_id, root_session_id, or item_ids is required")
        return self


class ProjectSummary(ContractModel):
    path: str | None = None
    vendors: list[str] = Field(default_factory=list)
    sessions: list[dict[str, Any]] | None = None


class ProjectListResponse(ContractModel):
    items: dict[str, ProjectSummary]


class SessionGraphSummary(ContractModel):
    graph_id: str | None = None
    root_session_id: str
    project: str | None = None
    title: str | None = None
    preview: str | None = None
    vendors: list[str] = Field(default_factory=list)
    session_ids: list[str] = Field(default_factory=list)
    runtime: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    warnings: list[str] | None = None


class ProjectSessionsResponse(ContractModel):
    items: list[SessionGraphSummary]


class SessionOverviewResponse(ContractModel):
    root_session_id: str
    sessions: list[dict[str, Any]]


class EvidenceReferences(ContractModel):
    session_id: str
    turn_id: str | None = None
    item_id: str | None = None
    event_ids: list[str] = Field(default_factory=list)


class ProjectionIdentity(ContractModel):
    name: str
    version: int = Field(ge=1)
    strategy: str


class ProjectionCoverage(ContractModel):
    retention: Literal["trajectory", "measurements"]
    content_complete: bool
    searched_resources: int | None = Field(default=None, ge=0)


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
    status: str
    references: EvidenceReferences


class TruncationStatus(ContractModel):
    total: int = Field(ge=0)
    truncated: bool


class SessionSummaryResponse(ContractModel):
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
    session_id: str
    selected_turn_id: str | None = None
    query: SearchQuery
    matches: list[SearchMatch] = Field(default_factory=list)
    total: int = Field(ge=0)
    truncated: bool
    projection: ProjectionIdentity
    coverage: ProjectionCoverage
    warnings: list[str] = Field(default_factory=list)


class SessionTreeResponse(ContractModel):
    root_session_id: str
    branches: list[dict[str, Any]] = Field(default_factory=list)


class GraphOverviewResponse(ContractModel):
    graph_id: str
    root_session_id: str
    project: str | None = None
    graph: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] | None = None
    sessions: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)


class SessionStatsResponse(ContractModel):
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
    warnings: list[str] = Field(default_factory=list)


class SessionModelUsageResponse(ContractModel):
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


class SessionRequestUsageResponse(ContractModel):
    root_session_id: str
    request_count: int = 0
    usage: dict[str, Any] = Field(default_factory=dict)
    estimated_cost: dict[str, Any] | None = None
    requests: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SessionToolUsageResponse(ContractModel):
    root_session_id: str
    tool_item_count: int = 0
    tool_output_chars: int = 0
    tool_output_original_tokens: int = 0
    allocated_real_token_cost: dict[str, Any] | None = None
    item_real_token_costs: list[dict[str, Any]] | None = None
    tool_items: list[dict[str, Any]] = Field(default_factory=list)
    attribution_policy: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class SessionEventsResponse(ContractModel):
    root_session_id: str | None = None
    type: str | None = None
    matches: list[dict[str, Any]] = Field(default_factory=list)


class CliSessionEventsResponse(ContractModel):
    id: str | None = None
    type: str | None = None
    matches: list[dict[str, Any]] = Field(default_factory=list)


class SessionItemsResponse(RootModel[list[dict[str, Any]]]):
    pass


class CliSessionGraphSummary(ContractModel):
    graph_id: str | None = None
    id: str
    project: str | None = None
    title: str | None = None
    vendors: list[str] = Field(default_factory=list)
    sessions: list[str] = Field(default_factory=list)
    runtime: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    warnings: list[str] | None = None


class CliProjectSessionsResponse(ContractModel):
    items: list[CliSessionGraphSummary]


class CliSessionOverviewResponse(ContractModel):
    id: str
    sessions: list[dict[str, Any]] = Field(default_factory=list)


class CliSessionStatsResponse(ContractModel):
    id: str
    scope: str | None = None
    vendor: str | None = None
    model: dict[str, Any] | None = None
    context: dict[str, Any] | None = None
    provider_usage_buckets: list[dict[str, Any]] | None = None
    runtime: dict[str, Any] | None = None
    messages: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    billed_token_usage: dict[str, Any] | None = None
    sessions: list[dict[str, Any]] | None = None
    warnings: list[str] | None = None


class CliSessionUsageResponse(ContractModel):
    id: str
    scope: str | None = None
    runtime: dict[str, Any] | None = None
    usage: dict[str, Any]
    turns: list[dict[str, Any]] = Field(default_factory=list)
    sessions: list[dict[str, Any]] | None = None
    warnings: list[str] | None = None
