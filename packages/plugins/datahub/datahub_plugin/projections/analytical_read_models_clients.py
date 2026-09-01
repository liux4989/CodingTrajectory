"""Persistent normalized rows for the datahub's analytical projections.

JSONL and :class:`~coding_trajectory.query.DocumentStore` remain authoritative.
This module only decomposes existing datahub projections into generic,
disposable entities suitable for ``IncrementalStore``.  Projection builders run
in process against one already-built store; hot reconstruction works directly
with payload dictionaries and deliberately does not validate every detail row
with Pydantic again.
"""

# ruff: noqa: F401, I001
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


from datahub_plugin.projections.analytical_read_models_facts import (
    _build_canonical_fact_rows_from_store,
)

from datahub_plugin.projections.analytical_read_models_reconstruction import (
    _adapter_execute,
    _project_items_from_session_facts,
    _project_list_from_store,
    _row_payload,
    _session_entrypoint,
    _telemetry_payload_id,
    _value,
)


class DocumentStoreApiClient:
    """In-process :class:`ServiceApiClient` over a supplied store.

    The adapter owns one in-memory ``IndexCache`` and never calls
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


class CanonicalFactsApiClient:
    """In-process :class:`ServiceApiClient` backed by persisted fact rows.

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
            method: {} for method in _FACT_KIND_METHOD.values()
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
            if method == "graph.overview":
                if (
                    validated.get("num_turns") is not None
                    or validated.get("drop_turns") is not None
                ):
                    raise ValueError(
                        "bounded graph.overview is not retained in canonical facts"
                    )
                if "narrative" in set(validated.get("include") or []):
                    raise ValueError(
                        "graph.overview narrative is not retained in canonical facts"
                    )
            if method == "graph.usage" and "flat_turns" in set(
                validated.get("include") or []
            ):
                raise ValueError(
                    "graph.usage flat_turns is not retained in canonical facts"
                )
            entrypoint = _session_entrypoint(validated)
            if method in _GRAPH_FACT_METHOD_KIND:
                entrypoint = self._graph_root(entrypoint)
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

    def _graph_root(self, entrypoint: str) -> str:
        """Map any session entrypoint to its graph root for graph.* lookups."""

        for stored in self._sessions:
            root = str(stored.get("root_session_id") or "")
            session_ids = {str(value) for value in stored.get("session_ids") or []}
            if entrypoint == root or entrypoint in session_ids:
                return root or entrypoint
        return entrypoint


def build_canonical_fact_rows(
    *,
    store: DocumentStore,
    current_dir: Path,
    project_list: Mapping[str, Any] | None = None,
    economics_detail: Literal["core", "evidence"] = "evidence",
) -> list[Mutation]:
    """Persist all analytical service inputs from one already-built store.

    Every fact payload is the output of a versioned canonical service contract.
    The builder never discovers sources.  All graph/session telemetry is
    dispatched in process through :class:`DocumentStoreApiClient`.
    """

    adapter = DocumentStoreApiClient(
        store, current_dir=current_dir, project_list=project_list
    )
    return _build_canonical_fact_rows_from_store(
        store=store,
        adapter=adapter,
        economics_detail=economics_detail,
    )
