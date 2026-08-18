"""Service layer implementing the session-api.json contract."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any
from uuid import UUID

from coding_trajectory import debug
from coding_trajectory.analysis.session_stats import (
    session_graph_title,
)
from coding_trajectory.contracts import service_contract
from coding_trajectory.discovery import (
    DiscoverySource,
    discover_project_metadata,
    discover_store,
    discover_store_from_files,
    format_discovery_sources,
    locate_session_files,
)
from coding_trajectory.ingestion.common import (
    format_datetime,
    normalize_project_key,
    prune_nones,
)
from coding_trajectory.ingestion.models import (
    Event,
    EventType,
    Item,
    Session,
    SessionGraph,
    Turn,
)
from coding_trajectory.query import DocumentStore, ResourceNotFoundError


def _optional_positive_int(params: dict[str, Any], key: str) -> int | None:
    value = params.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{key} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a positive integer") from exc
    if parsed < 1:
        raise ValueError(f"{key} must be a positive integer")
    return parsed


def _public_output_for_session_graph(_session_graph: SessionGraph, payload: Any) -> Any:
    """Identity seam over canonical session-graph output.

    The recursive public/internal session-id remapping machinery that lived
    here has been removed: it produced an identical payload in practice
    (every session id in canonical output is already a canonical UUID string),
    so the deep-copy walk was pure overhead. Kept as a no-op wrapper so the
    session.* handlers read uniformly; inline at the call sites if it ever
    needs to diverge.
    """
    return payload


def serialize_session_graph_detail(session_graph: SessionGraph) -> dict[str, Any]:
    vendors = sorted(
        {session.vendor.value for session in session_graph.sessions if session.vendor}
    )
    return prune_nones(
        {
            "graph_id": str(session_graph.root_session_id),
            "root_session_id": str(session_graph.root_session_id),
            "title": session_graph_title(session_graph),
            "vendors": vendors or None,
            "session_ids": [
                str(session.session_id) for session in session_graph.sessions
            ],
        }
    )


def serialize_event_detail(
    event: Event,
    *,
    related_item: Item | None = None,
) -> dict[str, Any]:
    return prune_nones(
        {
            "event_id": str(event.event_id),
            "session_id": str(event.session_id),
            "timestamp": format_datetime(event.timestamp),
            "type": event.type.value,
            "tool_call": serialize_tool_call_detail(
                event,
                related_item=related_item,
            ),
            "llm": serialize_llm_detail(event),
            "usage": serialize_usage_detail(event),
            "text": serialize_text_detail(event),
        }
    )


def serialize_tool_call_detail(
    event: Event,
    *,
    related_item: Item | None = None,
) -> dict[str, Any] | None:
    if event.type not in {
        EventType.TOOL_CALL_REQUESTED,
        EventType.TOOL_CALL_SUCCEEDED,
        EventType.TOOL_CALL_FAILED,
    }:
        return None

    payload = event.payload
    status_by_type = {
        EventType.TOOL_CALL_REQUESTED: "in_progress",
        EventType.TOOL_CALL_SUCCEEDED: "done",
        EventType.TOOL_CALL_FAILED: "failed",
    }
    item_result = (
        getattr(related_item, "output", None)
        if related_item is not None
        and event.type
        in {
            EventType.TOOL_CALL_SUCCEEDED,
            EventType.TOOL_CALL_FAILED,
        }
        else None
    )
    result = next(
        (
            value
            for value in (
                payload.get("result"),
                payload.get("tool_output"),
                payload.get("tool_text"),
                item_result,
            )
            if value is not None
        ),
        None,
    )
    return (
        prune_nones(
            {
                "tool_call_id": payload.get("tool_call_id"),
                "tool_name": payload.get("tool_name"),
                "input": payload.get("tool_args") or payload.get("input"),
                "result": result,
                "status": status_by_type.get(event.type),
            }
        )
        or None
    )


def serialize_usage_detail(event: Event) -> dict[str, Any] | None:
    if (
        event.type != EventType.VENDOR_RAW
        or event.payload.get("transcript_kind") != "usage"
    ):
        return None
    metrics = event.payload.get("metrics")
    usage = (
        metrics.get("usage") or metrics.get("last_token_usage")
        if isinstance(metrics, dict)
        else None
    )
    if not isinstance(usage, dict):
        return None
    return prune_nones(
        {
            "provider": (
                metrics.get("provider") if isinstance(metrics, dict) else None
            ),
            "model": metrics.get("model") if isinstance(metrics, dict) else None,
            **usage,
        }
    )


def serialize_llm_detail(event: Event) -> dict[str, Any] | None:
    if event.type != EventType.LLM_RESPONSE:
        return None

    usage = (
        event.payload.get("usage")
        if isinstance(event.payload.get("usage"), dict)
        else {}
    )
    return (
        prune_nones(
            {
                "model": event.payload.get("model")
                or event.payload.get("model_version"),
                "prompt_tokens": usage.get("prompt_tokens")
                or usage.get("input_tokens"),
                "completion_tokens": usage.get("completion_tokens")
                or usage.get("output_tokens"),
                "reported_total_tokens": usage.get("reported_total_tokens")
                or usage.get("total_tokens"),
                "stop_reason": event.payload.get("stop_reason"),
            }
        )
        or None
    )


def serialize_text_detail(event: Event) -> dict[str, Any] | None:
    text = event.payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return {"text": text.strip()}


def _parse_user_id(raw_id: str) -> UUID:
    try:
        return UUID(raw_id)
    except ValueError as exc:
        raise ValueError(f"invalid id: {raw_id!r} is not a valid UUID") from exc


def _normalize_user_id(raw_id: str) -> str:
    return str(_parse_user_id(raw_id))


def resolve_resource(
    store: DocumentStore, resource: str, raw_id: str
) -> SessionGraph | Session | Turn | Event | Item:
    resource_id = _parse_user_id(raw_id)

    if resource == "session_graph":
        return store.get_session_graph(resource_id)
    if resource == "session":
        return store.get_session(resource_id)
    if resource == "turn":
        return store.get_turn(resource_id)
    if resource == "event":
        return store.get_event(resource_id)
    if resource == "item":
        return store.get_item(resource_id)

    raise ValueError(f"unsupported resource: {resource}")


def resolve_collection(
    store: DocumentStore,
    resource: str,
    *,
    global_scope: bool = False,
    root_session_id: str | None = None,
    current_dir: Path | None = None,
    project_name: str | None = None,
    agent_vendor: str | None = None,
) -> list[SessionGraph | Session]:
    if resource == "session_graph":
        session_graphs = list(store.session_graphs.values())
        if not global_scope and current_dir is not None and project_name is None:
            current_project = normalize_project_key(current_dir.name)
            session_graphs = [
                item
                for item in session_graphs
                if item.project_identifier
                and normalize_project_key(item.project_identifier) == current_project
            ]
        if project_name is not None:
            key = normalize_project_key(project_name)
            session_graphs = [
                item
                for item in session_graphs
                if item.project_identifier
                and normalize_project_key(item.project_identifier) == key
            ]
        if agent_vendor is not None:
            session_graphs = [
                item
                for item in session_graphs
                if item.summary
                and any(v.value == agent_vendor for v in item.summary.vendors)
            ]
        return sorted(
            session_graphs,
            key=lambda item: (item.project_identifier or "", str(item.root_session_id)),
        )

    if resource == "session":
        sessions = list(store.sessions.values())
        if root_session_id:
            tid = _parse_user_id(root_session_id)
            sessions = [
                item
                for item in sessions
                if store.session_to_root.get(item.session_id) == tid
            ]
        return sorted(
            sessions, key=lambda item: (item.started_at, str(item.session_id))
        )

    raise ValueError(f"unsupported resource: {resource}")


# ---------------------------------------------------------------------------
# Index cache
# ---------------------------------------------------------------------------

_CACHE_DIR = Path.home() / ".coding-trajectory"
_CACHE_FILE = _CACHE_DIR / "index.json"


@dataclass
class IndexCache:
    """Disposable source and entry-point locator persisted between invocations.

    ``session_to_session_graph`` is the legacy persisted field name. It stores
    every supported session-graph entry point (graph, session, and turn IDs),
    while :attr:`entrypoint_to_root` exposes that meaning to core code.
    Canonical graph membership remains owned by :class:`DocumentStore`.
    """

    path_to_session_graph: dict[str, str] = field(default_factory=dict)
    session_to_session_graph: dict[str, str] = field(default_factory=dict)

    @property
    def entrypoint_to_root(self) -> dict[str, str]:
        """Return the legacy-backed entry-point mapping."""
        return self.session_to_session_graph

    def root_for_entrypoint(self, entrypoint_id: str) -> str:
        return self.entrypoint_to_root.get(entrypoint_id, entrypoint_id)

    def index_store(self, store: DocumentStore) -> None:
        """Record ownership with the same graph/session/turn precedence as lookup."""
        for turn_id, turn in store.turns.items():
            root_session_id = store.session_to_root.get(turn.session_id)
            if root_session_id is not None:
                self.entrypoint_to_root[str(turn_id)] = str(root_session_id)
        for session_id, root_session_id in store.session_to_root.items():
            self.entrypoint_to_root[str(session_id)] = str(root_session_id)
        for root_session_id in store.session_graphs:
            root = str(root_session_id)
            self.entrypoint_to_root[root] = root

    def index_discovery(
        self,
        *,
        sources: list[DiscoverySource],
        store: DocumentStore,
    ) -> None:
        """Record paths and entry points from one completed discovery result."""
        for source in sources:
            if source.root_session_id is not None:
                self.path_to_session_graph[str(source.path)] = str(
                    source.root_session_id
                )
        self.index_store(store)

    def paths_for_session_graph(self, root_session_id: str) -> list[str]:
        return [
            p for p, tid in self.path_to_session_graph.items() if tid == root_session_id
        ]

    def save(self) -> None:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(
            json.dumps(
                {
                    "path_to_session_graph": self.path_to_session_graph,
                    "session_to_session_graph": self.session_to_session_graph,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load(cls) -> IndexCache:
        if not _CACHE_FILE.exists():
            return cls()
        try:
            raw = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()
        cache = cls(
            path_to_session_graph=raw.get("path_to_session_graph", {}),
            session_to_session_graph=raw.get("session_to_session_graph", {}),
        )
        cache._prune_stale()
        return cache

    def _prune_stale(self) -> None:
        """Remove entries whose source files no longer exist."""
        stale = [p for p in self.path_to_session_graph if not Path(p).exists()]
        if not stale:
            return
        stale_tids = set()
        for p in stale:
            stale_tids.add(self.path_to_session_graph.pop(p))
        # A tid is fully stale only if no live path still references it.
        remove_tids = stale_tids - set(self.path_to_session_graph.values())
        if remove_tids:
            self.session_to_session_graph = {
                sid: t
                for sid, t in self.session_to_session_graph.items()
                if t not in remove_tids
            }


# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------


def _resolve_session_graph(store: DocumentStore, raw_id: str | None) -> SessionGraph:
    """Resolve a session graph by a session entry point."""
    if raw_id is None:
        session_graphs = list(store.session_graphs.values())
        if len(session_graphs) == 1:
            return session_graphs[0]
        if not session_graphs:
            raise ValueError("no session_graphs found in store")
        raise ValueError(
            "session_id is required when the store contains multiple session_graphs"
        )

    resource_id = _parse_user_id(raw_id)
    # Try the entry point as a graph id, then a session id, then a turn id.
    for resolve in (
        lambda: store.get_session_graph(resource_id),
        lambda: store.get_session_graph_for_session(
            store.get_session(resource_id).session_id
        ),
        lambda: store.get_session_graph_for_turn(resource_id),
    ):
        try:
            return resolve()
        except ResourceNotFoundError:
            continue
    raise ResourceNotFoundError(f"resource not found: {raw_id}")


def _session_graph_entrypoint_id(params: dict[str, Any]) -> str | None:
    """Return the public session entry point."""
    return (
        params.get("session_id")
        or params.get("root_session_id")
        or params.get("turn_id")
    )


def _build_store_full(
    *,
    global_scope: bool,
    current_dir: Path,
    cache: IndexCache,
    project_name: str | None = None,
    since_days: int | None = None,
    modified_since: Any | None = None,
    agent_vendor: str | None = None,
) -> tuple[DocumentStore, str]:
    """Full discovery — populates cache.path_to_session_graph."""
    discovery = discover_store(
        current_dir=current_dir,
        global_scope=global_scope,
        project_name=project_name,
        since_days=since_days,
        modified_since=modified_since,
        agent_vendor=agent_vendor,
    )
    cache.index_discovery(sources=discovery.sources, store=discovery.store)

    return discovery.store, format_discovery_sources(discovery.sources)


def _build_store_targeted(
    paths: list[str], cache: IndexCache
) -> tuple[DocumentStore, str]:
    """Targeted discovery — ingest only the files mapped to a session_graph."""
    if not paths:
        return DocumentStore.from_session_graphs([]), "(no targeted paths)"
    expanded_paths = _expand_targeted_paths([Path(p) for p in paths])
    discovery = discover_store_from_files(expanded_paths)
    cache.index_discovery(sources=discovery.sources, store=discovery.store)
    return discovery.store, format_discovery_sources(discovery.sources)


def _expand_targeted_paths(paths: list[Path]) -> list[Path]:
    expanded: list[Path] = []
    seen: set[Path] = set()

    for path in paths:
        resolved = path.resolve()
        if resolved not in seen and resolved.exists():
            expanded.append(resolved)
            seen.add(resolved)

        subagents_dir = resolved.with_suffix("") / "subagents"
        if not subagents_dir.is_dir():
            continue
        for subagent_path in sorted(subagents_dir.glob("*.jsonl")):
            subagent_resolved = subagent_path.resolve()
            if subagent_resolved in seen:
                continue
            expanded.append(subagent_resolved)
            seen.add(subagent_resolved)

    return expanded


def resolve_store(
    params: dict[str, Any],
    *,
    global_scope: bool,
    current_dir: Path,
    cache: IndexCache,
) -> tuple[DocumentStore, str]:
    """Build a store: use cached path index for targeted load, fall back to full discovery."""
    entrypoint_id = _session_graph_entrypoint_id(params)
    if entrypoint_id and cache.path_to_session_graph:
        normalized_entrypoint_id = _normalize_user_id(entrypoint_id)
        target_session_graph_id = cache.root_for_entrypoint(normalized_entrypoint_id)
        cached_paths = cache.paths_for_session_graph(target_session_graph_id)
        if cached_paths:
            return _build_store_targeted(cached_paths, cache)

    bulk_ids = params.get("session_ids")
    if bulk_ids:
        bulk_paths = _resolve_bulk_cached_paths(bulk_ids, cache)
        if bulk_paths is not None:
            return _build_store_targeted(bulk_paths, cache)

    if entrypoint_id:
        try:
            located = locate_session_files(
                session_id=_parse_user_id(entrypoint_id),
                current_dir=current_dir,
                global_scope=True,
                agent_vendor=params.get("agent_vendor"),
            )
        except ValueError:
            located = []
        if located:
            return _build_store_targeted([str(p) for p in located], cache)

    return _build_store_full(
        global_scope=global_scope,
        current_dir=current_dir,
        cache=cache,
        project_name=params.get("project_name"),
        since_days=params.get("since_days"),
        modified_since=params.get("modified_since"),
        agent_vendor=params.get("agent_vendor"),
    )


def _resolve_bulk_cached_paths(
    raw_ids: list[str], cache: IndexCache
) -> list[str] | None:
    """Collect targeted cached paths for a list of session IDs.

    Returns ``None`` when the cache path index is empty, signalling that
    the caller should fall back to full discovery. Otherwise returns the
    union of cached paths for every valid, indexed id. Malformed ids are
    skipped (the handler records them as errors in its own post-pass).
    Uncached-but-valid ids contribute no paths; the caller then builds a
    targeted store over an empty path set so the handler emits
    ``resource not found`` for those ids instead of silently falling back
    to global discovery.
    """
    if not cache.path_to_session_graph:
        return None
    paths: list[str] = []
    seen_roots: set[str] = set()
    for raw_id in raw_ids:
        try:
            normalized = _normalize_user_id(raw_id)
        except ValueError:
            continue
        root_id = cache.root_for_entrypoint(normalized)
        if root_id in seen_roots:
            continue
        seen_roots.add(root_id)
        paths.extend(cache.paths_for_session_graph(root_id))
    return paths


TEMPORARY_PROJECT_KEY = "(temporary)"


def project_list_metadata(
    params: dict[str, Any],
    *,
    global_scope: bool,
    current_dir: Path,
) -> dict[str, Any]:
    """Return project list data without fully ingesting session transcripts."""
    projects: dict[str, dict[str, Any]] = {}
    temporary_vendors: set[str] = set()
    temporary_sessions: list[dict[str, Any]] = []
    for item in discover_project_metadata(
        current_dir=current_dir,
        global_scope=global_scope,
        project_name=params.get("project_name"),
        since_days=params.get("since_days"),
        modified_since=params.get("modified_since"),
        agent_vendor=params.get("agent_vendor"),
    ):
        key = item.project_identifier
        if key.startswith("unknown-"):
            continue
        if item.category == "temporary":
            temporary_vendors.add(item.vendor.value)
            temporary_sessions.append(
                {
                    "project": key,
                    "path": str(item.path) if item.path is not None else None,
                    "vendor": item.vendor.value,
                }
            )
            continue
        entry = projects.setdefault(key, {"path": None, "vendors": set()})
        entry["vendors"].add(item.vendor.value)
        if entry["path"] is None and item.path is not None:
            entry["path"] = str(item.path)

    items = {
        key: {
            "path": value["path"],
            "vendors": sorted(value["vendors"]),
        }
        for key, value in sorted(projects.items())
    }
    if temporary_sessions:
        items[TEMPORARY_PROJECT_KEY] = {
            "path": None,
            "vendors": sorted(temporary_vendors),
            "sessions": sorted(
                temporary_sessions,
                key=lambda session: (session["project"], session["vendor"]),
            ),
        }

    return {"items": items}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceContext:
    store: DocumentStore
    global_scope: bool
    current_dir: Path
    discovery_note: str
    cache: IndexCache


ServiceHandler = Callable[[dict[str, Any], ServiceContext], Any]


def dispatch(
    method: str,
    params: dict[str, Any],
    *,
    store: DocumentStore,
    global_scope: bool,
    current_dir: Path,
    discovery_note: str,
    cache: IndexCache,
) -> Any:
    contract = service_contract(method)
    params = contract.validate_request(params)
    context = ServiceContext(
        store=store,
        global_scope=global_scope,
        current_dir=current_dir,
        discovery_note=discovery_note,
        cache=cache,
    )
    context.cache.index_store(context.store)
    try:
        handler = SERVICE_HANDLERS[method]
    except KeyError as exc:
        raise KeyError(f"no service handler registered for {method}") from exc
    result = handler(params, context)
    return contract.validate_response(result)


def _graph_handler(
    build: Callable[[dict[str, Any], SessionGraph], Any],
) -> ServiceHandler:
    """Resolve the graph and wrap the graph-level build result.

    Every ``graph.*`` projection follows the same preamble: resolve the graph
    from the entry-point id, build the projection, and pass it through the
    public output seam. Entry-point caching is centralized in :func:`dispatch`.
    """

    @wraps(build)
    def wrapper(params: dict[str, Any], context: ServiceContext) -> Any:
        session_graph = _resolve_session_graph(
            context.store, _session_graph_entrypoint_id(params)
        )
        from coding_trajectory.analysis.orchestration_runs import (
            orchestration_run_for_entrypoint,
        )

        entrypoint = _session_graph_entrypoint_id(params)
        session_graph = orchestration_run_for_entrypoint(
            session_graph,
            _parse_user_id(entrypoint) if entrypoint else None,
        )
        return _public_output_for_session_graph(
            session_graph, build(params, session_graph)
        )

    return wrapper


def _single_session_graph(
    session_graph: SessionGraph, entrypoint_id: str | None
) -> SessionGraph:
    """Select one canonical session from a graph for ``session.*`` methods."""
    from coding_trajectory.ingestion.indexes import build_session_graph_index

    index = build_session_graph_index(session_graph)
    selected: Session | None = None
    if entrypoint_id:
        resource_id = _parse_user_id(entrypoint_id)
        selected = index.sessions_by_id.get(resource_id)
        if selected is None:
            turn = index.turns_by_id.get(resource_id)
            if turn is not None:
                selected = index.sessions_by_id.get(turn.session_id)
    if selected is None:
        selected = index.sessions_by_id.get(session_graph.root_session_id)
    if selected is None:
        selected = min(
            session_graph.sessions,
            key=lambda item: (item.started_at, str(item.session_id)),
            default=None,
        )
    if selected is None:
        raise ValueError("session_graph has no sessions")
    return SessionGraph(
        root_session_id=selected.session_id,
        project_identifier=session_graph.project_identifier,
        summary=None,
        sessions=[selected],
    )


def _single_session_handler(
    build: Callable[[dict[str, Any], SessionGraph], Any],
) -> ServiceHandler:
    """Resolve one thread while retaining the source graph for cache lookup."""

    @wraps(build)
    def wrapper(params: dict[str, Any], context: ServiceContext) -> Any:
        session_graph = _resolve_session_graph(
            context.store, _session_graph_entrypoint_id(params)
        )
        selected_graph = _single_session_graph(
            session_graph, _session_graph_entrypoint_id(params)
        )
        return _public_output_for_session_graph(
            selected_graph, build(params, selected_graph)
        )

    return wrapper


def _handle_project_sessions(
    params: dict[str, Any], context: ServiceContext
) -> dict[str, Any]:
    from coding_trajectory.analysis.session_graph_views import (
        session_graph_has_visible_overview_content,
    )

    session_graphs = resolve_collection(
        context.store,
        "session_graph",
        global_scope=context.global_scope,
        current_dir=context.current_dir,
        project_name=params.get("project_name"),
        agent_vendor=params.get("agent_vendor"),
    )
    include = set(params.get("include") or [])
    items: list[dict[str, Any]] = []
    from coding_trajectory.analysis.orchestration_runs import orchestration_runs

    for lineage_graph in session_graphs:
        for graph in orchestration_runs(lineage_graph):
            if not session_graph_has_visible_overview_content(graph):
                continue
            item = {
                **serialize_session_graph_detail(graph),
                "project": graph.project_identifier,
                "lineage_root_session_id": str(lineage_graph.root_session_id),
            }
            if "usage" in include:
                from coding_trajectory.metrics import build_session_graph_usage

                usage = build_session_graph_usage(graph)
                item["usage"] = usage.get("total_usage") or {}
                item["warnings"] = usage.get("warnings") or []
                if "runtime" in include:
                    item["runtime"] = usage.get("runtime") or {}
            elif "runtime" in include:
                from coding_trajectory.metrics import build_session_graph_runtime

                item["runtime"] = build_session_graph_runtime(graph)
            items.append(_public_output_for_session_graph(graph, item))
    return {"items": items}


def _handle_project_list(
    params: dict[str, Any], context: ServiceContext
) -> dict[str, Any]:
    """Dispatch adapter for ``project.list``.

    Production traffic is short-circuited in :meth:`ServiceRuntime.call` to
    :func:`project_list_metadata` (which never builds a store). This handler
    keeps ``dispatch("project.list", ...)`` consistent with that path by
    delegating to the same canonical implementation, so the contract registry
    and the handler registry agree.
    """
    return project_list_metadata(
        params,
        global_scope=context.global_scope,
        current_dir=context.current_dir,
    )


@_single_session_handler
def _handle_session_overview(
    params: dict[str, Any], session_graph: SessionGraph
) -> Any:
    from coding_trajectory.analysis.projections import build_session_graph_overview

    return build_session_graph_overview(
        session_graph,
        num_turns=_optional_positive_int(params, "num_turns"),
        drop_turns=_optional_positive_int(params, "drop_turns"),
    )


def _handle_session_tree(params: dict[str, Any], context: ServiceContext) -> Any:
    from coding_trajectory.analysis.orchestration_runs import (
        build_conversation_tree,
        orchestration_run_for_entrypoint,
    )

    entrypoint = _session_graph_entrypoint_id(params)
    session_graph = _resolve_session_graph(
        context.store, entrypoint
    )
    tree = build_conversation_tree(session_graph)
    run = orchestration_run_for_entrypoint(
        session_graph,
        _parse_user_id(entrypoint) if entrypoint else None,
    )
    tree["selected_branch_id"] = str(run.root_session_id)
    return tree


@_single_session_handler
def _handle_session_stats(params: dict[str, Any], session_graph: SessionGraph) -> Any:
    return _build_stats_response(session_graph)


@_single_session_handler
def _handle_session_usage(params: dict[str, Any], session_graph: SessionGraph) -> Any:
    from coding_trajectory.metrics import build_session_graph_usage

    return build_session_graph_usage(session_graph, turn_id=params.get("turn_id"))


@_single_session_handler
def _handle_session_model_usage(
    params: dict[str, Any], session_graph: SessionGraph
) -> Any:
    from coding_trajectory.metrics import build_session_graph_model_usage

    return build_session_graph_model_usage(
        session_graph,
        turn_id=params.get("turn_id"),
    )


@_single_session_handler
def _handle_session_request_usage(
    params: dict[str, Any], session_graph: SessionGraph
) -> Any:
    from coding_trajectory.metrics import build_session_graph_request_usage

    include = set(params.get("include") or [])
    return build_session_graph_request_usage(
        session_graph,
        turn_id=params.get("turn_id"),
        include_causality="causality" in include,
        include_context_diagnostics="context" in include,
    )


@_single_session_handler
def _handle_session_tool_usage(
    params: dict[str, Any], session_graph: SessionGraph
) -> Any:
    from coding_trajectory.metrics import build_session_graph_tool_usage

    include = set(params.get("include") or [])
    return build_session_graph_tool_usage(
        session_graph,
        turn_id=params.get("turn_id"),
        include_item_real_token_costs="item_costs" in include,
        include_advanced_causality="causality" in include,
    )


@_graph_handler
def _handle_graph_overview(params: dict[str, Any], session_graph: SessionGraph) -> Any:
    from coding_trajectory.analysis.graph_views import build_graph_overview

    return build_graph_overview(
        session_graph,
        num_turns=_optional_positive_int(params, "num_turns"),
        drop_turns=_optional_positive_int(params, "drop_turns"),
        include_narrative="narrative" in set(params.get("include") or []),
    )


@_graph_handler
def _handle_graph_stats(params: dict[str, Any], session_graph: SessionGraph) -> Any:
    return _build_stats_response(
        session_graph,
        include_session_composition=(
            "session_composition" in set(params.get("include") or [])
        ),
    )


def _build_stats_response(
    session_graph: SessionGraph,
    *,
    include_session_composition: bool = True,
) -> dict[str, Any]:
    from coding_trajectory.metrics import build_session_graph_stats

    return build_session_graph_stats(
        session_graph,
        include_session_composition=include_session_composition,
    )


@_graph_handler
def _handle_graph_usage(params: dict[str, Any], session_graph: SessionGraph) -> Any:
    from coding_trajectory.metrics import build_session_graph_usage

    include = set(params.get("include") or [])
    return build_session_graph_usage(
        session_graph,
        turn_id=params.get("turn_id"),
        include_graph_turns="flat_turns" in include,
    )


def _handle_session_events(
    params: dict[str, Any], context: ServiceContext
) -> dict[str, Any]:
    from coding_trajectory.analysis.projections import build_event_scan
    from coding_trajectory.ingestion.indexes import (
        SessionGraphIndex,
        build_session_graph_index,
        item_for_event,
    )

    event_ids = params.get("event_ids")
    selected_turn_id = params.get("turn_id")
    if event_ids:
        matches: list[dict[str, Any]] = []
        root_session_id: str | None = None
        indexes_by_graph_id: dict[UUID, SessionGraphIndex] = {}
        for eid in event_ids:
            try:
                event = resolve_resource(context.store, "event", eid)
                session_graph = context.store.get_session_graph_for_session(
                    event.session_id
                )
                selected_graph = _single_session_graph(
                    session_graph,
                    params.get("session_id")
                    or params.get("root_session_id")
                    or selected_turn_id
                    or str(event.session_id),
                )
                allowed_event_ids = _event_ids_for_turn(
                    selected_graph,
                    selected_turn_id,
                )
                if (
                    allowed_event_ids is not None
                    and event.event_id not in allowed_event_ids
                ):
                    continue
                if root_session_id is None:
                    root_session_id = str(selected_graph.root_session_id)
                index = indexes_by_graph_id.get(selected_graph.root_session_id)
                if index is None:
                    index = build_session_graph_index(selected_graph)
                    indexes_by_graph_id[selected_graph.root_session_id] = index
                related_item = item_for_event(index, event.event_id)
                detail = _public_output_for_session_graph(
                    selected_graph,
                    serialize_event_detail(event, related_item=related_item),
                )
                matches.append(detail)
            except (ResourceNotFoundError, ValueError) as exc:
                debug.warn(
                    f"skipping unresolved event id {eid!r}: {exc}",
                    code="session.events.event_id_unresolved",
                    event_id=eid,
                )
                continue
        return {
            "root_session_id": root_session_id,
            "type": params.get("type"),
            "matches": matches,
        }

    entrypoint_id = _session_graph_entrypoint_id(params)
    session_graph = _resolve_session_graph(context.store, entrypoint_id)
    session_graph = _single_session_graph(session_graph, entrypoint_id)
    allowed_event_ids = _event_ids_for_turn(session_graph, selected_turn_id)

    event_type = params.get("type")
    if not event_type:
        all_events: list[dict[str, Any]] = []
        for session in session_graph.sessions:
            for event in session.events:
                if (
                    allowed_event_ids is not None
                    and event.event_id not in allowed_event_ids
                ):
                    continue
                detail = serialize_event_detail(event)
                all_events.append(
                    _public_output_for_session_graph(session_graph, detail)
                )
        limit = params.get("limit")
        if limit:
            all_events = all_events[:limit]
        return {
            "root_session_id": str(session_graph.root_session_id),
            "type": None,
            "matches": all_events,
        }

    result = build_event_scan(
        session_graph,
        event_type=event_type,
        filters=params.get("filters") or [],
        event_ids=allowed_event_ids,
    )
    limit = params.get("limit")
    if limit:
        result["matches"] = result["matches"][:limit]
    return _public_output_for_session_graph(session_graph, result)


def _handle_session_items(
    params: dict[str, Any], context: ServiceContext
) -> list[dict[str, Any]]:
    from coding_trajectory.analysis.projections import build_item_details
    from coding_trajectory.ingestion.indexes import (
        SessionGraphIndex,
        build_session_graph_index,
    )
    from coding_trajectory.ingestion.models import PlanItem

    item_ids = params.get("item_ids")
    selected_turn_id = params.get("turn_id")
    include_content = bool(params.get("include_content"))
    if item_ids:
        result: list[dict[str, Any]] = []
        indexes_by_graph_id: dict[UUID, SessionGraphIndex] = {}
        for item_id in item_ids:
            try:
                item = resolve_resource(context.store, "item", item_id)
                session_graph = context.store.get_session_graph_for_session(
                    item.session_id
                )
                if selected_turn_id is not None and str(item.turn_id) != str(
                    selected_turn_id
                ):
                    continue
                index = None
                if isinstance(item, PlanItem):
                    index = indexes_by_graph_id.get(session_graph.root_session_id)
                    if index is None:
                        index = build_session_graph_index(session_graph)
                        indexes_by_graph_id[session_graph.root_session_id] = index
                result.append(
                    _public_output_for_session_graph(
                        session_graph,
                        build_item_details(
                            item,
                            session_graph=session_graph,
                            include_content=include_content,
                            index=index,
                        ),
                    )
                )
            except (ResourceNotFoundError, ValueError) as exc:
                debug.warn(
                    f"skipping unresolved item id {item_id!r}: {exc}",
                    code="session.items.item_id_unresolved",
                    item_id=item_id,
                )
                continue
        return result

    entrypoint_id = params.get("session_id") or params.get("root_session_id")
    session_graph = _resolve_session_graph(context.store, entrypoint_id)
    session_graph = _single_session_graph(session_graph, entrypoint_id)

    types_filter = set(params["types"]) if params.get("types") else None
    projection_index: SessionGraphIndex | None = None
    result = []
    for session in session_graph.sessions:
        for turn in session.turns:
            if selected_turn_id is not None and str(turn.turn_id) != str(
                selected_turn_id
            ):
                continue
            for item in turn.items:
                if types_filter and item.kind not in types_filter:
                    continue
                if isinstance(item, PlanItem) and projection_index is None:
                    projection_index = build_session_graph_index(session_graph)
                result.append(
                    _public_output_for_session_graph(
                        session_graph,
                        build_item_details(
                            item,
                            session_graph=session_graph,
                            include_content=include_content,
                            index=projection_index,
                        ),
                    )
                )
    return result


def _event_ids_for_turn(
    session_graph: SessionGraph,
    turn_id: str | None,
) -> set[UUID] | None:
    if turn_id is None:
        return None
    parsed_turn_id = _parse_user_id(turn_id)
    for session in session_graph.sessions:
        for turn in session.turns:
            if turn.turn_id != parsed_turn_id:
                continue
            return {
                *turn.event_ids,
                *(
                    [turn.user_request_event_id]
                    if turn.user_request_event_id is not None
                    else []
                ),
                *(event_id for item in turn.items for event_id in item.event_ids),
            }
    raise ResourceNotFoundError(f"turn not found in selected session: {turn_id}")


SERVICE_HANDLERS: dict[str, ServiceHandler] = {
    "project.list": _handle_project_list,
    "project.sessions": _handle_project_sessions,
    "session.overview": _handle_session_overview,
    "session.tree": _handle_session_tree,
    "graph.overview": _handle_graph_overview,
    "session.stats": _handle_session_stats,
    "graph.stats": _handle_graph_stats,
    "session.usage": _handle_session_usage,
    "graph.usage": _handle_graph_usage,
    "session.model_usage": _handle_session_model_usage,
    "session.request_usage": _handle_session_request_usage,
    "session.tool_usage": _handle_session_tool_usage,
    "session.events": _handle_session_events,
    "session.items": _handle_session_items,
}
