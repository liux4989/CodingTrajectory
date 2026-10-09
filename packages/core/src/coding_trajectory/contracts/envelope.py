"""JSON envelope models for the service API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

API_PROTOCOL = "ct.api.v1"
CORE_PROTOCOL = "ct.core.v1"


class ApiEnvelopeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApiRequest(ApiEnvelopeModel):
    protocol: Literal["ct.api.v1"] = API_PROTOCOL
    id: str | None = Field(default=None, max_length=128)
    method: str = Field(min_length=1, max_length=128)
    method_version: int = Field(ge=1)
    params: dict[str, Any]


class ApiTransportMetadata(ApiEnvelopeModel):
    """Local live retention policy outside versioned method results."""

    source: Literal["local"] = "local"
    freshness: Literal["live"] = "live"
    content_scope: Literal["retained"] = "retained"


class ApiAvailability(ApiEnvelopeModel):
    state: Literal["complete", "partial", "unavailable", "unsupported"]
    missing: list[dict[str, str]]


class ApiSuccessResponse[ResultT](ApiEnvelopeModel):
    protocol: Literal["ct.api.v1"] = API_PROTOCOL
    id: Any
    method: str
    method_version: int
    ok: Literal[True]
    result: ResultT
    availability: ApiAvailability = Field(
        default_factory=lambda: ApiAvailability(state="complete", missing=[])
    )
    error: None = None
    meta: ApiTransportMetadata | None = None


class ApiErrorDetail(ApiEnvelopeModel):
    code: str
    message: str


class ApiErrorResponse(ApiEnvelopeModel):
    protocol: Literal["ct.api.v1"] = API_PROTOCOL
    id: Any
    method: Any
    method_version: int | None = None
    ok: Literal[False]
    result: None = None
    availability: ApiAvailability
    error: ApiErrorDetail
    meta: ApiTransportMetadata | None = None
