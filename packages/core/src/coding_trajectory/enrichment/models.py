"""Sidecar models for enrichment over canonical resources."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

class EnrichmentNote(BaseModel):
    subject: str
    message: str
    provenance: Literal["observed", "derived", "synthetic"] = "derived"
    confidence: Literal["high", "medium", "low"] = "medium"
    evidence_event_ids: list[UUID] = Field(default_factory=list)


class EnrichmentOverlay(BaseModel):
    structural: dict[str, Any] = Field(default_factory=dict)
    derived: dict[str, Any] = Field(default_factory=dict)
    agent_specific: dict[str, Any] = Field(default_factory=dict)
    plugins: dict[str, dict[str, Any]] = Field(default_factory=dict)
    notes: list[EnrichmentNote] = Field(default_factory=list)


class EnrichedTrajectory(BaseModel):
    trajectory_id: UUID
    enrichment: EnrichmentOverlay = Field(default_factory=EnrichmentOverlay)


class EnrichedSession(BaseModel):
    session_id: UUID
    enrichment: EnrichmentOverlay = Field(default_factory=EnrichmentOverlay)


class EnrichedTurn(BaseModel):
    turn_id: UUID
    enrichment: EnrichmentOverlay = Field(default_factory=EnrichmentOverlay)


class EnrichedStep(BaseModel):
    step_id: UUID
    enrichment: EnrichmentOverlay = Field(default_factory=EnrichmentOverlay)


class EnrichedEvent(BaseModel):
    event_id: UUID
    enrichment: EnrichmentOverlay = Field(default_factory=EnrichmentOverlay)
