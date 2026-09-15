"""Versioned contracts shared by the local collector and remote ingress.

These models describe checkpoint metadata and bounded chronicle artifacts, not
vendor JSONL records. The source files stay on the host that collected them.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coding_trajectory.control_plane.published_facts import (
    FACT_SET_SCHEMA_VERSION,
    MAX_FACT_ROWS_PER_GRAPH,
)
from coding_trajectory.ingestion.common import canonical_json


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


class CollectorRecoveryRequest(CollectorModel):
    workspace_id: UUID
    agent_id: UUID
    project_id: UUID
    agent_instance_id: UUID | None = None
    vendor: str | None = None
    native_session_id: str | None = None
    graph_ids: list[UUID] | None = Field(default=None, max_length=128)
    publication_idempotency_key: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def validate_source(self) -> CollectorRecoveryRequest:
        if (self.vendor is None) != (self.native_session_id is None):
            raise ValueError(
                "source recovery requires vendor and native session identity"
            )
        return self


class RecoveredSource(CollectorModel):
    source_id: UUID
    source_epoch: int = Field(ge=1)
    next_source_sequence: int = Field(ge=0)
    content_sha256: str | None = None


class RecoveredGraph(CollectorModel):
    """One published graph fact set visible to collector recovery."""

    graph_id: UUID
    schema_version: Literal["ct.published_facts.v1"] = FACT_SET_SCHEMA_VERSION
    fact_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    fact_count: int = Field(ge=1, le=MAX_FACT_ROWS_PER_GRAPH)
    published_sequence: int = Field(ge=0)
    observed_at: datetime


class CollectorRecoveryResponse(CollectorModel):
    next_publication_sequence: int = Field(ge=0)
    next_living_sequence: int | None = Field(default=None, ge=1)
    source: RecoveredSource | None = None
    authority_incarnation: UUID | None = None
    authority_sequence: int | None = Field(default=None, ge=0)
    graphs: list[RecoveredGraph] = Field(default_factory=list)
    publication_receipt: dict[str, Any] | None = None


class ProjectRegistrationRequest(CollectorModel):
    version: Literal[1] = 1
    workspace_id: UUID
    agent_id: UUID
    display_name: str = Field(min_length=1)
    repository_identity: str | None = None
    aliases: list[str] = Field(default_factory=list)


class ProjectRegistrationResponse(CollectorModel):
    project_id: UUID
    revision: int = Field(gt=0)
    committed_sequence: int = Field(gt=0)


class SourceCheckpoint(CollectorModel):
    segments: list[int] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_offsets(self) -> SourceCheckpoint:
        if any(offset < 1 for offset in self.segments):
            raise ValueError("source checkpoint offsets must be positive")
        return self


class SourceCheckpointPayload(CollectorModel):
    kind: Literal["ct.source_checkpoint.v1"] = "ct.source_checkpoint.v1"
    source_checkpoint: SourceCheckpoint
    chronicle_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ObservationRequest(CollectorModel):
    """One idempotent, metadata-only source checkpoint."""

    version: Literal[1] = 1
    workspace_id: UUID
    agent_id: UUID
    source_id: UUID
    source_epoch: int = Field(ge=1)
    source_sequence: int = Field(ge=0)
    event_id: str = Field(min_length=1)
    schema_version: Literal["ct.source_checkpoint.v1"] = "ct.source_checkpoint.v1"
    parser_version: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_current_checkpoint(self) -> ObservationRequest:
        SourceCheckpointPayload.model_validate(self.payload)
        digest = hashlib.sha256(canonical_json(self.payload).encode()).hexdigest()
        if digest != self.content_sha256:
            raise ValueError("source checkpoint digest mismatch")
        if self.event_id != f"checkpoint:{self.content_sha256}":
            raise ValueError("source checkpoint event identity mismatch")
        return self


class ObservationReceipt(CollectorModel):
    receipt_id: UUID
    outcome: Literal["accepted", "duplicate", "rejected", "conflict"]
    committed_sequence: int | None = Field(default=None, ge=1)
    details: dict[str, Any] = Field(default_factory=dict)


class SourceVectorEntry(CollectorModel):
    source_id: UUID
    source_epoch: int = Field(ge=1)
    source_sequence: int = Field(ge=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


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


class LivingObservationRequest(CollectorModel):
    """One contract-valid living change projected on its owning host."""

    version: Literal[1] = 1
    workspace_id: UUID
    agent_id: UUID
    agent_instance_id: UUID
    observation_sequence: int = Field(ge=1)
    observed_at: datetime
    kind: Literal["living.events", "living.sessions"]
    payload: dict[str, Any]


class LivingObservationReceipt(CollectorModel):
    committed_sequence: int = Field(ge=1)
