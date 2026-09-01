"""JSON envelope models for the service API."""

from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict

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
