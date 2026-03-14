"""Canonical data models for the ingestion layer."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class EventType(str, Enum):
    # Session Lifecycle
    SESSION_STARTED = "session.started"
    SESSION_RESUMED = "session.resumed"
    SESSION_ENDED = "session.ended"

    # User Interaction
    USER_PROMPT_SUBMITTED = "user.prompt.submitted"

    # Model Execution
    LLM_REQUEST_STARTED = "llm.request.started"
    LLM_REQUEST_COMPLETED = "llm.request.completed"
    LLM_STREAM_EVENT = "llm.stream.event"

    # Tool Lifecycle
    TOOL_CALL_REQUESTED = "tool.call.requested"
    TOOL_CALL_STARTED = "tool.call.started"
    TOOL_CALL_SUCCEEDED = "tool.call.succeeded"
    TOOL_CALL_FAILED = "tool.call.failed"

    # Permission Flow
    PERMISSION_REQUESTED = "permission.requested"
    PERMISSION_APPROVED = "permission.approved"
    PERMISSION_DENIED = "permission.denied"

    # Background Tasks
    BACKGROUND_TASK_STARTED = "background_task.started"
    BACKGROUND_TASK_COMPLETED = "background_task.completed"

    # Context Management
    CONTEXT_COMPACTION_STARTED = "context.compaction.started"
    CONTEXT_COMPACTION_COMPLETED = "context.compaction.completed"

    # Completion
    AGENT_RESPONSE_COMPLETED = "agent.response.completed"
    TASK_COMPLETED = "task.completed"


# ---------------------------------------------------------------------------
# Consumer-layer enums: category + tool kind
# ---------------------------------------------------------------------------


class EventCategory(str, Enum):
    """High-level grouping that links related events into one logical operation."""
    SESSION = "session"
    USER_INTERACTION = "user_interaction"
    LLM_INFERENCE = "llm_inference"
    TOOL_CALL = "tool_call"
    BACKGROUND_TASK = "background_task"
    PERMISSION = "permission"
    CONTEXT_COMPACTION = "context_compaction"


class ToolCallKind(str, Enum):
    """Agent/tool-specific classification within a tool call event."""
    REGULAR   = "regular"    # Bash, Read, Edit, grep — standard tools
    SUBAGENT  = "subagent"   # Task (Amp), TeamCreate (Claude Code)
    ORACLE    = "oracle"     # oracle (Amp): synchronous high-reasoning LLM sub-call
    LIBRARIAN = "librarian"  # librarian (Amp): external repo / doc search agent
    GITHUB    = "github"     # sub-tools inside librarian: search_github, read_github


# ---------------------------------------------------------------------------
# Typed detail models — one per event category (replace scattered payload keys)
# ---------------------------------------------------------------------------


class ToolCallDetail(BaseModel):
    """Typed fields for TOOL_CALL_* events."""
    tool_call_id: str | None = None
    tool_name: str | None = None
    kind: ToolCallKind = ToolCallKind.REGULAR
    input: dict[str, Any] | None = None
    result: Any | None = None
    status: str | None = None          # done | failed | in_progress


class LLMDetail(BaseModel):
    """Typed fields for LLM_REQUEST_* and LLM_STREAM_EVENT events."""
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    inference_ms: float | None = None
    reasoning_effort: str | None = None
    stop_reason: str | None = None


class TextDetail(BaseModel):
    """Typed fields for USER_PROMPT_SUBMITTED and AGENT_RESPONSE_COMPLETED events."""
    text: str
    active_editor: str | None = None
    visible_files: list[str] | None = None
    cursor_location: Any | None = None
    interrupted: bool | None = None


class EventProvenance(str, Enum):
    OBSERVED = "observed"
    DERIVED = "derived"
    SYNTHETIC = "synthetic"


class EventConfidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Vendor(str, Enum):
    CODEX_CLI = "codex_cli"
    CLAUDE_CODE = "claude_code"
    GEMINI_CLI = "gemini_cli"
    AMP = "amp"


# ---------------------------------------------------------------------------
# Vendor-specific extension models
# ---------------------------------------------------------------------------


class ClaudeCodeExtensions(BaseModel):
    """Extensions sourced from Claude Code JSONL logs."""

    # Multi-agent / teammates
    team_name: str | None = None
    agent_name: str | None = None
    is_sidechain: bool | None = None

    # Permission flow
    permission_mode: str | None = None  # e.g. "acceptEdits", "bypassPermissions", "default"

    # Context compaction
    compaction_tokens_before: int | None = None
    compaction_tokens_after: int | None = None

    # Message chain
    parent_uuid: str | None = None
    request_id: str | None = None


class CodexExtensions(BaseModel):
    """Extensions sourced from Codex CLI JSONL logs."""

    # Sandbox
    sandbox_id: str | None = None
    sandbox_mode: str | None = None     # e.g. "danger-full-access"
    approval_policy: str | None = None  # e.g. "never", "on-failure"

    # Patch info
    patch_path: str | None = None

    # Collaboration
    agent_nickname: str | None = None
    agent_role: str | None = None
    collaboration_mode: str | None = None

    # Token tracking
    model_context_window: int | None = None


class GeminiExtensions(BaseModel):
    """Extensions sourced from Gemini CLI JSONL logs."""

    # Gemini CLI uses sessionId + timestamp + type/payload structure
    session_file: str | None = None

    # Tool normalization hints observed in agentlens gemini parser
    raw_tool_type: str | None = None     # original tool name before normalization

    # Model metadata
    model_version: str | None = None
    realtime_active: bool | None = None


class AmpExtensions(BaseModel):
    """Extensions sourced from Amp thread JSON files (~/.local/share/amp/threads/)."""

    # Thread identity
    thread_id: str | None = None          # e.g. "T-019ba25f-c9f7-752e-9a1c-ea44dcd3eea4"
    thread_version: int | None = None     # "v" field — monotonic counter bumped on every save

    # Workspace / environment
    workspace_id: str | None = None       # first workspace URI from env.initial.trees[0].uri
    workspace_name: str | None = None     # displayName from env.initial.trees[0]
    git_url: str | None = None            # repository.url from env.initial.trees[0]
    git_ref: str | None = None            # repository.ref (branch/tag)

    # Client metadata
    agent_version: str | None = None      # env.initial.platform.clientVersion
    client_type: str | None = None        # env.initial.platform.clientType e.g. "cli"
    os_platform: str | None = None        # env.initial.platform.os

    # Thread metadata
    title: str | None = None
    agent_mode: str | None = None         # e.g. "smart"


class VendorExtensions(BaseModel):
    """Union bag of optional vendor-specific extension data attached to a Session."""

    claude_code: ClaudeCodeExtensions | None = None
    codex: CodexExtensions | None = None
    gemini: GeminiExtensions | None = None
    amp: AmpExtensions | None = None


# ---------------------------------------------------------------------------
# Core canonical models (strict fields remain required)
# ---------------------------------------------------------------------------


class Event(BaseModel):
    # Strict required fields
    event_id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    timestamp: datetime
    type: EventType
    vendor_source: Vendor

    # Optional fields
    turn_id: UUID | None = None
    actor: str | None = None
    provenance: EventProvenance = EventProvenance.OBSERVED
    confidence: EventConfidence = EventConfidence.HIGH

    # --- consumer layer: tree linkage ---
    # Set by SessionEnricher after ingestion; adapters leave these None.
    category: EventCategory | None = None
    event_group_id: UUID | None = None      # shared across all events in one logical operation
    parent_event_id: UUID | None = None     # direct parent in the tool-call tree

    # --- consumer layer: typed detail (one populated per event type) ---
    tool_call: ToolCallDetail | None = None  # TOOL_CALL_* events
    llm: LLMDetail | None = None            # LLM_REQUEST_* / LLM_STREAM_EVENT
    text: TextDetail | None = None          # USER_PROMPT_SUBMITTED / AGENT_RESPONSE_COMPLETED

    # Raw vendor payload — always preserved; shrinks as fields are formalised above
    payload: dict[str, Any] = Field(default_factory=dict)


# TODO: Artifact is defined per PRD (section 5.5) but not yet populated by any adapter.
# Adapters should emit Artifact instances when they produce external outputs
# (e.g. diffs, terminal output, tool results) and reference them via event_id.
class Artifact(BaseModel):
    artifact_id: UUID = Field(default_factory=uuid4)
    event_id: UUID
    type: str
    location: str


class Turn(BaseModel):
    turn_id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    user_request: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    event_ids: list[UUID] = Field(default_factory=list)


class TimelineItem(BaseModel):
    kind: Literal["event", "turn"]
    id: UUID


class TrajectorySummary(BaseModel):
    root_session_id: UUID | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    session_count: int = 0
    turn_count: int = 0
    event_count: int = 0
    agent_count: int | None = None
    vendors: list[Vendor] = Field(default_factory=list)


class TrajectorySessionRef(BaseModel):
    session_id: UUID
    parent_session_id: UUID | None = None
    vendor: Vendor
    role: Literal["primary", "subagent", "teammate", "handoff_target", "resumed"] | None = None
    agent_name: str | None = None
    team_name: str | None = None
    is_sidechain: bool | None = None
    collaboration_mode: str | None = None
    started_at: datetime
    ended_at: datetime | None = None


class TrajectoryEdge(BaseModel):
    type: Literal["spawned_subagent", "sidechain_of", "handoff_to", "resumed_from", "teammate_of"]
    source_session_id: UUID
    target_session_id: UUID
    evidence_event_ids: list[UUID] = Field(default_factory=list)
    provenance: EventProvenance = EventProvenance.OBSERVED
    confidence: EventConfidence = EventConfidence.HIGH
    metadata: dict[str, Any] | None = None


class TrajectoryOperation(BaseModel):
    operation_id: UUID = Field(default_factory=uuid4)
    kind: Literal["subagent", "handoff", "compact", "teammate", "resume"]
    scope: Literal["session_graph", "session_span"]
    status: Literal["started", "completed", "open", "failed"]
    session_ids: list[UUID] = Field(default_factory=list)
    event_ids: list[UUID] = Field(default_factory=list)
    turn_ids: list[UUID] = Field(default_factory=list)
    start_event_id: UUID
    end_event_id: UUID | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    provenance: EventProvenance | None = None
    confidence: EventConfidence | None = None
    metadata: dict[str, Any] | None = None


class TrajectorySection(BaseModel):
    section_id: UUID = Field(default_factory=uuid4)
    kind: Literal["main", "subagent", "handoff", "compact", "teammate", "resume"]
    title: str
    scope: Literal["session_graph", "session_span"]
    operation_id: UUID | None = None
    session_ids: list[UUID] = Field(default_factory=list)
    event_ids: list[UUID] = Field(default_factory=list)
    turn_ids: list[UUID] = Field(default_factory=list)
    started_at: datetime | None = None
    ended_at: datetime | None = None


class InferenceNote(BaseModel):
    subject: str
    message: str
    provenance: EventProvenance = EventProvenance.DERIVED
    confidence: EventConfidence = EventConfidence.MEDIUM


class Session(BaseModel):
    # Strict required fields
    session_id: UUID = Field(default_factory=uuid4)
    trajectory_id: UUID
    vendor: Vendor
    started_at: datetime

    # Optional fields
    parent_session_id: UUID | None = None
    ended_at: datetime | None = None
    timeline: list[TimelineItem] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    turns: list[Turn] = Field(default_factory=list)
    extensions: VendorExtensions | None = None


class Trajectory(BaseModel):
    trajectory_id: UUID = Field(default_factory=uuid4)
    project_identifier: str | None = None
    task_reference: str | None = None
    multi_agent_mode: Literal["cross_session", "in_session", "hybrid"] | None = None
    summary: TrajectorySummary | None = None
    session_refs: list[TrajectorySessionRef] = Field(default_factory=list)
    edges: list[TrajectoryEdge] = Field(default_factory=list)
    operations: list[TrajectoryOperation] = Field(default_factory=list)
    sections: list[TrajectorySection] = Field(default_factory=list)
    inference_notes: list[InferenceNote] = Field(default_factory=list)
    sessions: list[Session] = Field(default_factory=list)
