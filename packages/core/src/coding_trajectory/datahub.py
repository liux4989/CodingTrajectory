"""Trusted first-party projection boundary for the Datahub backend.

This module is intentionally narrower than the general core package.  It exposes
only the stable building blocks Datahub needs to discover and plan source work,
materialize canonical graphs/facts and project catalogs, and lazily reconstruct
retained event/item detail.  It is not a supported extension API for third-party
plugins; additions should correspond to a durable Datahub projection need.
"""

from __future__ import annotations

from pathlib import Path

from collections.abc import Iterable
from typing import Any

from coding_trajectory.analysis.graph_views import build_graph_overview
from coding_trajectory.analysis.measurements import (
    MeasurementMismatchError,
    attach_measurements,
)
from coding_trajectory.analysis.projections import build_item_details
from coding_trajectory.contracts import service_contract
from coding_trajectory.discovery import (
    DiscoveryResult,
    DiscoverySource,
    _matching_vendor_configs,
    discover_store,
    scan_parent_turn_ids,
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
    SourceInput,
    IncrementalGraphBuild,
    MessagesForPath,
    SourceGraphRelationship,
    plan_session_graph_components_from_files,
    rebuild_affected_session_graphs,
    rebuild_affected_session_graphs_from_files,
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
from coding_trajectory.token_counter import (
    counter_for_session_graph,
    scoped_counter,
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


def rebuild_affected_session_graphs_with_measurements(
    *,
    sources: Iterable[SourceInput],
) -> IncrementalGraphBuild:
    """Rebuild compact graphs and attach exact content measurements.

    Evidence projections (exact statistics, tool attribution, composition)
    consume precomputed primitives instead of a resident full trajectory.
    Per source, a transient full-fidelity ingest measures bodies with the
    graph's effective tokenizer and is discarded immediately; canonical ids
    line up with the compact graph by construction.
    """

    build = rebuild_affected_session_graphs_from_files(
        sources=sources, retention="measurements"
    )
    if not build.graphs:
        return build
    provenance_by_session = {str(prov.session_id): prov for prov in build.provenance}

    selected = sorted(build.selected_source_paths)
    candidates: list[tuple[Vendor, Any, Path]] = []
    for raw_path in selected:
        path = Path(raw_path)
        for vendor, adapter_cls, _base_dir, _pattern in _matching_vendor_configs(path):
            candidates.append((vendor, adapter_cls, path))
            break
    cut_inputs = scan_parent_turn_ids(candidates)
    adapter_cls_by_path = {path: cls for _v, cls, path in candidates}

    for graph in build.graphs:
        counter = counter_for_session_graph(graph)
        with scoped_counter(counter):
            for session in graph.sessions:
                prov = provenance_by_session.get(str(session.session_id))
                if prov is None:
                    raise MeasurementMismatchError(
                        f"no provenance for session {session.session_id}"
                    )
                source_path = Path(prov.source_path)
                adapter_cls = adapter_cls_by_path.get(source_path)
                if adapter_cls is None:
                    raise MeasurementMismatchError(
                        f"no vendor adapter for source {prov.source_path}"
                    )
                adapter = adapter_cls()
                full_session = adapter.ingest_file(
                    source_path,
                    parent_started_turn_ids=cut_inputs.get(source_path),
                )
                full_session = stabilize_session(
                    full_session, vendor=prov.vendor, source=source_path
                )
                attach_measurements(session, full_session)
    return build



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
