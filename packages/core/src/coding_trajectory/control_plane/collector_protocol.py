"""Versioned contracts shared by the local collector and remote ingress.

These models intentionally describe *canonical observations*, not vendor JSONL
records.  The source files stay on the host that collected them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CollectorModel(BaseModel):
    """Strict wire model for collector ingress."""

    model_config = ConfigDict(extra="forbid")


class SourceRegistrationRequest(CollectorModel):
    version: Literal[1] = 1
    workspace_id: UUID
    agent_id: UUID
    vendor: str = Field(min_length=1)
    native_session_id: str = Field(min_length=1)
    project_id: UUID | None = None
    source_epoch: int = Field(default=1, ge=1)
    rollover: bool = False


class SourceRegistrationResponse(CollectorModel):
    source_id: UUID
    source_epoch: int = Field(ge=1)


class ObservationRequest(CollectorModel):
    """One idempotent, host-normalized source snapshot."""

    version: Literal[1] = 1
    workspace_id: UUID
    agent_id: UUID
    source_id: UUID
    source_epoch: int = Field(ge=1)
    source_sequence: int = Field(ge=0)
    event_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    parser_version: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    payload: dict[str, Any]


class ObservationReceipt(CollectorModel):
    receipt_id: UUID
    outcome: Literal["accepted", "duplicate", "rejected", "conflict"]
    committed_sequence: int | None = Field(default=None, ge=1)
    details: dict[str, Any] = Field(default_factory=dict)


class LeaseHeartbeatRequest(CollectorModel):
    version: Literal[1] = 1
    workspace_id: UUID
    agent_id: UUID
    agent_instance_id: UUID
    observation_sequence: int = Field(ge=1)
    observed_at: datetime
    lease_seconds: int = Field(default=90, ge=15, le=3600)
    runtime_state: Literal["living", "idle", "terminal", "unknown"] = "unknown"
    source_watermarks: dict[str, int] = Field(default_factory=dict)


class LeaseHeartbeatResponse(CollectorModel):
    committed_sequence: int = Field(ge=1)
    lease_expires_at: datetime
