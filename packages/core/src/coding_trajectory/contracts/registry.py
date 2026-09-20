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
        ServiceContract("project.list", 5, ProjectListRequest, ProjectListResponse),
        ServiceContract(
            "project.sessions",
            5,
            ProjectSessionsRequest,
            ProjectSessionsResponse,
        ),
        ServiceContract(
            "session.overview",
            5,
            SessionOverviewRequest,
            SessionOverviewResponse,
        ),
        ServiceContract(
            "session.summary",
            3,
            SessionSummaryRequest,
            SessionSummaryResponse,
        ),
        ServiceContract(
            "session.search",
            2,
            SessionSearchRequest,
            SessionSearchResponse,
        ),
        ServiceContract("session.tree", 4, SessionTreeRequest, SessionTreeResponse),
        ServiceContract(
            "graph.overview",
            5,
            GraphOverviewRequest,
            GraphOverviewResponse,
        ),
        ServiceContract(
            "session.stats",
            4,
            SessionStatsRequest,
            SessionStatsResponse,
            CliSessionStatsResponse,
        ),
        ServiceContract("graph.stats", 4, GraphStatsRequest, SessionStatsResponse),
        ServiceContract(
            "session.usage",
            4,
            SessionUsageRequest,
            SessionUsageResponse,
            CliSessionUsageResponse,
        ),
        ServiceContract("graph.usage", 4, GraphUsageRequest, SessionUsageResponse),
        ServiceContract(
            "session.model_usage",
            4,
            SessionModelUsageRequest,
            SessionModelUsageResponse,
        ),
        ServiceContract(
            "session.request_usage",
            5,
            SessionRequestUsageRequest,
            SessionRequestUsageResponse,
        ),
        ServiceContract(
            "session.tool_usage", 5, SessionToolUsageRequest, SessionToolUsageResponse
        ),
        ServiceContract(
            "session.events",
            5,
            SessionEventsRequest,
            SessionEventsResponse,
        ),
        ServiceContract(
            "session.items",
            5,
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
            3,
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
