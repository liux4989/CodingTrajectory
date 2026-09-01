"""In-process builders for durable datahub read models.

The builder in this module deliberately stops at storage-neutral entity records.
It performs canonical core discovery once, invokes versioned core service handlers
against that in-memory store, and returns Pydantic-validated records that a durable
store can upsert.  Route reconstruction helpers operate on the already-validated
payload dictionaries so a hot read does not rebuild canonical models row by row.
"""

# ruff: noqa: F401
from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from coding_trajectory import debug
from coding_trajectory.datahub import (
    DiscoveryResult,
    DiscoverySource,
    DocumentError,
    DocumentStore,
    IndexCache,
    SessionGraph,
    discover_store,
    dispatch,
)
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_RECENT_HORIZON_DAYS = 7

EntityKind = Literal[
    "project_catalog",
    "project_contribution",
    "session_timeline_contribution",
    "project",
    "session",
    "overview",
    "session_timeline",
    "project_detail",
]
IssueDisposition = Literal["failed", "inconclusive"]
BuildStatus = Literal["success", "partial", "failed", "inconclusive"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectPayload(_StrictModel):
    name: str
    path: str | None = None
    vendors: list[str] = Field(default_factory=list)


class ProjectContributionPayload(ProjectPayload):
    root_session_id: str


class SessionPayload(BaseModel):
    """Canonical ``project.sessions`` row plus overview-only cost evidence."""

    model_config = ConfigDict(extra="allow")

    root_session_id: str
    graph_id: str | None = None
    project: str | None = None
    title: str | None = None
    preview: str | None = None
    vendors: list[str] = Field(default_factory=list)
    session_ids: list[str] = Field(default_factory=list)
    runtime: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    cost_usd: float | None = None
    pricing_confidence: str | None = None


class TimelineSessionPayload(_StrictModel):
    id: str
    title: str | None = None
    v: list[str] = Field(default_factory=list)


class TimelineContributionPayload(_StrictModel):
    date: str
    session: TimelineSessionPayload


class TimelineBucketPayload(_StrictModel):
    date: str
    count: int = Field(ge=0)
    sessions: list[TimelineSessionPayload] = Field(default_factory=list)


class SessionTimelinePayload(_StrictModel):
    timeline: list[TimelineBucketPayload] = Field(default_factory=list)
    total: int = Field(ge=0)


class ProjectDetailPayload(ProjectPayload):
    since_days: int | None = None
    sessions: list[dict[str, Any]] = Field(default_factory=list)
    session_count: int = Field(ge=0)


class OverviewPayload(_StrictModel):
    projects: dict[str, Any]
    sessions: dict[str, Any]


class ReadModelEntity(_StrictModel):
    """One storage-neutral, materializable datahub entity."""

    entity_kind: EntityKind
    entity_id: str
    scope_id: str
    sort_key: str
    payload: dict[str, Any]
    root_session_id: str | None = None
    project_name: str | None = None

    def as_mutation(self) -> dict[str, Any]:
        """Return the generic mutation shape used by the incremental store."""

        return {
            "entity_kind": self.entity_kind,
            "entity_key": self.entity_id,
            "scope_key": self.scope_id,
            "partition_key": self.project_name or "",
            "sort_key": self.sort_key,
            "tiebreaker": self.root_session_id or self.entity_id,
            "payload": self.payload,
        }


class SourceGraphRelationship(_StrictModel):
    """Authoritative source-file membership captured by canonical discovery."""

    source_path: str
    root_session_id: str
    vendor: str
    size: int | None = Field(default=None, ge=0)
    mtime_ns: int | None = Field(default=None, ge=0)


class BuildIssue(_StrictModel):
    """A retained failure or inconclusive outcome from one build stage."""

    issue_id: str
    disposition: IssueDisposition
    stage: str
    message: str
    code: str | None = None
    source_path: str | None = None
    root_session_id: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class GraphMaterialization(_StrictModel):
    root_session_id: str
    entities: list[ReadModelEntity] = Field(default_factory=list)
    issues: list[BuildIssue] = Field(default_factory=list)


class ReadModelBuild(_StrictModel):
    status: BuildStatus
    scope_id: str
    since_days: int = Field(ge=1)
    built_at: datetime
    entities: list[ReadModelEntity] = Field(default_factory=list)
    source_relationships: list[SourceGraphRelationship] = Field(default_factory=list)
    issues: list[BuildIssue] = Field(default_factory=list)
