"""Compact, request-driven ``ct.living_sessions.v2`` session inventory.

This projection is intentionally narrower than ``living.events``: it persists
only session metadata and source checkpoints, never transcript, prompt,
response, or tool payloads.  It is therefore safe to use as a cheap global
discovery surface without an MCP server, hook, or running application.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from coding_trajectory.discovery import discover_source_candidates, discover_store_from_files
from coding_trajectory.ingestion.common import format_datetime
from coding_trajectory.ingestion.models import Session, SessionGraph, SessionStatus
from coding_trajectory.living_events import LivingEventsStore, ProjectedResource
from coding_trajectory.living_sources import LivingSourceSnapshot, inventory_source_changes

SCHEMA_VERSION = "ct.living_sessions.v2"
_STORE_FORMAT_VERSION = "living-sessions-store-v2"


def default_database_path(*, current_dir: Path, global_scope: bool) -> Path:
    override = os.environ.get("CT_LIVING_SESSIONS_DB")
    if override:
        return Path(override).expanduser().resolve()
    scope = "global" if global_scope else hashlib.sha256(
        str(current_dir.resolve()).encode("utf-8")
    ).hexdigest()[:16]
    return Path.home() / ".coding-trajectory" / "living-sessions" / f"{scope}-v2.sqlite3"


def serve_living_sessions(
    params: dict[str, Any], *, current_dir: Path, global_scope: bool
) -> dict[str, Any]:
    """Reconcile changed JSONL sources and return a stable inventory page."""

    database = LivingEventsStore(
        default_database_path(current_dir=current_dir, global_scope=global_scope),
        schema_version=SCHEMA_VERSION,
        store_format_version=_STORE_FORMAT_VERSION,
    )
    if params.get("through") is not None:
        return _page(database, params)

    candidates = discover_source_candidates(current_dir=current_dir, global_scope=global_scope)
    previous = database.source_snapshots()
    inventory = inventory_source_changes(candidates, previous)
    states = dict(previous)
    candidate_by_path = {str(value.path.expanduser().resolve()): value for value in candidates}
    for change in inventory.changes:
        states[change.current.path] = change.current

    issues: list[dict[str, Any]] = []
    rebuild_paths = {change.current.path for change in inventory.changes if change.needs_rebuild}
    rebuild_paths.update(
        path for path, state in states.items()
        if state.status in {"ready", "partial"} and state.materialized_revision is None
    )
    for change in inventory.changes:
        if change.current.status == "error":
            issues.append(_source_issue(change.current, "living.sessions.source_scan_failed"))

    # A source and its known ancestors are re-ingested together so root IDs
    # remain stable when an appended child supplies a relationship update.
    for paths in _groups(states, previous, rebuild_paths):
        old_roots = {
            value.root_session_id for path in paths
            if (value := previous.get(path)) is not None and value.root_session_id
        }
        live_paths = sorted(
            path for path in paths
            if (state := states.get(path)) is not None
            and state.status in {"ready", "partial"}
            and path in candidate_by_path
        )
        if not live_paths:
            continue
        try:
            discovery = discover_store_from_files([Path(path) for path in live_paths], retention="measurements")
            source_by_session = {
                state.session_id: state for state in states.values() if state.session_id
            }
            resources: list[ProjectedResource] = []
            roots = set(old_roots)
            root_by_path = {str(source.path.expanduser().resolve()): str(source.root_session_id)
                            for source in discovery.sources if source.root_session_id is not None}
            for graph in discovery.store.session_graphs.values():
                root_id = str(graph.root_session_id)
                roots.add(root_id)
                resources.extend(_project_graph(graph, states=states, source_by_session=source_by_session))
            revision = database.publish(resources, affected_roots=sorted(roots))
            for path in paths:
                state = states.get(path)
                if state is not None:
                    states[path] = state.model_copy(update={
                        "root_session_id": root_by_path.get(path, state.root_session_id),
                        "materialized_revision": revision,
                    })
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            for path in live_paths:
                states[path] = states[path].model_copy(update={"status": "error", "error": error})
                issues.append(_source_issue(states[path], "living.sessions.source_rebuild_failed"))

    changed_paths = {change.current.path for change in inventory.changes} | rebuild_paths
    database.save_source_snapshots([states[path] for path in sorted(changed_paths) if path in states])
    result = _page(database, params)
    result["issues"].extend(issues)
    return result


def _groups(
    states: dict[str, LivingSourceSnapshot], previous: dict[str, LivingSourceSnapshot], rebuild: set[str]
) -> list[set[str]]:
    active = {path: value for path, value in states.items() if value.status in {"ready", "partial"} and value.session_id}
    by_session = {value.session_id: path for path, value in active.items() if value.session_id}
    neighbours = {path: set() for path in active}
    for path, value in active.items():
        parent = by_session.get(value.parent_session_id or "")
        if parent:
            neighbours[path].add(parent)
            neighbours[parent].add(path)
    groups: list[set[str]] = []
    seen: set[str] = set()
    for path in sorted(active):
        if path in seen:
            continue
        group, pending = set(), [path]
        while pending:
            current = pending.pop()
            if current in group:
                continue
            group.add(current)
            seen.add(current)
            pending.extend(neighbours[current] - group)
        if group & rebuild:
            groups.append(group)
    # Deleted/error sources have no active node. Rebuild their surviving prior
    # component when possible; only publish an empty root when it is gone.
    for path in rebuild:
        if path not in active and previous.get(path) and previous[path].root_session_id:
            root_id = previous[path].root_session_id
            surviving = {
                candidate
                for candidate, state in active.items()
                if state.root_session_id == root_id
            }
            if surviving:
                if not any(surviving <= group for group in groups):
                    groups.append(surviving)
            else:
                groups.append({path})
    return groups


def _project_graph(
    graph: SessionGraph, *, states: dict[str, LivingSourceSnapshot], source_by_session: dict[str, LivingSourceSnapshot]
) -> list[ProjectedResource]:
    root_id = str(graph.root_session_id)
    result: list[ProjectedResource] = []
    for session in graph.sessions:
        session_id = str(session.session_id)
        snapshot = source_by_session.get(session_id)
        payload = _session_payload(session, root_id=root_id, snapshot=snapshot)
        result.append(ProjectedResource(
            kind="session", key=session_id, root_session_id=root_id,
            path={"root_session_id": root_id, "session_id": session_id},
            sort_key=f"0:{payload.get('latest_activity_at') or payload.get('started_at') or ''}:{session_id}",
            view=payload, details=payload,
        ))
    return result


def _session_payload(session: Session, *, root_id: str, snapshot: LivingSourceSnapshot | None) -> dict[str, Any]:
    latest = max((turn.ended_at or turn.started_at for turn in session.turns), default=session.ended_at or session.started_at)
    state = (
        "living"
        if session.status == SessionStatus.LIVING
        else "not_living"
    )
    payload: dict[str, Any] = {
        "session_id": str(session.session_id), "root_session_id": root_id,
        "vendor": session.vendor.value, "model": session.model,
        "reasoning_effort": session.reasoning_effort, "cwd": session.cwd,
        "started_at": format_datetime(session.started_at), "ended_at": format_datetime(session.ended_at),
        "latest_activity_at": format_datetime(latest), "state": state,
        "latest_turn_status": (
            session.latest_turn_status.value
            if session.latest_turn_status is not None
            else None
        ),
        "source_readiness": _readiness(snapshot),
    }
    return {key: value for key, value in payload.items() if value is not None}


def _readiness(snapshot: LivingSourceSnapshot | None) -> dict[str, Any]:
    if snapshot is None:
        return {"status": "unknown"}
    return {key: value for key, value in {
        "status": snapshot.status, "committed_offset": snapshot.committed_offset,
        "size": snapshot.size, "mtime_ns": snapshot.mtime_ns,
    }.items() if value is not None}


def _source_issue(snapshot: LivingSourceSnapshot, code: str) -> dict[str, Any]:
    return {"severity": "error", "code": code, "message": f"{snapshot.path}: {snapshot.error or snapshot.status}"}


def _page(database: LivingEventsStore, params: dict[str, Any]) -> dict[str, Any]:
    return database.page(mode="view", scope={}, after=params.get("after"), through=params.get("through"), limit=int(params.get("limit") or 50))


__all__ = ["SCHEMA_VERSION", "default_database_path", "serve_living_sessions"]
