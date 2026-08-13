"""Persistent normalized rows for the dashboard's analytical projections.

JSONL and :class:`~coding_trajectory.query.DocumentStore` remain authoritative.
This module only decomposes existing dashboard projections into generic,
disposable entities suitable for ``IncrementalStore``.  Projection builders run
in process against one already-built store; hot reconstruction works directly
with payload dictionaries and deliberately does not validate every detail row
with Pydantic again.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, TypeAlias

from pydantic import ValidationError

from coding_trajectory.contracts import service_contract
from coding_trajectory.ingestion.common import normalize_project_key
from coding_trajectory.query import DocumentError, DocumentStore, ResourceNotFoundError
from coding_trajectory.service import IndexCache, dispatch

try:
    from . import cache_breaks, error_collection, model_usage, token_efficiency
except ImportError:  # pragma: no cover - direct plugin-directory imports
    import cache_breaks  # type: ignore[no-redef]
    import error_collection  # type: ignore[no-redef]
    import model_usage  # type: ignore[no-redef]
    import token_efficiency  # type: ignore[no-redef]


CtJson: TypeAlias = Callable[[list[str]], dict[str, Any]]
Mutation: TypeAlias = dict[str, Any]
PersistedRow: TypeAlias = Mapping[str, Any] | Any

MODEL_META = "analytical.model_usage.meta.v1"
MODEL_SESSION = "analytical.model_usage.session.v1"
MODEL_TURN = "analytical.model_usage.turn.v1"
ERROR_META = "analytical.error_collection.meta.v1"
ERROR_ITEM = "analytical.error_collection.item.v1"
CACHE_META = "analytical.cache_breaks.meta.v1"
CACHE_ITEM = "analytical.cache_breaks.item.v1"
TOKEN_INDEX_META = "analytical.token_efficiency.index_meta.v1"
TOKEN_PROJECT = "analytical.token_efficiency.project.v1"
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

_FACT_METHOD_KIND = {
    "session.model_usage": FACT_MODEL_USAGE,
    "session.tool_usage": FACT_TOOL_USAGE,
    "session.usage": FACT_SESSION_USAGE,
    "session.stats": FACT_SESSION_STATS,
    "session.overview": FACT_SESSION_OVERVIEW,
}
_FACT_KIND_METHOD = {kind: method for method, kind in _FACT_METHOD_KIND.items()}
_FACT_ENTITY_KINDS = (
    FACT_PROJECT,
    FACT_PROJECT_SESSION,
    FACT_MODEL_USAGE,
    FACT_TOOL_USAGE,
    FACT_SESSION_USAGE,
    FACT_SESSION_STATS,
    FACT_SESSION_OVERVIEW,
)

DetailName = Literal["sessions", "turns", "errors", "breaks", "projects"]
TokenDetailName = Literal["patterns", "hotspots", "outliers"]


class DocumentStoreCtJson:
    """Small ``ct --json``-compatible adapter over a supplied store.

    Only the ``api call`` and ``api batch`` argv forms used by dashboard modules
    are accepted.  The adapter owns one in-memory ``IndexCache`` and never calls
    ``resolve_store``, project discovery, or a subprocess.

    ``project.list`` cannot recover filesystem paths from ``DocumentStore``.
    Callers that retained canonical project metadata should supply the complete
    ``{"items": ...}`` response as ``project_list``.  Otherwise a contract-valid
    list (project name plus graph vendors, path ``None``) is derived from the
    store.
    """

    def __init__(
        self,
        store: DocumentStore,
        *,
        current_dir: Path,
        discovery_note: str = "prebuilt in-memory DocumentStore",
        project_list: Mapping[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.current_dir = current_dir.resolve()
        self.discovery_note = discovery_note
        self.cache = IndexCache()
        self.cache.index_store(store)
        raw_project_list = (
            dict(project_list)
            if project_list is not None
            else _project_list_from_store(store)
        )
        self.project_list = service_contract("project.list").validate_response(
            raw_project_list
        )

    def __call__(self, argv: list[str]) -> dict[str, Any]:
        return _adapter_argv(self, argv)

    def execute(self, request: Any) -> dict[str, Any]:
        return _adapter_execute(self, request)

    def call(self, method: str, params: Mapping[str, Any]) -> Any:
        """Validate and execute one canonical service method."""

        contract = service_contract(method)
        validated = contract.validate_request(dict(params))
        if method == "project.list":
            return self.project_list
        return dispatch(
            method,
            validated,
            store=self.store,
            global_scope=True,
            current_dir=self.current_dir,
            discovery_note=self.discovery_note,
            cache=self.cache,
        )


class CanonicalFactsCtJson:
    """``ct --json`` adapter backed only by persisted canonical fact rows.

    Construction is a single linear pass over current ``EntityRow`` objects or
    not-yet-persisted mutation dictionaries.  Calls do not touch a
    ``DocumentStore``, source files, discovery, subprocesses, or Pydantic model
    graphs.  Canonical request and response contracts remain the API boundary.

    Fact sets are horizon-scoped by their producer.  Consequently
    ``since_days`` and ``modified_since`` are accepted and contract-validated,
    but cannot widen or narrow the retained fact snapshot.  Project and vendor
    filters are applied to ``project.sessions`` rows exactly as the in-memory
    collection handler does.
    """

    def __init__(self, rows: Iterable[PersistedRow]) -> None:
        self._projects: dict[str, dict[str, Any]] = {}
        self._sessions: list[dict[str, Any]] = []
        self._telemetry: dict[str, dict[str, dict[str, Any]]] = {
            method: {} for method in _FACT_METHOD_KIND
        }
        for row in rows:
            if _value(row, "deleted"):
                continue
            kind = str(_value(row, "entity_kind") or "")
            if kind not in _FACT_ENTITY_KINDS:
                continue
            payload = _row_payload(row)
            if kind == FACT_PROJECT:
                name = str(payload.get("name") or "")
                item = payload.get("item")
                if name and isinstance(item, dict):
                    self._projects[name] = item
                continue
            if kind == FACT_PROJECT_SESSION:
                self._sessions.append(payload)
                continue
            method = _FACT_KIND_METHOD.get(kind)
            if method is None:
                continue
            entrypoint = str(_value(row, "tiebreaker") or "")
            if not entrypoint:
                entrypoint = _telemetry_payload_id(method, payload)
            if entrypoint:
                self._telemetry[method][entrypoint] = payload

        self._sessions.sort(
            key=lambda item: (
                str(item.get("project") or ""),
                str(item.get("root_session_id") or ""),
            )
        )
        if not self._projects:
            self._projects = _project_items_from_session_facts(self._sessions)

    def __call__(self, argv: list[str]) -> dict[str, Any]:
        return _adapter_argv(self, argv)

    def execute(self, request: Any) -> dict[str, Any]:
        return _adapter_execute(self, request)

    def call(self, method: str, params: Mapping[str, Any]) -> Any:
        """Validate and answer one supported service method from facts."""

        contract = service_contract(method)
        validated = contract.validate_request(dict(params))
        if method == "project.list":
            result = self._project_list(validated)
        elif method == "project.sessions":
            result = self._project_sessions(validated)
        elif method in self._telemetry:
            if validated.get("turn_id") is not None:
                raise ValueError(
                    f"turn-scoped {method} is not retained in canonical facts"
                )
            if method == "session.overview" and (
                validated.get("num_turns") is not None
                or validated.get("drop_turns") is not None
            ):
                raise ValueError(
                    "bounded session.overview is not retained in canonical facts"
                )
            entrypoint = _session_entrypoint(validated)
            result = self._telemetry[method].get(entrypoint)
            if result is None:
                raise ResourceNotFoundError(f"resource not found: {entrypoint}")
        else:
            raise KeyError(f"canonical facts do not support service method: {method}")
        return contract.validate_response(result)

    def _project_list(self, params: Mapping[str, Any]) -> dict[str, Any]:
        project_name = params.get("project_name")
        project_key = normalize_project_key(str(project_name)) if project_name else None
        vendor = params.get("agent_vendor")
        items = {
            name: item
            for name, item in sorted(self._projects.items())
            if (project_key is None or normalize_project_key(name) == project_key)
            and (vendor is None or vendor in (item.get("vendors") or []))
        }
        return {"items": items}

    def _project_sessions(self, params: Mapping[str, Any]) -> dict[str, Any]:
        project_name = params.get("project_name")
        project_key = normalize_project_key(str(project_name)) if project_name else None
        vendor = params.get("agent_vendor")
        include = set(params.get("include") or [])
        items: list[dict[str, Any]] = []
        for stored in self._sessions:
            if project_key is not None:
                stored_project = stored.get("project")
                if (
                    not stored_project
                    or normalize_project_key(str(stored_project)) != project_key
                ):
                    continue
            if vendor is not None and vendor not in (stored.get("vendors") or []):
                continue
            item = dict(stored)
            if "usage" not in include:
                item.pop("usage", None)
                item.pop("warnings", None)
            if "runtime" not in include:
                item.pop("runtime", None)
            items.append(item)
        return {"items": items}


def build_canonical_fact_rows(
    *,
    store: DocumentStore,
    current_dir: Path,
    project_list: Mapping[str, Any] | None = None,
) -> list[Mutation]:
    """Persist all analytical service inputs from one already-built store.

    Every fact payload is the output of a versioned canonical service contract.
    The builder never discovers sources.  All graph/session telemetry is
    dispatched in process through :class:`DocumentStoreCtJson`.
    """

    adapter = DocumentStoreCtJson(
        store, current_dir=current_dir, project_list=project_list
    )
    return build_canonical_fact_rows_from_ct_json(ct_json=adapter)


def build_canonical_root_fact_rows(
    *,
    store: DocumentStore,
    root_session_id: str,
    current_dir: Path,
    project_list: Mapping[str, Any] | None = None,
    include_projects: bool = False,
) -> list[Mutation]:
    """Build the complete replaceable fact partition for one affected root."""

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
    adapter = DocumentStoreCtJson(
        root_store, current_dir=current_dir, project_list=project_list
    )
    rows = build_canonical_fact_rows_from_ct_json(
        ct_json=adapter,
        root_session_ids=(root_session_id,),
        include_projects=include_projects,
    )
    if not any(row["entity_kind"] == FACT_PROJECT_SESSION for row in rows):
        raise ResourceNotFoundError(f"resource not found: {root_session_id}")
    return rows


def build_canonical_fact_rows_from_ct_json(
    *,
    ct_json: CtJson,
    root_session_ids: Iterable[str] | None = None,
    include_projects: bool = True,
) -> list[Mutation]:
    """Build normalized fact mutations through a canonical ``ct_json`` seam.

    ``root_session_ids`` limits graph facts without changing their keys or
    partitions.  This makes a targeted repair equivalent to deleting the
    affected root partition and applying this result.  Project-list facts are
    global and are normally replaced only during a full rebuild.
    """

    selected_roots = (
        {str(value) for value in root_session_ids}
        if root_session_ids is not None
        else None
    )
    projects_payload = (
        _ct_api_call(ct_json, "project.list", {}) if include_projects else None
    )
    sessions_payload = _ct_api_call(
        ct_json,
        "project.sessions",
        {"include": ["runtime", "usage"]},
    )
    session_rows = [
        item
        for item in sessions_payload.get("items") or []
        if isinstance(item, dict)
        and (
            selected_roots is None
            or str(item.get("root_session_id") or "") in selected_roots
        )
    ]

    rows: list[Mutation] = []
    if include_projects:
        assert projects_payload is not None
        project_items = projects_payload.get("items") or {}
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

    targets: list[tuple[str, str]] = []
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
        session_ids = [
            str(value)
            for value in item.get("session_ids") or []
            if value is not None and str(value)
        ]
        for target_id in dict.fromkeys((root_id, *session_ids)):
            targets.append((root_id, target_id))

    requests: list[dict[str, Any]] = []
    request_facts: dict[str, tuple[str, str, str]] = {}
    for root_id, target_id in targets:
        for method, kind in _FACT_METHOD_KIND.items():
            request_id = f"{method}:{root_id}:{target_id}"
            requests.append(
                {
                    "id": request_id,
                    "method": method,
                    "params": {"session_id": target_id},
                }
            )
            request_facts[request_id] = (kind, root_id, target_id)

    for request_chunk in _chunks(requests, 320):
        batch = ct_json(
            [
                "api",
                "batch",
                "--global-scope",
                "--requests",
                json.dumps(request_chunk),
            ]
        )
        returned_ids: set[str] = set()
        for response in batch.get("items") or []:
            if not isinstance(response, dict):
                continue
            request_id = str(response.get("id") or "")
            fact = request_facts.get(request_id)
            if fact is None:
                raise RuntimeError(
                    f"canonical fact batch returned unknown id: {request_id}"
                )
            returned_ids.add(request_id)
            if not response.get("ok"):
                error = response.get("error") or {}
                message = error.get("message") if isinstance(error, dict) else error
                raise RuntimeError(
                    str(message or f"canonical fact dispatch failed: {request_id}")
                )
            result = response.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(
                    f"canonical fact dispatch returned invalid result: {request_id}"
                )
            kind, root_id, target_id = fact
            rows.append(
                _fact_mutation(
                    kind,
                    target_id,
                    partition_key=root_id,
                    sort_key=target_id,
                    payload=result,
                )
            )
        missing = {str(item["id"]) for item in request_chunk} - returned_ids
        if missing:
            raise RuntimeError(
                "canonical fact batch omitted responses: " + ", ".join(sorted(missing))
            )
    return rows


def canonical_fact_entity_kinds(*, include_projects: bool = True) -> tuple[str, ...]:
    """Return kinds used to gather or replace canonical fact rows."""

    return _FACT_ENTITY_KINDS if include_projects else _FACT_ENTITY_KINDS[1:]


def build_model_usage_rows(
    *,
    store: DocumentStore,
    current_dir: Path,
    since_days: int = 7,
    project_name: str | None = None,
    model_key: str | None = None,
    project_list: Mapping[str, Any] | None = None,
) -> list[Mutation]:
    """Run the existing Model Usage projection once and normalize its details."""

    adapter = DocumentStoreCtJson(
        store, current_dir=current_dir, project_list=project_list
    )
    return build_model_usage_rows_from_ct_json(
        ct_json=adapter,
        since_days=since_days,
        project_name=project_name,
        model_key=model_key,
    )


def build_model_usage_rows_from_ct_json(
    *,
    ct_json: CtJson,
    since_days: int = 7,
    project_name: str | None = None,
    model_key: str | None = None,
) -> list[Mutation]:
    """Normalize Model Usage through any contract-compatible adapter."""

    projection = model_usage.build_projection(
        ct_json=ct_json,
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


def build_error_collection_rows(
    *,
    store: DocumentStore,
    current_dir: Path,
    since_days: int = 7,
    project_name: str | None = None,
    project_list: Mapping[str, Any] | None = None,
) -> list[Mutation]:
    """Run Error Collection once and persist independently pageable errors."""

    adapter = DocumentStoreCtJson(
        store, current_dir=current_dir, project_list=project_list
    )
    return build_error_collection_rows_from_ct_json(
        ct_json=adapter,
        since_days=since_days,
        project_name=project_name,
    )


def build_error_collection_rows_from_ct_json(
    *,
    ct_json: CtJson,
    since_days: int = 7,
    project_name: str | None = None,
) -> list[Mutation]:
    """Normalize Error Collection through any contract-compatible adapter."""

    projection = error_collection.build_projection(
        ct_json=ct_json,
        since_days=since_days,
        project_name=project_name,
    )
    scope = _scope_key(
        "error_collection", since_days=since_days, project_name=project_name
    )
    rows = [
        _mutation(
            ERROR_META,
            scope,
            scope,
            "",
            _without(projection, "errors"),
        )
    ]
    rows.extend(
        _detail_mutations(
            ERROR_ITEM,
            scope,
            projection.get("errors") or [],
            id_of=lambda row, index: str(row.get("id") or index),
            partition="errors",
        )
    )
    return rows


def build_cache_break_rows(
    *,
    store: DocumentStore,
    current_dir: Path,
    since_days: int = 7,
    project_name: str | None = None,
    project_list: Mapping[str, Any] | None = None,
) -> list[Mutation]:
    """Run Cache Breaks once and persist independently pageable break events."""

    adapter = DocumentStoreCtJson(
        store, current_dir=current_dir, project_list=project_list
    )
    return build_cache_break_rows_from_ct_json(
        ct_json=adapter,
        since_days=since_days,
        project_name=project_name,
    )


def build_cache_break_rows_from_ct_json(
    *,
    ct_json: CtJson,
    since_days: int = 7,
    project_name: str | None = None,
) -> list[Mutation]:
    """Normalize Cache Breaks through any contract-compatible adapter."""

    projection = cache_breaks.build_projection(
        ct_json=ct_json,
        since_days=since_days,
        project_name=project_name,
    )
    scope = _scope_key("cache_breaks", since_days=since_days, project_name=project_name)
    rows = [
        _mutation(
            CACHE_META,
            scope,
            scope,
            "",
            _without(projection, "breaks"),
        )
    ]
    rows.extend(
        _detail_mutations(
            CACHE_ITEM,
            scope,
            projection.get("breaks") or [],
            id_of=lambda row, index: (
                f"{row.get('session_id') or ''}:{row.get('turn_id') or index}:"
                f"{row.get('type') or ''}"
            ),
            partition="breaks",
        )
    )
    return rows


def build_standard_analytical_rows(
    *,
    store: DocumentStore,
    current_dir: Path,
    since_days: int = 7,
    project_name: str | None = None,
    project_list: Mapping[str, Any] | None = None,
) -> list[Mutation]:
    """Build Model Usage, Error Collection, and Cache Breaks from one store.

    Each existing projection is called exactly once through one adapter and one
    canonical index cache over the supplied authoritative store.
    """

    adapter = DocumentStoreCtJson(
        store, current_dir=current_dir, project_list=project_list
    )
    return build_standard_analytical_rows_from_ct_json(
        ct_json=adapter,
        since_days=since_days,
        project_name=project_name,
    )


def build_standard_analytical_rows_from_ct_json(
    *,
    ct_json: CtJson,
    since_days: int = 7,
    project_name: str | None = None,
) -> list[Mutation]:
    """Build standard analytical materializations without a source/store scan."""

    return [
        *build_model_usage_rows_from_ct_json(
            ct_json=ct_json,
            since_days=since_days,
            project_name=project_name,
        ),
        *build_error_collection_rows_from_ct_json(
            ct_json=ct_json,
            since_days=since_days,
            project_name=project_name,
        ),
        *build_cache_break_rows_from_ct_json(
            ct_json=ct_json,
            since_days=since_days,
            project_name=project_name,
        ),
    ]


def build_token_efficiency_index_rows(
    *,
    store: DocumentStore,
    current_dir: Path,
    since_days: int = 30,
    project_list: Mapping[str, Any] | None = None,
) -> list[Mutation]:
    """Run the Token Efficiency index once and normalize project rows."""

    adapter = DocumentStoreCtJson(
        store, current_dir=current_dir, project_list=project_list
    )
    return build_token_efficiency_index_rows_from_ct_json(
        ct_json=adapter,
        since_days=since_days,
    )


def build_token_efficiency_index_rows_from_ct_json(
    *,
    ct_json: CtJson,
    since_days: int = 30,
) -> list[Mutation]:
    """Normalize the Token Efficiency index from persisted canonical facts."""

    projection = token_efficiency.build_index_projection(
        ct_json=ct_json, since_days=since_days
    )
    scope = _scope_key("token_efficiency_index", since_days=since_days)
    rows = [
        _mutation(
            TOKEN_INDEX_META,
            scope,
            scope,
            "",
            _without(projection, "projects"),
        )
    ]
    rows.extend(
        _detail_mutations(
            TOKEN_PROJECT,
            scope,
            projection.get("projects") or [],
            id_of=lambda row, index: str(row.get("project_name") or index),
            partition="projects",
        )
    )
    return rows


def build_token_efficiency_project_rows(
    *,
    store: DocumentStore,
    current_dir: Path,
    project_name: str,
    since_days: int = 30,
    project_list: Mapping[str, Any] | None = None,
) -> list[Mutation]:
    """Run one existing Token Efficiency project projection once."""

    adapter = DocumentStoreCtJson(
        store, current_dir=current_dir, project_list=project_list
    )
    return build_token_efficiency_project_rows_from_ct_json(
        ct_json=adapter,
        project_name=project_name,
        since_days=since_days,
    )


def build_token_efficiency_project_rows_from_ct_json(
    *,
    ct_json: CtJson,
    project_name: str,
    since_days: int = 30,
) -> list[Mutation]:
    """Normalize one Token Efficiency project from canonical persisted facts."""

    projection = token_efficiency.build_project_projection(
        ct_json=ct_json,
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
                    id_of=lambda row, index, grain=grain: (
                        f"{grain}:{_token_row_id(detail, row, index)}"
                    ),
                    partition=f"{project_name}:{grain}",
                )
            )
    return rows


def reconstruct_model_usage(
    meta_row: PersistedRow,
    *,
    detail: Literal["sessions", "turns"],
    rows: Iterable[PersistedRow],
    page: Any,
    limit: int,
) -> dict[str, Any]:
    return _reconstruct(meta_row, detail=detail, rows=rows, page=page, limit=limit)


def reconstruct_error_collection(
    meta_row: PersistedRow,
    *,
    rows: Iterable[PersistedRow],
    page: Any,
    limit: int,
) -> dict[str, Any]:
    return _reconstruct(meta_row, detail="errors", rows=rows, page=page, limit=limit)


def reconstruct_cache_breaks(
    meta_row: PersistedRow,
    *,
    rows: Iterable[PersistedRow],
    page: Any,
    limit: int,
) -> dict[str, Any]:
    return _reconstruct(meta_row, detail="breaks", rows=rows, page=page, limit=limit)


def reconstruct_token_efficiency_index(
    meta_row: PersistedRow,
    *,
    rows: Iterable[PersistedRow],
    page: Any,
    limit: int,
) -> dict[str, Any]:
    return _reconstruct(meta_row, detail="projects", rows=rows, page=page, limit=limit)


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


def tombstone_stale_rows(
    existing_rows: Iterable[PersistedRow],
    replacements: Sequence[Mutation],
    *,
    scope_key: str,
    entity_kinds: Iterable[str],
    partition_key: str | None = None,
) -> list[Mutation]:
    """Return tombstones for rows absent from a complete replacement slice.

    Builders describe the complete output for one route/filter scope.  A
    transaction-scoped materializer can query the previous rows in that scope,
    pass them here, and apply the returned tombstones with the replacements.
    This keeps disappearing errors/breaks/outliers from surviving a rebuild.
    """

    kinds = set(entity_kinds)
    retained = {
        str(row["entity_key"])
        for row in replacements
        if row.get("scope_key") == scope_key and row.get("entity_kind") in kinds
    }
    tombstones: list[Mutation] = []
    for row in existing_rows:
        if _value(row, "scope_key") != scope_key:
            continue
        if partition_key is not None and _value(row, "partition_key") != partition_key:
            continue
        entity_kind = str(_value(row, "entity_kind") or "")
        entity_key = str(_value(row, "entity_key") or "")
        if entity_kind not in kinds or not entity_key or entity_key in retained:
            continue
        tombstones.append(
            {
                "entity_kind": entity_kind,
                "entity_key": entity_key,
                "scope_key": scope_key,
                "partition_key": str(_value(row, "partition_key") or ""),
                "sort_key": str(_value(row, "sort_key") or ""),
                "tiebreaker": str(_value(row, "tiebreaker") or entity_key),
                "payload": {},
                "deleted": True,
            }
        )
    return tombstones


def tombstone_canonical_root_facts(
    existing_rows: Iterable[PersistedRow],
    replacements: Sequence[Mutation],
    *,
    root_session_id: str,
) -> list[Mutation]:
    """Tombstone facts removed from one complete root replacement."""

    return tombstone_stale_rows(
        existing_rows,
        replacements,
        scope_key=CANONICAL_FACT_SCOPE,
        entity_kinds=canonical_fact_entity_kinds(include_projects=False),
        partition_key=root_session_id,
    )


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


def _adapter_argv(adapter: Any, argv: list[str]) -> dict[str, Any]:
    if len(argv) < 2 or argv[0] != "api":
        raise ValueError("only ct api call/batch argv is supported")
    action = argv[1]
    if action == "call":
        if len(argv) < 3:
            raise ValueError("api call requires a method")
        method = argv[2]
        params = _json_flag(argv[3:], "--params", default={})
        request_id = _flag(argv[3:], "--id")
        return adapter.execute({"id": request_id, "method": method, "params": params})
    if action == "batch":
        requests = _json_flag(argv[2:], "--requests", default=None)
        if not isinstance(requests, list):
            raise ValueError("api batch requires a JSON request array")
        return {"items": [adapter.execute(item) for item in requests]}
    raise ValueError(f"unsupported ct api action: {action}")


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


def _ct_api_call(
    ct_json: CtJson, method: str, params: Mapping[str, Any]
) -> dict[str, Any]:
    response = ct_json(
        [
            "api",
            "call",
            method,
            "--global-scope",
            "--params",
            json.dumps(dict(params)),
        ]
    )
    if not response.get("ok"):
        error = response.get("error") or {}
        message = error.get("message") if isinstance(error, dict) else error
        raise RuntimeError(str(message or f"ct api request failed: {method}"))
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"ct api request returned invalid result: {method}")
    return result


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


def _chunks(values: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _json_flag(argv: Sequence[str], name: str, *, default: Any) -> Any:
    raw = _flag(argv, name)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON for {name}: {exc.msg}") from exc


def _flag(argv: Sequence[str], name: str) -> str | None:
    try:
        index = argv.index(name)
    except ValueError:
        return None
    if index + 1 >= len(argv):
        raise ValueError(f"{name} requires a value")
    return argv[index + 1]


def _error_item(request_id: Any, method: Any, message: str) -> dict[str, Any]:
    return {
        "id": request_id,
        "method": method,
        "ok": False,
        "error": {"message": message},
    }


def _scope_key(route: str, **filters: Any) -> str:
    normalized = {key: value for key, value in filters.items() if value is not None}
    raw = json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str)
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


__all__ = [
    "CANONICAL_FACT_SCOPE",
    "CACHE_ITEM",
    "CACHE_META",
    "CanonicalFactsCtJson",
    "DocumentStoreCtJson",
    "ERROR_ITEM",
    "ERROR_META",
    "FACT_MODEL_USAGE",
    "FACT_PROJECT",
    "FACT_PROJECT_SESSION",
    "FACT_SESSION_OVERVIEW",
    "FACT_SESSION_STATS",
    "FACT_SESSION_USAGE",
    "FACT_TOOL_USAGE",
    "MODEL_META",
    "MODEL_SESSION",
    "MODEL_TURN",
    "TOKEN_HOTSPOT",
    "TOKEN_INDEX_META",
    "TOKEN_OUTLIER",
    "TOKEN_PATTERN",
    "TOKEN_PROJECT",
    "TOKEN_PROJECT_META",
    "build_cache_break_rows",
    "build_cache_break_rows_from_ct_json",
    "build_canonical_fact_rows",
    "build_canonical_fact_rows_from_ct_json",
    "build_canonical_root_fact_rows",
    "build_error_collection_rows",
    "build_error_collection_rows_from_ct_json",
    "build_model_usage_rows",
    "build_model_usage_rows_from_ct_json",
    "build_standard_analytical_rows",
    "build_standard_analytical_rows_from_ct_json",
    "build_token_efficiency_index_rows",
    "build_token_efficiency_index_rows_from_ct_json",
    "build_token_efficiency_project_rows",
    "build_token_efficiency_project_rows_from_ct_json",
    "canonical_fact_entity_kinds",
    "analytical_scope_key",
    "page_metadata",
    "reconstruct_cache_breaks",
    "reconstruct_error_collection",
    "reconstruct_model_usage",
    "reconstruct_token_efficiency_index",
    "reconstruct_token_efficiency_project",
    "tombstone_stale_rows",
    "tombstone_canonical_root_facts",
]
