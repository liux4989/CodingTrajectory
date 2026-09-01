"""Compatibility facade for the decomposed projection."""

# ruff: noqa: F401
from __future__ import annotations

from datahub_plugin.projections.read_models_contracts import *
from datahub_plugin.projections.read_models_contracts import (
    BuildIssue,
    GraphMaterialization,
    OverviewPayload,
    ProjectContributionPayload,
    ProjectDetailPayload,
    ProjectPayload,
    ReadModelBuild,
    ReadModelEntity,
    SessionPayload,
    SessionTimelinePayload,
    SourceGraphRelationship,
    TimelineBucketPayload,
    TimelineContributionPayload,
    TimelineSessionPayload,
    _StrictModel,
)
from datahub_plugin.projections.read_models_materialization import *
from datahub_plugin.projections.read_models_materialization import (
    build_read_models,
    build_read_models_from_discovery,
    materialize_graph,
)
from datahub_plugin.projections.read_models_reconstruction import *
from datahub_plugin.projections.read_models_reconstruction import (
    _activity_buckets,
    _build_status,
    _datahub_session_item,
    _debug_issues,
    _entity,
    _graph_cost,
    _graph_started_at,
    _issue,
    _number,
    _overview_activity,
    _overview_coverage,
    _overview_session,
    _parse_utc_datetime,
    _project_catalog_entities,
    _project_entities,
    _project_path,
    _project_scope_id,
    _recent_scope,
    _row_kind,
    _row_payload,
    _row_sort_key,
    _source_relationships,
    aggregate_read_models,
    reconstruct_overview,
    reconstruct_project_detail,
    reconstruct_projects,
    reconstruct_recent_work,
    reconstruct_session_timeline,
    reconstruct_sessions,
)
