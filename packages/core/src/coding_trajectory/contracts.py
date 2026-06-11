"""Versioned Pydantic contracts for the public ct service methods."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, RootModel


class ContractModel(BaseModel):
    """Contract base that permits additive fields within one method version."""

    model_config = ConfigDict(extra="allow")


class SessionEntryRequest(ContractModel):
    session_id: str | None = None
    root_session_id: str | None = None
    turn_id: str | None = None


class ProjectListRequest(ContractModel):
    agent_vendor: str | None = None


class ProjectSessionsRequest(ContractModel):
    project_name: str | None = None
    since_days: int | None = Field(default=None, ge=1)
    modified_since: Any | None = None
    agent_vendor: str | None = None


class ProjectLogfileRequest(ContractModel):
    pass


class SessionOverviewRequest(SessionEntryRequest):
    num_turns: int | None = Field(default=None, ge=1)
    drop_turns: int | None = Field(default=None, ge=1)


class SessionStatsRequest(SessionEntryRequest):
    pass


class SessionTurnUsageRequest(SessionEntryRequest):
    turn_id: str
    extra_billing: bool = False


class SessionUsageRequest(SessionEntryRequest):
    extra_billing: bool = False


class SessionToolUsageRequest(SessionEntryRequest):
    extra_billing: bool = False


class ItemDetailsRequest(ContractModel):
    item_ids: list[str]


class EventDetailRequest(ContractModel):
    event_id: str


class EventScanRequest(SessionEntryRequest):
    type: str
    filters: list[str] = Field(default_factory=list)


class ProjectSummary(ContractModel):
    path: str | None = None
    vendors: list[str] = Field(default_factory=list)
    sessions: list[dict[str, Any]] | None = None


class ProjectListResponse(ContractModel):
    items: dict[str, ProjectSummary]


class SessionGraphSummary(ContractModel):
    root_session_id: str
    title: str | None = None
    vendors: list[str] = Field(default_factory=list)
    session_ids: list[str] = Field(default_factory=list)


class ProjectSessionsResponse(ContractModel):
    items: list[SessionGraphSummary]


class SessionOverviewResponse(ContractModel):
    root_session_id: str
    sessions: list[dict[str, Any]] = Field(default_factory=list)


class SessionStatsResponse(ContractModel):
    root_session_id: str | None = None
    model: dict[str, Any] = Field(default_factory=dict)
    context_window: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)
    messages: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    quota: dict[str, Any] | None = None
    provider_usage_buckets: list[dict[str, Any]] = Field(default_factory=list)


class SessionUsageResponse(ContractModel):
    session_id: str
    total_usage: dict[str, Any]
    extra_billing: bool = False
    runtime: dict[str, Any] = Field(default_factory=dict)
    turns: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SessionTurnUsageResponse(ContractModel):
    root_session_id: str
    token_usage: dict[str, Any]
    cost: float | None = None
    extra_billing: bool = False
    turns: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SessionToolUsageResponse(ContractModel):
    root_session_id: str


class EventDetailResponse(ContractModel):
    event_id: str
    session_id: str
    timestamp: str
    type: str
    tool_call: dict[str, Any] | None = None
    llm: dict[str, Any] | None = None
    text: dict[str, Any] | None = None


class EventScanResponse(ContractModel):
    root_session_id: str | None = None
    type: str
    matches: list[dict[str, Any]] = Field(default_factory=list)


class ItemDetailsResponse(RootModel[list[dict[str, Any]]]):
    pass


class PublicSessionGraphSummary(ContractModel):
    id: str
    title: str | None = None
    vendors: list[str] = Field(default_factory=list)
    sessions: list[str] = Field(default_factory=list)


class PublicProjectSessionsResponse(ContractModel):
    items: list[PublicSessionGraphSummary]


class PublicSessionOverviewResponse(ContractModel):
    id: str
    sessions: list[dict[str, Any]] = Field(default_factory=list)


class PublicSessionStatsResponse(ContractModel):
    id: str
    vendor: str | None = None
    model: dict[str, Any] | None = None
    context: dict[str, Any] | None = None
    provider_usage_buckets: list[dict[str, Any]] | None = None
    runtime: dict[str, Any] | None = None
    messages: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    quota: dict[str, Any] | None = None
    warnings: list[str] | None = None


class PublicSessionUsageResponse(ContractModel):
    id: str
    extra_billing: bool = False
    runtime: dict[str, Any] | None = None
    usage: dict[str, Any]
    cost: float | None = None
    turns: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] | None = None


class PublicEventDetailResponse(ContractModel):
    id: str
    session: str
    timestamp: str
    type: str
    tool_call: dict[str, Any] | None = None
    llm: dict[str, Any] | None = None
    text: dict[str, Any] | None = None


class PublicEventScanResponse(ContractModel):
    id: str
    type: str
    matches: list[dict[str, Any]] = Field(default_factory=list)


@dataclass(frozen=True)
class ServiceContract:
    method: str
    version: int
    request_model: type[BaseModel]
    response_model: type[BaseModel]
    public_response_model: type[BaseModel] | None = None

    schema_version: ClassVar[int] = 1

    def validate_request(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.request_model.model_validate(params).model_dump(
            mode="json",
            exclude_none=True,
        )

    def validate_response(self, payload: Any) -> Any:
        return self.response_model.model_validate(payload).model_dump(
            mode="json",
            exclude_none=True,
        )

    def validate_public_response(self, payload: Any) -> Any:
        model = self.public_response_model or self.response_model
        return model.model_validate(payload).model_dump(
            mode="json",
            exclude_none=True,
        )

    def schema(self, *, command: str) -> dict[str, Any]:
        response_model = self.public_response_model or self.response_model
        return {
            "schema_version": self.schema_version,
            "command": command,
            "method": self.method,
            "method_version": self.version,
            "request": self.request_model.model_json_schema(),
            "response": response_model.model_json_schema(),
        }


SERVICE_CONTRACTS = {
    contract.method: contract
    for contract in (
        ServiceContract("project.list", 1, ProjectListRequest, ProjectListResponse),
        ServiceContract(
            "project.sessions",
            1,
            ProjectSessionsRequest,
            ProjectSessionsResponse,
            PublicProjectSessionsResponse,
        ),
        ServiceContract(
            "project.logfile", 1, ProjectLogfileRequest, ProjectSessionsResponse
        ),
        ServiceContract(
            "session.overview",
            1,
            SessionOverviewRequest,
            SessionOverviewResponse,
            PublicSessionOverviewResponse,
        ),
        ServiceContract(
            "session.stats",
            1,
            SessionStatsRequest,
            SessionStatsResponse,
            PublicSessionStatsResponse,
        ),
        ServiceContract(
            "session.turn_usage", 1, SessionTurnUsageRequest, SessionTurnUsageResponse
        ),
        ServiceContract(
            "session.usage",
            1,
            SessionUsageRequest,
            SessionUsageResponse,
            PublicSessionUsageResponse,
        ),
        ServiceContract(
            "session.tool_usage", 1, SessionToolUsageRequest, SessionToolUsageResponse
        ),
        ServiceContract(
            "item.details",
            1,
            ItemDetailsRequest,
            ItemDetailsResponse,
            ItemDetailsResponse,
        ),
        ServiceContract(
            "event.detail",
            1,
            EventDetailRequest,
            EventDetailResponse,
            PublicEventDetailResponse,
        ),
        ServiceContract(
            "event.scan",
            1,
            EventScanRequest,
            EventScanResponse,
            PublicEventScanResponse,
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
