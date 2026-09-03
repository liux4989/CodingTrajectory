"""Versioned contracts shared by the local collector and remote ingress.

These models describe checkpoint metadata and bounded shareable artifacts, not
vendor JSONL records. The source files stay on the host that collected them.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coding_trajectory.control_plane.shareable import (
    MAX_SHAREABLE_PUBLICATION_BYTES,
    SHAREABLE_GRAPH_SCHEMA_VERSION,
    ShareableGraphArtifact,
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
    shareable_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


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

    @model_validator(mode="after")
    def validate_current_checkpoint(self) -> ObservationRequest:
        if self.schema_version != "ct.source_checkpoint.v1":
            return self
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


class ShareableArtifactPublication(CollectorModel):
    artifact_id: UUID
    schema_version: Literal["ct.shareable_graph.v1"] = SHAREABLE_GRAPH_SCHEMA_VERSION
    payload: ShareableGraphArtifact
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    serialized_bytes: int = Field(ge=1)
    source_ids: list[UUID] = Field(min_length=1)
    observed_at: datetime

    @model_validator(mode="after")
    def validate_artifact(self) -> ShareableArtifactPublication:
        if self.artifact_id != self.payload.graph.root_session_id:
            raise ValueError("artifact_id must match the shareable graph root")
        if self.content_sha256 != self.payload.digest():
            raise ValueError("shareable artifact digest mismatch")
        if self.serialized_bytes != len(self.payload.canonical_bytes()):
            raise ValueError("shareable artifact byte count mismatch")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("shareable artifact source_ids must be unique")
        return self


class ArtifactPublicationRequest(CollectorModel):
    """One complete, locally assembled project artifact publication."""

    version: Literal[1] = 1
    workspace_id: UUID
    agent_id: UUID
    project_id: UUID
    publication_sequence: int = Field(ge=0)
    source_vector: list[SourceVectorEntry] = Field(min_length=1)
    artifacts: list[ShareableArtifactPublication] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_publication(self) -> ArtifactPublicationRequest:
        source_ids = [entry.source_id for entry in self.source_vector]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("publication source_vector must contain unique sources")
        known = set(source_ids)
        artifact_ids = [artifact.artifact_id for artifact in self.artifacts]
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("publication artifact_id values must be unique")
        if any(not set(artifact.source_ids) <= known for artifact in self.artifacts):
            raise ValueError("artifact source_ids must be present in source_vector")
        represented = {
            source_id
            for artifact in self.artifacts
            for source_id in artifact.source_ids
        }
        if represented != known:
            raise ValueError("every source_vector entry must belong to an artifact")
        encoded = canonical_json(
            self.model_dump(mode="json", exclude_none=True)
        ).encode()
        if len(encoded) > MAX_SHAREABLE_PUBLICATION_BYTES:
            raise ValueError("shareable project publication exceeds 16 MiB")
        return self


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
