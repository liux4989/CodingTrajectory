"""In-process builders for durable datahub read models.

The builder in this module deliberately stops at storage-neutral entity records.
It performs canonical core discovery once, invokes versioned core service handlers
against that in-memory store, and returns Pydantic-validated records that a durable
store can upsert.  Route reconstruction helpers operate on the already-validated
payload dictionaries so a hot read does not rebuild canonical models row by row.
"""

# ruff: noqa: F401, I001
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
from datahub_plugin.projections.read_models_contracts import (
    BuildIssue,
    GraphMaterialization,
    ProjectContributionPayload,
    ReadModelBuild,
    ReadModelEntity,
    SessionPayload,
    TimelineContributionPayload,
    TimelineSessionPayload,
)
from datahub_plugin.projections.read_models_reconstruction import (
    _build_status,
    _debug_issues,
    _entity,
    _graph_cost,
    _graph_started_at,
    _issue,
    _project_catalog_entities,
    _project_path,
    _recent_scope,
    _source_relationships,
    aggregate_read_models,
)


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


def build_read_models(
    *,
    current_dir: Path,
    since_days: int = DEFAULT_RECENT_HORIZON_DAYS,
) -> ReadModelBuild:
    """Build the first datahub read-model slice from one canonical discovery.

    Transcript ingestion occurs exactly once.  Every metric-bearing session row
    is then produced by ``dispatch`` and its versioned Pydantic contract, never by
    duplicating runtime or token-accounting rules in the datahub plugin.
    """

    if since_days < 1:
        raise ValueError("since_days must be a positive integer")
    current_dir = current_dir.resolve()
    scope_id = _recent_scope(since_days)
    built_at = datetime.now(UTC)
    issues: list[BuildIssue] = []

    try:
        with debug.debug_scope() as discovery_debug:
            discovery = discover_store(
                current_dir=current_dir,
                global_scope=True,
                since_days=since_days,
            )
    except DocumentError as exc:
        issues.append(
            _issue(
                "inconclusive",
                stage="discovery",
                message=str(exc),
                code="read_model.discovery_empty",
            )
        )
        return ReadModelBuild(
            status="inconclusive",
            scope_id=scope_id,
            since_days=since_days,
            built_at=built_at,
            issues=issues,
        )
    except Exception as exc:  # noqa: BLE001 - retain adapter failure evidence
        issues.append(
            _issue(
                "failed",
                stage="discovery",
                message=str(exc),
                code="read_model.discovery_failed",
            )
        )
        return ReadModelBuild(
            status="failed",
            scope_id=scope_id,
            since_days=since_days,
            built_at=built_at,
            issues=issues,
        )

    return build_read_models_from_discovery(
        discovery,
        current_dir=current_dir,
        since_days=since_days,
        built_at=built_at,
        discovery_issues=_debug_issues(discovery_debug.as_records(), stage="discovery"),
    )


def build_read_models_from_discovery(
    discovery: DiscoveryResult,
    *,
    current_dir: Path,
    since_days: int = DEFAULT_RECENT_HORIZON_DAYS,
    built_at: datetime | None = None,
    discovery_issues: Sequence[BuildIssue] = (),
    project_catalog: Mapping[str, Any] | None = None,
) -> ReadModelBuild:
    """Project an already-discovered canonical store without re-reading JSONL.

    Bootstrap callers use this seam to share one in-memory ``DocumentStore``
    across core and analytical read-model builders.  It is intentionally
    separate from :func:`build_read_models`, whose public behavior still owns
    canonical global discovery for standalone callers.
    """

    if since_days < 1:
        raise ValueError("since_days must be a positive integer")
    current_dir = current_dir.resolve()
    scope_id = _recent_scope(since_days)
    issues = list(discovery_issues)
    relationships, relationship_issues = _source_relationships(discovery.sources)
    issues.extend(relationship_issues)

    source_paths_by_root: dict[str, list[str]] = {}
    for relationship in relationships:
        source_paths_by_root.setdefault(relationship.root_session_id, []).append(
            relationship.source_path
        )

    contribution_entities: list[ReadModelEntity] = []
    for graph in sorted(
        discovery.store.session_graphs.values(),
        key=lambda item: (item.project_identifier or "", str(item.root_session_id)),
    ):
        graph_result = materialize_graph(
            graph,
            current_dir=current_dir,
            since_days=since_days,
            source_paths=source_paths_by_root.get(str(graph.root_session_id), []),
        )
        contribution_entities.extend(graph_result.entities)
        issues.extend(graph_result.issues)

    catalog_entities = _project_catalog_entities(
        project_catalog or {}, scope_id=scope_id
    )
    entities = [
        *catalog_entities,
        *contribution_entities,
        *aggregate_read_models(
            [*catalog_entities, *contribution_entities], since_days=since_days
        ),
    ]
    status = _build_status(entities, issues)
    return ReadModelBuild(
        status=status,
        scope_id=scope_id,
        since_days=since_days,
        built_at=built_at or datetime.now(UTC),
        entities=entities,
        source_relationships=relationships,
        issues=issues,
    )


def materialize_graph(
    session_graph: SessionGraph,
    *,
    current_dir: Path,
    since_days: int = DEFAULT_RECENT_HORIZON_DAYS,
    project_metadata: Mapping[str, Any] | None = None,
    source_paths: Sequence[str] = (),
) -> GraphMaterialization:
    """Materialize one already-ingested graph for incremental replacement.

    The returned contribution entities are keyed by the graph root.  A durable
    store can replace those rows and rebuild project/overview aggregates from all
    current contribution and session rows without touching unrelated transcripts.
    """

    if since_days < 1:
        raise ValueError("since_days must be a positive integer")
    root_id = str(session_graph.root_session_id)
    scope_id = _recent_scope(since_days)
    issues: list[BuildIssue] = []
    store = DocumentStore.from_session_graphs([session_graph])
    cache = IndexCache()
    discovery_note = "\n".join(source_paths)

    try:
        with debug.debug_scope() as graph_debug:
            sessions_result = dispatch(
                "project.sessions",
                {"include": ["runtime", "usage"]},
                store=store,
                global_scope=True,
                current_dir=current_dir.resolve(),
                discovery_note=discovery_note,
                cache=cache,
            )
    except Exception as exc:  # noqa: BLE001 - retain projection failure evidence
        issues.append(
            _issue(
                "failed",
                stage="graph_projection",
                message=str(exc),
                code="read_model.graph_projection_failed",
                root_session_id=root_id,
            )
        )
        return GraphMaterialization(root_session_id=root_id, issues=issues)

    issues.extend(
        _debug_issues(
            graph_debug.as_records(),
            stage="graph_projection",
            root_session_id=root_id,
        )
    )
    try:
        with debug.debug_scope() as cost_debug:
            cost_usd, pricing_confidence = _graph_cost(
                store=store,
                root_session_id=root_id,
                current_dir=current_dir,
                cache=cache,
                discovery_note=discovery_note,
            )
    except Exception as exc:  # noqa: BLE001 - pricing is optional evidence
        cost_usd = None
        pricing_confidence = None
        issues.append(
            _issue(
                "inconclusive",
                stage="graph_cost",
                message=str(exc),
                code="read_model.graph_cost_failed",
                root_session_id=root_id,
            )
        )
    else:
        issues.extend(
            _debug_issues(
                cost_debug.as_records(),
                stage="graph_cost",
                root_session_id=root_id,
            )
        )
    rows = sessions_result.get("items") or []
    if not rows:
        issues.append(
            _issue(
                "inconclusive",
                stage="graph_projection",
                message="canonical project.sessions omitted a graph with no visible content",
                code="read_model.graph_not_visible",
                root_session_id=root_id,
            )
        )
        return GraphMaterialization(root_session_id=root_id, issues=issues)

    row = dict(rows[0])
    if cost_usd is not None:
        row["cost_usd"] = cost_usd
        row["pricing_confidence"] = pricing_confidence
    session_payload = SessionPayload.model_validate(row).model_dump(
        mode="json", exclude_none=True
    )
    runtime = session_payload.get("runtime") or {}
    started_at = str(runtime.get("started_at") or _graph_started_at(session_graph))
    project_name = str(session_payload.get("project") or "unknown")
    project_path = _project_path(session_graph, project_metadata)
    vendors = [str(value) for value in session_payload.get("vendors") or []]
    timeline_date = started_at[:10] if started_at else "unknown"

    session_entity = ReadModelEntity(
        entity_kind="session",
        entity_id=root_id,
        scope_id=scope_id,
        root_session_id=root_id,
        project_name=project_name,
        sort_key=f"{started_at}\x00{root_id}",
        payload=session_payload,
    )
    project_contribution = _entity(
        entity_kind="project_contribution",
        entity_id=root_id,
        scope_id=scope_id,
        root_session_id=root_id,
        project_name=project_name,
        sort_key=f"{project_name.casefold()}\x00{root_id}",
        payload_model=ProjectContributionPayload(
            name=project_name,
            path=project_path,
            vendors=vendors,
            root_session_id=root_id,
        ),
    )
    timeline_contribution = _entity(
        entity_kind="session_timeline_contribution",
        entity_id=root_id,
        scope_id=scope_id,
        root_session_id=root_id,
        project_name=project_name,
        sort_key=f"{timeline_date}\x00{started_at}\x00{root_id}",
        payload_model=TimelineContributionPayload(
            date=timeline_date,
            session=TimelineSessionPayload(
                id=root_id,
                title=session_payload.get("title"),
                v=vendors,
            ),
        ),
    )
    return GraphMaterialization(
        root_session_id=root_id,
        entities=[session_entity, project_contribution, timeline_contribution],
        issues=issues,
    )
