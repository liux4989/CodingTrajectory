"""Pydantic ingress contracts for indexed committed-publication reads."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PublishedCatalogRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: UUID
    snapshot_sequence: int | None = Field(default=None, ge=0)
    cursor: str | None = Field(default=None, min_length=1, max_length=4096)
    limit: int = Field(default=50, ge=1, le=200)
    project_name: str | None = Field(default=None, min_length=1, max_length=256)
    agent_vendor: str | None = Field(default=None, min_length=1, max_length=64)
    since_days: int | None = Field(default=None, ge=1, le=36500)
    resource_id: UUID | None = None
    kind: Literal["sessions", "projects", "detail"] = "sessions"


class PublicationChangesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: UUID
    after_revision: int = Field(ge=0)
    snapshot_sequence: int | None = Field(default=None, ge=0)
    cursor: str | None = Field(default=None, min_length=1, max_length=4096)
    limit: int = Field(default=100, ge=1, le=200)
