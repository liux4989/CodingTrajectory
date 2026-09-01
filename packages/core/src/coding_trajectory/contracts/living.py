"""Contracts for the living.* snapshot/delta protocol."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import ConfigDict, Field

from coding_trajectory.contracts.base import ContractModel, RequestModel


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
    latest_turn_status: (
        Literal["running", "interrupted", "completed", "incomplete"] | None
    ) = None
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
