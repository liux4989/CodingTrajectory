"""JSON envelope models for the service API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CORE_PROTOCOL = "ct.core.v1"


class ApiEnvelopeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApiTransportMetadata(ApiEnvelopeModel):
    """Authority and snapshot facts carried outside versioned method results."""

    workspace_id: str
    snapshot_sequence: int
    source: Literal["remote"]
    freshness: Literal["authoritative"]
    content_scope: Literal["chronicle"]


class ApiAvailability(ApiEnvelopeModel):
    state: Literal["complete", "partial", "unavailable", "unsupported"]
    missing: list[dict[str, str]]


class ApiSuccessResponse[ResultT](ApiEnvelopeModel):
    protocol: Literal["ct.core.v1"] = CORE_PROTOCOL
    id: Any
    method: str
    ok: Literal[True]
    data: ResultT
    availability: ApiAvailability = Field(
        default_factory=lambda: ApiAvailability(state="complete", missing=[])
    )
    error: None = None
    meta: ApiTransportMetadata | None = None


class ApiErrorDetail(ApiEnvelopeModel):
    code: str
    message: str


class ApiErrorResponse(ApiEnvelopeModel):
    protocol: Literal["ct.core.v1"] = CORE_PROTOCOL
    id: Any
    method: Any
    ok: Literal[False]
    data: None = None
    availability: ApiAvailability
    error: ApiErrorDetail
    meta: ApiTransportMetadata | None = None
