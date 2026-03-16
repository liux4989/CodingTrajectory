"""Shared models for enrichment notes."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class EnrichmentNote(BaseModel):
    subject: str
    message: str
    provenance: Literal["observed", "derived", "synthetic"] = "derived"
    confidence: Literal["high", "medium", "low"] = "medium"
    evidence_event_ids: list[UUID] = Field(default_factory=list)
