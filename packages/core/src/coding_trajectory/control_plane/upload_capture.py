"""Frozen canonical repository → upload outbox contracts."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coding_trajectory.control_plane.chronicle import ChronicleGraphArtifact
from coding_trajectory.control_plane.upload_chunks import MAX_GRAPH_BYTES


class CaptureSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    vendor: str = Field(min_length=1, max_length=64)
    native_session_id: str = Field(min_length=1, max_length=128)
    segments: tuple[int, ...] = Field(min_length=1, max_length=1000)
    chronicle_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    source_generation: str = Field(default="legacy-v1", min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_offsets(self):
        if any(offset < 1 for offset in self.segments):
            raise ValueError("capture requires complete positive source offsets")
        if self.observed_at.tzinfo is None:
            raise ValueError("capture timestamps require a timezone")
        return self


class CanonicalCapture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact: ChronicleGraphArtifact
    sources: tuple[CaptureSource, ...] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_scope(self):
        for session in self.artifact.sessions:
            for turn in session.turns:
                if (
                    turn.user_request is not None
                    and turn.user_request.content != "[content omitted]"
                ) or any(
                    item.measurements.text_preview is not None for item in turn.items
                ):
                    raise ValueError("upload captures must omit conversation bodies")
        if len(self.artifact.canonical_bytes()) > MAX_GRAPH_BYTES:
            raise ValueError("canonical graph exceeds the 8 MiB compatibility ceiling")
        identities = {
            (source.vendor, source.native_session_id) for source in self.sources
        }
        sessions = {
            (session.vendor.value, str(session.session_id))
            for session in self.artifact.sessions
        }
        if len(identities) != len(self.sources) or identities != sessions:
            raise ValueError(
                "capture requires one complete source identity per session"
            )
        return self


class UploadCapturePage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    repository_id: str = Field(min_length=1, max_length=128)
    cursor: str = Field(min_length=1, max_length=4096)
    captures: tuple[CanonicalCapture, ...] = Field(min_length=1, max_length=16)
