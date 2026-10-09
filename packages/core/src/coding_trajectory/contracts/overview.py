"""Self-contained live overview display records."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_serializer

from coding_trajectory.contracts.base import ContractModel, RequestModel


class OverviewPage(RequestModel):
    direction: Literal["older"] = "older"
    requested_limit: int = Field(ge=1, le=200)
    start_ordinal: int = Field(ge=0)
    end_ordinal_exclusive: int = Field(ge=0)
    returned: int = Field(ge=0)
    total: int = Field(ge=0)
    has_more: bool
    next_cursor: str | None


class ContentCount(RequestModel):
    total: int = Field(ge=0)
    returned: int = Field(ge=0)
    truncated: bool


class TurnContentCoverage(RequestModel):
    assistant_responses: ContentCount
    activities: ContentCount
    item_ids: ContentCount
    text_trimmed: bool


class OverviewSession(RequestModel):
    session_id: str
    session_rank: int = Field(ge=0)
    run_root_session_id: str
    parent_session_id: str | None
    parent_in_run: bool
    edge_type: str | None
    relationship: str
    status: str
    latest_turn_status: str | None
    started_at: datetime | None
    ended_at: datetime | None
    vendor: str
    title: str | None
    model: str | None
    agent_name: str | None
    cwd: str | None
    agent_path: str | None
    multi_agent_version: str | None
    multi_agent_mode: str | None
    source_turn_total: int = Field(ge=0)
    narrative_turn_total: int = Field(ge=0)
    filtered_turn_total: int = Field(ge=0)


class OverviewRequestPreview(RequestModel):
    content: str = Field(max_length=280)
    source: str | None
    event_id: str | None


class OverviewAssistant(RequestModel):
    item_id: str
    preview: str = Field(max_length=280)


class OverviewActivity(RequestModel):
    """One semantic activity cell, not one canonical item.

    Fields mirror build_overview_flows; descriptions and evidence membership
    belong to that projector. Per-turn caps are semantic display bounds.
    """

    tool: str
    item_ids: list[str] = Field(min_length=1)
    count: int | None = Field(default=None, ge=1)
    status: str | None = None
    outcome: Literal["succeeded", "failed"] | None = None
    wrapper_status: str | None = None
    cmd: str | None = None
    commands: list[str] | None = None
    path: str | None = None
    paths: list[str] | None = None
    path_counts: dict[str, int] | None = None
    query: str | None = None
    queries: list[str] | None = None
    url: str | None = None
    urls: list[str] | None = None
    target: str | None = None
    targets: list[str] | None = None
    items: str | None = None
    task: str | None = None
    session: str | None = None


class OverviewReferences(RequestModel):
    item_ids: list[str] = Field(max_length=100)
    user_request_event_id: str | None


class OverviewTurn(RequestModel):
    global_ordinal: int = Field(ge=0)
    session_id: str
    session_narrative_ordinal: int = Field(ge=0)
    source_turn_ordinal: int = Field(ge=0)
    turn_id: str
    source_sequence: int = Field(ge=0)
    started_at: datetime | None
    ended_at: datetime | None
    timestamp_state: Literal["complete", "partial", "missing"]
    status: str
    user_request: OverviewRequestPreview | None
    assistant_responses: list[OverviewAssistant] = Field(max_length=8)
    activities: list[OverviewActivity] = Field(max_length=8)
    refs: OverviewReferences
    content_coverage: TurnContentCoverage

    @field_serializer("activities")
    def serialize_activities(self, activities: list[OverviewActivity]):
        # Preserve the projector's sparse cells rather than inventing null fields.
        return [activity.model_dump(exclude_none=True) for activity in activities]


class OverviewResponse(ContractModel):
    graph_id: str
    root_session_id: str
    lineage_root_session_id: str
    entrypoint_id: str
    project: dict[str, str | None]
    orchestration: dict[str, Any]
    fork_origin: dict[str, Any] | None
    totals: dict[str, int]
    sessions: list[OverviewSession]
    edges: list[dict[str, Any]]
    turns: list[OverviewTurn]
    page: OverviewPage
    coverage: dict[str, Any]
