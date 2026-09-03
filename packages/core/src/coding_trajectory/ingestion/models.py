"""Canonical data models for the ingestion layer."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, TypeAlias
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """Base for internal record models that reject unknown fields."""

    model_config = ConfigDict(extra="forbid")


class FrozenStrictModel(StrictModel):
    """StrictModel variant that is also immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class EventType(str, Enum):
    USER_PROMPT_SUBMITTED = "user.prompt.submitted"
    TOOL_CALL_REQUESTED = "tool.call.requested"
    TOOL_CALL_SUCCEEDED = "tool.call.succeeded"
    TOOL_CALL_FAILED = "tool.call.failed"
    LLM_RESPONSE = "llm.response"
    VENDOR_RAW = "vendor.raw"


class Vendor(str, Enum):
    CODEX_CLI = "codex_cli"
    CLAUDE_CODE = "claude_code"
    PI = "pi"


class ToolStatus(str, Enum):
    REQUESTED = "requested"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class TurnStatus(str, Enum):
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"


class SessionStatus(str, Enum):
    """Whether the session currently has a running canonical turn.

    This is intentionally not a task outcome: an ended Codex turn is
    ``not_living`` until a resumed or follow-up turn begins in the same thread.
    """

    LIVING = "living"
    NOT_LIVING = "not_living"


# ---------------------------------------------------------------------------
# Vendor-specific extension models (session-level metadata)
# ---------------------------------------------------------------------------


class ClaudeCodeExtensions(BaseModel):
    team_name: str | None = None
    agent_name: str | None = None
    agent_role: str | None = None
    description: str | None = None
    title: str | None = None
    is_sidechain: bool | None = None
    permission_mode: str | None = None
    mode: str | None = None
    last_prompt: str | None = None
    parent_uuid: str | None = None
    request_id: str | None = None
    tool_use_id: str | None = None
    spawn_depth: int | None = None


class CanonicalSpawnOrigin(BaseModel):
    """Content-free canonical origin for one spawned child session."""

    event_id: UUID
    turn_id: UUID | None = None
    item_id: UUID | None = None
    tool_name: str | None = None


class CodexExtensions(BaseModel):
    sandbox_id: str | None = None
    sandbox_mode: str | None = None
    approval_policy: str | None = None
    agent_nickname: str | None = None
    agent_role: str | None = None
    collaboration_mode: str | None = None
    multi_agent_version: str | None = None
    multi_agent_mode: str | None = None
    agent_path: str | None = None
    cwd: str | None = None
    preview: str | None = None
    title: str | None = None
    forked_from_id: str | None = None
    spawn_parent_thread_id: str | None = None
    spawn_depth: int | None = None
    spawn_agent_nickname: str | None = None
    spawn_agent_role: str | None = None
    # child session id -> spawn tool-call call_id, captured from
    # sub_agent_activity{kind:started} events in THIS session's log. Backs the
    # forked_from edge origin for children spawned here.
    spawn_links: dict[str, str] = Field(default_factory=dict)
    canonical_spawn_origins: dict[str, CanonicalSpawnOrigin] = Field(
        default_factory=dict
    )


class PiExtensions(BaseModel):
    session_file: str | None = None
    cwd: str | None = None
    title: str | None = None
    provider: str | None = None
    model: str | None = None
    thinking_level: str | None = None


class VendorExtensions(BaseModel):
    claude_code: ClaudeCodeExtensions | None = None
    codex: CodexExtensions | None = None
    pi: PiExtensions | None = None


# ---------------------------------------------------------------------------
# Core canonical models
# ---------------------------------------------------------------------------


class ContextCategoryObservation(BaseModel):
    key: str
    label: str
    tokens: int
    confidence: str
    source: str | None = None


class ContextUsageObservation(BaseModel):
    source_event_id: UUID | None = None
    timestamp: datetime
    source: str
    model: str | None = None
    provider: str | None = None
    context_window_tokens: int | None = None
    used_input_tokens: int = 0
    usage: dict[str, Any] = Field(default_factory=dict)
    cumulative_usage: dict[str, Any] | None = None
    categories: list[ContextCategoryObservation] = Field(default_factory=list)


class ContextSourceObservation(BaseModel):
    timestamp: datetime
    key: str
    label: str
    text: str
    source: str
    # Vendor-reported token count for sources whose text is not captured in the
    # log (e.g. Claude Code's cached system-prompt prefix). When present and the
    # observed text is empty, composition and cost accounting use this in place
    # of a visible-text token estimate.
    reported_tokens: int | None = None


class RuntimeObservation(BaseModel):
    timestamp: datetime
    kind: str
    turn_id_raw: str | None = None
    trace_id: str | None = None
    duration_ms: int | None = None
    time_to_first_token_ms: int | None = None
    reason: str | None = None
    num_turns: int | None = None
    # Compaction metadata. Only populated for evicting compaction boundaries
    # (Claude Code's ``claude_compact_boundary``); Codex's ``context_compacted``
    # carries no pre/post delta in the event itself, so these stay ``None``.
    pre_tokens: int | None = None
    post_tokens: int | None = None
    cumulative_dropped_tokens: int | None = None
    trigger: str | None = None
    # Effort-change metadata. Populated only for ``effort_changed`` observations
    # — emitted when a turn's reasoning effort differs from the prior turn's
    # (Codex ``turn_context.effort``); both ends are real strings.
    effort_from: str | None = None
    effort_to: str | None = None


# Evicting-compaction observation kinds. Codex emits ``context_compacted`` (a
# full eviction with no pre/post delta in the event); Claude Code emits
# ``claude_compact_boundary`` (a full eviction with pre/post/trigger metadata).
# Both count as a compaction for stats and overview activities.
COMPACTION_KINDS = frozenset({"context_compacted", "claude_compact_boundary"})

# Map observation kinds to a compaction mechanism label. ``eviction_boundary``
# (Claude Code) carries pre/post/dropped/trigger; ``context_compacted``
# (Codex) does not. The label drives per-provider rendering so a bare Codex
# compaction doesn't show as empty pre→post / dropped cells.
COMPACTION_MECHANISMS = {
    "claude_compact_boundary": "eviction_boundary",
    "context_compacted": "context_compacted",
}


class Event(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    timestamp: datetime
    type: EventType
    vendor_source: Vendor
    actor: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ItemMeasurements(BaseModel):
    """Content-derived sizes and small summaries for a body-free compact item.

    Computed from the transient full-fidelity item with the session graph's
    effective tokenizer, then the bodies are discarded.  Projections consume
    these primitives exactly as they would size the resident bodies.
    """

    input_chars: int = 0
    input_tokens: int = 0
    output_chars: int = 0
    output_tokens: int = 0
    text_chars: int = 0
    text_tokens: int = 0
    projection_only: bool = False
    output_truncated: bool = False
    output_original_tokens: int | None = None
    input_summary: str | None = None
    text_preview: str | None = None
    tool_summary: dict[str, Any] | None = None


class ContextSourceMeasurement(BaseModel):
    """Size of one starting-context source whose text was discarded."""

    timestamp: datetime
    key: str
    label: str
    reported_tokens: int | None = None
    chars: int = 0
    tokens: int = 0


class EventTextMeasurement(BaseModel):
    """Text size of one dropped LLM_RESPONSE event (composition fallback)."""

    timestamp: datetime
    chars: int = 0
    tokens: int = 0


class SessionMeasurements(BaseModel):
    """Session-level content primitives for body-free compact sessions."""

    context_sources: list[ContextSourceMeasurement] = Field(default_factory=list)
    llm_response_count: int = 0
    llm_response_text_sizes: list[EventTextMeasurement] = Field(default_factory=list)


class ItemBase(BaseModel):
    item_id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    turn_id: UUID
    sequence: int
    started_at: datetime
    completed_at: datetime | None = None
    status: str | None = None
    event_ids: list[UUID] = Field(default_factory=list)
    vendor_data: dict[str, Any] = Field(default_factory=dict)
    measurements: ItemMeasurements | None = None


class AgentMessageItem(ItemBase):
    kind: Literal["agent_message"] = "agent_message"
    text: str | None = None
    role: Literal["assistant"] = "assistant"


class ToolCallItem(ItemBase):
    kind: Literal["tool_call"] = "tool_call"
    tool_name: str | None = None
    tool_call_id: str | None = None
    input: Any = None
    output: Any = None


class CommandExecutionItem(ItemBase):
    kind: Literal["command_execution"] = "command_execution"
    tool_name: str | None = None
    tool_call_id: str | None = None
    command: Any = None
    exit_code: int | None = None
    output: Any = None


class FileChangeItem(ItemBase):
    kind: Literal["file_change"] = "file_change"
    tool_name: str | None = None
    tool_call_id: str | None = None
    path: str | None = None
    operation: str | None = None
    input: Any = None
    output: Any = None


class ReasoningItem(ItemBase):
    kind: Literal["reasoning"] = "reasoning"
    text: str | None = None


class PlanItem(ItemBase):
    kind: Literal["plan"] = "plan"
    tool_name: str | None = None
    tool_call_id: str | None = None
    input: Any = None
    output: Any = None


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
    member_id: str
    session_id: UUID | None = None
    name: str | None = None
    color: str | None = None
    team_name: str | None = None
    agent_type: str | None = None
    summary: str | None = None


class TeamTaskState(BaseModel):
    task_id: str
    title: str | None = None
    status: str | None = None
    member_id: str | None = None
    summary: str | None = None
    blocked_by: list[str] = Field(default_factory=list)
    updated_fields: list[str] = Field(default_factory=list)


class TeamTurnState(BaseModel):
    members: list[TeamMemberState] = Field(default_factory=list)
    tasks: list[TeamTaskState] = Field(default_factory=list)


class Turn(BaseModel):
    turn_id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    sequence: int
    started_at: datetime
    ended_at: datetime | None = None
    user_request_event_id: UUID | None = None  # ref into Session.events
    event_ids: list[UUID] = Field(default_factory=list)
    items: list[Item] = Field(default_factory=list)
    team_state: TeamTurnState | None = None
    status: TurnStatus = TurnStatus.COMPLETED


class Session(BaseModel):
    session_id: UUID = Field(default_factory=uuid4)
    vendor: Vendor
    # Most recently observed model configuration for this session. These are
    # configuration facts, not aggregate usage attribution; model changes
    # remain represented in the underlying turn/event evidence.
    model: str | None = None
    reasoning_effort: str | None = None
    agent_name: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    parent_session_id: UUID | None = None
    cwd: str | None = None
    events: list[Event] = Field(default_factory=list)
    turns: list[Turn] = Field(default_factory=list)
    context_usage: list[ContextUsageObservation] = Field(default_factory=list)
    context_sources: list[ContextSourceObservation] = Field(default_factory=list)
    runtime_observations: list[RuntimeObservation] = Field(default_factory=list)
    measurements: SessionMeasurements | None = None
    extensions: VendorExtensions | None = None
    status: SessionStatus = SessionStatus.NOT_LIVING

    @property
    def latest_turn_status(self) -> TurnStatus | None:
        """Preserve the latest turn terminal/running state beside liveness."""

        return self.turns[-1].status if self.turns else None


class SessionEdge(BaseModel):
    type: Literal[
        "spawned_subagent",
        "sidechain_of",
        "forked_from",
        "handoff_to",
        "resumed_from",
        "teammate_of",
    ]
    source_session_id: UUID
    target_session_id: UUID
    source_turn_id: UUID | None = None
    source_item_id: UUID | None = None
    source_event_id: UUID | None = None
    provenance: Literal["observed", "derived"] = "derived"
    confidence: Literal["high", "medium", "low"] = "medium"
    evidence_event_ids: list[UUID] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None


class SessionGraphSummary(BaseModel):
    root_session_id: UUID | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    session_count: int = 0
    turn_count: int = 0
    vendors: list[Vendor] = Field(default_factory=list)


class SessionGraph(BaseModel):
    root_session_id: UUID = Field(default_factory=uuid4)
    project_identifier: str | None = None
    summary: SessionGraphSummary | None = None
    edges: list[SessionEdge] = Field(default_factory=list)
    sessions: list[Session] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize_summary_root(self) -> SessionGraph:
        """Keep the legacy summary mirror derived from the canonical graph root."""
        if self.summary is None:
            return self
        if self.summary.root_session_id is None:
            self.summary = self.summary.model_copy(
                update={"root_session_id": self.root_session_id}
            )
        elif self.summary.root_session_id != self.root_session_id:
            raise ValueError(
                "summary.root_session_id must match SessionGraph.root_session_id"
            )
        return self
