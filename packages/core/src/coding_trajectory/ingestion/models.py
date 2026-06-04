"""Canonical data models for the ingestion layer."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, TypeAlias
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class EventType(str, Enum):
    USER_PROMPT_SUBMITTED = "user.prompt.submitted"
    TOOL_CALL_REQUESTED   = "tool.call.requested"
    TOOL_CALL_SUCCEEDED   = "tool.call.succeeded"
    TOOL_CALL_FAILED      = "tool.call.failed"
    LLM_RESPONSE          = "llm.response"
    VENDOR_RAW            = "vendor.raw"


class Vendor(str, Enum):
    CODEX_CLI   = "codex_cli"
    CLAUDE_CODE = "claude_code"
    AMP         = "amp"
    PI          = "pi"


class ToolStatus(str, Enum):
    REQUESTED   = "requested"
    IN_PROGRESS = "in_progress"
    COMPLETED   = "completed"
    FAILED      = "failed"


class TurnStatus(str, Enum):
    RUNNING     = "running"
    INTERRUPTED = "interrupted"
    COMPLETED   = "completed"
    INCOMPLETE  = "incomplete"


class SessionStatus(str, Enum):
    ACTIVE     = "active"
    COMPLETED  = "completed"
    INCOMPLETE = "incomplete"


# ---------------------------------------------------------------------------
# Vendor-specific extension models (session-level metadata)
# ---------------------------------------------------------------------------

class ClaudeCodeExtensions(BaseModel):
    team_name:      str | None = None
    agent_name:     str | None = None
    agent_role:     str | None = None
    description:    str | None = None
    title:          str | None = None
    is_sidechain:   bool | None = None
    permission_mode: str | None = None
    parent_uuid:    str | None = None
    request_id:     str | None = None


class CodexExtensions(BaseModel):
    sandbox_id:         str | None = None
    sandbox_mode:       str | None = None
    approval_policy:    str | None = None
    agent_nickname:     str | None = None
    agent_role:         str | None = None
    collaboration_mode: str | None = None
    cwd:                str | None = None
    title:              str | None = None
    forked_from_id:     str | None = None
    spawn_parent_thread_id: str | None = None
    spawn_depth:        int | None = None
    spawn_agent_nickname: str | None = None
    spawn_agent_role:   str | None = None


class AmpExtensions(BaseModel):
    thread_id:        str | None = None
    thread_version:   int | None = None
    parent_thread_id: str | None = None
    workspace_id:     str | None = None
    workspace_name:   str | None = None
    git_url:          str | None = None
    git_ref:          str | None = None
    agent_version:    str | None = None
    client_type:      str | None = None
    os_platform:      str | None = None
    title:            str | None = None
    agent_mode:       str | None = None


class PiExtensions(BaseModel):
    session_file:  str | None = None
    cwd:           str | None = None
    title:         str | None = None
    provider:      str | None = None
    model:         str | None = None
    thinking_level: str | None = None


class VendorExtensions(BaseModel):
    claude_code: ClaudeCodeExtensions | None = None
    codex:       CodexExtensions | None = None
    amp:         AmpExtensions | None = None
    pi:          PiExtensions | None = None


# ---------------------------------------------------------------------------
# Core canonical models
# ---------------------------------------------------------------------------

class Event(BaseModel):
    event_id:      UUID = Field(default_factory=uuid4)
    session_id:    UUID
    timestamp:     datetime
    type:          EventType
    vendor_source: Vendor
    actor:         str | None = None
    payload:       dict[str, Any] = Field(default_factory=dict)


class StepTextItem(BaseModel):
    kind:      Literal["text"] = "text"
    text:      str
    event_ids: list[UUID] = Field(default_factory=list)


class StepToolItem(BaseModel):
    kind:         Literal["tool"] = "tool"
    tool_name:    str | None = None
    tool_call_id: str | None = None
    input:        Any = None
    output:       Any = None
    status:       ToolStatus = ToolStatus.REQUESTED
    event_ids:    list[UUID] = Field(default_factory=list)


StepItem: TypeAlias = Annotated[StepTextItem | StepToolItem, Field(discriminator="kind")]


class Step(BaseModel):
    """One LLM call/response cycle within a Turn."""
    step_id:     UUID = Field(default_factory=uuid4)
    session_id:  UUID
    turn_id:     UUID
    sequence:    int
    timestamp:   datetime
    vendor:      Vendor
    items:       list[StepItem] = Field(default_factory=list)
    vendor_data: dict[str, Any] = Field(default_factory=dict)  # secondary vendor-specific metadata
    event_ids:   list[UUID] = Field(default_factory=list)      # refs into Session.events


class TeamMemberState(BaseModel):
    member_id:  str
    session_id: UUID | None = None
    name:       str | None = None
    color:      str | None = None
    team_name:  str | None = None
    agent_type: str | None = None
    summary:    str | None = None


class TeamTaskState(BaseModel):
    task_id:         str
    title:           str | None = None
    status:          str | None = None
    member_id:       str | None = None
    summary:         str | None = None
    blocked_by:      list[str] = Field(default_factory=list)
    updated_fields:  list[str] = Field(default_factory=list)


class TeamTurnState(BaseModel):
    members: list[TeamMemberState] = Field(default_factory=list)
    tasks:   list[TeamTaskState] = Field(default_factory=list)


class Turn(BaseModel):
    turn_id:               UUID = Field(default_factory=uuid4)
    session_id:            UUID
    sequence:              int
    started_at:            datetime
    ended_at:              datetime | None = None
    user_request_event_id: UUID | None = None    # ref into Session.events
    event_ids:             list[UUID] = Field(default_factory=list)
    steps:                 list[Step] = Field(default_factory=list)
    team_state:            TeamTurnState | None = None
    status:                TurnStatus = TurnStatus.COMPLETED


class Session(BaseModel):
    session_id:        UUID = Field(default_factory=uuid4)
    vendor:            Vendor
    agent_name:        str | None = None
    started_at:        datetime
    ended_at:          datetime | None = None
    parent_session_id: UUID | None = None
    cwd:               str | None = None
    events:            list[Event] = Field(default_factory=list)
    turns:             list[Turn] = Field(default_factory=list)
    extensions:        VendorExtensions | None = None
    status:            SessionStatus = SessionStatus.COMPLETED


class SessionEdge(BaseModel):
    type:               Literal["spawned_subagent", "sidechain_of", "forked_from", "handoff_to", "resumed_from", "teammate_of"]
    source_session_id:  UUID
    target_session_id:  UUID
    source_turn_id:     UUID | None = None
    source_step_id:     UUID | None = None
    source_event_id:    UUID | None = None
    provenance:         Literal["observed", "derived"] = "derived"
    confidence:         Literal["high", "medium", "low"] = "medium"
    evidence_event_ids: list[UUID] = Field(default_factory=list)
    metadata:           dict[str, Any] | None = None


class SessionGraphSummary(BaseModel):
    root_session_id: UUID | None = None
    started_at:      datetime | None = None
    ended_at:        datetime | None = None
    session_count:   int = 0
    turn_count:      int = 0
    vendors:         list[Vendor] = Field(default_factory=list)


class SessionGraph(BaseModel):
    root_session_id:      UUID = Field(default_factory=uuid4)
    project_identifier: str | None = None
    summary:            SessionGraphSummary | None = None
    edges:              list[SessionEdge] = Field(default_factory=list)
    sessions:           list[Session] = Field(default_factory=list)
