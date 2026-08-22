"""Request-driven ``ct.living_events.v1`` projection and durable change store.

The authoritative coding-agent JSONL remains outside this database.  Each API
request rebuilds the affected canonical graph through the ordinary CT discovery
boundary, then atomically publishes changed hierarchy resources into a small,
versioned SQLite read model.  No watcher, monitor thread, or resident service is
owned by this module.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from coding_trajectory.analysis.activity_flow import build_overview_flows
from coding_trajectory.analysis.item_details import build_item_details
from coding_trajectory.analysis.request_lineage import effective_user_request
from coding_trajectory.discovery import (
    discover_source_candidates,
    discover_store_from_files,
)
from coding_trajectory.ingestion.common import (
    canonical_json,
    format_datetime,
    last_complete_line_offset,
    stable_uuid,
)
from coding_trajectory.ingestion.indexes import build_session_graph_index
from coding_trajectory.ingestion.models import (
    AgentMessageItem,
    PlanItem,
    RuntimeObservation,
    Session,
    SessionEdge,
    SessionGraph,
    Turn,
)
from coding_trajectory.living_sources import (
    LivingSourceSnapshot,
    inventory_source_changes,
)
from coding_trajectory.query import DocumentStore

SCHEMA_VERSION = "ct.living_events.v1"
_STORE_FORMAT_VERSION = "living-events-store-v1"
_VIEW_STRING_LIMIT = 500
_VIEW_VALUE_LIMIT = 2000
_RETAINED_REVISIONS = 512
_COMPACTION_MECHANISMS = {
    "claude_compact_boundary": "eviction_boundary",
    "context_compacted": "context_compacted",
}


@dataclass(frozen=True, slots=True)
class ProjectedResource:
    kind: str
    key: str
    root_session_id: str
    path: dict[str, str]
    sort_key: str
    view: dict[str, Any]
    details: dict[str, Any]


def default_database_path(*, current_dir: Path, global_scope: bool) -> Path:
    """Return the per-scope derived SQLite path, with an explicit override."""

    override = os.environ.get("CT_LIVING_EVENTS_DB")
    if override:
        return Path(override).expanduser().resolve()
    scope = (
        "global"
        if global_scope
        else hashlib.sha256(str(current_dir.resolve()).encode("utf-8")).hexdigest()[:16]
    )
    return Path.home() / ".coding-trajectory" / "living-events" / f"{scope}.sqlite3"


def serve_living_events(
    params: dict[str, Any],
    *,
    cache: Any,
    current_dir: Path,
    global_scope: bool,
) -> dict[str, Any]:
    """Reconcile cheap source checkpoints, then serve one cursor page."""

    scope = dict(params.get("scope") or {})
    database = LivingEventsStore(
        default_database_path(current_dir=current_dir, global_scope=global_scope)
    )
    if params.get("through") is not None:
        return _page_living_events(database, params, scope=scope)
    routing_scope = database.routing_scope(scope)

    candidates = discover_source_candidates(
        current_dir=current_dir,
        global_scope=global_scope,
    )
    previous = database.source_snapshots()
    inventory = inventory_source_changes(candidates, previous)
    states = dict(previous)
    candidate_by_path = {
        str(value.path.expanduser().resolve()): value for value in candidates
    }
    for change in inventory.changes:
        states[change.current.path] = change.current

    rebuild_paths = {
        change.current.path for change in inventory.changes if change.needs_rebuild
    }
    rebuild_paths.update(
        path
        for path, state in states.items()
        if state.status in {"ready", "partial"} and state.materialized_revision is None
    )
    issues: list[dict[str, Any]] = [
        {
            "severity": "error",
            "code": "living.source_scan_failed",
            "message": f"{change.current.path}: {change.current.error}",
        }
        for change in inventory.changes
        if change.current.status == "error"
    ]

    groups = _affected_source_groups(
        states=states,
        previous=previous,
        rebuild_paths=rebuild_paths,
    )
    scoped_groups = _groups_for_scope(
        groups,
        states=states,
        scope=routing_scope,
        cache=cache,
    )
    if _scope_has_source_hint(routing_scope, cache=cache):
        groups = scoped_groups

    for paths in groups:
        old_roots = {
            value.root_session_id
            for path in paths
            if (value := previous.get(path)) is not None
            and value.root_session_id is not None
        }
        live_paths = sorted(
            path
            for path in paths
            if (state := states.get(path)) is not None
            and state.status in {"ready", "partial"}
            and path in candidate_by_path
        )
        try:
            resources: list[ProjectedResource] = []
            roots = set(old_roots)
            root_by_path: dict[str, str] = {}
            if live_paths:
                discovery = discover_store_from_files(
                    [Path(path) for path in live_paths]
                )
                cache.index_discovery(sources=discovery.sources, store=discovery.store)
                for graph in discovery.store.session_graphs.values():
                    root_id = str(graph.root_session_id)
                    roots.add(root_id)
                    graph_paths = tuple(
                        str(source.path)
                        for source in discovery.sources
                        if source.root_session_id == graph.root_session_id
                    )
                    resources.extend(_project_graph(graph, source_paths=graph_paths))
                root_by_path = {
                    str(source.path.expanduser().resolve()): str(source.root_session_id)
                    for source in discovery.sources
                    if source.root_session_id is not None
                }
            revision = database.publish(
                resources,
                affected_roots=sorted(roots),
            )
            for path in paths:
                state = states.get(path)
                if state is None:
                    continue
                states[path] = state.model_copy(
                    update={
                        "root_session_id": root_by_path.get(
                            path, state.root_session_id
                        ),
                        "materialized_revision": revision,
                    }
                )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            for path in live_paths:
                state = states.get(path)
                if state is not None:
                    states[path] = state.model_copy(
                        update={"status": "error", "error": error}
                    )
            issues.append(
                {
                    "severity": "error",
                    "code": "living.source_rebuild_failed",
                    "message": (
                        f"failed to rebuild {len(live_paths)} source(s): {error}"
                    ),
                }
            )

    changed_paths = {value.current.path for value in inventory.changes}
    changed_paths.update(rebuild_paths)
    database.save_source_snapshots(
        [states[path] for path in sorted(changed_paths) if path in states]
    )
    result = _page_living_events(database, params, scope=scope)
    result["issues"].extend(issues)
    return result


def _affected_source_groups(
    *,
    states: dict[str, LivingSourceSnapshot],
    previous: dict[str, LivingSourceSnapshot],
    rebuild_paths: set[str],
) -> list[set[str]]:
    active = {
        path: value
        for path, value in states.items()
        if value.status in {"ready", "partial"} and value.session_id is not None
    }
    path_by_session = {
        value.session_id: path for path, value in active.items() if value.session_id
    }
    neighbours: dict[str, set[str]] = {path: set() for path in active}
    for path, value in active.items():
        parent_path = path_by_session.get(value.parent_session_id or "")
        if parent_path is None:
            continue
        neighbours[path].add(parent_path)
        neighbours[parent_path].add(path)

    component_by_path: dict[str, set[str]] = {}
    visited: set[str] = set()
    for path in sorted(active):
        if path in visited:
            continue
        component: set[str] = set()
        stack = [path]
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            component.add(current)
            stack.extend(neighbours.get(current, ()))
        for member in component:
            component_by_path[member] = component

    raw_groups: list[set[str]] = []
    for seed in sorted(rebuild_paths):
        group = {seed}
        group.update(component_by_path.get(seed, ()))
        old = previous.get(seed)
        if old is not None and old.root_session_id is not None:
            group.update(
                path
                for path, state in active.items()
                if state.root_session_id == old.root_session_id
            )
        raw_groups.append(group)

    merged: list[set[str]] = []
    for group in raw_groups:
        overlaps = [value for value in merged if value & group]
        if not overlaps:
            merged.append(set(group))
            continue
        combined = set(group)
        for value in overlaps:
            combined.update(value)
            merged.remove(value)
        merged.append(combined)
    return sorted(
        merged,
        key=lambda group: (
            sum(states[path].size for path in group if path in states),
            min(group),
        ),
    )


def _groups_for_scope(
    groups: list[set[str]],
    *,
    states: dict[str, LivingSourceSnapshot],
    scope: dict[str, Any],
    cache: Any,
) -> list[set[str]]:
    raw_ids = {
        str(value)
        for key in ("root_session_id", "session_id")
        if (value := scope.get(key))
    }
    for key in ("turn_id", "item_id"):
        value = scope.get(key)
        if not value:
            continue
        raw_ids.add(str(value))
        mapped = cache.root_for_entrypoint(str(value))
        if mapped:
            raw_ids.add(str(mapped))
    if not raw_ids:
        return []
    return [
        group
        for group in groups
        if any(
            (state := states.get(path)) is not None
            and (state.session_id in raw_ids or state.root_session_id in raw_ids)
            for path in group
        )
    ]


def _scope_has_source_hint(scope: dict[str, Any], *, cache: Any) -> bool:
    if scope.get("root_session_id") or scope.get("session_id"):
        return True
    for key in ("turn_id", "item_id"):
        value = scope.get(key)
        if value and cache.root_for_entrypoint(str(value)) != str(value):
            return True
    return False


def _page_living_events(
    database: LivingEventsStore,
    params: dict[str, Any],
    *,
    scope: dict[str, Any],
) -> dict[str, Any]:
    return database.page(
        mode=str(params.get("mode") or "view"),
        scope=scope,
        after=params.get("after"),
        through=params.get("through"),
        limit=int(params.get("limit") or 50),
    )


def query_living_events(
    params: dict[str, Any],
    *,
    document_store: DocumentStore,
    cache: Any,
    current_dir: Path,
    global_scope: bool,
) -> dict[str, Any]:
    """Publish the current canonical resources and serve one stable cursor page."""

    scope = dict(params.get("scope") or {})
    database = LivingEventsStore(
        default_database_path(current_dir=current_dir, global_scope=global_scope)
    )

    # A continuation pins an already-published revision.  Do not publish a new
    # revision while traversing it, even though ordinary discovery already gave
    # this short-lived request a fresh in-memory graph.
    if params.get("through") is None:
        graphs = _selected_graphs(document_store, scope)
        resources: list[ProjectedResource] = []
        roots: list[str] = []
        for graph in graphs:
            root_id = str(graph.root_session_id)
            roots.append(root_id)
            source_paths = tuple(cache.paths_for_session_graph(root_id))
            resources.extend(_project_graph(graph, source_paths=source_paths))
        database.publish(resources, affected_roots=roots)

    return database.page(
        mode=str(params.get("mode") or "view"),
        scope=scope,
        after=params.get("after"),
        through=params.get("through"),
        limit=int(params.get("limit") or 50),
    )


def _selected_graphs(store: DocumentStore, scope: dict[str, Any]) -> list[SessionGraph]:
    raw_item_id = scope.get("item_id")
    raw_turn_id = scope.get("turn_id")
    raw_session_id = scope.get("session_id")
    raw_root_id = scope.get("root_session_id")

    graph: SessionGraph | None = None
    if raw_item_id:
        item = store.get_item(_parse_uuid(raw_item_id, "item_id"))
        graph = store.get_session_graph_for_session(item.session_id)
        if raw_turn_id and str(item.turn_id) != str(raw_turn_id):
            raise ValueError("item_id does not belong to turn_id")
        if raw_session_id and str(item.session_id) != str(raw_session_id):
            raise ValueError("item_id does not belong to session_id")
    elif raw_turn_id:
        turn = store.get_turn(_parse_uuid(raw_turn_id, "turn_id"))
        graph = store.get_session_graph_for_session(turn.session_id)
        if raw_session_id and str(turn.session_id) != str(raw_session_id):
            raise ValueError("turn_id does not belong to session_id")
    elif raw_session_id:
        graph = store.get_session_graph_for_session(
            _parse_uuid(raw_session_id, "session_id")
        )
    elif raw_root_id:
        graph = store.get_session_graph(_parse_uuid(raw_root_id, "root_session_id"))

    if graph is not None:
        if raw_root_id and str(graph.root_session_id) != str(raw_root_id):
            raise ValueError("scope resource does not belong to root_session_id")
        return [graph]
    return sorted(
        store.session_graphs.values(),
        key=lambda value: str(value.root_session_id),
    )


def _parse_uuid(value: Any, field: str) -> UUID:
    try:
        return UUID(str(value))
    except ValueError as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def _project_graph(
    graph: SessionGraph, *, source_paths: tuple[str, ...]
) -> list[ProjectedResource]:
    root_id = str(graph.root_session_id)
    index = build_session_graph_index(graph)
    resources: list[ProjectedResource] = []
    source_by_session = _source_paths_by_session(graph, source_paths)

    for session in sorted(
        graph.sessions, key=lambda value: (value.started_at, str(value.session_id))
    ):
        session_id = str(session.session_id)
        checkpoints = _context_checkpoints(session)
        session_payload = {
            "session_id": session_id,
            "root_session_id": root_id,
            "vendor": session.vendor.value,
            "status": session.status.value,
            "agent_name": session.agent_name,
            "cwd": session.cwd,
            "started_at": format_datetime(session.started_at),
            "ended_at": format_datetime(session.ended_at),
            "turn_count": len(session.turns),
            "item_count": sum(len(turn.items) for turn in session.turns),
            "context_checkpoint_count": len(checkpoints),
            "source_checkpoint": _source_checkpoint(source_by_session.get(session_id)),
        }
        session_payload = _without_nones(session_payload)
        resources.append(
            ProjectedResource(
                kind="session",
                key=session_id,
                root_session_id=root_id,
                path={"root_session_id": root_id, "session_id": session_id},
                sort_key=f"0:{session.started_at.isoformat()}:{session_id}",
                view=session_payload,
                details=session_payload,
            )
        )

        for checkpoint in checkpoints:
            checkpoint_id = str(checkpoint["context_checkpoint_id"])
            resources.append(
                ProjectedResource(
                    kind="context_checkpoint",
                    key=checkpoint_id,
                    root_session_id=root_id,
                    path={
                        "root_session_id": root_id,
                        "session_id": session_id,
                        "context_checkpoint_id": checkpoint_id,
                    },
                    sort_key=(
                        f"1:{session.started_at.isoformat()}:"
                        f"{checkpoint['timestamp']}:{checkpoint_id}"
                    ),
                    view=checkpoint,
                    details=checkpoint,
                )
            )

        for turn in sorted(session.turns, key=lambda value: value.sequence):
            turn_id = str(turn.turn_id)
            preceding = _preceding_checkpoint(turn, checkpoints)
            request = effective_user_request(index, turn, session=session)
            turn_payload = {
                "turn_id": turn_id,
                "session_id": session_id,
                "sequence": turn.sequence,
                "status": turn.status.value,
                "started_at": format_datetime(turn.started_at),
                "ended_at": format_datetime(turn.ended_at),
                "preceding_context_checkpoint_id": preceding,
                "user_request": _request_content(request),
                "assistant_responses": [
                    _inline_content(item.text)
                    for item in turn.items
                    if isinstance(item, AgentMessageItem) and item.text
                ],
                "activity": [
                    activity
                    for activity in build_overview_flows(turn.items)
                    if "text" not in activity
                ],
                "item_count": len(turn.items),
            }
            turn_payload = _without_nones(turn_payload)
            resources.append(
                ProjectedResource(
                    kind="turn",
                    key=turn_id,
                    root_session_id=root_id,
                    path={
                        "root_session_id": root_id,
                        "session_id": session_id,
                        "turn_id": turn_id,
                    },
                    sort_key=(
                        f"2:{session.started_at.isoformat()}:"
                        f"{turn.sequence:012d}:{turn_id}"
                    ),
                    view=turn_payload,
                    details=turn_payload,
                )
            )

            for item in sorted(turn.items, key=lambda value: value.sequence):
                item_id = str(item.item_id)
                detail = build_item_details(
                    item,
                    session_graph=graph,
                    include_content=True,
                    index=index if isinstance(item, PlanItem) else None,
                )
                view = dict(detail)
                if view.get("type") != "assistant_response" and isinstance(
                    view.get("shape"), dict
                ):
                    view["shape"] = _view_shape(
                        view["shape"],
                        session_id=session_id,
                        turn_id=turn_id,
                        item_id=item_id,
                        event_ids=[str(value) for value in item.event_ids],
                    )
                path = {
                    "root_session_id": root_id,
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "item_id": item_id,
                }
                resources.append(
                    ProjectedResource(
                        kind="item",
                        key=item_id,
                        root_session_id=root_id,
                        path=path,
                        sort_key=(
                            f"3:{session.started_at.isoformat()}:"
                            f"{turn.sequence:012d}:{item.sequence:012d}:{item_id}"
                        ),
                        view=view,
                        details=detail,
                    )
                )

    for edge in graph.edges:
        edge_payload = _edge_resource(graph, edge)
        edge_id = str(edge_payload["edge_id"])
        resources.append(
            ProjectedResource(
                kind="session_edge",
                key=edge_id,
                root_session_id=root_id,
                path={
                    "root_session_id": root_id,
                    "edge_id": edge_id,
                    "source_session_id": str(edge.source_session_id),
                    "target_session_id": str(edge.target_session_id),
                },
                sort_key=f"4:{edge.type}:{edge_id}",
                view=edge_payload,
                details=edge_payload,
            )
        )

    return resources


def _source_paths_by_session(
    graph: SessionGraph, source_paths: tuple[str, ...]
) -> dict[str, Path]:
    paths = [Path(value).expanduser().resolve() for value in source_paths]
    result: dict[str, Path] = {}
    for session in graph.sessions:
        session_id = str(session.session_id).lower()
        exact = next(
            (path for path in paths if session_id in str(path).lower()),
            None,
        )
        if exact is not None:
            result[str(session.session_id)] = exact
    root_key = str(graph.root_session_id)
    if root_key not in result and paths:
        result[root_key] = min(
            paths,
            key=lambda path: (
                "subagents" in path.parts,
                len(path.parts),
                str(path),
            ),
        )
    return result


def _source_checkpoint(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        stat = path.stat()
        with path.open("rb") as handle:
            committed = last_complete_line_offset(handle, stat.st_size)
        trailing = max(stat.st_size - committed, 0)
        return {
            "path": str(path),
            "file_identity": f"{stat.st_dev}:{stat.st_ino}",
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "committed_offset": committed,
            "trailing_bytes": trailing,
            "status": "partial" if trailing else "ready",
        }
    except OSError as exc:
        return {
            "path": str(path),
            "size": 0,
            "mtime_ns": 0,
            "committed_offset": 0,
            "trailing_bytes": 0,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }


def _context_checkpoints(session: Session) -> list[dict[str, Any]]:
    observations = sorted(
        (
            value
            for value in session.runtime_observations
            if value.kind in _COMPACTION_MECHANISMS
        ),
        key=lambda value: (value.timestamp, value.kind, value.trace_id or ""),
    )
    turns = sorted(session.turns, key=lambda value: (value.started_at, value.sequence))
    result: list[dict[str, Any]] = []
    for sequence, observation in enumerate(observations, start=1):
        checkpoint_id = str(
            stable_uuid(
                session.vendor,
                session.session_id,
                resource="context_checkpoint",
                sequence=sequence,
                timestamp=observation.timestamp,
                kind=observation.kind,
                trace_id=observation.trace_id,
            )
        )
        after_turn, before_turn = _checkpoint_turn_bounds(observation, turns)
        dropped = None
        if observation.pre_tokens is not None and observation.post_tokens is not None:
            dropped = max(observation.pre_tokens - observation.post_tokens, 0)
        result.append(
            _without_nones(
                {
                    "context_checkpoint_id": checkpoint_id,
                    "session_id": str(session.session_id),
                    "sequence": sequence,
                    "timestamp": format_datetime(observation.timestamp),
                    "mechanism": _COMPACTION_MECHANISMS[observation.kind],
                    "trigger": observation.trigger,
                    "pre_tokens": observation.pre_tokens,
                    "post_tokens": observation.post_tokens,
                    "dropped_tokens": dropped,
                    "effective_after_turn_id": after_turn,
                    "effective_before_turn_id": before_turn,
                    "source_event_ids": [],
                }
            )
        )
    return result


def _checkpoint_turn_bounds(
    observation: RuntimeObservation, turns: list[Turn]
) -> tuple[str | None, str | None]:
    before = next(
        (turn for turn in turns if turn.started_at > observation.timestamp), None
    )
    eligible_after = [
        turn
        for turn in turns
        if (turn.ended_at or turn.started_at) <= observation.timestamp
    ]
    after = eligible_after[-1] if eligible_after else None
    return (
        str(after.turn_id) if after is not None else None,
        str(before.turn_id) if before is not None else None,
    )


def _preceding_checkpoint(turn: Turn, checkpoints: list[dict[str, Any]]) -> str | None:
    preceding = [
        value
        for value in checkpoints
        if _parse_datetime(str(value["timestamp"])) <= turn.started_at
    ]
    if not preceding:
        return None
    return str(preceding[-1]["context_checkpoint_id"])


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _request_content(request: dict[str, Any] | None) -> dict[str, Any] | None:
    if not request:
        return None
    result = {key: value for key, value in request.items() if key != "content"}
    content = request.get("content")
    if isinstance(content, str):
        result["content"] = _inline_content(content)
    return result


def _inline_content(value: Any) -> dict[str, Any]:
    text = value if isinstance(value, str) else canonical_json(value)
    return {
        "state": "inline",
        "value": value,
        "size_chars": len(text),
        "ref": None,
    }


def _view_shape(
    shape: dict[str, Any],
    *,
    session_id: str,
    turn_id: str,
    item_id: str,
    event_ids: list[str],
) -> dict[str, Any]:
    return {
        key: _view_value(
            value,
            session_id=session_id,
            turn_id=turn_id,
            item_id=item_id,
            event_ids=event_ids,
            field_path=f"shape.{key}",
        )
        for key, value in shape.items()
    }


def _view_value(
    value: Any,
    *,
    session_id: str,
    turn_id: str,
    item_id: str,
    event_ids: list[str],
    field_path: str,
) -> Any:
    serialized = value if isinstance(value, str) else canonical_json(value)
    limit = _VIEW_STRING_LIMIT if isinstance(value, str) else _VIEW_VALUE_LIMIT
    if len(serialized) > limit:
        return {
            "$type": "content_ref",
            "size_chars": len(serialized),
            "ref": {
                "session_id": session_id,
                "turn_id": turn_id,
                "item_id": item_id,
                "event_ids": event_ids,
                "field_path": field_path,
            },
        }
    if isinstance(value, dict):
        return {
            key: _view_value(
                child,
                session_id=session_id,
                turn_id=turn_id,
                item_id=item_id,
                event_ids=event_ids,
                field_path=f"{field_path}.{key}",
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _view_value(
                child,
                session_id=session_id,
                turn_id=turn_id,
                item_id=item_id,
                event_ids=event_ids,
                field_path=f"{field_path}[{index}]",
            )
            for index, child in enumerate(value)
        ]
    return value


def _edge_resource(graph: SessionGraph, edge: SessionEdge) -> dict[str, Any]:
    edge_id = str(
        stable_uuid(
            "ct",
            graph.root_session_id,
            resource="session_edge",
            type=edge.type,
            source_session_id=edge.source_session_id,
            target_session_id=edge.target_session_id,
            source_turn_id=edge.source_turn_id,
            source_item_id=edge.source_item_id,
            source_event_id=edge.source_event_id,
        )
    )
    return _without_nones(
        {
            "edge_id": edge_id,
            "type": edge.type,
            "source_session_id": str(edge.source_session_id),
            "target_session_id": str(edge.target_session_id),
            "source_turn_id": str(edge.source_turn_id) if edge.source_turn_id else None,
            "source_item_id": str(edge.source_item_id) if edge.source_item_id else None,
            "source_event_id": str(edge.source_event_id)
            if edge.source_event_id
            else None,
            "provenance": edge.provenance,
            "confidence": edge.confidence,
            "evidence_event_ids": [str(value) for value in edge.evidence_event_ids],
            "metadata": edge.metadata,
        }
    )


def _without_nones(value: dict[str, Any]) -> dict[str, Any]:
    return {key: child for key, child in value.items() if child is not None}


class LivingEventsStore:
    """Versioned SQLite resource store with signed snapshot/delta cursors."""

    def __init__(
        self,
        path: Path,
        *,
        schema_version: str = SCHEMA_VERSION,
        store_format_version: str = _STORE_FORMAT_VERSION,
    ) -> None:
        self.path = path.expanduser().resolve()
        self.schema_version = schema_version
        self.store_format_version = store_format_version
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS living_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS living_revisions (
                    revision INTEGER PRIMARY KEY,
                    committed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS living_resource_versions (
                    resource_kind TEXT NOT NULL,
                    resource_key TEXT NOT NULL,
                    valid_from_revision INTEGER NOT NULL,
                    valid_to_revision INTEGER,
                    root_session_id TEXT NOT NULL,
                    session_id TEXT,
                    turn_id TEXT,
                    item_id TEXT,
                    source_session_id TEXT,
                    target_session_id TEXT,
                    path_json TEXT NOT NULL,
                    sort_key TEXT NOT NULL,
                    view_payload_json TEXT NOT NULL,
                    detail_payload_z BLOB NOT NULL,
                    payload_digest TEXT NOT NULL,
                    PRIMARY KEY(resource_kind, resource_key, valid_from_revision)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_living_resource_current
                    ON living_resource_versions(resource_kind, resource_key)
                    WHERE valid_to_revision IS NULL;
                CREATE INDEX IF NOT EXISTS idx_living_resource_snapshot
                    ON living_resource_versions(
                        root_session_id, sort_key, valid_from_revision,
                        valid_to_revision
                    );
                CREATE TABLE IF NOT EXISTS living_changes (
                    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    revision INTEGER NOT NULL,
                    operation TEXT NOT NULL,
                    resource_kind TEXT NOT NULL,
                    resource_key TEXT NOT NULL,
                    root_session_id TEXT NOT NULL,
                    session_id TEXT,
                    turn_id TEXT,
                    item_id TEXT,
                    source_session_id TEXT,
                    target_session_id TEXT,
                    path_json TEXT NOT NULL,
                    reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_living_changes_revision
                    ON living_changes(revision, change_id);
                CREATE TABLE IF NOT EXISTS living_sources (
                    path TEXT PRIMARY KEY,
                    vendor TEXT NOT NULL,
                    file_identity TEXT,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    ctime_ns INTEGER NOT NULL,
                    committed_offset INTEGER NOT NULL,
                    prefix_checksum TEXT,
                    tail_checksum TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    session_id TEXT,
                    parent_session_id TEXT,
                    root_session_id TEXT,
                    materialized_revision INTEGER,
                    cwd TEXT,
                    title TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_living_sources_root
                    ON living_sources(root_session_id, session_id);
                """
            )
            for table in ("living_resource_versions", "living_changes"):
                for column in (
                    "session_id",
                    "turn_id",
                    "item_id",
                    "source_session_id",
                    "target_session_id",
                ):
                    self._ensure_column(connection, table, column, "TEXT")
                self._backfill_path_columns(connection, table)
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_living_resource_scope
                    ON living_resource_versions(
                        root_session_id, session_id, turn_id, item_id,
                        valid_from_revision, valid_to_revision, sort_key
                    );
                CREATE INDEX IF NOT EXISTS idx_living_resource_order
                    ON living_resource_versions(
                        sort_key, resource_kind, resource_key,
                        valid_from_revision, valid_to_revision
                    );
                CREATE INDEX IF NOT EXISTS idx_living_change_scope
                    ON living_changes(
                        root_session_id, session_id, turn_id, item_id,
                        revision, change_id
                    );
                """
            )
            self._set_default(connection, "store_format_version", self.store_format_version)
            self._set_default(connection, "revision", "0")
            self._set_default(connection, "pruned_through", "0")
            self._set_default(connection, "cursor_secret", secrets.token_hex(32))
            actual = self._metadata(connection, "store_format_version")
            if actual != self.store_format_version:
                raise ValueError(
                    "living-events store format is incompatible: "
                    f"expected {self.store_format_version}, found {actual}"
                )

    def publish(
        self,
        resources: list[ProjectedResource],
        *,
        affected_roots: list[str],
    ) -> int:
        roots = sorted(set(affected_roots))
        if not roots:
            return self.current_revision()
        incoming = {(value.kind, value.key): value for value in resources}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_revision = self._current_revision(connection)
            placeholders = ",".join("?" for _ in roots)
            current_rows = connection.execute(
                f"""
                SELECT * FROM living_resource_versions
                 WHERE valid_to_revision IS NULL
                   AND root_session_id IN ({placeholders})
                """,
                roots,
            ).fetchall()
            current = {
                (row["resource_kind"], row["resource_key"]): row for row in current_rows
            }
            changed: list[ProjectedResource] = []
            for identity, resource in incoming.items():
                row = current.get(identity)
                if row is None or row["payload_digest"] != _resource_digest(resource):
                    changed.append(resource)
            removed = [
                row for identity, row in current.items() if identity not in incoming
            ]
            if not changed and not removed:
                connection.rollback()
                return current_revision

            revision = current_revision + 1
            connection.execute(
                "INSERT INTO living_revisions(revision, committed_at) VALUES(?, ?)",
                (revision, datetime.now().astimezone().isoformat()),
            )
            for resource in changed:
                connection.execute(
                    """
                    UPDATE living_resource_versions
                       SET valid_to_revision = ?
                     WHERE resource_kind = ? AND resource_key = ?
                       AND valid_to_revision IS NULL
                    """,
                    (revision, resource.kind, resource.key),
                )
                connection.execute(
                    """
                    INSERT INTO living_resource_versions(
                        resource_kind, resource_key, valid_from_revision,
                        valid_to_revision, root_session_id, session_id, turn_id,
                        item_id, source_session_id, target_session_id,
                        path_json, sort_key,
                        view_payload_json, detail_payload_z, payload_digest
                    ) VALUES(?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resource.kind,
                        resource.key,
                        revision,
                        resource.root_session_id,
                        resource.path.get("session_id"),
                        resource.path.get("turn_id"),
                        resource.path.get("item_id"),
                        resource.path.get("source_session_id"),
                        resource.path.get("target_session_id"),
                        canonical_json(resource.path),
                        resource.sort_key,
                        canonical_json(resource.view),
                        sqlite3.Binary(
                            zlib.compress(
                                canonical_json(resource.details).encode("utf-8")
                            )
                        ),
                        _resource_digest(resource),
                    ),
                )
                self._insert_change(
                    connection,
                    revision=revision,
                    operation="upsert",
                    resource_kind=resource.kind,
                    resource_key=resource.key,
                    root_session_id=resource.root_session_id,
                    path=resource.path,
                )
            for row in removed:
                connection.execute(
                    """
                    UPDATE living_resource_versions
                       SET valid_to_revision = ?
                     WHERE resource_kind = ? AND resource_key = ?
                       AND valid_to_revision IS NULL
                    """,
                    (revision, row["resource_kind"], row["resource_key"]),
                )
                self._insert_change(
                    connection,
                    revision=revision,
                    operation="remove",
                    resource_kind=row["resource_kind"],
                    resource_key=row["resource_key"],
                    root_session_id=row["root_session_id"],
                    path=json.loads(row["path_json"]),
                )
            self._set_metadata(connection, "revision", str(revision))
            self._prune(connection, revision)
            connection.commit()
        return revision

    def current_revision(self) -> int:
        with self._connect() as connection:
            return self._current_revision(connection)

    def routing_scope(self, scope: dict[str, Any]) -> dict[str, Any]:
        """Resolve persisted ancestors without changing the cursor-bound scope."""

        with self._connect() as connection:
            return self._expand_scope(
                connection,
                scope,
                self._current_revision(connection),
            )

    def source_snapshots(self) -> dict[str, LivingSourceSnapshot]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM living_sources ORDER BY path"
            ).fetchall()
        return {
            str(row["path"]): LivingSourceSnapshot.model_validate(dict(row))
            for row in rows
        }

    def save_source_snapshots(self, snapshots: list[LivingSourceSnapshot]) -> None:
        if not snapshots:
            return
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO living_sources(
                    path, vendor, file_identity, size, mtime_ns, ctime_ns,
                    committed_offset, prefix_checksum, tail_checksum, status,
                    error, session_id, parent_session_id, root_session_id,
                    materialized_revision, cwd, title
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    vendor = excluded.vendor,
                    file_identity = excluded.file_identity,
                    size = excluded.size,
                    mtime_ns = excluded.mtime_ns,
                    ctime_ns = excluded.ctime_ns,
                    committed_offset = excluded.committed_offset,
                    prefix_checksum = excluded.prefix_checksum,
                    tail_checksum = excluded.tail_checksum,
                    status = excluded.status,
                    error = excluded.error,
                    session_id = excluded.session_id,
                    parent_session_id = excluded.parent_session_id,
                    root_session_id = excluded.root_session_id,
                    materialized_revision = excluded.materialized_revision,
                    cwd = excluded.cwd,
                    title = excluded.title
                """,
                [
                    (
                        value.path,
                        value.vendor,
                        value.file_identity,
                        value.size,
                        value.mtime_ns,
                        value.ctime_ns,
                        value.committed_offset,
                        value.prefix_checksum,
                        value.tail_checksum,
                        value.status,
                        value.error,
                        value.session_id,
                        value.parent_session_id,
                        value.root_session_id,
                        value.materialized_revision,
                        value.cwd,
                        value.title,
                    )
                    for value in snapshots
                ],
            )

    def page(
        self,
        *,
        mode: str,
        scope: dict[str, Any],
        after: str | None,
        through: str | None,
        limit: int,
    ) -> dict[str, Any]:
        if mode not in {"view", "details"}:
            raise ValueError("mode must be 'view' or 'details'")
        scope_hash = hashlib.sha256(canonical_json(scope).encode("utf-8")).hexdigest()
        with self._connect() as connection:
            current = self._current_revision(connection)
            pruned_through = int(self._metadata(connection, "pruned_through") or "0")
            through_cursor = (
                self._decode_cursor(connection, through) if through else None
            )
            if through_cursor is not None:
                self._validate_cursor_scope(through_cursor, scope_hash)
                if through_cursor.get("kind") != "watermark":
                    raise ValueError("through must be a watermark cursor")
                snapshot_revision = int(through_cursor["revision"])
            else:
                snapshot_revision = current
            if snapshot_revision > current:
                raise ValueError("through cursor is ahead of the current revision")
            if snapshot_revision <= pruned_through and snapshot_revision != current:
                raise ValueError("through cursor has expired")

            after_cursor = self._decode_cursor(connection, after) if after else None
            if after_cursor is not None:
                self._validate_cursor_scope(after_cursor, scope_hash)
            expanded_scope = self._expand_scope(connection, scope, snapshot_revision)
            watermark = self._encode_cursor(
                connection,
                {
                    "kind": "watermark",
                    "revision": snapshot_revision,
                    "scope": scope_hash,
                },
            )

            if after_cursor is None or after_cursor.get("kind") == "snapshot":
                if (
                    after_cursor is not None
                    and int(after_cursor["through"]) != snapshot_revision
                ):
                    raise ValueError("snapshot cursor does not match through")
                return self._snapshot_page(
                    connection,
                    mode=mode,
                    scope=expanded_scope,
                    scope_hash=scope_hash,
                    revision=snapshot_revision,
                    after_cursor=after_cursor,
                    watermark=watermark,
                    limit=limit,
                )

            if after_cursor.get("kind") not in {"watermark", "delta"}:
                raise ValueError("after cursor has an unsupported kind")
            base_revision = int(
                after_cursor.get("base_revision", after_cursor.get("revision", 0))
            )
            if base_revision > current or base_revision < pruned_through:
                return self._reset_page(
                    connection,
                    mode=mode,
                    scope=expanded_scope,
                    scope_hash=scope_hash,
                    revision=snapshot_revision,
                    watermark=watermark,
                    reason="cursor_expired",
                )
            if (
                after_cursor.get("kind") == "delta"
                and int(after_cursor["through"]) != snapshot_revision
            ):
                raise ValueError("delta cursor does not match through")
            return self._delta_page(
                connection,
                mode=mode,
                scope=expanded_scope,
                scope_hash=scope_hash,
                base_revision=base_revision,
                through_revision=snapshot_revision,
                after_change_id=int(after_cursor.get("change_id", 0)),
                watermark=watermark,
                limit=limit,
            )

    def _snapshot_page(
        self,
        connection: sqlite3.Connection,
        *,
        mode: str,
        scope: dict[str, Any],
        scope_hash: str,
        revision: int,
        after_cursor: dict[str, Any] | None,
        watermark: str,
        limit: int,
    ) -> dict[str, Any]:
        position = str(after_cursor.get("position") or "") if after_cursor else ""
        conditions = [
            "valid_from_revision <= ?",
            "(valid_to_revision IS NULL OR valid_to_revision > ?)",
        ]
        bindings: list[Any] = [revision, revision]
        scope_sql, scope_bindings = _scope_sql(scope)
        conditions.extend(scope_sql)
        bindings.extend(scope_bindings)
        if position:
            position_parts = position.split("\x00", 2)
            if len(position_parts) != 3:
                raise ValueError("snapshot cursor position is invalid")
            conditions.append("(sort_key, resource_kind, resource_key) > (?, ?, ?)")
            bindings.extend(position_parts)
        bindings.append(limit + 1)
        rows = connection.execute(
            f"""
            SELECT * FROM living_resource_versions
             WHERE {" AND ".join(conditions)}
             ORDER BY sort_key, resource_kind, resource_key
             LIMIT ?
            """,
            bindings,
        ).fetchall()
        page_rows = rows[:limit]
        has_more = len(rows) > limit
        changes = []
        for row in page_rows:
            cursor = self._encode_cursor(
                connection,
                {
                    "kind": "snapshot",
                    "through": revision,
                    "position": _row_position(row),
                    "scope": scope_hash,
                },
            )
            changes.append(
                self._change_from_resource_row(row, mode=mode, cursor=cursor)
            )
        next_cursor = changes[-1]["cursor"] if has_more and changes else None
        return {
            "schema_version": self.schema_version,
            "mode": mode,
            "page_kind": "snapshot",
            "through": watermark,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "changes": changes,
            "issues": [],
        }

    def _delta_page(
        self,
        connection: sqlite3.Connection,
        *,
        mode: str,
        scope: dict[str, Any],
        scope_hash: str,
        base_revision: int,
        through_revision: int,
        after_change_id: int,
        watermark: str,
        limit: int,
    ) -> dict[str, Any]:
        conditions = ["revision > ?", "revision <= ?", "change_id > ?"]
        bindings: list[Any] = [base_revision, through_revision, after_change_id]
        scope_sql, scope_bindings = _scope_sql(scope)
        conditions.extend(scope_sql)
        bindings.extend(scope_bindings)
        bindings.append(limit + 1)
        rows = connection.execute(
            f"""
            SELECT * FROM living_changes
             WHERE {" AND ".join(conditions)}
             ORDER BY change_id
             LIMIT ?
            """,
            bindings,
        ).fetchall()
        page_rows = rows[:limit]
        has_more = len(rows) > limit
        changes: list[dict[str, Any]] = []
        for row in page_rows:
            cursor = self._encode_cursor(
                connection,
                {
                    "kind": "delta",
                    "base_revision": base_revision,
                    "through": through_revision,
                    "change_id": row["change_id"],
                    "scope": scope_hash,
                },
            )
            resource = self._resource_for_change(connection, row, mode=mode)
            changes.append(
                {
                    "cursor": cursor,
                    "revision": row["revision"],
                    "operation": row["operation"],
                    "resource_kind": row["resource_kind"],
                    "path": json.loads(row["path_json"]),
                    "resource": resource,
                    "reason": row["reason"],
                }
            )
        next_cursor = changes[-1]["cursor"] if has_more and changes else None
        return {
            "schema_version": self.schema_version,
            "mode": mode,
            "page_kind": "delta",
            "through": watermark,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "changes": changes,
            "issues": [],
        }

    def _reset_page(
        self,
        connection: sqlite3.Connection,
        *,
        mode: str,
        scope: dict[str, Any],
        scope_hash: str,
        revision: int,
        watermark: str,
        reason: str,
    ) -> dict[str, Any]:
        root_id = str(scope.get("root_session_id") or "*")
        cursor = self._encode_cursor(
            connection,
            {
                "kind": "watermark",
                "revision": revision,
                "scope": scope_hash,
            },
        )
        return {
            "schema_version": self.schema_version,
            "mode": mode,
            "page_kind": "delta",
            "through": watermark,
            "next_cursor": None,
            "has_more": False,
            "changes": [
                {
                    "cursor": cursor,
                    "revision": revision,
                    "operation": "reset",
                    "resource_kind": "session",
                    "path": {"root_session_id": root_id},
                    "resource": None,
                    "reason": reason,
                }
            ],
            "issues": [],
        }

    def _change_from_resource_row(
        self, row: sqlite3.Row, *, mode: str, cursor: str
    ) -> dict[str, Any]:
        return {
            "cursor": cursor,
            "revision": row["valid_from_revision"],
            "operation": "upsert",
            "resource_kind": row["resource_kind"],
            "path": json.loads(row["path_json"]),
            "resource": self._payload(row, mode=mode),
            "reason": None,
        }

    def _resource_for_change(
        self, connection: sqlite3.Connection, change: sqlite3.Row, *, mode: str
    ) -> dict[str, Any] | None:
        if change["operation"] != "upsert":
            return None
        row = connection.execute(
            """
            SELECT * FROM living_resource_versions
             WHERE resource_kind = ? AND resource_key = ?
               AND valid_from_revision = ?
             LIMIT 1
            """,
            (
                change["resource_kind"],
                change["resource_key"],
                change["revision"],
            ),
        ).fetchone()
        return self._payload(row, mode=mode) if row is not None else None

    @staticmethod
    def _payload(row: sqlite3.Row, *, mode: str) -> dict[str, Any]:
        if mode == "details":
            return json.loads(zlib.decompress(row["detail_payload_z"]).decode("utf-8"))
        return json.loads(row["view_payload_json"])

    def _expand_scope(
        self,
        connection: sqlite3.Connection,
        scope: dict[str, Any],
        revision: int,
    ) -> dict[str, Any]:
        expanded = {key: value for key, value in scope.items() if value}
        if expanded.get("item_id"):
            lookup_kind = "item"
        elif expanded.get("turn_id"):
            lookup_kind = "turn"
        elif expanded.get("session_id"):
            lookup_kind = "session"
        else:
            lookup_kind = None
        lookup_key = (
            expanded.get("item_id")
            or expanded.get("turn_id")
            or expanded.get("session_id")
        )
        if lookup_kind and lookup_key:
            row = connection.execute(
                """
                SELECT path_json FROM living_resource_versions
                 WHERE resource_kind = ? AND resource_key = ?
                   AND valid_from_revision <= ?
                   AND (valid_to_revision IS NULL OR valid_to_revision > ?)
                 ORDER BY valid_from_revision DESC LIMIT 1
                """,
                (lookup_kind, str(lookup_key), revision, revision),
            ).fetchone()
            if row is not None:
                path = json.loads(row["path_json"])
                for key, value in path.items():
                    existing = expanded.get(key)
                    if existing and existing != value:
                        raise ValueError(f"scope {key} does not match {lookup_kind}")
                    expanded.setdefault(key, value)
        return expanded

    def _insert_change(
        self,
        connection: sqlite3.Connection,
        *,
        revision: int,
        operation: str,
        resource_kind: str,
        resource_key: str,
        root_session_id: str,
        path: dict[str, Any],
        reason: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO living_changes(
                revision, operation, resource_kind, resource_key,
                root_session_id, session_id, turn_id, item_id,
                source_session_id, target_session_id, path_json, reason
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision,
                operation,
                resource_kind,
                resource_key,
                root_session_id,
                path.get("session_id"),
                path.get("turn_id"),
                path.get("item_id"),
                path.get("source_session_id"),
                path.get("target_session_id"),
                canonical_json(path),
                reason,
            ),
        )

    def _prune(self, connection: sqlite3.Connection, revision: int) -> None:
        cutoff = revision - _RETAINED_REVISIONS
        if cutoff <= 0:
            return
        connection.execute("DELETE FROM living_changes WHERE revision <= ?", (cutoff,))
        connection.execute(
            "DELETE FROM living_revisions WHERE revision <= ?", (cutoff,)
        )
        connection.execute(
            """
            DELETE FROM living_resource_versions
             WHERE valid_to_revision IS NOT NULL AND valid_to_revision <= ?
            """,
            (cutoff,),
        )
        previous = int(self._metadata(connection, "pruned_through") or "0")
        if cutoff > previous:
            self._set_metadata(connection, "pruned_through", str(cutoff))

    def _encode_cursor(
        self, connection: sqlite3.Connection, value: dict[str, Any]
    ) -> str:
        raw = canonical_json(value).encode("utf-8")
        signature = hmac.new(
            self._metadata(connection, "cursor_secret").encode("utf-8"),
            raw,
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(signature + raw).decode("ascii").rstrip("=")

    def _decode_cursor(
        self, connection: sqlite3.Connection, token: str | None
    ) -> dict[str, Any] | None:
        if token is None:
            return None
        try:
            padded = token + "=" * (-len(token) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
            signature, raw = decoded[:32], decoded[32:]
            expected = hmac.new(
                self._metadata(connection, "cursor_secret").encode("utf-8"),
                raw,
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("signature mismatch")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("cursor payload is not an object")
            return value
        except Exception as exc:
            raise ValueError("invalid or expired living-events cursor") from exc

    @staticmethod
    def _validate_cursor_scope(cursor: dict[str, Any], scope_hash: str) -> None:
        if cursor.get("scope") != scope_hash:
            raise ValueError("cursor scope does not match request scope")

    @staticmethod
    def _set_default(connection: sqlite3.Connection, key: str, value: str) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO living_metadata(key, value) VALUES(?, ?)",
            (key, value),
        )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        existing = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _backfill_path_columns(connection: sqlite3.Connection, table: str) -> None:
        rows = connection.execute(
            f"""
            SELECT rowid AS source_rowid, path_json FROM {table}
             WHERE session_id IS NULL
               AND turn_id IS NULL
               AND item_id IS NULL
               AND source_session_id IS NULL
               AND target_session_id IS NULL
            """
        ).fetchall()
        updates = []
        for row in rows:
            path = json.loads(row["path_json"])
            updates.append(
                (
                    path.get("session_id"),
                    path.get("turn_id"),
                    path.get("item_id"),
                    path.get("source_session_id"),
                    path.get("target_session_id"),
                    row["source_rowid"],
                )
            )
        if updates:
            connection.executemany(
                f"""
                UPDATE {table}
                   SET session_id = ?, turn_id = ?, item_id = ?,
                       source_session_id = ?, target_session_id = ?
                 WHERE rowid = ?
                """,
                updates,
            )

    @staticmethod
    def _set_metadata(connection: sqlite3.Connection, key: str, value: str) -> None:
        connection.execute(
            """
            INSERT INTO living_metadata(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    @staticmethod
    def _metadata(connection: sqlite3.Connection, key: str) -> str:
        row = connection.execute(
            "SELECT value FROM living_metadata WHERE key = ?", (key,)
        ).fetchone()
        return str(row[0]) if row is not None else ""

    @classmethod
    def _current_revision(cls, connection: sqlite3.Connection) -> int:
        return int(cls._metadata(connection, "revision") or "0")


def _resource_digest(resource: ProjectedResource) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "path": resource.path,
                "sort_key": resource.sort_key,
                "view": resource.view,
                "details": resource.details,
            }
        ).encode("utf-8")
    ).hexdigest()


def _scope_sql(scope: dict[str, Any]) -> tuple[list[str], list[Any]]:
    conditions: list[str] = []
    bindings: list[Any] = []
    root_id = scope.get("root_session_id")
    if root_id:
        conditions.append("root_session_id = ?")
        bindings.append(root_id)
    session_id = scope.get("session_id")
    if session_id:
        conditions.append(
            """
            (
                (resource_kind = 'session_edge' AND
                    (source_session_id = ? OR target_session_id = ?))
                OR
                (resource_kind != 'session_edge' AND session_id = ?)
            )
            """
        )
        bindings.extend((session_id, session_id, session_id))
    turn_id = scope.get("turn_id")
    if turn_id:
        conditions.append(
            """
            (
                resource_kind IN ('session', 'context_checkpoint', 'session_edge')
                OR turn_id = ?
            )
            """
        )
        bindings.append(turn_id)
    item_id = scope.get("item_id")
    if item_id:
        conditions.append(
            """
            (
                resource_kind IN (
                    'session', 'turn', 'context_checkpoint', 'session_edge'
                )
                OR item_id = ?
            )
            """
        )
        bindings.append(item_id)
    return conditions, bindings


def _row_position(row: sqlite3.Row) -> str:
    return "\x00".join((row["sort_key"], row["resource_kind"], row["resource_key"]))


__all__ = [
    "LivingEventsStore",
    "ProjectedResource",
    "default_database_path",
    "query_living_events",
    "serve_living_events",
]
