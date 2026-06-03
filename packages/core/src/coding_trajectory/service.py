"""Service layer implementing the session-api.json contract."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from coding_trajectory.discovery import (
    DiscoverySource,
    discover_store,
    discover_store_from_file,
    discover_store_from_files,
    format_discovery_sources,
)
from coding_trajectory.ingestion.common import format_datetime, normalize_project_key, prune_nones
from coding_trajectory.ingestion.models import Event, EventType, Session, Step, StepItem, SessionGraph, Turn
from coding_trajectory.query import DocumentError, DocumentStore, ResourceNotFoundError


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
    if extensions and extensions.amp and extensions.amp.title:
        return extensions.amp.title
    if extensions and extensions.claude_code and extensions.claude_code.title:
        return extensions.claude_code.title
    if extensions and extensions.gemini and extensions.gemini.title:
        return extensions.gemini.title
    return None


def _first_user_prompt_title(session: Session) -> str | None:
    for event in session.events:
        if event.type != EventType.USER_PROMPT_SUBMITTED:
            continue
        text = event.payload.get("text")
        if not isinstance(text, str):
            continue
        title = " ".join(text.split())
        return title or None
    return None


def _session_graph_title(session_graph: SessionGraph) -> str | None:
    by_id = {session.session_id: session for session in session_graph.sessions}
    root = by_id.get(session_graph.root_session_id)
    if root is not None:
        title = _session_title(root) or _first_user_prompt_title(root)
        if title:
            return title
    for session in session_graph.sessions:
        title = _session_title(session) or _first_user_prompt_title(session)
        if title:
            return title
    return None


def serialize_session_graph_detail(session_graph: SessionGraph) -> dict[str, Any]:
    vendors = sorted({session.vendor.value for session in session_graph.sessions if session.vendor})
    return prune_nones(
        {
            "root_session_id": str(session_graph.root_session_id),
            "title": _session_graph_title(session_graph),
            "vendors": vendors or None,
            "session_ids": [str(session.session_id) for session in session_graph.sessions],
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
            "step_ids": [str(step.step_id) for step in turn.steps],
        }
    )


def serialize_step_detail(step: Step) -> dict[str, Any]:
    return prune_nones(
        {
            "step_id": str(step.step_id),
            "session_id": str(step.session_id),
            "turn_id": str(step.turn_id),
            "items": [serialize_step_item(item) for item in step.items],
            "event_ids": [str(eid) for eid in step.event_ids],
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


def serialize_step_item(item: StepItem) -> dict[str, Any]:
    return prune_nones(item.model_dump(mode="json"))


def serialize_tool_call_detail(event: Event) -> dict[str, Any] | None:
    if event.type not in {EventType.TOOL_CALL_REQUESTED, EventType.TOOL_CALL_SUCCEEDED, EventType.TOOL_CALL_FAILED}:
        return None

    payload = event.payload
    status_by_type = {
        EventType.TOOL_CALL_REQUESTED: "in_progress",
        EventType.TOOL_CALL_SUCCEEDED: "done",
        EventType.TOOL_CALL_FAILED: "failed",
    }
    return prune_nones(
        {
            "tool_call_id": payload.get("tool_call_id"),
            "tool_name": payload.get("tool_name"),
            "input": payload.get("tool_args") or payload.get("input"),
            "result": payload.get("result") or payload.get("tool_output") or payload.get("tool_text"),
            "status": status_by_type.get(event.type),
        }
    ) or None


def serialize_llm_detail(event: Event) -> dict[str, Any] | None:
    if event.type != EventType.LLM_RESPONSE:
        return None

    usage = event.payload.get("usage") if isinstance(event.payload.get("usage"), dict) else {}
    return prune_nones(
        {
            "model": event.payload.get("model") or event.payload.get("model_version"),
            "input_tokens": usage.get("input_tokens") or usage.get("prompt_tokens"),
            "output_tokens": usage.get("output_tokens") or usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "stop_reason": event.payload.get("stop_reason"),
        }
    ) or None


def serialize_text_detail(event: Event) -> dict[str, Any] | None:
    text = event.payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return {"text": text.strip()}


def resolve_resource(store: DocumentStore, resource: str, raw_id: str) -> SessionGraph | Session | Turn | Event | Step:
    resource_id = UUID(raw_id)

    if resource == "session_graph":
        return store.get_session_graph(resource_id)
    if resource == "session":
        return store.get_session(resource_id)
    if resource == "turn":
        return store.get_turn(resource_id)
    if resource == "event":
        return store.get_event(resource_id)
    if resource == "step":
        return store.get_step(resource_id)

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
                if item.project_identifier and normalize_project_key(item.project_identifier) == current_project
            ]
        if project_name is not None:
            key = normalize_project_key(project_name)
            session_graphs = [
                item
                for item in session_graphs
                if item.project_identifier and normalize_project_key(item.project_identifier) == key
            ]
        if agent_vendor is not None:
            session_graphs = [
                item
                for item in session_graphs
                if item.summary and any(v.value == agent_vendor for v in item.summary.vendors)
            ]
        return sorted(session_graphs, key=lambda item: (item.project_identifier or "", str(item.root_session_id)))

    if resource == "session":
        sessions = list(store.sessions.values())
        if root_session_id:
            tid = UUID(root_session_id)
            sessions = [
                item
                for item in sessions
                if store.session_to_root.get(item.session_id) == tid
            ]
        return sorted(sessions, key=lambda item: (item.started_at, str(item.session_id)))

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
        return [p for p, tid in self.path_to_session_graph.items() if tid == root_session_id]

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
        resource_id = UUID(raw_id)
        try:
            return store.get_session_graph(resource_id)
        except ResourceNotFoundError:
            try:
                session = store.get_session(resource_id)
                return store.get_session_graph_for_session(session.session_id)
            except ResourceNotFoundError:
                return store.get_session_graph_for_turn(resource_id)
    session_graphs = list(store.session_graphs.values())
    if len(session_graphs) == 1:
        return session_graphs[0]
    if not session_graphs:
        raise ValueError("no session_graphs found in store")
    raise ValueError("session_id is required when the store contains multiple session_graphs")


def _session_graph_entrypoint_id(params: dict[str, Any]) -> str | None:
    """Return the public session entry point."""
    return params.get("session_id") or params.get("root_session_id") or params.get("turn_id")


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


def _build_store_full(*, global_scope: bool, current_dir: Path, cache: IndexCache) -> tuple[DocumentStore, str]:
    """Full discovery — populates cache.path_to_session_graph."""
    discovery = discover_store(current_dir=current_dir, global_scope=global_scope)
    _update_path_index(cache, discovery.sources)
    _update_session_index(cache, discovery.store)

    return discovery.store, format_discovery_sources(discovery.sources)


def _build_store_targeted(paths: list[str], cache: IndexCache) -> tuple[DocumentStore, str]:
    """Targeted discovery — ingest only the files mapped to a session_graph."""
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
        target_session_graph_id = cache.session_to_session_graph.get(entrypoint_id, entrypoint_id)
        cached_paths = cache.paths_for_session_graph(target_session_graph_id)
        if cached_paths:
            return _build_store_targeted(cached_paths, cache)

    effective_global_scope = global_scope or bool(params.get("project_name"))
    return _build_store_full(global_scope=effective_global_scope, current_dir=current_dir, cache=cache)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


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
    from coding_trajectory.analysis.projections import (
        build_event_scan,
        build_step_details,
        build_session_graph_narrative,
        build_session_graph_overview,
    )
    from coding_trajectory.metrics import build_session_graph_metrics, build_session_graph_tool_usage

    if method == "project.sessions":
        session_graphs = resolve_collection(
            store,
            "session_graph",
            global_scope=global_scope,
            current_dir=current_dir,
            project_name=params.get("project_name"),
            agent_vendor=params.get("agent_vendor"),
        )
        return {"items": [serialize_session_graph_detail(t) for t in session_graphs]}

    if method == "project.list":
        session_graphs = resolve_collection(
            store,
            "session_graph",
            global_scope=global_scope,
            current_dir=current_dir,
            agent_vendor=params.get("agent_vendor"),
        )
        projects: dict[str, dict] = {}
        for t in session_graphs:
            if not t.project_identifier:
                continue
            key = t.project_identifier
            if key.startswith("unknown-"):
                continue
            if key not in projects:
                projects[key] = {"vendors": set(), "path": None}
            for s in t.sessions:
                if s.vendor:
                    projects[key]["vendors"].add(s.vendor.value)
                if projects[key]["path"] is None and s.cwd:
                    projects[key]["path"] = s.cwd
        return {
            "items": {
                k: {"path": v["path"], "vendors": sorted(v["vendors"])}
                for k, v in sorted(projects.items())
            },
        }

    if method == "project.logfile":
        session_graphs = list(store.session_graphs.values())
        if not session_graphs:
            raise ValueError("no session_graphs found in log file")
        return {"items": [serialize_session_graph_detail(t) for t in session_graphs]}

    if method == "session.overview":
        session_graph = _resolve_session_graph(store, _session_graph_entrypoint_id(params))
        view = params.get("view", "overview")
        if view == "overview":
            result = build_session_graph_overview(
                session_graph,
                num_turns=_optional_positive_int(params, "num_turns"),
                drop_turns=_optional_positive_int(params, "drop_turns"),
            )
        elif view == "narrative":
            result = build_session_graph_narrative(
                session_graph,
                num_turns=_optional_positive_int(params, "num_turns"),
                drop_turns=_optional_positive_int(params, "drop_turns"),
            )
        else:
            raise ValueError(f"unsupported session overview view: {view}")
        for session in session_graph.sessions:
            cache.session_to_session_graph[str(session.session_id)] = str(session_graph.root_session_id)
        return result

    if method == "session.stats":
        session_graph = _resolve_session_graph(store, _session_graph_entrypoint_id(params))
        extra_billing = bool(params.get("extra_billing"))
        for session in session_graph.sessions:
            cache.session_to_session_graph[str(session.session_id)] = str(session_graph.root_session_id)
        return build_session_graph_metrics(
            session_graph,
            extra_billing=extra_billing,
        )

    if method == "session.turn_usage":
        if not params.get("turn_id"):
            raise ValueError("missing required param: turn_id")
        session_graph = _resolve_session_graph(store, _session_graph_entrypoint_id(params))
        extra_billing = bool(params.get("extra_billing"))
        for session in session_graph.sessions:
            cache.session_to_session_graph[str(session.session_id)] = str(session_graph.root_session_id)
        result = build_session_graph_metrics(
            session_graph,
            extra_billing=extra_billing,
            include_steps=True,
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
                "cost": turn["cost"],
                "extra_billing": turn["extra_billing"],
                "steps": turn.get("steps", []),
            }
            for session in result["sessions"]
            for turn in session["turns"]
            if str(turn["turn_id"]) == turn_id
        ]
        return {
            "root_session_id": result["root_session_id"],
            "token_usage": result["token_usage"],
            "cost": result["cost"],
            "extra_billing": result["extra_billing"],
            "turns": turns,
            "warnings": result.get("warnings") or [],
        }

    if method == "session.tool_usage":
        session_graph = _resolve_session_graph(store, _session_graph_entrypoint_id(params))
        extra_billing = bool(params.get("extra_billing"))
        for session in session_graph.sessions:
            cache.session_to_session_graph[str(session.session_id)] = str(session_graph.root_session_id)
        return build_session_graph_tool_usage(
            session_graph,
            extra_billing=extra_billing,
        )

    if method == "step.details":
        step_ids = params.get("step_ids")
        if not step_ids:
            raise ValueError("missing required param: step_ids")
        if isinstance(step_ids, str):
            step_ids = [step_ids]
        result: list[dict[str, Any]] = []
        for step_id in step_ids:
            step = resolve_resource(store, "step", step_id)
            session_graph = store.get_session_graph_for_session(step.session_id)
            result.append(build_step_details(step, session_graph=session_graph))
        return result

    if method == "event.detail":
        event_id = params.get("event_id")
        if not event_id:
            raise ValueError("missing required param: event_id")
        event = resolve_resource(store, "event", event_id)
        return serialize_event_detail(event)

    if method == "event.scan":
        session_graph = _resolve_session_graph(store, _session_graph_entrypoint_id(params))
        event_type = params.get("type")
        if not event_type:
            raise ValueError("missing required param: type")
        filters: list[str] = params.get("filters") or []
        return build_event_scan(session_graph, event_type=event_type, filters=filters)

    raise KeyError(method)
