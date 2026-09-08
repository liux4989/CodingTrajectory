"""Authoritative Datahub HTTP route inventory."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Route:
    method: str
    pattern: str
    handler: str
    query: tuple[str, ...] = ()
    streaming: bool = False


ROUTES: tuple[Route, ...] = (
    Route("GET", "/api/datahub/events", "revision_events", streaming=True),
    Route("GET", "/api/datahub/snapshot", "snapshot"),
    Route("GET", "/api/datahub/changes", "changes", ("after_revision",)),
    Route("GET", "/api/overview", "overview", ("since_days",)),
    Route("GET", "/api/today", "today"),
    Route("GET", "/api/projects", "projects", ("agent_vendor", "limit", "cursor")),
    Route(
        "GET",
        "/api/projects/detail",
        "project_detail",
        ("project_name", "since_days", "limit", "cursor"),
    ),
    Route(
        "GET",
        "/api/sessions",
        "sessions",
        ("since_days", "project_name", "agent_vendor", "limit", "cursor"),
    ),
    Route(
        "GET",
        "/api/sessions/timeline",
        "session_timeline",
        ("since_days", "limit", "cursor"),
    ),
    Route(
        "GET",
        "/api/sessions/context-window",
        "context_window",
        ("session_id", "turn_id"),
    ),
    Route("GET", "/api/sessions/graph", "graph_detail", ("session_id",)),
    Route("GET", "/api/sessions/tree", "session_tree", ("session_id",)),
    Route(
        "GET",
        "/api/sessions/evidence-timeline",
        "session_evidence_timeline",
        ("session_id",),
    ),
    Route(
        "GET",
        "/api/sessions/events",
        "session_event_details",
        ("event_ids", "turn_id", "type"),
    ),
    Route(
        "GET",
        "/api/sessions/items",
        "session_item_details",
        ("item_ids", "include_content", "turn_id"),
    ),
    Route(
        "GET",
        "/api/model-usage",
        "model_usage",
        (
            "since_days",
            "project_name",
            "model_key",
            "detail",
            "limit",
            "cursor",
            "revision",
        ),
    ),
    Route(
        "GET",
        "/api/token-efficiency/project",
        "token_efficiency_project",
        ("project_name", "since_days", "limit", "cursor", "detail", "grain"),
    ),
    Route(
        "GET",
        "/api/code-time/report",
        "code_time_report",
        ("window", "project", "agent_vendor"),
    ),
    Route(
        "GET",
        "/api/code-time/forecasts",
        "code_time_forecasts",
        ("kind", "project", "target_harness_name", "status", "limit"),
    ),
    Route(
        "GET",
        "/api/code-time/calibration",
        "code_time_calibration",
        ("kind", "project", "target_harness_name", "target_model", "estimator_model"),
    ),
    Route("POST", "/api/refresh", "request_refresh"),
)

__all__ = ["ROUTES", "Route"]
