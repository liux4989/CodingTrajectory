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
    from . import error_collection as error_collection_mod
    from . import model_usage as model_usage_mod
    from . import session_analysis as session_analysis_mod
except ImportError:
    import cleanup as cleanup_mod
    import context_window as context_window_mod
    import error_collection as error_collection_mod
    import model_usage as model_usage_mod
    import session_analysis as session_analysis_mod


@dataclass(frozen=True, slots=True)
class CacheEntry:
    created_at: float
    value: dict[str, Any]


class TtlCache:
    def __init__(self, ttl_seconds: float) -> None:
        self._ttl_seconds = ttl_seconds
        self._entries: dict[tuple[Any, ...], CacheEntry] = {}
        self._lock = threading.Lock()

    def get_or_set(
        self, key: tuple[Any, ...], factory: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
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
        return self._cache.get_or_set(("overview",), self._overview_uncached)

    def _overview_uncached(self) -> dict[str, Any]:
        payload = _ct_json(
            [
                "api",
                "batch",
                "--global-scope",
                "--requests",
                json.dumps(
                    [
                        {"id": "projects", "method": "project.list", "params": {}},
                        {
                            "id": "sessions",
                            "method": "project.sessions",
                            "params": {
                                "since_days": 1,
                                "include": ["runtime", "usage"],
                            },
                        },
                    ]
                ),
            ]
        )
        responses = {
            str(item.get("id")): item
            for item in payload.get("items") or []
            if isinstance(item, dict)
        }
        projects = _batch_result(responses, "projects")
        sessions = _batch_result(responses, "sessions")
        project_items = projects.get("items") or {}
        if not isinstance(project_items, dict):
            raise RuntimeError("project.list returned invalid items")
        session_items = [
            _dashboard_session_item(item)
            for item in sessions.get("items") or []
            if isinstance(item, dict)
        ]
        vendor_counts: dict[str, int] = {}
        for item in project_items.values():
            for vendor in item.get("vendors") or []:
                vendor_counts[vendor] = vendor_counts.get(vendor, 0) + 1
        activity = _overview_activity(session_items)
        return {
            "projects": {"count": len(project_items), "vendors": vendor_counts},
            "sessions": {
                "count": len(session_items),
                "window_days": 1,
                "runtime": activity["runtime"],
                "usage": activity["usage"],
                "top_projects": activity["top_projects"],
                "top_sessions": activity["top_sessions"],
                "warnings": activity["warnings"],
                "errors": [],
            },
        }

    def projects(self, query: dict[str, list[str]]) -> dict[str, Any]:
        vendor = _first(query, "agent_vendor")
        return self._cache.get_or_set(
            ("projects", vendor), lambda: self._projects_uncached(vendor)
        )

    def project_detail(self, query: dict[str, list[str]]) -> dict[str, Any]:
        project_name = _first(query, "project_name")
        if not project_name:
            raise ValueError("project_name is required")
        projects = _ct_json(
            ["project", "list", "--params", json.dumps({}), "--output", "json"]
        )
        items = projects.get("items") or {}
        meta = items.get(project_name)
        if not meta:
            raise ValueError(f"project not found: {project_name}")
        since_days_raw = _first(query, "since_days")
        sessions_params: dict[str, Any] = {"project_name": project_name}
        if since_days_raw is not None:
            sessions_params["since_days"] = int(since_days_raw)
        sessions = _ct_json(
            [
                "project",
                "sessions",
                "--params",
                json.dumps(sessions_params),
                "--output",
                "json",
            ]
        )
        return {
            "name": project_name,
            "path": meta.get("path"),
            "vendors": meta.get("vendors") or [],
            "since_days": sessions_params.get("since_days"),
            "sessions": sessions.get("items") or [],
            "session_count": len(sessions.get("items") or []),
        }

    def sessions(self, query: dict[str, list[str]]) -> dict[str, Any]:
        params = _session_query_params(query)
        return self._cache.get_or_set(
            ("sessions", json.dumps(params, sort_keys=True)),
            lambda: self._sessions_uncached(params),
        )

    def session_data(self, query: dict[str, list[str]]) -> dict[str, Any]:
        params = _session_data_query_params(query)
        return self._cache.get_or_set(
            ("session_data", json.dumps(params, sort_keys=True)),
            lambda: self._session_data_uncached(params),
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

    def model_usage(self, query: dict[str, list[str]]) -> dict[str, Any]:
        return model_usage_mod.build_projection(
            ct_json=_ct_json,
            since_days=_int(query, "since_days", 7),
            project_name=_first(query, "project_name"),
        )

    def error_collection(self, query: dict[str, list[str]]) -> dict[str, Any]:
        return error_collection_mod.build_projection(
            ct_json=_ct_json,
            since_days=_int(query, "since_days", 7),
            project_name=_first(query, "project_name"),
        )

    def session_analysis(self, body: dict[str, Any]) -> dict[str, Any]:
        session_id = body.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id is required")
        refresh = bool(body.get("refresh"))
        provider = body.get("provider", "codex")
        if not isinstance(provider, str):
            raise ValueError("provider must be codex or pi")
        analysis = session_analysis_mod.build_or_load_analysis(
            session_id.strip(),
            ct_json=_ct_json,
            refresh=refresh,
            provider=provider,
        )
        return {
            "status": "ready",
            "artifact_path": analysis.artifact_path,
            "analysis": analysis.model_dump(mode="json"),
        }

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
                for vendor, stats in sorted(
                    vendor_stats.items(), key=lambda x: -x[1]["count"]
                )
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
            if isinstance(target, cleanup_mod.ProjectTarget)
            and target.path in selected_paths
        ]
        _require_all_selected(selected_paths, [target.path for target in selected])
        result = cleanup_mod.apply_project_selection(
            argparse.Namespace(
                older_than=_first(query, "older_than") or "30d",
                path=_first(query, "path"),
                trash=action == "trash",
                delete=action == "delete",
                confirm=True,
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
            if isinstance(target, cleanup_mod.SessionTarget)
            and target.path in selected_paths
        ]
        _require_all_selected(selected_paths, [target.path for target in selected])
        result = cleanup_mod.apply_session_selection(
            argparse.Namespace(
                agent_vendor=_first(query, "agent_vendor"),
                trash=action == "trash",
                delete=action == "delete",
                confirm=True,
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
        payload = _ct_json(
            ["project", "list", "--params", json.dumps(params), "--output", "json"]
        )
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
        payload = _ct_json(
            ["project", "sessions", "--params", json.dumps(params), "--output", "json"]
        )
        return {"items": payload.get("items") or []}

    def _session_data_uncached(self, params: dict[str, Any]) -> dict[str, Any]:
        include = set(params.get("include") or ["metadata", "runtime", "usage"])
        request: dict[str, Any] = {
            "since_days": params.get("since_days"),
            "include": sorted(include & {"runtime", "usage"}),
        }
        if params.get("project_name"):
            request["project_name"] = params["project_name"]
        if params.get("agent_vendor"):
            request["agent_vendor"] = params["agent_vendor"]
        payload = _ct_json(
            [
                "api",
                "call",
                "project.sessions",
                "--global-scope",
                "--params",
                json.dumps(request),
            ]
        )
        if not payload.get("ok"):
            error = payload.get("error") or {}
            return {
                "items": [],
                "errors": [{"message": error.get("message") or "request failed"}],
            }
        result = payload.get("result") or {}
        return {
            "items": [
                _dashboard_session_item(item)
                for item in result.get("items") or []
                if isinstance(item, dict)
            ],
            "errors": [],
        }


def _session_query_params(query: dict[str, list[str]]) -> dict[str, Any]:
    params: dict[str, Any] = {
        "since_days": None
        if _bool(query, "all_time")
        else _int(query, "since_days", 30)
    }
    project_name = _first(query, "project_name")
    vendor = _first(query, "agent_vendor")
    if project_name:
        params["project_name"] = project_name
    if vendor:
        params["agent_vendor"] = vendor
    return params


def _session_data_query_params(query: dict[str, list[str]]) -> dict[str, Any]:
    params: dict[str, Any] = {
        "since_days": _int(query, "since_days", 30),
        "include": ["metadata", "runtime", "usage"],
    }
    project_name = _first(query, "project_name")
    vendor = _first(query, "agent_vendor")
    if project_name:
        params["project_name"] = project_name
    if vendor:
        params["agent_vendor"] = vendor
    include = _first(query, "include")
    if include:
        params["include"] = [
            item.strip() for item in include.split(",") if item.strip()
        ]
    return params


def _overview_activity(items: list[dict[str, Any]]) -> dict[str, Any]:
    runtime_totals = {
        "execution_seconds": 0,
        "wait_seconds": 0,
        "turns": 0,
        "tool_calls": 0,
        "failed_tool_calls": 0,
    }
    usage_totals = {
        "total_tokens": 0,
        "cost_usd": 0.0,
        "known_cost_count": 0,
        "missing_cost_count": 0,
    }
    project_stats: dict[str, dict[str, Any]] = {}
    warnings: list[dict[str, Any]] = []

    for item in items:
        runtime = item.get("runtime") or {}
        usage = item.get("usage") or {}
        for key in runtime_totals:
            runtime_totals[key] += _number(runtime.get(key))
        usage_totals["total_tokens"] += int(_number(usage.get("total_tokens")))
        if isinstance(usage, dict) and isinstance(usage.get("cost_usd"), int | float):
            usage_totals["cost_usd"] += float(usage["cost_usd"])
            usage_totals["known_cost_count"] += 1
        else:
            usage_totals["missing_cost_count"] += 1

        project = str(item.get("project") or "unknown")
        stats = project_stats.setdefault(
            project,
            {
                "project": project,
                "count": 0,
                "vendors": {},
                "execution_seconds": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "known_cost_count": 0,
            },
        )
        stats["count"] += 1
        stats["execution_seconds"] += _number(runtime.get("execution_seconds"))
        stats["total_tokens"] += int(_number(usage.get("total_tokens")))
        if isinstance(usage, dict) and isinstance(usage.get("cost_usd"), int | float):
            stats["cost_usd"] += float(usage["cost_usd"])
            stats["known_cost_count"] += 1
        for vendor in item.get("vendors") or []:
            vendor_key = str(vendor)
            stats["vendors"][vendor_key] = stats["vendors"].get(vendor_key, 0) + 1

        item_warnings = item.get("warnings") or []
        for warning in item_warnings:
            warnings.append(
                {
                    "session_id": item.get("id"),
                    "project": project,
                    "message": str(warning),
                }
            )

    top_projects = sorted(
        (
            {
                **stats,
                "vendors": dict(sorted(stats["vendors"].items())),
            }
            for stats in project_stats.values()
        ),
        key=lambda stats: (-stats["count"], stats["project"]),
    )[:8]

    top_sessions = sorted(
        (
            _overview_session(item)
            for item in items
            if int(_number((item.get("usage") or {}).get("total_tokens"))) > 0
        ),
        key=lambda item: item["total_tokens"],
        reverse=True,
    )[:8]

    return {
        "runtime": runtime_totals,
        "usage": usage_totals,
        "top_projects": top_projects,
        "top_sessions": top_sessions,
        "warnings": warnings,
    }


def _batch_result(
    responses: dict[str, dict[str, Any]], request_id: str
) -> dict[str, Any]:
    response = responses.get(request_id)
    if response is None:
        raise RuntimeError(f"ct api batch omitted response: {request_id}")
    if not response.get("ok"):
        error = response.get("error") or {}
        raise RuntimeError(
            str(error.get("message") or f"ct api request failed: {request_id}")
        )
    result = response.get("result") or {}
    if not isinstance(result, dict):
        raise RuntimeError(f"ct api request returned invalid result: {request_id}")
    return result


def _dashboard_session_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("root_session_id") or item.get("id"),
        "project": item.get("project"),
        "title": item.get("title"),
        "vendors": item.get("vendors") or [],
        "sessions": item.get("session_ids") or item.get("sessions") or [],
        "runtime": item.get("runtime") or {},
        "usage": item.get("usage") or {},
        "warnings": item.get("warnings") or [],
    }


def _overview_session(item: dict[str, Any]) -> dict[str, Any]:
    runtime = item.get("runtime") or {}
    usage = item.get("usage") or {}
    vendors = item.get("vendors") or []
    return {
        "id": item.get("id"),
        "title": item.get("title"),
        "project": item.get("project"),
        "vendor": str(vendors[0]) if vendors else "unknown",
        "vendors": vendors,
        "started_at": runtime.get("started_at"),
        "execution_seconds": int(_number(runtime.get("execution_seconds"))),
        "wait_seconds": int(_number(runtime.get("wait_seconds"))),
        "turns": int(_number(runtime.get("turns"))),
        "tool_calls": int(_number(runtime.get("tool_calls"))),
        "failed_tool_calls": int(_number(runtime.get("failed_tool_calls"))),
        "total_tokens": int(_number(usage.get("total_tokens"))),
    }


def _number(value: Any) -> int | float:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int | float):
        return value
    return 0


def _project_cleanup_preview(query: dict[str, list[str]]) -> cleanup_mod.CleanupPreview:
    return cleanup_mod.preview_project_cleanup(
        argparse.Namespace(
            older_than=_first(query, "older_than") or "30d",
            path=_first(query, "path"),
            trash=False,
            delete=False,
            confirm=False,
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
            for item in sorted(
                preview.skipped, key=lambda target: (target.kind, target.path)
            )
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
        raise ValueError(
            f"selected path is no longer a cleanup candidate: {sorted(missing)[0]}"
        )


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
        raise RuntimeError(
            "ct executable not found; set CT_COMMAND to the ct command path"
        )
    command = [*shlex.split(ct), *args]
    try:
        completed = subprocess.run(
            command, check=False, text=True, capture_output=True, timeout=30
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ct command timed out: {' '.join(command)}") from exc
    if completed.returncode != 0:
        message = (
            completed.stderr.strip() or completed.stdout.strip() or "ct command failed"
        )
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
