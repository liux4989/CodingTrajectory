"""Trusted first-party projection boundary for the Datahub backend.

This module is intentionally narrower than the general core package.  It exposes
only the stable building blocks Datahub needs to discover and plan source work,
materialize canonical graphs/facts and project catalogs, and lazily reconstruct
retained event/item detail.  It is not a supported extension API for third-party
plugins; additions should correspond to a durable Datahub projection need.
"""

from __future__ import annotations

from pathlib import Path

from coding_trajectory.analysis.graph_views import build_graph_overview
from coding_trajectory.analysis.projections import build_item_details
from coding_trajectory.contracts import service_contract
from coding_trajectory.discovery import (
    DiscoveryResult,
    DiscoverySource,
    discover_store,
    stabilize_session,
)
from coding_trajectory.ingestion.adapters.claude_code import ClaudeCodeAdapter
from coding_trajectory.ingestion.adapters.codex import CodexAdapter
from coding_trajectory.ingestion.adapters.pi import PiAdapter
from coding_trajectory.ingestion.common import (
    canonical_json,
    last_complete_line_offset,
    normalize_project_key,
)
from coding_trajectory.ingestion.incremental import (
    GraphBuildIssue,
    IncrementalGraphBuild,
    MessagesForPath,
    SourceGraphRelationship,
    plan_session_graph_components_from_files,
    rebuild_affected_session_graphs,
    rebuild_affected_session_graphs_from_files,
    rebuild_affected_session_graphs_with_measurements,
)
from coding_trajectory.ingestion.indexes import (
    SessionGraphIndex,
    build_session_graph_index,
)
from coding_trajectory.ingestion.models import (
    PlanItem,
    Session,
    SessionEdge,
    SessionGraph,
    Vendor,
)
from coding_trajectory.ingestion.provenance import SessionProvenance
from coding_trajectory.query import (
    DocumentError,
    DocumentStore,
    ResourceNotFoundError,
)
from coding_trajectory.service import (
    IndexCache,
    dispatch,
    project_list_metadata,
    serialize_event_detail,
)

_DETAIL_ADAPTERS = {
    Vendor.CODEX_CLI: CodexAdapter,
    Vendor.CLAUDE_CODE: ClaudeCodeAdapter,
    Vendor.PI: PiAdapter,
}


def hydrate_retained_session(
    source: Path,
    *,
    vendor: Vendor,
    parent_source: Path | None = None,
) -> Session:
    """Re-ingest one retained source at full fidelity for lazy Datahub detail.

    The optional parent source preserves the same inherited-turn cut used by
    graph ingestion, keeping canonical identifiers stable during hydration.
    """

    adapter_cls = _DETAIL_ADAPTERS[vendor]
    adapter = adapter_cls()
    parent_started = (
        adapter.scan_started_turn_ids(parent_source)
        if parent_source is not None
        else None
    )
    session = adapter.ingest_file(source, parent_started_turn_ids=parent_started)
    return stabilize_session(session, vendor=vendor, source=source)


__all__ = [
    "DiscoveryResult",
    "DiscoverySource",
    "DocumentError",
    "DocumentStore",
    "GraphBuildIssue",
    "IncrementalGraphBuild",
    "IndexCache",
    "MessagesForPath",
    "PlanItem",
    "ResourceNotFoundError",
    "Session",
    "SessionEdge",
    "SessionGraph",
    "SessionGraphIndex",
    "SessionProvenance",
    "SourceGraphRelationship",
    "Vendor",
    "build_graph_overview",
    "build_item_details",
    "build_session_graph_index",
    "canonical_json",
    "discover_store",
    "dispatch",
    "hydrate_retained_session",
    "last_complete_line_offset",
    "normalize_project_key",
    "plan_session_graph_components_from_files",
    "project_list_metadata",
    "rebuild_affected_session_graphs",
    "rebuild_affected_session_graphs_from_files",
    "rebuild_affected_session_graphs_with_measurements",
    "serialize_event_detail",
    "service_contract",
]
