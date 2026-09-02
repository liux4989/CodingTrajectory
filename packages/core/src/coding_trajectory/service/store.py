"""Store construction and the disposable index cache for the service layer."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from coding_trajectory.discovery import (
    DiscoverySource,
    discover_project_metadata,
    discover_store,
    discover_store_from_files,
    format_discovery_sources,
    locate_session_files,
)
from coding_trajectory.ingestion.common import normalize_project_key
from coding_trajectory.ingestion.models import (
    Event,
    Item,
    Session,
    SessionGraph,
    Turn,
)
from coding_trajectory.query import DocumentStore, ResourceNotFoundError
from coding_trajectory.service.serializers import _normalize_user_id, _parse_user_id


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
    include_descendants: bool = True,
) -> tuple[DocumentStore, str]:
    """Build a store: use cached path index for targeted load, fall back to full discovery."""
    entrypoint_id = _session_graph_entrypoint_id(params)
    if entrypoint_id and not include_descendants:
        normalized_entrypoint_id = _normalize_user_id(entrypoint_id)
        target_session_graph_id = cache.root_for_entrypoint(normalized_entrypoint_id)
        cached_paths = cache.paths_for_session_graph(target_session_graph_id)
        filename_match = any(
            normalized_entrypoint_id in Path(path).stem.lower() for path in cached_paths
        )
        if target_session_graph_id == normalized_entrypoint_id or filename_match:
            try:
                located = locate_session_files(
                    session_id=_parse_user_id(entrypoint_id),
                    current_dir=current_dir,
                    global_scope=True,
                    agent_vendor=params.get("agent_vendor"),
                    include_descendants=False,
                )
            except ValueError:
                located = []
            if located:
                return _build_store_targeted([str(path) for path in located], cache)

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
                include_descendants=include_descendants,
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
