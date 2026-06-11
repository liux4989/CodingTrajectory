from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

try:
    from . import cleanup as cleanup_mod
    from . import context_window as context_window_mod
except ImportError:
    import cleanup as cleanup_mod
    import context_window as context_window_mod


@dataclass(frozen=True, slots=True)
class CacheEntry:
    created_at: float
    value: dict[str, Any]


class TtlCache:
    def __init__(self, ttl_seconds: float) -> None:
        self._ttl_seconds = ttl_seconds
        self._entries: dict[tuple[Any, ...], CacheEntry] = {}
        self._lock = threading.Lock()

    def get_or_set(self, key: tuple[Any, ...], factory: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry and now - entry.created_at < self._ttl_seconds:
                return entry.value
        value = factory()
        with self._lock:
            self._entries[key] = CacheEntry(time.monotonic(), value)
        return value

    def clear_prefix(self, prefix: tuple[Any, ...]) -> None:
        with self._lock:
            for key in list(self._entries):
                if key[: len(prefix)] == prefix:
                    del self._entries[key]


class DashboardDataService:
    def __init__(self, *, cache_ttl_seconds: float = 12) -> None:
        self._cache = TtlCache(cache_ttl_seconds)

    def overview(self) -> dict[str, Any]:
        projects = self.projects({})
        sessions = self.sessions({})
        project_items = projects.get("items", [])
        vendor_counts: dict[str, int] = {}
        for item in project_items:
            for vendor in item.get("vendors") or []:
                vendor_counts[vendor] = vendor_counts.get(vendor, 0) + 1
        return {
            "projects": {"count": len(project_items), "vendors": vendor_counts},
            "sessions": {"count": len(sessions.get("items", []))},
        }

    def projects(self, query: dict[str, list[str]]) -> dict[str, Any]:
        vendor = _first(query, "agent_vendor")
        return self._cache.get_or_set(("projects", vendor), lambda: self._projects_uncached(vendor))

    def project_detail(self, query: dict[str, list[str]]) -> dict[str, Any]:
        project_name = _first(query, "project_name")
        if not project_name:
            raise ValueError("project_name is required")
        projects = _ct_json(["project", "list", "--params", json.dumps({}), "--output", "json"])
        items = projects.get("items") or {}
        meta = items.get(project_name)
        if not meta:
            raise ValueError(f"project not found: {project_name}")
        sessions_params: dict[str, Any] = {"project_name": project_name, "since_days": None}
        sessions = _ct_json(
            ["project", "sessions", "--params", json.dumps(sessions_params), "--output", "json"]
        )
        return {
            "name": project_name,
            "path": meta.get("path"),
            "vendors": meta.get("vendors") or [],
            "sessions": sessions.get("items") or [],
            "session_count": len(sessions.get("items") or []),
        }

    def sessions(self, query: dict[str, list[str]]) -> dict[str, Any]:
        params = _session_query_params(query)
        return self._cache.get_or_set(
            ("sessions", json.dumps(params, sort_keys=True)),
            lambda: self._sessions_uncached(params),
        )

    def session_timeline(self, query: dict[str, list[str]]) -> dict[str, Any]:
        items = self.sessions(query).get("items") or []
        by_date: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            date_key = str(item.get("date") or item.get("created_at") or "unknown")[:10]
            by_date.setdefault(date_key, []).append(
                {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "v": item.get("vendors") or [],
                }
            )
        timeline = [
            {"date": date, "count": len(entries), "sessions": entries}
            for date, entries in sorted(by_date.items(), reverse=True)
        ]
        return {"timeline": timeline, "total": len(items)}

    def context_window(self, query: dict[str, list[str]]) -> dict[str, Any]:
        session_id = _first(query, "session_id")
        if not session_id:
            raise ValueError("session_id is required")
        return context_window_mod.build_projection(
            session_id,
            turn_id=_first(query, "turn_id"),
        ).model_dump(mode="json")

    def vendors(self, query: dict[str, list[str]]) -> dict[str, Any]:
        vendor_stats: dict[str, dict[str, Any]] = {}
        for item in self.projects(query).get("items") or []:
            name = item.get("name")
            for vendor in item.get("vendors") or []:
                if vendor not in vendor_stats:
                    vendor_stats[vendor] = {"count": 0, "projects": []}
                vendor_stats[vendor]["count"] += 1
                vendor_stats[vendor]["projects"].append(name)
        for vendor in vendor_stats:
            vendor_stats[vendor]["projects"].sort()
        return {
            "vendors": {
                vendor: {
                    "project_count": stats["count"],
                    "projects": stats["projects"],
                }
                for vendor, stats in sorted(vendor_stats.items(), key=lambda x: -x[1]["count"])
            }
        }

    def project_cleanup_preview(self, query: dict[str, list[str]]) -> dict[str, Any]:
        return _preview_payload(_project_cleanup_preview(query))

    def session_cleanup_preview(self, query: dict[str, list[str]]) -> dict[str, Any]:
        return _preview_payload(_session_cleanup_preview(query))

    def apply_project_cleanup(self, body: dict[str, Any]) -> dict[str, Any]:
        action = _cleanup_action(body, allow_trash=False)
        selected_paths = _selected_paths(body)
        query = _body_query(body)
        preview = _project_cleanup_preview(query)
        selected = [
            target
            for target in preview.candidates
            if isinstance(target, cleanup_mod.ProjectTarget) and target.path in selected_paths
        ]
        _require_all_selected(selected_paths, [target.path for target in selected])
        result = cleanup_mod.apply_project_selection(
            argparse.Namespace(
                older_than=_first(query, "older_than") or "30d",
                path=_first(query, "path"),
                trash=action == "trash",
                delete=action == "delete",
                confirm=True,
                tui=False,
                detail=True,
            ),
            preview,
            action,
            selected,
        )
        self._cache.clear_prefix(("projects",))
        return result

    def apply_session_cleanup(self, body: dict[str, Any]) -> dict[str, Any]:
        action = _cleanup_action(body)
        selected_paths = _selected_paths(body)
        query = _body_query(body)
        preview = _session_cleanup_preview(query)
        selected = [
            target
            for target in preview.candidates
            if isinstance(target, cleanup_mod.SessionTarget) and target.path in selected_paths
        ]
        _require_all_selected(selected_paths, [target.path for target in selected])
        result = cleanup_mod.apply_session_selection(
            argparse.Namespace(
                agent_vendor=_first(query, "agent_vendor"),
                trash=action == "trash",
                delete=action == "delete",
                confirm=True,
                tui=False,
                detail=True,
            ),
            preview,
            action,
            selected,
        )
        self._cache.clear_prefix(("sessions",))
        return result

    def _projects_uncached(self, vendor: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if vendor:
            params["agent_vendor"] = vendor
        payload = _ct_json(["project", "list", "--params", json.dumps(params), "--output", "json"])
        items = payload.get("items") or {}
        return {
            "items": [
                {
                    "name": name,
                    "path": item.get("path"),
                    "vendors": item.get("vendors") or [],
                }
                for name, item in sorted(items.items())
            ]
        }

    def _sessions_uncached(self, params: dict[str, Any]) -> dict[str, Any]:
        payload = _ct_json(["project", "sessions", "--params", json.dumps(params), "--output", "json"])
        return {"items": payload.get("items") or []}


def _session_query_params(query: dict[str, list[str]]) -> dict[str, Any]:
    params: dict[str, Any] = {
        "since_days": None if _bool(query, "all_time") else _int(query, "since_days", 30)
    }
    project_name = _first(query, "project_name")
    vendor = _first(query, "agent_vendor")
    if project_name:
        params["project_name"] = project_name
    if vendor:
        params["agent_vendor"] = vendor
    return params


def _project_cleanup_preview(query: dict[str, list[str]]) -> cleanup_mod.CleanupPreview:
    return cleanup_mod.preview_project_cleanup(
        argparse.Namespace(
            older_than=_first(query, "older_than") or "30d",
            path=_first(query, "path"),
            trash=False,
            delete=False,
            confirm=False,
            tui=False,
            detail=False,
        )
    )


def _session_cleanup_preview(query: dict[str, list[str]]) -> cleanup_mod.CleanupPreview:
    return cleanup_mod.preview_session_cleanup(
        argparse.Namespace(
            agent_vendor=_first(query, "agent_vendor"),
            trash=False,
            delete=False,
            confirm=False,
            tui=False,
            detail=False,
        )
    )


def _preview_payload(preview: cleanup_mod.CleanupPreview) -> dict[str, Any]:
    skipped_reasons: dict[str, int] = {}
    for item in preview.skipped:
        for reason in item.reason:
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
    return {
        "target_kind": preview.target_kind,
        "filters": preview.filters,
        "summary": {
            "candidate_count": len(preview.candidates),
            "skipped_count": len(preview.skipped),
            "skipped_reasons": dict(sorted(skipped_reasons.items())),
        },
        "candidates": [target.model_dump(mode="json") for target in preview.candidates],
        "skipped": [
            item.model_dump(mode="json")
            for item in sorted(preview.skipped, key=lambda target: (target.kind, target.path))
        ],
    }


def _cleanup_action(
    body: dict[str, Any],
    *,
    allow_trash: bool = True,
) -> cleanup_mod.Action:
    action = body.get("action")
    allowed = {"trash", "delete"} if allow_trash else {"delete"}
    if action not in allowed:
        raise ValueError(
            "action must be trash or delete" if allow_trash else "action must be delete"
        )
    return action


def _selected_paths(body: dict[str, Any]) -> set[str]:
    raw = body.get("paths")
    if not isinstance(raw, list) or not raw:
        raise ValueError("paths must be a non-empty list")
    paths = {item for item in raw if isinstance(item, str) and item}
    if len(paths) != len(raw):
        raise ValueError("paths must contain only non-empty strings")
    return paths


def _require_all_selected(requested: set[str], matched: list[str]) -> None:
    missing = requested - set(matched)
    if missing:
        raise ValueError(f"selected path is no longer a cleanup candidate: {sorted(missing)[0]}")


def _body_query(body: dict[str, Any]) -> dict[str, list[str]]:
    raw = body.get("filters") or {}
    if not isinstance(raw, dict):
        raise ValueError("filters must be an object")
    query: dict[str, list[str]] = {}
    for key, value in raw.items():
        if isinstance(key, str) and value is not None:
            query[key] = [str(value)]
    return query


def _ct_json(args: list[str]) -> dict[str, Any]:
    ct = os.environ.get("CT_COMMAND") or shutil.which("ct")
    if not ct:
        raise RuntimeError("ct executable not found; set CT_COMMAND to the ct command path")
    command = [*shlex.split(ct), *args]
    try:
        completed = subprocess.run(command, check=False, text=True, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ct command timed out: {' '.join(command)}") from exc
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "ct command failed"
        raise RuntimeError(message)
    return json.loads(completed.stdout)


def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key) or []
    value = values[0].strip() if values else ""
    return value or None


def _int(query: dict[str, list[str]], key: str, default: int) -> int:
    value = _first(query, key)
    if value is None:
        return default
    return int(value)


def _bool(query: dict[str, list[str]], key: str) -> bool:
    value = _first(query, key)
    return value is not None and value.lower() in {"1", "true", "yes", "on"}
