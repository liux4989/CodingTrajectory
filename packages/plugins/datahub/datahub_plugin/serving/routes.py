"""Canonical Datahub query-method inventory.

HTTP is deliberately reduced to one ``POST /api/datahub/query`` protocol.
These method names remain transport-independent so local and hosted adapters
can expose different capability coverage without inventing different routes.
"""

from __future__ import annotations

from dataclasses import dataclass

DATAHUB_PROTOCOL = "ct.datahub.v1"
DATAHUB_ENDPOINT = "/api/datahub/query"


@dataclass(frozen=True, slots=True)
class QueryMethod:
    name: str
    handler: str
    params: tuple[str, ...] = ()


METHODS: tuple[QueryMethod, ...] = (
    QueryMethod("datahub.snapshot", "snapshot"),
    QueryMethod("datahub.changes", "changes", ("after_revision",)),
    QueryMethod("overview", "overview", ("since_days",)),
    QueryMethod("today", "today"),
    QueryMethod("projects", "projects", ("agent_vendor", "limit", "cursor")),
    QueryMethod("project.detail", "project_detail", ("project_name", "since_days", "limit", "cursor")),
    QueryMethod("sessions", "sessions", ("since_days", "project_name", "agent_vendor", "limit", "cursor")),
    QueryMethod("sessions.timeline", "session_timeline", ("since_days", "limit", "cursor")),
    QueryMethod("session.context-window", "context_window", ("session_id", "turn_id")),
    QueryMethod("session.graph", "graph_detail", ("session_id",)),
    QueryMethod("session.tree", "session_tree", ("session_id",)),
    QueryMethod("session.evidence-timeline", "session_evidence_timeline", ("session_id",)),
    QueryMethod("session.events", "session_event_details", ("event_ids", "turn_id", "type")),
    QueryMethod("session.items", "session_item_details", ("item_ids", "include_content", "turn_id")),
    QueryMethod("model-usage", "model_usage", ("since_days", "project_name", "model_key", "detail", "limit", "cursor", "revision")),
    QueryMethod("token-efficiency.project", "token_efficiency_project", ("project_name", "since_days", "limit", "cursor", "detail", "grain")),
    QueryMethod("code-time.report", "code_time_report", ("window", "project", "agent_vendor")),
    QueryMethod("code-time.forecasts", "code_time_forecasts", ("kind", "project", "target_harness_name", "status", "limit")),
    QueryMethod("code-time.calibration", "code_time_calibration", ("kind", "project", "target_harness_name", "target_model", "estimator_model")),
    QueryMethod("datahub.refresh", "request_refresh"),
)

METHODS_BY_NAME = {method.name: method for method in METHODS}

__all__ = ["DATAHUB_ENDPOINT", "DATAHUB_PROTOCOL", "METHODS", "METHODS_BY_NAME", "QueryMethod"]
