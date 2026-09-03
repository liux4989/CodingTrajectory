"""JSON envelope models for the service API."""

from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict

ResultT = TypeVar("ResultT")


class ApiEnvelopeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApiTransportMetadata(ApiEnvelopeModel):
    """Authority and snapshot facts carried outside versioned method results."""

    workspace_id: str
    snapshot_sequence: int
    source: Literal["remote"]
    freshness: Literal["authoritative"]
    content_scope: Literal["shareable"]


class ApiSuccessResponse(ApiEnvelopeModel, Generic[ResultT]):
    id: Any
    method: str
    ok: Literal[True]
    result: ResultT
    meta: ApiTransportMetadata | None = None


class ApiErrorDetail(ApiEnvelopeModel):
    message: str


class ApiErrorResponse(ApiEnvelopeModel):
    id: Any
    method: Any
    ok: Literal[False]
    error: ApiErrorDetail
    meta: ApiTransportMetadata | None = None
