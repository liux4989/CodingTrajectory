"""Versioned Pydantic contracts for the public ct service methods."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, RootModel, TypeAdapter, model_validator


class ContractModel(BaseModel):
    """Response base that permits additive fields within one method version."""

    model_config = ConfigDict(extra="allow")


class RequestModel(BaseModel):
    """Strict request base; parameter changes require a method-version bump."""

    model_config = ConfigDict(extra="forbid")


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


class SessionEventsRequest(RequestModel):
    session_id: str | None = None
    root_session_id: str | None = None
    turn_id: str | None = None
    event_ids: list[str] | None = None
    type: str | None = None
    filters: list[str] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def require_scope_or_event_ids(self) -> SessionEventsRequest:
        if not (
            self.session_id
            or self.root_session_id
            or self.turn_id
            or self.event_ids
        ):
            raise ValueError(
                "session_id, root_session_id, turn_id, or event_ids is required"
            )
        return self


class SessionItemsRequest(RequestModel):
    item_ids: list[str] | None = None
    session_id: str | None = None
    root_session_id: str | None = None
    turn_id: str | None = None
    types: list[str] | None = None
    include_content: bool = False

    @model_validator(mode="after")
    def require_scope_or_item_ids(self) -> SessionItemsRequest:
        if not (self.session_id or self.root_session_id or self.item_ids):
            raise ValueError("session_id, root_session_id, or item_ids is required")
        return self


class LivingScope(RequestModel):
    """Optional hierarchy scope for living-event snapshot and delta reads."""

    root_session_id: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None


class LivingEventsRequest(RequestModel):
    """Frozen ``ct.living_events.v1`` request contract."""

    mode: Literal["view", "details"] = "view"
    scope: LivingScope = Field(default_factory=LivingScope)
    after: str | None = Field(default=None, max_length=4096)
    through: str | None = Field(default=None, max_length=4096)
    limit: int = Field(default=50, ge=1, le=200)


class LivingSessionsRequest(RequestModel):
    """``ct.living_sessions.v2`` global inventory request contract."""

    after: str | None = Field(default=None, max_length=4096)
    through: str | None = Field(default=None, max_length=4096)
    limit: int = Field(default=50, ge=1, le=200)


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


class LivingResourcePath(ContractModel):
    root_session_id: str
    session_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    context_checkpoint_id: str | None = None
    edge_id: str | None = None
    source_session_id: str | None = None
    target_session_id: str | None = None


class LivingContentReferenceTarget(ContractModel):
    session_id: str
    turn_id: str
    item_id: str
    event_ids: list[str] = Field(default_factory=list)
    field_path: str | None = None


class LivingContentReference(ContractModel):
    model_config = ConfigDict(extra="allow", serialize_by_alias=True)

    type: Literal["content_ref"] = Field(alias="$type")
    size_chars: int = Field(ge=0)
    ref: LivingContentReferenceTarget


class LivingInlineContent(ContractModel):
    state: Literal["inline"]
    value: Any
    size_chars: int = Field(ge=0)
    ref: None = None


class LivingUserRequest(ContractModel):
    type: Literal["message", "command"]
    source: str
    content: LivingInlineContent


class LivingSourceCheckpoint(ContractModel):
    path: str
    file_identity: str | None = None
    size: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)
    committed_offset: int = Field(ge=0)
    trailing_bytes: int = Field(ge=0)
    status: Literal["ready", "partial", "error", "deleted"]
    error: str | None = None
    last_success_revision: int | None = Field(default=None, ge=0)


class LivingSessionResource(ContractModel):
    session_id: str
    root_session_id: str
    vendor: str
    model: str | None = None
    reasoning_effort: str | None = None
    status: str
    latest_turn_status: str | None = None
    agent_name: str | None = None
    cwd: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    turn_count: int = Field(ge=0)
    item_count: int = Field(ge=0)
    context_checkpoint_count: int = Field(ge=0)
    source_checkpoint: LivingSourceCheckpoint | None = None


class LivingTurnResource(ContractModel):
    turn_id: str
    session_id: str
    sequence: int = Field(ge=0)
    status: str
    started_at: datetime
    ended_at: datetime | None = None
    preceding_context_checkpoint_id: str | None = None
    user_request: LivingUserRequest | None = None
    assistant_responses: list[LivingInlineContent] = Field(default_factory=list)
    activity: list[dict[str, Any]] = Field(default_factory=list)
    item_count: int = Field(ge=0)


class LivingItemResource(ContractModel):
    item_id: str
    session_id: str
    turn_id: str
    kind: str
    type: str
    operations: list[str] | None = None
    shape: dict[str, LivingContentReference | Any] | None = None
    event_ids: list[str] = Field(default_factory=list)


class LivingContextCheckpointResource(ContractModel):
    context_checkpoint_id: str
    session_id: str
    sequence: int = Field(ge=1)
    timestamp: datetime
    mechanism: str
    trigger: str | None = None
    pre_tokens: int | None = Field(default=None, ge=0)
    post_tokens: int | None = Field(default=None, ge=0)
    dropped_tokens: int | None = Field(default=None, ge=0)
    effective_after_turn_id: str | None = None
    effective_before_turn_id: str | None = None
    source_event_ids: list[str] = Field(default_factory=list)


class LivingSessionEdgeResource(ContractModel):
    edge_id: str
    type: str
    source_session_id: str
    target_session_id: str
    source_turn_id: str | None = None
    source_item_id: str | None = None
    source_event_id: str | None = None
    provenance: Literal["observed", "derived"]
    confidence: Literal["high", "medium", "low"]
    evidence_event_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None


LivingResource = (
    LivingSessionResource
    | LivingTurnResource
    | LivingItemResource
    | LivingContextCheckpointResource
    | LivingSessionEdgeResource
)


class LivingChange(ContractModel):
    cursor: str
    revision: int = Field(ge=0)
    operation: Literal["upsert", "remove", "reset"]
    resource_kind: Literal[
        "session",
        "turn",
        "item",
        "context_checkpoint",
        "session_edge",
    ]
    path: LivingResourcePath
    resource: LivingResource | None = None
    reason: str | None = None


class LivingProtocolIssue(ContractModel):
    severity: Literal["warning", "error"]
    code: str
    message: str
    path: LivingResourcePath | None = None


class LivingEventsResponse(ContractModel):
    schema_version: Literal["ct.living_events.v1"]
    mode: Literal["view", "details"]
    page_kind: Literal["snapshot", "delta"]
    through: str
    next_cursor: str | None = None
    has_more: bool
    changes: list[LivingChange] = Field(default_factory=list)
    issues: list[LivingProtocolIssue] = Field(default_factory=list)


class LivingSessionReadiness(ContractModel):
    status: Literal["ready", "partial", "error", "deleted", "unknown"]
    committed_offset: int | None = Field(default=None, ge=0)
    size: int | None = Field(default=None, ge=0)
    mtime_ns: int | None = Field(default=None, ge=0)


class LivingSessionInventoryResource(ContractModel):
    session_id: str
    root_session_id: str
    vendor: str
    model: str | None = None
    reasoning_effort: str | None = None
    cwd: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    latest_activity_at: datetime | None = None
    state: Literal["living", "not_living"]
    latest_turn_status: Literal["running", "interrupted", "completed", "incomplete"] | None = None
    source_readiness: LivingSessionReadiness


class LivingSessionsChange(ContractModel):
    cursor: str
    revision: int = Field(ge=0)
    operation: Literal["upsert", "remove", "reset"]
    resource_kind: Literal["session"]
    path: LivingResourcePath
    resource: LivingSessionInventoryResource | None = None
    reason: str | None = None


class LivingSessionsResponse(ContractModel):
    schema_version: Literal["ct.living_sessions.v2"]
    mode: Literal["view"]
    page_kind: Literal["snapshot", "delta"]
    through: str
    next_cursor: str | None = None
    has_more: bool
    changes: list[LivingSessionsChange] = Field(default_factory=list)
    issues: list[LivingProtocolIssue] = Field(default_factory=list)


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


ResultT = TypeVar("ResultT")


class ApiEnvelopeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApiSuccessResponse(ApiEnvelopeModel, Generic[ResultT]):
    id: Any
    method: str
    ok: Literal[True]
    result: ResultT


class ApiErrorDetail(ApiEnvelopeModel):
    message: str


class ApiErrorResponse(ApiEnvelopeModel):
    id: Any
    method: Any
    ok: Literal[False]
    error: ApiErrorDetail


@dataclass(frozen=True)
class ServiceContract:
    method: str
    version: int
    request_model: type[BaseModel]
    response_model: type[BaseModel]
    cli_response_model: type[BaseModel] | None = None

    schema_version: ClassVar[int] = 2

    def validate_request(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.request_model.model_validate(params).model_dump(
            mode="python",
            exclude_none=True,
        )

    def validate_response(self, payload: Any) -> Any:
        return self.response_model.model_validate(payload).model_dump(
            mode="json",
            exclude_none=True,
        )

    def validate_cli_response(self, payload: Any) -> Any:
        model = self.cli_response_model or self.response_model
        return model.model_validate(payload).model_dump(
            mode="json",
            exclude_none=True,
        )

    def schema(self, *, command: str) -> dict[str, Any]:
        schema = {
            "schema_version": self.schema_version,
            "command": command,
            "method": self.method,
            "method_version": self.version,
            "request": self.request_model.model_json_schema(),
            "response": TypeAdapter(
                ApiSuccessResponse[self.response_model] | ApiErrorResponse
            ).json_schema(),
            "result": self.response_model.model_json_schema(),
        }
        if self.cli_response_model is not None:
            schema["cli_response"] = self.cli_response_model.model_json_schema()
        return schema


SERVICE_CONTRACTS = {
    contract.method: contract
    for contract in (
        ServiceContract("project.list", 2, ProjectListRequest, ProjectListResponse),
        ServiceContract(
            "project.sessions",
            2,
            ProjectSessionsRequest,
            ProjectSessionsResponse,
            CliProjectSessionsResponse,
        ),
        ServiceContract(
            "session.overview",
            2,
            SessionOverviewRequest,
            SessionOverviewResponse,
            CliSessionOverviewResponse,
        ),
        ServiceContract("session.tree", 2, SessionTreeRequest, SessionTreeResponse),
        ServiceContract(
            "graph.overview",
            2,
            GraphOverviewRequest,
            GraphOverviewResponse,
        ),
        ServiceContract(
            "session.stats",
            2,
            SessionStatsRequest,
            SessionStatsResponse,
            CliSessionStatsResponse,
        ),
        ServiceContract(
            "graph.stats", 2, GraphStatsRequest, SessionStatsResponse
        ),
        ServiceContract(
            "session.usage",
            2,
            SessionUsageRequest,
            SessionUsageResponse,
            CliSessionUsageResponse,
        ),
        ServiceContract("graph.usage", 2, GraphUsageRequest, SessionUsageResponse),
        ServiceContract(
            "session.model_usage",
            2,
            SessionModelUsageRequest,
            SessionModelUsageResponse,
        ),
        ServiceContract(
            "session.request_usage",
            2,
            SessionRequestUsageRequest,
            SessionRequestUsageResponse,
        ),
        ServiceContract(
            "session.tool_usage", 2, SessionToolUsageRequest, SessionToolUsageResponse
        ),
        ServiceContract(
            "session.events",
            2,
            SessionEventsRequest,
            SessionEventsResponse,
            CliSessionEventsResponse,
        ),
        ServiceContract(
            "session.items",
            2,
            SessionItemsRequest,
            SessionItemsResponse,
        ),
        ServiceContract(
            "living.events",
            1,
            LivingEventsRequest,
            LivingEventsResponse,
        ),
        ServiceContract(
            "living.sessions",
            2,
            LivingSessionsRequest,
            LivingSessionsResponse,
        ),
    )
}


def service_contract(method: str) -> ServiceContract:
    try:
        return SERVICE_CONTRACTS[method]
    except KeyError as exc:
        raise KeyError(f"unknown service method: {method}") from exc


def command_schema(method: str, *, command: str) -> dict[str, Any]:
    return service_contract(method).schema(command=command)
