"""Stateless live inventory and retained-resource query contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from coding_trajectory.contracts.base import ContractModel, RequestModel


class LivingScope(RequestModel):
    root_session_id: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None

    @field_validator("root_session_id", "session_id", "turn_id", "item_id")
    @classmethod
    def canonical_id(cls, value: str | None) -> str | None:
        return str(UUID(value.removeprefix("T-"))) if value is not None else None

    @model_validator(mode="after")
    def one_scope(self) -> Self:
        if (self.root_session_id is not None) + (self.session_id is not None) != 1:
            raise ValueError("scope requires exactly one root_session_id or session_id")
        return self


class LivingEventsRequest(RequestModel):
    scope: LivingScope
    mode: Literal["view", "details"] = "view"
    cursor: str | None = Field(default=None, min_length=1, max_length=4096)
    limit: int = Field(default=50, ge=1, le=200, strict=True)


class LivingSessionsRequest(RequestModel):
    root_session_id: str | None = None
    session_id: str | None = None
    project_name: str | None = Field(default=None, min_length=1)
    horizon_days: int = Field(default=3, ge=1, le=30, strict=True)
    cursor: str | None = Field(default=None, min_length=1, max_length=4096)
    limit: int = Field(default=50, ge=1, le=200, strict=True)

    @field_validator("root_session_id", "session_id")
    @classmethod
    def canonical_id(cls, value: str | None) -> str | None:
        return str(UUID(value.removeprefix("T-"))) if value is not None else None

    @model_validator(mode="after")
    def one_selector(self) -> Self:
        if (
            sum(
                value is not None
                for value in (self.root_session_id, self.session_id, self.project_name)
            )
            > 1
        ):
            raise ValueError(
                "root_session_id, session_id and project_name are mutually exclusive"
            )
        return self


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
    status: str | None = None
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


class LivingEventResource(ContractModel):
    resource_kind: Literal[
        "session", "turn", "item", "context_checkpoint", "session_edge"
    ]
    path: LivingResourcePath
    resource: LivingResource
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class LivingProtocolIssue(ContractModel):
    severity: Literal["warning", "error"]
    code: str
    message: str
    path: LivingResourcePath | None = None


class LivingEventsResponse(ContractModel):
    schema_version: Literal["ct.living_events.v2"]
    mode: Literal["view", "details"]
    resources: list[LivingEventResource] = Field(default_factory=list)
    total: int = Field(ge=0)
    returned: int = Field(ge=0)
    next_cursor: str | None = None
    issues: list[LivingProtocolIssue] = Field(default_factory=list)


class LivingSessionMetadata(ContractModel):
    session_id: str
    root_session_id: str
    lineage_root_session_id: str
    vendor: str
    project: str
    cwd: str | None = None
    modified: datetime
    size: int = Field(ge=0)
    state: Literal["living", "inactive"]


class LivingSessionInventoryResource(LivingSessionMetadata):
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class LivingSessionsResponse(ContractModel):
    schema_version: Literal["ct.living_sessions.v3"]
    items: list[LivingSessionInventoryResource] = Field(default_factory=list)
    total: int = Field(ge=0)
    returned: int = Field(ge=0)
    next_cursor: str | None = None
    issues: list[LivingProtocolIssue] = Field(default_factory=list)
