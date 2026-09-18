"""Method registry binding request/response contracts to service methods."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel, TypeAdapter

from coding_trajectory.contracts.envelope import (
    ApiErrorResponse,
    ApiSuccessResponse,
)
from coding_trajectory.contracts.living import (
    LivingEventsRequest,
    LivingEventsResponse,
    LivingSessionsRequest,
    LivingSessionsResponse,
)
from coding_trajectory.contracts.session import (
    CliProjectSessionsResponse,
    CliSessionEventsResponse,
    CliSessionItemsResponse,
    CliSessionOverviewResponse,
    CliSessionStatsResponse,
    CliSessionUsageResponse,
    GraphOverviewRequest,
    GraphOverviewResponse,
    GraphStatsRequest,
    GraphUsageRequest,
    ProjectListRequest,
    ProjectListResponse,
    ProjectSessionsRequest,
    ProjectSessionsResponse,
    SessionEventsRequest,
    SessionEventsResponse,
    SessionItemsRequest,
    SessionItemsResponse,
    SessionModelUsageRequest,
    SessionModelUsageResponse,
    SessionOverviewRequest,
    SessionOverviewResponse,
    SessionRequestUsageRequest,
    SessionRequestUsageResponse,
    SessionSearchRequest,
    SessionSearchResponse,
    SessionStatsRequest,
    SessionStatsResponse,
    SessionSummaryRequest,
    SessionSummaryResponse,
    SessionToolUsageRequest,
    SessionToolUsageResponse,
    SessionTreeRequest,
    SessionTreeResponse,
    SessionUsageRequest,
    SessionUsageResponse,
)


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
            exclude_none=False,
        )

    def validate_cli_response(self, payload: Any) -> Any:
        model = self.cli_response_model or self.response_model
        return model.model_validate(payload).model_dump(
            mode="json",
            exclude_none=False,
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
        ServiceContract("project.list", 4, ProjectListRequest, ProjectListResponse),
        ServiceContract(
            "project.sessions",
            4,
            ProjectSessionsRequest,
            ProjectSessionsResponse,
            CliProjectSessionsResponse,
        ),
        ServiceContract(
            "session.overview",
            3,
            SessionOverviewRequest,
            SessionOverviewResponse,
            CliSessionOverviewResponse,
        ),
        ServiceContract(
            "session.summary",
            2,
            SessionSummaryRequest,
            SessionSummaryResponse,
        ),
        ServiceContract(
            "session.search",
            2,
            SessionSearchRequest,
            SessionSearchResponse,
        ),
        ServiceContract("session.tree", 3, SessionTreeRequest, SessionTreeResponse),
        ServiceContract(
            "graph.overview",
            3,
            GraphOverviewRequest,
            GraphOverviewResponse,
        ),
        ServiceContract(
            "session.stats",
            3,
            SessionStatsRequest,
            SessionStatsResponse,
            CliSessionStatsResponse,
        ),
        ServiceContract("graph.stats", 3, GraphStatsRequest, SessionStatsResponse),
        ServiceContract(
            "session.usage",
            3,
            SessionUsageRequest,
            SessionUsageResponse,
            CliSessionUsageResponse,
        ),
        ServiceContract("graph.usage", 3, GraphUsageRequest, SessionUsageResponse),
        ServiceContract(
            "session.model_usage",
            3,
            SessionModelUsageRequest,
            SessionModelUsageResponse,
        ),
        ServiceContract(
            "session.request_usage",
            3,
            SessionRequestUsageRequest,
            SessionRequestUsageResponse,
        ),
        ServiceContract(
            "session.tool_usage", 3, SessionToolUsageRequest, SessionToolUsageResponse
        ),
        ServiceContract(
            "session.events",
            4,
            SessionEventsRequest,
            SessionEventsResponse,
            CliSessionEventsResponse,
        ),
        ServiceContract(
            "session.items",
            4,
            SessionItemsRequest,
            SessionItemsResponse,
            CliSessionItemsResponse,
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
