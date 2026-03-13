"""Canonical data models for the ingestion layer."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
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

    # Subtasks
    SUBTASK_STARTED = "subtask.started"
    SUBTASK_COMPLETED = "subtask.completed"

    # Background Tasks
    BACKGROUND_TASK_STARTED = "background_task.started"
    BACKGROUND_TASK_COMPLETED = "background_task.completed"

    # Context Management
    CONTEXT_COMPACTION_STARTED = "context.compaction.started"
    CONTEXT_COMPACTION_COMPLETED = "context.compaction.completed"

    # Completion
    AGENT_RESPONSE_COMPLETED = "agent.response.completed"
    TASK_COMPLETED = "task.completed"


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
    events: list[Event] = Field(default_factory=list)


class Session(BaseModel):
    # Strict required fields
    session_id: UUID = Field(default_factory=uuid4)
    trajectory_id: UUID
    vendor: Vendor
    started_at: datetime

    # Optional fields
    parent_session_id: UUID | None = None
    ended_at: datetime | None = None
    turns: list[Turn] = Field(default_factory=list)
    extensions: VendorExtensions | None = None


class Trajectory(BaseModel):
    trajectory_id: UUID = Field(default_factory=uuid4)
    task_reference: str | None = None
    sessions: list[Session] = Field(default_factory=list)
