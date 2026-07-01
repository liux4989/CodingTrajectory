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
    pi:          PiExtensions | None = None


# ---------------------------------------------------------------------------
# Core canonical models
# ---------------------------------------------------------------------------

class ContextCategoryObservation(BaseModel):
    key:        str
    label:      str
    tokens:     int
    confidence: str
    source:     str | None = None


class ContextUsageObservation(BaseModel):
    source_event_id:       UUID | None = None
    timestamp:             datetime
    source:                str
    model:                 str | None = None
    provider:              str | None = None
    context_window_tokens: int | None = None
    used_input_tokens:     int = 0
    usage:                 dict[str, Any] = Field(default_factory=dict)
    cumulative_usage:      dict[str, Any] | None = None
    categories:            list[ContextCategoryObservation] = Field(default_factory=list)


class ContextSourceObservation(BaseModel):
    timestamp: datetime
    key:       str
    label:     str
    text:      str
    source:    str


class RuntimeObservation(BaseModel):
    timestamp:                 datetime
    kind:                      str
    turn_id_raw:               str | None = None
    trace_id:                  str | None = None
    duration_ms:               int | None = None
    time_to_first_token_ms:    int | None = None
    reason:                    str | None = None
    num_turns:                 int | None = None


class Event(BaseModel):
    event_id:      UUID = Field(default_factory=uuid4)
    session_id:    UUID
    timestamp:     datetime
    type:          EventType
    vendor_source: Vendor
    actor:         str | None = None
    payload:       dict[str, Any] = Field(default_factory=dict)


class ItemBase(BaseModel):
    item_id:      UUID = Field(default_factory=uuid4)
    session_id:   UUID
    turn_id:      UUID
    sequence:     int
    started_at:   datetime
    completed_at: datetime | None = None
    status:       str | None = None
    event_ids:    list[UUID] = Field(default_factory=list)
    vendor_data:  dict[str, Any] = Field(default_factory=dict)


class AgentMessageItem(ItemBase):
    kind: Literal["agent_message"] = "agent_message"
    text: str | None = None
    role: Literal["assistant"] = "assistant"


class ToolCallItem(ItemBase):
    kind:         Literal["tool_call"] = "tool_call"
    tool_name:    str | None = None
    tool_call_id: str | None = None
    input:        Any = None
    output:       Any = None


class CommandExecutionItem(ItemBase):
    kind:         Literal["command_execution"] = "command_execution"
    tool_name:    str | None = None
    tool_call_id: str | None = None
    command:      Any = None
    exit_code:    int | None = None
    output:       Any = None


class FileChangeItem(ItemBase):
    kind:         Literal["file_change"] = "file_change"
    tool_name:    str | None = None
    tool_call_id: str | None = None
    path:         str | None = None
    operation:    str | None = None
    input:        Any = None
    output:       Any = None


class ReasoningItem(ItemBase):
    kind: Literal["reasoning"] = "reasoning"
    text: str | None = None


class PlanItem(ItemBase):
    kind:         Literal["plan"] = "plan"
    tool_name:    str | None = None
    tool_call_id: str | None = None
    input:        Any = None
    output:       Any = None


Item: TypeAlias = Annotated[
    AgentMessageItem
    | ToolCallItem
    | CommandExecutionItem
    | FileChangeItem
    | ReasoningItem
    | PlanItem,
    Field(discriminator="kind"),
]


_TOOL_SHAPED_ITEM_KINDS: frozenset[str] = frozenset(
    {"tool_call", "command_execution", "file_change", "plan"}
)


def is_tool_shaped_item(item: Item) -> bool:
    return item.kind in _TOOL_SHAPED_ITEM_KINDS


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
    items:                 list[Item] = Field(default_factory=list)
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
    context_usage:     list[ContextUsageObservation] = Field(default_factory=list)
    context_sources:   list[ContextSourceObservation] = Field(default_factory=list)
    runtime_observations: list[RuntimeObservation] = Field(default_factory=list)
    extensions:        VendorExtensions | None = None
    status:            SessionStatus = SessionStatus.COMPLETED


class SessionEdge(BaseModel):
    type:               Literal["spawned_subagent", "sidechain_of", "forked_from", "handoff_to", "resumed_from", "teammate_of"]
    source_session_id:  UUID
    target_session_id:  UUID
    source_turn_id:     UUID | None = None
    source_item_id:     UUID | None = None
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
