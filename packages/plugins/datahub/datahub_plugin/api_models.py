"""Strict serialization contracts for Datahub-owned HTTP response payloads.

These models describe final route payloads, not persistence rows.  Canonical
service responses (graph and code-time APIs) deliberately remain outside this
module because their upstream contracts own those shapes.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from datahub_plugin.projections.context_window.models import ContextWindowProjection
from datahub_plugin.projections.session_timeline import SessionEvidenceTimeline
from datahub_plugin.projections.token_efficiency_models import ProjectProjection

DELIVERY_FAMILIES = (
    "overview",
    "projects",
    "sessions",
    "model-usage",
    "token-efficiency",
    "context-window",
    "session-timeline",
    "session-tree",
    "session-graph",
)
DeliveryFamily = Literal[*DELIVERY_FAMILIES]


class StrictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuntimeTotals(StrictResponse):
    execution_seconds: float
    wait_seconds: float
    turns: float
    tool_calls: float
    failed_tool_calls: float


class UsageTotals(StrictResponse):
    processed_tokens: int
    cost_usd: float
    known_cost_count: int
    missing_cost_count: int


class ProjectSummary(StrictResponse):
    project: str
    count: int
    vendors: dict[str, int]
    execution_seconds: float
    processed_tokens: int
    cost_usd: float
    known_cost_count: int


class SessionSummary(StrictResponse):
    id: str | None = None
    title: str | None = None
    project: str | None = None
    vendor: str
    vendors: list[str]
    started_at: str | None = None
    execution_seconds: int
    wait_seconds: int
    turns: int
    tool_calls: int
    failed_tool_calls: int
    processed_tokens: int


class WarningSummary(StrictResponse):
    session_id: str | None = None
    project: str
    message: str


class SessionOverview(StrictResponse):
    count: int
    window_days: int
    runtime: RuntimeTotals
    usage: UsageTotals
    top_projects: list[ProjectSummary]
    top_sessions: list[SessionSummary]
    warnings: list[WarningSummary]
    errors: list[Any]


class ProjectsOverview(StrictResponse):
    count: int
    vendors: dict[str, int]


class OverviewPayload(StrictResponse):
    schema_version: Literal[1]
    revision: int
    generated_at: str
    cohort: dict[str, int]
    coverage: dict[str, int]
    projects: ProjectsOverview
    sessions: SessionOverview
    warnings: list[WarningSummary]


class ActivityBucket(StrictResponse):
    bucket: str
    sessions: int
    execution_seconds: int
    processed_tokens: int
    cost_usd: float


class TodayPayload(OverviewPayload):
    hourly: list[ActivityBucket]
    daily: list[ActivityBucket]
    warnings: list[WarningSummary]


class ProjectItem(StrictResponse):
    name: str
    path: str | None
    vendors: list[str]


class ProjectsPayload(StrictResponse):
    items: list[ProjectItem]
    page: CursorPageMetadata


class SessionItem(StrictResponse):
    root_session_id: str
    lineage_root_session_id: str | None = None
    graph_id: str | None = None
    vendors: list[str]
    session_ids: list[str]
    title: str | None = None
    preview: str | None = None
    project: str | None = None


class CursorPageMetadata(StrictResponse):
    revision: int
    next_cursor: str | None
    has_more: bool


class SessionPage(StrictResponse):
    items: list[SessionItem]
    page: CursorPageMetadata


class DatahubFreshness(StrictResponse):
    last_refresh_at: str | None
    lag_seconds: float | None


class DatahubSourceStatus(StrictResponse):
    ready: int
    ingesting: int
    failed: int
    incomplete: int


class BootstrapStatus(StrictResponse):
    ready: bool
    scan_started_at: str | None
    scan_finished_at: str | None
    error: str | None
    last_result: dict[str, Any] | None = None
    coverage: dict[str, Any] | None = None


class DatahubSnapshot(StrictResponse):
    revision: int
    generated_at: str
    freshness: DatahubFreshness
    catching_up: bool
    source_status: DatahubSourceStatus
    minimum_available_revision: int
    bootstrap: BootstrapStatus


class DatahubUpsert(StrictResponse):
    entity_type: str
    entity_id: str
    revision: int
    payload: Any


class DatahubDeletion(StrictResponse):
    entity_type: str
    entity_id: str
    revision: int


class DatahubChanges(StrictResponse):
    from_revision: int
    to_revision: int
    reset_required: bool
    upserts: list[DatahubUpsert]
    deletions: list[DatahubDeletion]
    invalidations: list[str]
    freshness: DatahubFreshness
    catching_up: bool
    source_status: DatahubSourceStatus


class ContextWindowPayload(ContextWindowProjection):
    pass


class SessionEvidenceTimelinePayload(SessionEvidenceTimeline):
    pass


class TokenEfficiencyProjectPayload(ProjectProjection):
    pass


API_RESPONSE_MODELS = (
    OverviewPayload,
    TodayPayload,
    ProjectsPayload,
    SessionPage,
    ContextWindowPayload,
    SessionEvidenceTimelinePayload,
    TokenEfficiencyProjectPayload,
    DatahubSnapshot,
    DatahubChanges,
)

API_RESPONSE_BY_HANDLER: dict[str, type[BaseModel]] = {
    "overview": OverviewPayload,
    "today": TodayPayload,
    "projects": ProjectsPayload,
    "sessions": SessionPage,
    "context_window": ContextWindowPayload,
    "session_evidence_timeline": SessionEvidenceTimelinePayload,
    "token_efficiency_project": TokenEfficiencyProjectPayload,
    "snapshot": DatahubSnapshot,
    "changes": DatahubChanges,
}


def validate_api_response(handler: str, payload: Any) -> None:
    """Check generated route contracts without rewriting the wire payload."""

    model = API_RESPONSE_BY_HANDLER.get(handler)
    if model is not None:
        model.model_validate(payload)
