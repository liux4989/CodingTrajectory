"""Script executions and direct-item references between turns and items."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class SourceReference(BaseModel):
    session_id: str
    item_id: str
    tool_name: str | None = None
    source_field: str
    language: str
    link: str
    record_indices: list[int]
    record_digests: list[str]
    source_sha256: str
    source_log: str
    source_log_sha256: str
    provider_call_id: str | None = None
    local_only: bool = True


class ExternalCall(BaseModel):
    observation: Literal["observed", "source_only"]
    tool_name: str
    item_id: str | None = None
    provider_call_id: str | None = None
    evidence_ref: dict[str, Any]
    ownership_basis: str
    arguments_preview: str | None = Field(default=None, max_length=280)
    arguments_ref: dict[str, Any] | None = None
    arguments_coverage: Literal[
        "recorded", "not_recorded", "truncated", "source_reconstructed"
    ]
    outcome: Literal["succeeded", "failed", "unfinished"] | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: float | None = Field(default=None, ge=0)
    result_ref: dict[str, Any] | None = None
    result_coverage: Literal["recorded", "not_recorded"] = "not_recorded"
    failure: str | None = None


class ContextReferences(BaseModel):
    session_id: str
    turn_id: str
    user_request_event_id: str | None = None
    previous_item_id: str | None = None
    next_item_id: str | None = None
    preceding_assistant_item_ids: list[str]
    following_assistant_item_ids: list[str]


class ExecutionResult(BaseModel):
    invocation_status: str | None = None
    lifecycle: Literal["completed", "failed", "yielded", "interrupted", "unknown"]
    started_at: datetime
    invocation_completed_at: datetime | None = None
    runtime_completed_at: datetime | None = None
    runtime_execution_id: str | None = None
    lifecycle_coverage: Literal["invocation_only", "runtime_checkpoint"]
    emitted_output_ref: dict[str, Any] | None = None
    output_coverage: Literal["recorded", "not_recorded"]
    output_scope: Literal["invocation", "script"] = "invocation"
    failure_output_ref: dict[str, Any] | None = None


class ToolScriptExecution(BaseModel):
    kind: Literal["tool_script"] = "tool_script"
    unit_id: str
    item_ids: list[str]
    control_item_ids: list[str] = Field(default_factory=list)
    schema_version: Literal["ct.tool_script.v1"] = "ct.tool_script.v1"
    session_id: str
    turn_id: str
    item_id: str
    tool_name: str | None = None
    source: SourceReference
    source_preview: str | None = Field(default=None, max_length=280)
    source_preview_truncated: bool = False
    open_source_link: str
    context_refs: ContextReferences
    observed_external_calls: list[ExternalCall]
    source_only_calls: list[ExternalCall]
    inventory_coverage: Literal["complete", "partial", "unknown"]
    result: ExecutionResult
    inspection_log_ref: dict[str, str]
    local_only: bool = True


class DirectItemReference(BaseModel):
    kind: Literal["item"] = "item"
    unit_id: str
    item_id: str


ExecutionUnit = Annotated[
    DirectItemReference | ToolScriptExecution, Field(discriminator="kind")
]


class TurnExecutionLayer(BaseModel):
    session_id: str
    turn_id: str
    units: list[ExecutionUnit] = Field(default_factory=list)
