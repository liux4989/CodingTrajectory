"""Service layer implementing the session-api.json contract."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from coding_trajectory.discovery import (
    DiscoverySource,
    discover_project_metadata,
    discover_store,
    discover_store_from_file,
    discover_store_from_files,
    format_discovery_sources,
)
from coding_trajectory.contracts import service_contract
from coding_trajectory.ingestion.common import (
    format_datetime,
    normalize_project_key,
    prune_nones,
)
from coding_trajectory.ingestion.models import (
    Event,
    EventType,
    Session,
    Item,
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


def _session_title(session: Session) -> str | None:
    extensions = session.extensions
    if extensions and extensions.codex and extensions.codex.title:
        return extensions.codex.title
    if extensions and extensions.claude_code and extensions.claude_code.title:
        return extensions.claude_code.title
    if extensions and extensions.pi and extensions.pi.title:
        return extensions.pi.title
    return None


def _session_graph_title(session_graph: SessionGraph) -> str | None:
    by_id = {session.session_id: session for session in session_graph.sessions}
    root = by_id.get(session_graph.root_session_id)
    if root is not None:
        title = _session_title(root)
        if title:
            return title
    for session in session_graph.sessions:
        title = _session_title(session)
        if title:
            return title
    return None


def _public_session_id_for_session(session: Session) -> str:
    return str(session.session_id)


def _public_session_id_map(session_graph: SessionGraph) -> dict[str, str]:
    return {
        str(session.session_id): _public_session_id_for_session(session)
        for session in session_graph.sessions
    }


def _public_session_id_value(raw_id: str, session_ids: dict[str, str]) -> str:
    try:
        normalized = str(UUID(raw_id))
    except ValueError:
        return raw_id
    return session_ids.get(normalized, raw_id)


def _render_public_session_ids(value: Any, session_ids: dict[str, str]) -> Any:
    if isinstance(value, list):
        return [_render_public_session_ids(item, session_ids) for item in value]

    if not isinstance(value, dict):
        return value

    rendered: dict[str, Any] = {}
    for key, item in value.items():
        if key == "payload":
            rendered[key] = item
            continue
        if key in {
            "root_session_id",
            "session_id",
            "parent_session_id",
            "agent_session_id",
            "handoff_session_id",
        }:
            rendered[key] = (
                _public_session_id_value(item, session_ids)
                if isinstance(item, str)
                else item
            )
            continue
        if key in {"session_ids", "forked_session_ids"} and isinstance(item, list):
            rendered[key] = [
                _public_session_id_value(entry, session_ids)
                if isinstance(entry, str)
                else entry
                for entry in item
            ]
            continue
        rendered[key] = _render_public_session_ids(item, session_ids)
    return rendered


def _public_output_for_session_graph(session_graph: SessionGraph, payload: Any) -> Any:
    return _render_public_session_ids(payload, _public_session_id_map(session_graph))


def serialize_session_graph_detail(session_graph: SessionGraph) -> dict[str, Any]:
    vendors = sorted(
        {session.vendor.value for session in session_graph.sessions if session.vendor}
    )
    return prune_nones(
        {
            "root_session_id": str(session_graph.root_session_id),
            "title": _session_graph_title(session_graph),
            "vendors": vendors or None,
            "session_ids": [
                str(session.session_id) for session in session_graph.sessions
            ],
        }
    )


def serialize_session_detail(session: Session) -> dict[str, Any]:
    return prune_nones(
        {
            "session_id": str(session.session_id),
            "status": session.status,
            "turn_ids": [str(turn.turn_id) for turn in session.turns],
            "event_ids": [str(event.event_id) for event in session.events],
        }
    )


def serialize_turn_detail(turn: Turn) -> dict[str, Any]:
    return prune_nones(
        {
            "turn_id": str(turn.turn_id),
            "session_id": str(turn.session_id),
            "status": turn.status,
            "event_ids": [str(event_id) for event_id in turn.event_ids],
            "item_ids": [str(item.item_id) for item in turn.items],
        }
    )


def serialize_item_detail(item: Item) -> dict[str, Any]:
    return prune_nones(
        {
            "item_id": str(item.item_id),
            "session_id": str(item.session_id),
            "turn_id": str(item.turn_id),
            "kind": item.kind,
            **item.model_dump(
                mode="json", exclude={"item_id", "session_id", "turn_id", "kind"}
            ),
        }
    )


def serialize_event_detail(event: Event) -> dict[str, Any]:
    return prune_nones(
        {
            "event_id": str(event.event_id),
            "session_id": str(event.session_id),
            "timestamp": format_datetime(event.timestamp),
            "type": event.type.value,
            "tool_call": serialize_tool_call_detail(event),
            "llm": serialize_llm_detail(event),
            "text": serialize_text_detail(event),
        }
    )


def serialize_tool_call_detail(event: Event) -> dict[str, Any] | None:
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
    return (
        prune_nones(
            {
                "tool_call_id": payload.get("tool_call_id"),
                "tool_name": payload.get("tool_name"),
                "input": payload.get("tool_args") or payload.get("input"),
                "result": payload.get("result")
                or payload.get("tool_output")
                or payload.get("tool_text"),
                "status": status_by_type.get(event.type),
            }
        )
        or None
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
    """Lazy index persisted to ~/.coding-trajectory/index.json."""

    path_to_session_graph: dict[str, str] = field(default_factory=dict)
    session_to_session_graph: dict[str, str] = field(default_factory=dict)

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
        live_tids = set(self.path_to_session_graph.values())
        for tid in stale_tids - live_tids:
            self.session_to_session_graph = {
                sid: t for sid, t in self.session_to_session_graph.items() if t != tid
            }


# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------


def _resolve_session_graph(store: Any, raw_id: str | None) -> Any:
    """Resolve a session graph by a session entry point."""
    if raw_id is not None:
        resource_id = _parse_user_id(raw_id)
        try:
            return store.get_session_graph(resource_id)
        except ResourceNotFoundError:
            try:
                session = store.get_session(resource_id)
                return store.get_session_graph_for_session(session.session_id)
            except ResourceNotFoundError:
                try:
                    return store.get_session_graph_for_turn(resource_id)
                except ResourceNotFoundError:
                    raise ResourceNotFoundError(
                        f"resource not found: {raw_id}"
                    ) from None
    session_graphs = list(store.session_graphs.values())
    if len(session_graphs) == 1:
        return session_graphs[0]
    if not session_graphs:
        raise ValueError("no session_graphs found in store")
    raise ValueError(
        "session_id is required when the store contains multiple session_graphs"
    )


def _session_graph_entrypoint_id(params: dict[str, Any]) -> str | None:
    """Return the public session entry point."""
    return (
        params.get("session_id")
        or params.get("root_session_id")
        or params.get("turn_id")
    )


def _update_path_index(cache: IndexCache, sources: list[DiscoverySource]) -> None:
    for source in sources:
        if source.root_session_id is not None:
            cache.path_to_session_graph[str(source.path)] = str(source.root_session_id)


def _update_session_index(cache: IndexCache, store: DocumentStore) -> None:
    for session_id, root_session_id in store.session_to_root.items():
        cache.session_to_session_graph[str(session_id)] = str(root_session_id)
    for turn_id, turn in store.turns.items():
        root_session_id = store.session_to_root.get(turn.session_id)
        if root_session_id is not None:
            cache.session_to_session_graph[str(turn_id)] = str(root_session_id)


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
    _update_path_index(cache, discovery.sources)
    _update_session_index(cache, discovery.store)

    return discovery.store, format_discovery_sources(discovery.sources)


def _build_store_targeted(
    paths: list[str], cache: IndexCache
) -> tuple[DocumentStore, str]:
    """Targeted discovery — ingest only the files mapped to a session_graph."""
    if not paths:
        return DocumentStore.from_session_graphs([]), "(no targeted paths)"
    expanded_paths = _expand_targeted_paths([Path(p) for p in paths])
    discovery = discover_store_from_files(expanded_paths)
    _update_path_index(cache, discovery.sources)
    _update_session_index(cache, discovery.store)
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
    log_file: Path | None,
    global_scope: bool,
    current_dir: Path,
    cache: IndexCache,
) -> tuple[DocumentStore, str]:
    """Build a store: use cached path index for targeted load, fall back to full discovery."""
    if log_file is not None:
        discovery = discover_store_from_file(log_file)
        _update_path_index(cache, discovery.sources)
        return discovery.store, format_discovery_sources(discovery.sources)

    entrypoint_id = _session_graph_entrypoint_id(params)
    if entrypoint_id and cache.path_to_session_graph:
        normalized_entrypoint_id = _normalize_user_id(entrypoint_id)
        target_session_graph_id = cache.session_to_session_graph.get(
            normalized_entrypoint_id, normalized_entrypoint_id
        )
        cached_paths = cache.paths_for_session_graph(target_session_graph_id)
        if cached_paths:
            return _build_store_targeted(cached_paths, cache)

    bulk_ids = params.get("session_ids")
    if bulk_ids:
        bulk_paths = _resolve_bulk_cached_paths(bulk_ids, cache)
        if bulk_paths is not None:
            return _build_store_targeted(bulk_paths, cache)

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
        root_id = cache.session_to_session_graph.get(normalized, normalized)
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


def project_sessions_metadata(
    params: dict[str, Any],
    *,
    global_scope: bool,
    current_dir: Path,
) -> dict[str, Any]:
    """List project sessions using the same visible-turn semantics as overview."""
    from coding_trajectory.analysis.session_graph_views import (
        session_graph_has_visible_overview_content,
    )

    discovery = discover_store(
        current_dir=current_dir,
        global_scope=global_scope,
        project_name=params.get("project_name"),
        since_days=params.get("since_days"),
        modified_since=params.get("modified_since"),
        agent_vendor=params.get("agent_vendor"),
    )
    items = [
        prune_nones(
            {
                "root_session_id": str(graph.root_session_id),
                "title": _session_graph_title(graph),
                "vendors": sorted(
                    {session.vendor.value for session in graph.sessions if session.vendor}
                )
                or None,
                "session_ids": [str(session.session_id) for session in graph.sessions],
                "project": graph.project_identifier,
            }
        )
        for graph in sorted(
            discovery.store.session_graphs.values(),
            key=lambda item: (item.project_identifier or "", str(item.root_session_id)),
        )
        if session_graph_has_visible_overview_content(graph)
    ]
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
    store: Any,
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
    try:
        handler = SERVICE_HANDLERS[method]
    except KeyError as exc:
        raise KeyError(f"no service handler registered for {method}") from exc
    result = handler(params, context)
    return contract.validate_response(result)


def _cache_session_graph(context: ServiceContext, session_graph: SessionGraph) -> None:
    for session in session_graph.sessions:
        context.cache.session_to_session_graph[str(session.session_id)] = str(
            session_graph.root_session_id
        )


def _handle_project_sessions(
    params: dict[str, Any], context: ServiceContext
) -> dict[str, Any]:
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
    for graph in session_graphs:
        item = {
            **serialize_session_graph_detail(graph),
            "project": graph.project_identifier,
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
    session_graphs = resolve_collection(
        context.store,
        "session_graph",
        global_scope=context.global_scope,
        current_dir=context.current_dir,
        agent_vendor=params.get("agent_vendor"),
    )
    projects: dict[str, dict[str, Any]] = {}
    for graph in session_graphs:
        if not graph.project_identifier or graph.project_identifier.startswith(
            "unknown-"
        ):
            continue
        project = projects.setdefault(
            graph.project_identifier,
            {"vendors": set(), "path": None},
        )
        for session in graph.sessions:
            if session.vendor:
                project["vendors"].add(session.vendor.value)
            if project["path"] is None and session.cwd:
                project["path"] = session.cwd
    return {
        "items": {
            name: {"path": value["path"], "vendors": sorted(value["vendors"])}
            for name, value in sorted(projects.items())
        }
    }


def _handle_project_logfile(
    _params: dict[str, Any], context: ServiceContext
) -> dict[str, Any]:
    session_graphs = list(context.store.session_graphs.values())
    if not session_graphs:
        raise ValueError("no session_graphs found in log file")
    return {
        "items": [
            _public_output_for_session_graph(
                graph, serialize_session_graph_detail(graph)
            )
            for graph in session_graphs
        ]
    }


def _handle_session_overview(
    params: dict[str, Any], context: ServiceContext
) -> dict[str, Any]:
    from coding_trajectory.analysis.projections import build_session_graph_overview

    session_graph = _resolve_session_graph(
        context.store, _session_graph_entrypoint_id(params)
    )
    result = build_session_graph_overview(
        session_graph,
        num_turns=_optional_positive_int(params, "num_turns"),
        drop_turns=_optional_positive_int(params, "drop_turns"),
    )
    _cache_session_graph(context, session_graph)
    return _public_output_for_session_graph(session_graph, result)


def _handle_session_stats(
    params: dict[str, Any], context: ServiceContext
) -> dict[str, Any]:
    from coding_trajectory.metrics import (
        build_session_graph_context_stats,
        build_session_graph_stats_token_usage,
    )

    session_graph = _resolve_session_graph(
        context.store, _session_graph_entrypoint_id(params)
    )
    _cache_session_graph(context, session_graph)
    stats_usage = build_session_graph_stats_token_usage(session_graph)
    result = build_session_graph_context_stats(
        session_graph,
        allocated_usage_by_item=stats_usage["allocated_usage_by_item"],
        allocated_usage_by_context_source=stats_usage[
            "allocated_usage_by_context_source"
        ],
    )
    if stats_usage.get("billed_token_usage"):
        result["billed_token_usage"] = stats_usage["billed_token_usage"]
    return _public_output_for_session_graph(
        session_graph,
        result,
    )


def _handle_session_turn_usage(
    params: dict[str, Any],
    context: ServiceContext,
) -> dict[str, Any]:
    from coding_trajectory.metrics import build_session_graph_metrics

    session_graph = _resolve_session_graph(
        context.store, _session_graph_entrypoint_id(params)
    )
    _cache_session_graph(context, session_graph)
    result = build_session_graph_metrics(
        session_graph,
    )
    turn_id = str(params["turn_id"])
    turns = [
        {
            "session_id": session["session_id"],
            "vendor": session["vendor"],
            "session_status": session.get("status"),
            "turn_id": turn["turn_id"],
            "sequence": turn["sequence"],
            "status": turn.get("status"),
            "token_usage": turn["token_usage"],
        }
        for session in result["sessions"]
        for turn in session["turns"]
        if str(turn["turn_id"]) == turn_id
    ]
    return _public_output_for_session_graph(
        session_graph,
        {
            "root_session_id": result["root_session_id"],
            "token_usage": result["token_usage"],
            "turns": turns,
            "warnings": result.get("warnings") or [],
        },
    )


def _handle_session_usage(
    params: dict[str, Any], context: ServiceContext
) -> dict[str, Any]:
    from coding_trajectory.metrics import build_session_graph_usage

    session_graph = _resolve_session_graph(
        context.store, _session_graph_entrypoint_id(params)
    )
    _cache_session_graph(context, session_graph)
    return _public_output_for_session_graph(
        session_graph,
        build_session_graph_usage(
            session_graph,
            turn_id=params.get("turn_id"),
        ),
    )


def _handle_session_model_usage(
    params: dict[str, Any], context: ServiceContext
) -> dict[str, Any]:
    from coding_trajectory.metrics import build_session_graph_model_usage

    session_graph = _resolve_session_graph(
        context.store, _session_graph_entrypoint_id(params)
    )
    _cache_session_graph(context, session_graph)
    return _public_output_for_session_graph(
        session_graph,
        build_session_graph_model_usage(session_graph),
    )


def _handle_session_tool_usage(
    params: dict[str, Any],
    context: ServiceContext,
) -> dict[str, Any]:
    from coding_trajectory.metrics import build_session_graph_tool_usage

    session_graph = _resolve_session_graph(
        context.store, _session_graph_entrypoint_id(params)
    )
    _cache_session_graph(context, session_graph)
    return _public_output_for_session_graph(
        session_graph,
        build_session_graph_tool_usage(
            session_graph,
        ),
    )


def _handle_session_events(
    params: dict[str, Any], context: ServiceContext
) -> dict[str, Any]:
    from coding_trajectory.analysis.projections import build_event_scan

    event_ids = params.get("event_ids")
    if event_ids:
        matches: list[dict[str, Any]] = []
        root_session_id: str | None = None
        for eid in event_ids:
            try:
                event = resolve_resource(context.store, "event", eid)
                session_graph = context.store.get_session_graph_for_session(
                    event.session_id
                )
                if root_session_id is None:
                    root_session_id = str(session_graph.root_session_id)
                detail = _public_output_for_session_graph(
                    session_graph, serialize_event_detail(event)
                )
                matches.append(detail)
            except (ResourceNotFoundError, ValueError):
                continue
        return {
            "root_session_id": root_session_id,
            "type": params.get("type"),
            "matches": matches,
        }

    entrypoint_id = (
        params.get("session_id")
        or params.get("root_session_id")
        or params.get("turn_id")
    )
    session_graph = _resolve_session_graph(context.store, entrypoint_id)
    _cache_session_graph(context, session_graph)

    event_type = params.get("type")
    if not event_type:
        all_events: list[dict[str, Any]] = []
        for session in session_graph.sessions:
            for event in session.events:
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
    )
    limit = params.get("limit")
    if limit:
        result["matches"] = result["matches"][:limit]
    return _public_output_for_session_graph(session_graph, result)


def _handle_session_items(
    params: dict[str, Any], context: ServiceContext
) -> list[dict[str, Any]]:
    from coding_trajectory.analysis.projections import build_item_details

    item_ids = params.get("item_ids")
    if item_ids:
        result: list[dict[str, Any]] = []
        for item_id in item_ids:
            try:
                item = resolve_resource(context.store, "item", item_id)
                session_graph = context.store.get_session_graph_for_session(
                    item.session_id
                )
                result.append(
                    _public_output_for_session_graph(
                        session_graph,
                        build_item_details(item, session_graph=session_graph),
                    )
                )
            except (ResourceNotFoundError, ValueError):
                continue
        return result

    entrypoint_id = params.get("session_id") or params.get("root_session_id")
    session_graph = _resolve_session_graph(context.store, entrypoint_id)
    _cache_session_graph(context, session_graph)

    types_filter = set(params["types"]) if params.get("types") else None
    result = []
    for session in session_graph.sessions:
        for turn in session.turns:
            for item in turn.items:
                if types_filter and item.kind not in types_filter:
                    continue
                result.append(
                    _public_output_for_session_graph(
                        session_graph,
                        build_item_details(item, session_graph=session_graph),
                    )
                )
    return result


SERVICE_HANDLERS: dict[str, ServiceHandler] = {
    "project.list": _handle_project_list,
    "project.sessions": _handle_project_sessions,
    "project.logfile": _handle_project_logfile,
    "session.overview": _handle_session_overview,
    "session.stats": _handle_session_stats,
    "session.turn_usage": _handle_session_turn_usage,
    "session.usage": _handle_session_usage,
    "session.model_usage": _handle_session_model_usage,
    "session.tool_usage": _handle_session_tool_usage,
    "session.events": _handle_session_events,
    "session.items": _handle_session_items,
}
