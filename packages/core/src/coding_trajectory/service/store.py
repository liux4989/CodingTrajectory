"""Request-scoped source topology and retained stores for the service layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from coding_trajectory.analysis.orchestration_runs import orchestration_runs
from coding_trajectory.discovery import (
    DiscoveryCandidate,
    DiscoveryResult,
    DiscoverySource,
    _claude_project_path_from_source,
    _ingest_sessions,
    discover_source_candidates,
    format_discovery_sources,
    infer_project_identifier,
    locate_session_files,
)
from coding_trajectory.ingestion.adapters.base import SessionHeader, SourceTopology
from coding_trajectory.ingestion.common import normalize_project_key
from coding_trajectory.ingestion.graph import (
    assemble_project_session_graphs,
    build_session_graph,
)
from coding_trajectory.ingestion.models import (
    Event,
    Item,
    Session,
    SessionGraph,
    Turn,
    Vendor,
)
from coding_trajectory.ingestion.retained import retain_session_graph
from coding_trajectory.ingestion.topology import TopologyRun, orchestration_topology
from coding_trajectory.project_identity import local_project_id
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


@dataclass
class IndexCache:
    """In-memory source and entry-point holder for one request or batch.

    ``session_to_session_graph`` keeps the existing locator API. It stores
    every supported session-graph entry point (graph, session, and turn IDs),
    while :attr:`entrypoint_to_root` exposes that meaning to core code.
    Canonical graph membership remains owned by :class:`DocumentStore`.
    """

    path_to_session_graph: dict[str, str] = field(default_factory=dict)
    session_to_session_graph: dict[str, str] = field(default_factory=dict)
    _candidates: dict[str, DiscoveryCandidate] = field(default_factory=dict, repr=False)
    _topologies: dict[str, SourceTopology] = field(default_factory=dict, repr=False)
    _batch_mode: bool = field(default=False, repr=False)
    _runs: list[TopologyRun] | None = field(default=None, repr=False)
    _graphs: dict[str, SessionGraph] = field(default_factory=dict, repr=False)
    _full_topology: bool = field(default=False, repr=False)

    def _reset(self) -> None:
        self.path_to_session_graph.clear()
        self.session_to_session_graph.clear()
        self._candidates.clear()
        self._topologies.clear()
        self._runs = None
        self._graphs.clear()
        self._full_topology = False

    @property
    def entrypoint_to_root(self) -> dict[str, str]:
        """Return the request's entry-point mapping."""
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


def _refresh_topology(
    cache: IndexCache, current_dir: Path, raw_ids: list[str] | None = None
) -> list[TopologyRun]:
    if not cache._batch_mode:
        cache._reset()
    elif cache._runs is not None and (raw_ids is not None or cache._full_topology):
        return cache._runs

    if not cache._candidates:
        cache._candidates = {
            str(candidate.path.resolve()): candidate
            for candidate in discover_source_candidates(
                current_dir=current_dir, global_scope=True
            )
        }

    def scan(path: str) -> SourceTopology | None:
        if path not in cache._topologies:
            candidate = cache._candidates[path]
            topology = candidate.adapter_cls().scan_topology(candidate.path)
            if topology is not None:
                cache._topologies[path] = topology.model_copy(
                    update={"project": topology.project or candidate.path.stem}
                )
        return cache._topologies.get(path)

    # Locate entry files first, but never limit membership to their parent
    # chain: cross-vendor children and conversation forks need project topology.
    projects: set[str] | None = set() if raw_ids else None
    for raw in raw_ids or []:
        try:
            paths = locate_session_files(
                session_id=_parse_user_id(raw),
                current_dir=current_dir,
                global_scope=True,
                include_descendants=False,
            )
            if not paths:
                projects = None
                break
            for source in paths:
                path = str(source.resolve())
                topology = scan(path) if path in cache._candidates else None
                if topology is None:
                    projects = None
                    break
                projects.add(normalize_project_key(topology.project or ""))
            if projects is None:
                break
        except (OSError, ValueError):
            projects = None
            break

    for path, candidate in cache._candidates.items():
        try:
            if projects is not None and path not in cache._topologies:
                # Claude's encoded source directory overrides its header cwd;
                # other vendors use header cwd. Match topology's project-name
                # partition, not exact directories or Codex's tracked config.
                project = None
                if candidate.vendor == Vendor.CLAUDE_CODE:
                    project_path = _claude_project_path_from_source(candidate.path)
                    if project_path is not None:
                        project = project_path.name or None
                else:
                    header = candidate.adapter_cls().scan_header(candidate.path)
                    if header is not None:
                        project = (
                            Path(header.cwd).name if header.cwd else candidate.path.stem
                        )
                # Uncertain source identity is never grounds for exclusion.
                if (
                    project is not None
                    and normalize_project_key(project) not in projects
                ):
                    continue
            scan(path)
        except (OSError, ValueError):
            # Malformed/changing sources can be retried by the next request.
            continue
    cache._runs = orchestration_topology(cache._topologies)
    cache._full_topology = projects is None
    for run in cache._runs:
        root = str(run.root_session_id)
        cache.path_to_session_graph.update((path, root) for path in run.source_paths)
        cache.entrypoint_to_root.update((str(sid), root) for sid in run.session_ids)
    return cache._runs


def _run_project_id(run: TopologyRun) -> str:
    return local_project_id(
        Path(run.header.cwd) if run.header.cwd else None,
        fallback=run.header.project or str(run.root_session_id),
    )


def _filter_runs(
    runs: list[TopologyRun],
    params: dict[str, Any],
    *,
    global_scope: bool,
    current_dir: Path,
) -> list[TopologyRun]:
    project = params.get("project_name") or (None if global_scope else current_dir.name)
    cutoff = params.get("modified_since")
    if isinstance(cutoff, str):
        cutoff = datetime.fromisoformat(cutoff)
    if cutoff is not None and cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=UTC)
    return [
        run
        for run in runs
        if (
            not project
            or normalize_project_key(run.header.project or "")
            == normalize_project_key(project)
        )
        and (
            not params.get("project_id") or _run_project_id(run) == params["project_id"]
        )
        and (
            not params.get("agent_vendor")
            or params["agent_vendor"] in {v.value for v in run.vendors}
        )
        and (cutoff is None or run.modified >= cutoff)
    ]


def project_sessions_metadata(
    params: dict[str, Any], *, global_scope: bool, current_dir: Path, cache: IndexCache
) -> dict[str, Any]:
    """Unpaged orchestration inventory; no canonical ingestion or metrics."""
    runs = _refresh_topology(cache, current_dir)
    items = [
        {
            "graph_id": str(run.root_session_id),
            "root_session_id": str(run.root_session_id),
            "lineage_root_session_id": str(run.lineage_root_session_id),
            "project_id": _run_project_id(run),
            "project": run.header.project,
            "title": run.header.title,
            "preview": run.header.preview,
            "vendors": [vendor.value for vendor in run.vendors],
            "session_ids": [str(sid) for sid in run.session_ids],
            "modified": run.modified,
        }
        for run in _filter_runs(
            runs, params, global_scope=global_scope, current_dir=current_dir
        )
    ]
    return {
        "items": sorted(
            items, key=lambda item: (item["project_id"], item["root_session_id"])
        )
    }


def _discover_requested_sources(paths: list[str], cache: IndexCache) -> DiscoveryResult:
    """Reuse canonical two-pass discovery, supplying this request's headers.

    Discovery's parent-turn cutting and resumed-segment coalescing stay intact.
    Only its header pass is overridden to avoid reparsing topology.
    """

    class CachedHeader:
        def scan_header(self, source: Path) -> SessionHeader:
            header = cache._topologies[str(source.resolve())]
            return SessionHeader(
                session_id=header.session_id,
                vendor=header.vendor,
                parent_session_id=header.parent_session_id,
                title=header.title,
                cwd=header.cwd,
            )

    adapter_types = {}
    candidates = []
    for path in paths:
        candidate = cache._candidates[path]
        adapter_cls = candidate.adapter_cls
        if adapter_cls not in adapter_types:
            adapter_types[adapter_cls] = type(
                f"Cached{adapter_cls.__name__}", (CachedHeader, adapter_cls), {}
            )
        candidates.append((candidate.vendor, adapter_types[adapter_cls], Path(path)))
    ingested, provenance = _ingest_sessions(candidates)
    sessions_by_project: dict[str, list[Session]] = {}
    for source in ingested:
        project = infer_project_identifier(
            source.session, source.paths[0], fallback=source.paths[0].stem
        )
        sessions_by_project.setdefault(project or source.paths[0].stem, []).append(
            source.session
        )
    graphs = [
        graph
        for project, sessions in sorted(sessions_by_project.items())
        for graph in assemble_project_session_graphs(project, sessions)
    ]
    store = DocumentStore.from_session_graphs(graphs)
    sources = [
        DiscoverySource(
            vendor=source.vendor,
            path=path,
            root_session_id=store.session_to_root.get(source.session.session_id),
        )
        for source in ingested
        for path in source.paths
    ]
    return DiscoveryResult(store=store, sources=sources, provenance=provenance)


def resolve_store(
    params: dict[str, Any],
    *,
    global_scope: bool,
    current_dir: Path,
    cache: IndexCache,
    include_descendants: bool = True,
    selector: str = "run",
) -> tuple[DocumentStore, str]:
    """Resolve requested runs once; ``selector='lineage'`` is for session.tree.

    The compatibility ``include_descendants`` argument does not widen an
    orchestration run into conversation forks. Ancestors are read only to trim
    inherited Codex turns, and are not returned as unrelated detail graphs.
    """
    if selector not in {"run", "lineage"}:
        raise ValueError(f"unsupported topology selector: {selector}")
    entrypoint_id = _session_graph_entrypoint_id(params)
    raw_ids = (
        params.get("session_ids")
        if "session_ids" in params
        else ([entrypoint_id] if entrypoint_id else None)
    )
    runs = _refresh_topology(cache, current_dir, raw_ids)
    # A preceding tree handler may have indexed the assembled lineage graph.
    # Selection must still use orchestration-run ownership for every item.
    for run in runs:
        cache.entrypoint_to_root.update(
            (str(sid), str(run.root_session_id)) for sid in run.session_ids
        )
    if raw_ids is not None:
        requested = set()
        for raw in raw_ids:
            try:
                requested.add(cache.root_for_entrypoint(_normalize_user_id(raw)))
            except ValueError:
                continue
        selected = [run for run in runs if str(run.root_session_id) in requested]
    else:
        selected = _filter_runs(
            runs, params, global_scope=global_scope, current_dir=current_dir
        )
    if selector == "lineage":
        tree_ids = params.get("lineage_session_ids")
        tree_roots = (
            {cache.root_for_entrypoint(_normalize_user_id(raw)) for raw in tree_ids}
            if tree_ids is not None
            else {str(run.root_session_id) for run in selected}
        )
        lineages = {
            run.lineage_root_session_id
            for run in selected
            if str(run.root_session_id) in tree_roots
        }
        roots = {run.root_session_id for run in selected}
        selected = [
            run
            for run in runs
            if run.root_session_id in roots or run.lineage_root_session_id in lineages
        ]
    if not selected:
        return DocumentStore.from_session_graphs([]), "(no matching sources)"
    missing = [run for run in selected if str(run.root_session_id) not in cache._graphs]
    description = "(request-scoped retained graphs)"
    if missing:
        # One union discovery for the request or batch, including ancestors needed
        # for fork cutting. No unrelated siblings or workspace preparation.
        paths = sorted(
            {path for run in missing for path in run.source_paths + run.ancestor_paths}
        )
        discovery = _discover_requested_sources(paths, cache)
        description = format_discovery_sources(discovery.sources)
        needed = {str(run.root_session_id) for run in missing}
        for graph in discovery.store.session_graphs.values():
            for run_graph in orchestration_runs(graph):
                root = str(run_graph.root_session_id)
                if root in needed:
                    cache._graphs[root] = retain_session_graph(run_graph)
    graphs = {
        str(run.root_session_id): cache._graphs[str(run.root_session_id)]
        for run in selected
        if str(run.root_session_id) in cache._graphs
    }
    store = DocumentStore.from_session_graphs(list(graphs.values()))
    cache.index_store(store)
    if selector == "lineage":
        lineage_graphs = []
        for lineage in sorted(
            {run.lineage_root_session_id for run in selected}, key=str
        ):
            owned = [
                graphs[str(run.root_session_id)]
                for run in selected
                if run.lineage_root_session_id == lineage
                and str(run.root_session_id) in graphs
            ]
            if owned:
                lineage_graphs.append(
                    build_session_graph(
                        root_session_id=lineage,
                        project_identifier=owned[0].project_identifier or "",
                        sessions=[
                            session for graph in owned for session in graph.sessions
                        ],
                    )
                )
        store = DocumentStore.from_session_graphs(lineage_graphs)
    return store, description


def project_list_metadata(
    params: dict[str, Any],
    *,
    global_scope: bool,
    current_dir: Path,
    cache: IndexCache | None = None,
) -> dict[str, Any]:
    """Return project list data without fully ingesting session transcripts."""
    projects: dict[str, dict[str, Any]] = {}
    runs = _refresh_topology(cache or IndexCache(), current_dir)
    for run in _filter_runs(
        runs, params, global_scope=global_scope, current_dir=current_dir
    ):
        name = run.header.project or str(run.root_session_id)
        key = _run_project_id(run)
        if name.startswith("unknown-"):
            continue
        entry = projects.setdefault(
            key, {"display_name": name, "path": run.header.cwd, "vendors": set()}
        )
        entry["vendors"].update(vendor.value for vendor in run.vendors)

    items = {
        key: {
            "project_id": key,
            "display_name": value["display_name"],
            "path": value["path"],
            "vendors": sorted(value["vendors"]),
        }
        for key, value in sorted(projects.items())
        if not params.get("project_id") or key == params["project_id"]
    }
    return {"items": items}
