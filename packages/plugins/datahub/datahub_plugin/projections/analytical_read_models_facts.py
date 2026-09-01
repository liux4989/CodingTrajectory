"""Persistent normalized rows for the datahub's analytical projections.

JSONL and :class:`~coding_trajectory.query.DocumentStore` remain authoritative.
This module only decomposes existing datahub projections into generic,
disposable entities suitable for ``IncrementalStore``.  Projection builders run
in process against one already-built store; hot reconstruction works directly
with payload dictionaries and deliberately does not validate every detail row
with Pydantic again.
"""

# ruff: noqa: F401
from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from coding_trajectory.datahub import (
    DocumentError,
    DocumentStore,
    IndexCache,
    ResourceNotFoundError,
    SessionGraph,
    build_graph_overview,
    canonical_json,
    dispatch,
    normalize_project_key,
    service_contract,
)
from coding_trajectory.metrics import (
    build_session_graph_stats,
    build_session_graph_usage,
    iter_graph_economics_contributions,
)
from coding_trajectory.runtime import ServiceApiClient
from pydantic import ValidationError

from datahub_plugin.projections import model_usage, token_efficiency

if TYPE_CHECKING:
    from datahub_plugin.projections.analytical_read_models_clients import (
        DocumentStoreApiClient,
    )

type Mutation = dict[str, Any]
type PersistedRow = Mapping[str, Any] | Any

MODEL_META = "analytical.model_usage.meta.v1"
MODEL_SESSION = "analytical.model_usage.session.v1"
MODEL_TURN = "analytical.model_usage.turn.v1"
TOKEN_PROJECT_META = "analytical.token_efficiency.project_meta.v1"
TOKEN_PATTERN = "analytical.token_efficiency.pattern.v1"
TOKEN_HOTSPOT = "analytical.token_efficiency.hotspot.v1"
TOKEN_OUTLIER = "analytical.token_efficiency.outlier.v1"

CANONICAL_FACT_SCOPE = "analytical.canonical_facts.v1"
FACT_PROJECT = "analytical.fact.project.v1"
FACT_PROJECT_SESSION = "analytical.fact.project_session.v1"
FACT_MODEL_USAGE = "analytical.fact.session_model_usage.v1"
FACT_TOOL_USAGE = "analytical.fact.session_tool_usage.v1"
FACT_SESSION_USAGE = "analytical.fact.session_usage.v1"
FACT_SESSION_STATS = "analytical.fact.session_stats.v1"
FACT_SESSION_OVERVIEW = "analytical.fact.session_overview.v1"
FACT_ECONOMICS_CORE = "analytical.fact.economics_core.v1"
FACT_GRAPH_OVERVIEW = "analytical.fact.graph_overview.v1"
FACT_GRAPH_STATS = "analytical.fact.graph_stats.v1"
FACT_GRAPH_USAGE = "analytical.fact.graph_usage.v1"
FACT_SESSION_TREE = "analytical.fact.session_tree.v1"

_FACT_METHOD_KIND = {
    "session.model_usage": FACT_MODEL_USAGE,
    "session.tool_usage": FACT_TOOL_USAGE,
    "session.usage": FACT_SESSION_USAGE,
    "session.stats": FACT_SESSION_STATS,
    "session.overview": FACT_SESSION_OVERVIEW,
}
# Graph projections are retained once per root with the fixed parameter shapes
# the datahub renders; variants (narrative, flat_turns, turn windows) stay
# available through live dispatch only.
_GRAPH_FACT_METHOD_KIND = {
    "session.tree": FACT_SESSION_TREE,
    "graph.overview": FACT_GRAPH_OVERVIEW,
    "graph.stats": FACT_GRAPH_STATS,
    "graph.usage": FACT_GRAPH_USAGE,
}
_GRAPH_FACT_PARAMS: dict[str, dict[str, Any]] = {
    "session.tree": {},
    "graph.overview": {},
    "graph.stats": {"include": ["session_composition"]},
    "graph.usage": {},
}
_FACT_KIND_METHOD = {
    kind: method
    for method, kind in {**_FACT_METHOD_KIND, **_GRAPH_FACT_METHOD_KIND}.items()
}
_FACT_ENTITY_KINDS = (
    FACT_PROJECT,
    FACT_PROJECT_SESSION,
    FACT_MODEL_USAGE,
    FACT_TOOL_USAGE,
    FACT_SESSION_USAGE,
    FACT_SESSION_STATS,
    FACT_SESSION_OVERVIEW,
    FACT_ECONOMICS_CORE,
    FACT_GRAPH_OVERVIEW,
    FACT_GRAPH_STATS,
    FACT_GRAPH_USAGE,
    FACT_SESSION_TREE,
)

DetailName = Literal["sessions", "turns", "errors", "breaks", "projects"]
TokenDetailName = Literal["patterns", "hotspots", "outliers"]


from datahub_plugin.projections.analytical_read_models_reconstruction import (
    _detail_mutations,
    _fact_mutation,
    _mutation,
    _scope_key,
    _token_row_id,
    _without,
)


def build_canonical_root_fact_rows(
    *,
    store: DocumentStore,
    root_session_id: str,
    current_dir: Path,
    project_list: Mapping[str, Any] | None = None,
    include_projects: bool = False,
    economics_detail: Literal["core", "evidence"] = "evidence",
) -> list[Mutation]:
    """Build the complete replaceable fact partition for one affected root."""
    from datahub_plugin.projections.analytical_read_models_clients import (
        DocumentStoreApiClient,
    )

    graph = next(
        (
            item
            for graph_id, item in store.session_graphs.items()
            if str(graph_id) == root_session_id
        ),
        None,
    )
    if graph is None:
        raise ResourceNotFoundError(f"resource not found: {root_session_id}")
    root_store = DocumentStore.from_session_graphs([graph])
    adapter = DocumentStoreApiClient(
        root_store, current_dir=current_dir, project_list=project_list
    )
    rows = _build_canonical_fact_rows_from_store(
        store=root_store,
        adapter=adapter,
        root_session_ids=(root_session_id,),
        include_projects=include_projects,
        economics_detail=economics_detail,
    )
    if not any(row["entity_kind"] == FACT_PROJECT_SESSION for row in rows):
        raise ResourceNotFoundError(f"resource not found: {root_session_id}")
    return rows


def _build_canonical_fact_rows_from_store(
    *,
    store: DocumentStore,
    adapter: DocumentStoreApiClient,
    root_session_ids: Iterable[str] | None = None,
    include_projects: bool = True,
    economics_detail: Literal["core", "evidence"] = "evidence",
) -> list[Mutation]:
    """Build legacy fact rows from one core economics pass per session.

    The persisted fact shapes remain unchanged.  Only their construction is
    replaced: core now shares token metrics, graph indexes, pricing work, and
    reconciliation across the five canonical projections.
    """

    selected_roots = (
        {str(value) for value in root_session_ids}
        if root_session_ids is not None
        else None
    )
    rows: list[Mutation] = []
    if include_projects:
        project_items = adapter.call("project.list", {}).get("items") or {}
        if not isinstance(project_items, dict):
            raise RuntimeError("project.list fact payload has invalid items")
        for name, item in sorted(project_items.items()):
            if not isinstance(item, dict):
                continue
            rows.append(
                _fact_mutation(
                    FACT_PROJECT,
                    str(name),
                    partition_key="projects",
                    sort_key=str(name),
                    payload={"name": str(name), "item": item},
                )
            )

    sessions_payload = adapter.call(
        "project.sessions", {"include": ["runtime", "usage"]}
    )
    session_rows = [
        item
        for item in sessions_payload.get("items") or []
        if isinstance(item, dict)
        and (
            selected_roots is None
            or str(item.get("root_session_id") or "") in selected_roots
            or str(item.get("lineage_root_session_id") or "") in selected_roots
        )
    ]
    for item in session_rows:
        root_id = str(item.get("root_session_id") or "")
        if not root_id:
            raise RuntimeError("project.sessions fact row is missing root_session_id")
        rows.append(
            _fact_mutation(
                FACT_PROJECT_SESSION,
                root_id,
                partition_key=root_id,
                sort_key=f"{item.get('project') or ''}\0{root_id}",
                payload=item,
            )
        )
        from coding_trajectory.analysis.orchestration_runs import (
            build_conversation_tree,
            orchestration_run_for_entrypoint,
        )

        lineage_graph = store.get_session_graph_for_session(UUID(root_id))
        graph = orchestration_run_for_entrypoint(lineage_graph, UUID(root_id))
        session_ids = [
            str(value)
            for value in item.get("session_ids") or []
            if value is not None and str(value)
        ]
        target_ids = set(dict.fromkeys((root_id, *session_ids)))
        built_targets: set[str] = set()
        for session_id, contribution in iter_graph_economics_contributions(
            graph, detail=economics_detail
        ):
            target_id = str(session_id)
            if target_id not in target_ids:
                continue
            built_targets.add(target_id)
            rows.extend(
                _economics_fact_rows(
                    root_id=root_id,
                    target_id=target_id,
                    contribution=contribution,
                    economics_detail=economics_detail,
                )
            )
        missing_targets = target_ids - built_targets
        if missing_targets:
            raise RuntimeError(
                "core economics omitted session entrypoints: "
                + ", ".join(sorted(missing_targets))
            )
        rows.extend(
            _graph_fact_rows(
                root_id=root_id,
                graph=graph,
                conversation_tree={
                    **build_conversation_tree(lineage_graph),
                    "selected_branch_id": root_id,
                },
            )
        )
    return rows


def _graph_fact_rows(
    *,
    root_id: str,
    graph: SessionGraph,
    conversation_tree: dict[str, object],
) -> list[Mutation]:
    """Build the root-partitioned graph.* facts for one session graph."""

    payloads = {
        "session.tree": conversation_tree,
        "graph.overview": build_graph_overview(graph, include_narrative=False),
        "graph.stats": build_session_graph_stats(
            graph, include_session_composition=True
        ),
        "graph.usage": build_session_graph_usage(graph, include_graph_turns=False),
    }
    rows: list[Mutation] = []
    for method, kind in _GRAPH_FACT_METHOD_KIND.items():
        rows.append(
            _fact_mutation(
                kind,
                root_id,
                partition_key=root_id,
                sort_key=root_id,
                payload=service_contract(method).validate_response(payloads[method]),
            )
        )
    return rows


def _economics_fact_rows(
    *,
    root_id: str,
    target_id: str,
    contribution: Any,
    economics_detail: Literal["core", "evidence"],
) -> list[Mutation]:
    payloads = {
        "session.model_usage": (FACT_MODEL_USAGE, contribution.model_usage),
        "session.usage": (FACT_SESSION_USAGE, contribution.usage),
    }
    if economics_detail == "evidence":
        payloads.update(
            {
                "session.tool_usage": (FACT_TOOL_USAGE, contribution.tool_usage),
                "session.stats": (FACT_SESSION_STATS, contribution.stats),
                "session.overview": (
                    FACT_SESSION_OVERVIEW,
                    contribution.overview,
                ),
            }
        )
    rows: list[Mutation] = []
    for method, (kind, payload) in payloads.items():
        if payload is None:
            raise RuntimeError(f"core economics evidence omitted required fact: {kind}")
        rows.append(
            _fact_mutation(
                kind,
                target_id,
                partition_key=root_id,
                sort_key=target_id,
                payload=service_contract(method).validate_response(payload),
            )
        )
    rows.append(
        _fact_mutation(
            FACT_ECONOMICS_CORE,
            target_id,
            partition_key=root_id,
            sort_key=target_id,
            payload={
                "schema_version": contribution.schema_version,
                "root_session_id": str(contribution.root_session_id),
                "project": contribution.project,
                "reconciliation": contribution.reconciliation.model_dump(mode="json"),
            },
        )
    )
    return rows


def canonical_fact_entity_kinds(*, include_projects: bool = True) -> tuple[str, ...]:
    """Return kinds used to gather or replace canonical fact rows."""

    return _FACT_ENTITY_KINDS if include_projects else _FACT_ENTITY_KINDS[1:]


def build_model_usage_rows(
    *,
    client: ServiceApiClient,
    since_days: int = 7,
    project_name: str | None = None,
    model_key: str | None = None,
) -> list[Mutation]:
    """Normalize the Model Usage projection through the in-process client."""

    projection = model_usage.build_projection(
        client=client,
        since_days=since_days,
        project_name=project_name,
        model_key=model_key,
    )
    scope = _scope_key(
        "model_usage",
        since_days=since_days,
        project_name=project_name,
        model_key=model_key,
    )
    meta = _without(projection, "sessions", "turns")
    rows = [_mutation(MODEL_META, scope, scope, "", meta)]
    rows.extend(
        _detail_mutations(
            MODEL_SESSION,
            scope,
            projection.get("sessions") or [],
            id_of=lambda row, _index: str(row.get("id") or ""),
            partition="sessions",
        )
    )
    rows.extend(
        _detail_mutations(
            MODEL_TURN,
            scope,
            projection.get("turns") or [],
            id_of=lambda row, index: (
                f"{row.get('session_id') or ''}:{row.get('turn_id') or index}"
            ),
            partition="turns",
        )
    )
    return rows


def build_token_efficiency_project_rows(
    *,
    client: ServiceApiClient,
    project_name: str,
    since_days: int = 30,
) -> list[Mutation]:
    """Normalize one Token Efficiency project from canonical persisted facts."""

    projection = token_efficiency.build_project_projection(
        client=client,
        project_name=project_name,
        since_days=since_days,
    )
    scope = _scope_key(
        "token_efficiency_project",
        since_days=since_days,
        project_name=project_name,
    )
    rows = [
        _mutation(
            TOKEN_PROJECT_META,
            scope,
            scope,
            project_name,
            _without(projection, "patterns", "hotspots", "outliers"),
        )
    ]
    for detail, kind in (
        ("patterns", TOKEN_PATTERN),
        ("hotspots", TOKEN_HOTSPOT),
        ("outliers", TOKEN_OUTLIER),
    ):
        groups = projection.get(detail) or {}
        if not isinstance(groups, dict):
            continue
        for grain, detail_rows in groups.items():
            if not isinstance(detail_rows, list):
                continue
            rows.extend(
                _detail_mutations(
                    kind,
                    scope,
                    detail_rows,
                    id_of=lambda row, index, grain=grain, detail=detail: (
                        f"{grain}:{_token_row_id(detail, row, index)}"
                    ),
                    partition=f"{project_name}:{grain}",
                )
            )
    return rows
