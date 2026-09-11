"""Pydantic ingress contracts for indexed committed-publication reads."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from coding_trajectory.contracts.session import SessionGraphSummary


class ProjectionCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: UUID
    resource_projection_versions: list[int]


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
    kind: Literal["sessions", "projects", "detail", "status"] = "sessions"


class PublicationChangesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: UUID
    after_revision: int = Field(ge=0)
    snapshot_sequence: int | None = Field(default=None, ge=0)
    cursor: str | None = Field(default=None, min_length=1, max_length=4096)
    limit: int = Field(default=100, ge=1, le=200)


class CatalogReadRequest(BaseModel):
    """V2 selection-aware catalog; legacy sequence inputs are not accepted."""

    model_config = ConfigDict(extra="forbid")
    workspace_id: UUID
    kind: Literal["status", "projects", "sessions", "changes", "resources"]
    resource_kind: Literal["graph", "tree", "item"] | None = None
    resource_ids: list[UUID] = Field(default_factory=list, max_length=100)
    after_revision: int | None = Field(default=None, ge=0)
    selection: str | None = Field(default=None, min_length=1, max_length=128)
    cursor: str | None = Field(default=None, min_length=1, max_length=128)
    limit: int = Field(default=50, ge=1, le=200)
    population: Literal["registered", "published"] = "published"
    project_name: str | None = Field(default=None, min_length=1, max_length=256)
    agent_vendor: str | None = Field(default=None, min_length=1, max_length=64)
    since_days: int | None = Field(default=None, ge=1, le=36500)
    modified_since: datetime | None = None
    include: list[Literal["runtime", "usage"]] = Field(
        default_factory=list, max_length=2
    )


class CatalogSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str
    authority_incarnation: UUID
    publication_revision: int = Field(ge=0)
    project_metadata_revision: int = Field(ge=0)
    workspace_sequence: int | None = Field(default=None, ge=0)
    evaluated_at: datetime
    expires_at: datetime


class CatalogProject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["project"] = "project"
    project_id: UUID
    name: str
    modified_at: datetime | None
    vendors: list[str]


class CatalogSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["session"] = "session"
    artifact_id: UUID
    project_id: UUID
    revision: int = Field(ge=0)
    coverage: Literal["complete", "projection_unavailable"]
    projection: SessionGraphSummary | None


class CatalogChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["change"] = "change"
    artifact_id: UUID
    revision: int = Field(ge=0)
    deleted: bool


class ResourceProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource_kind: Literal["graph", "tree", "item"]
    resource_id: UUID
    page_index: int = Field(default=0, ge=0, le=9999)
    page_count: int = Field(default=1, ge=1, le=10000)
    coverage: Literal["complete", "budget_exceeded"]
    payload: dict[str, Any] | None


class ResourceProjections(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["ct.resource_projections.v1", "ct.resource_projections.v2"]
    rows: list[ResourceProjection] = Field(max_length=10000)


class CatalogResource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["resource"] = "resource"
    resource_kind: Literal["graph", "tree", "item"]
    resource_id: UUID
    page_index: int = Field(default=0, ge=0)
    page_count: int = Field(default=1, ge=1)
    coverage: Literal[
        "complete", "not_found", "projection_unavailable", "budget_exceeded"
    ]
    payload: dict[str, Any] | None


class CatalogReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["ct.catalog.v2"]
    workspace_id: UUID
    selection: CatalogSelection
    last_publication_at: datetime | None
    authority_observed_at: datetime
    minimum_available_revision: int = Field(ge=0)
    minimum_project_metadata_revision: int = Field(ge=0)
    coverage: Literal["complete", "partial", "unknown"]
    items: list[CatalogProject | CatalogSession | CatalogChange | CatalogResource]
    next_cursor: str | None
    reset_required: bool = False
