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
    )
}


def service_contract(method: str) -> ServiceContract:
    try:
        return SERVICE_CONTRACTS[method]
    except KeyError as exc:
        raise KeyError(f"unknown service method: {method}") from exc


def command_schema(method: str, *, command: str) -> dict[str, Any]:
    return service_contract(method).schema(command=command)
