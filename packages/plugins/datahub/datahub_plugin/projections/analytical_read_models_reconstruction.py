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
from typing import Any, Literal
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


def reconstruct_model_usage(
    meta_row: PersistedRow,
    *,
    detail: Literal["sessions", "turns"],
    rows: Iterable[PersistedRow],
    page: Any,
    limit: int,
) -> dict[str, Any]:
    return _reconstruct(meta_row, detail=detail, rows=rows, page=page, limit=limit)


def reconstruct_token_efficiency_project(
    meta_row: PersistedRow,
    *,
    detail: TokenDetailName,
    grain: str,
    rows: Iterable[PersistedRow],
    page: Any,
    limit: int,
) -> dict[str, Any]:
    """Return one requested Token Efficiency detail/grain page.

    The response keeps the legacy ``patterns``/``hotspots``/``outliers`` map
    shape, but contains only the requested grain.  ``pages`` is additive.
    """

    payload = dict(_row_payload(meta_row))
    payload[detail] = {grain: [_row_payload(row) for row in rows]}
    payload["pages"] = {detail: {grain: page_metadata(page, limit=limit)}}
    return payload


def page_metadata(page: Any, *, limit: int) -> dict[str, Any]:
    """Extract additive API metadata from an ``IncrementalStore`` page."""

    return {
        "revision": _value(page, "revision"),
        "limit": limit,
        "next_cursor": _value(page, "next_cursor"),
        "has_more": _value(page, "next_cursor") is not None,
    }


def analytical_scope_key(route: str, **filters: Any) -> str:
    """Return the stable scope used by builders and indexed queries."""

    return _scope_key(route, **filters)


def _reconstruct(
    meta_row: PersistedRow,
    *,
    detail: DetailName,
    rows: Iterable[PersistedRow],
    page: Any,
    limit: int,
) -> dict[str, Any]:
    payload = dict(_row_payload(meta_row))
    payload[detail] = [_row_payload(row) for row in rows]
    # ModelUsagePayload requires both detail lists; the unfetched sibling
    # page stays empty so single-detail responses still satisfy the contract.
    payload.setdefault("turns" if detail == "sessions" else "sessions", [])
    payload["pages"] = {detail: page_metadata(page, limit=limit)}
    return payload


def _project_list_from_store(store: DocumentStore) -> dict[str, Any]:
    projects: dict[str, dict[str, Any]] = {}
    for graph in store.session_graphs.values():
        name = graph.project_identifier
        if not name or name.startswith("unknown-"):
            continue
        target = projects.setdefault(name, {"path": None, "vendors": set()})
        target["vendors"].update(
            session.vendor.value for session in graph.sessions if session.vendor
        )
    return {
        "items": {
            name: {"path": value["path"], "vendors": sorted(value["vendors"])}
            for name, value in sorted(projects.items())
        }
    }


def _adapter_execute(adapter: Any, request: Any) -> dict[str, Any]:
    if not isinstance(request, dict):
        return _error_item(None, None, "request must be an object")
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}
    if not isinstance(method, str) or not method:
        return _error_item(request_id, method, "method is required")
    if not isinstance(params, dict):
        return _error_item(request_id, method, "params must be an object")
    try:
        result = adapter.call(method, params)
    except (
        KeyError,
        ValueError,
        ValidationError,
        ResourceNotFoundError,
        DocumentError,
    ) as exc:
        return _error_item(request_id, method, str(exc))
    return {
        "id": request_id,
        "method": method,
        "ok": True,
        "result": result,
    }


def _fact_mutation(
    entity_kind: str,
    entity_id: str,
    *,
    partition_key: str,
    sort_key: str,
    payload: Mapping[str, Any],
) -> Mutation:
    mutation = _mutation(
        entity_kind,
        CANONICAL_FACT_SCOPE,
        entity_id,
        partition_key,
        payload,
    )
    mutation["sort_key"] = sort_key
    mutation["tiebreaker"] = entity_id
    return mutation


def _session_entrypoint(params: Mapping[str, Any]) -> str:
    for key in ("session_id", "root_session_id", "turn_id"):
        value = params.get(key)
        if value is not None and str(value):
            return str(value)
    raise ValueError("session_id, root_session_id, or turn_id is required")


def _telemetry_payload_id(method: str, payload: Mapping[str, Any]) -> str:
    if method == "session.usage":
        return str(payload.get("session_id") or "")
    return str(payload.get("root_session_id") or "")


def _project_items_from_session_facts(
    sessions: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    projects: dict[str, dict[str, Any]] = {}
    for session in sessions:
        name = str(session.get("project") or "")
        if not name:
            continue
        target = projects.setdefault(name, {"path": None, "vendors": []})
        target["vendors"] = sorted(
            set(target["vendors"])
            | {str(value) for value in session.get("vendors") or []}
        )
    return projects


def _error_item(request_id: Any, method: Any, message: str) -> dict[str, Any]:
    return {
        "id": request_id,
        "method": method,
        "ok": False,
        "error": {"message": message},
    }


def _scope_key(route: str, **filters: Any) -> str:
    normalized = {key: value for key, value in filters.items() if value is not None}
    raw = canonical_json(normalized)
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"{route}:{digest}"


def _mutation(
    entity_kind: str,
    scope_key: str,
    row_id: str,
    partition_key: str,
    payload: Mapping[str, Any],
    *,
    position: int = 0,
) -> Mutation:
    stable_id = str(row_id) or str(position)
    entity_key = hashlib.sha256(
        f"{entity_kind}\0{scope_key}\0{stable_id}".encode()
    ).hexdigest()
    return {
        "entity_kind": entity_kind,
        "entity_key": entity_key,
        "scope_key": scope_key,
        "partition_key": partition_key,
        "sort_key": f"{position:012d}",
        "tiebreaker": stable_id,
        "payload": dict(payload),
    }


def _detail_mutations(
    entity_kind: str,
    scope_key: str,
    rows: Iterable[Any],
    *,
    id_of: Callable[[dict[str, Any], int], str],
    partition: str,
) -> list[Mutation]:
    mutations: list[Mutation] = []
    for index, raw_row in enumerate(rows):
        if not isinstance(raw_row, dict):
            continue
        mutations.append(
            _mutation(
                entity_kind,
                scope_key,
                id_of(raw_row, index),
                partition,
                raw_row,
                position=index,
            )
        )
    return mutations


def _token_row_id(detail: str, row: dict[str, Any], index: int) -> str:
    if detail == "patterns":
        return str(row.get("key") or index)
    if detail == "hotspots":
        return str(row.get("key") or row.get("resource") or index)
    return f"{row.get('session_id') or ''}:{row.get('turn_id') or index}"


def _without(payload: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    excluded = set(keys)
    return {key: value for key, value in payload.items() if key not in excluded}


def _row_payload(row: PersistedRow) -> dict[str, Any]:
    payload = _value(row, "payload")
    if not isinstance(payload, dict):
        raise TypeError("persisted analytical row payload must be an object")
    return payload


def _value(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)
