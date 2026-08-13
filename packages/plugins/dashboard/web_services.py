from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:
    from . import cache_breaks as cache_breaks_mod
    from . import cleanup as cleanup_mod
    from .codex_app_server import CodexAppServerManager, close_active_app_servers
    from . import context_window as context_window_mod
    from . import error_collection as error_collection_mod
    from . import model_usage as model_usage_mod
    from . import session_analysis as session_analysis_mod
    from . import token_efficiency as token_efficiency_mod
    from .jobs import JobRunner, JobStore
    from .source_data import DashboardSourceData
    from .work_manager import DashboardWorkManager
except ImportError:
    import cache_breaks as cache_breaks_mod
    import cleanup as cleanup_mod
    from codex_app_server import CodexAppServerManager, close_active_app_servers
    import context_window as context_window_mod
    import error_collection as error_collection_mod
    import model_usage as model_usage_mod
    import session_analysis as session_analysis_mod
    import token_efficiency as token_efficiency_mod
    from jobs import JobRunner, JobStore
    from source_data import DashboardSourceData
    from work_manager import DashboardWorkManager


class DashboardDataService:
    def __init__(
        self,
        *,
        cache_ttl_seconds: float = 60,
        stale_ttl_seconds: float = 120,
    ) -> None:
        self._work = DashboardWorkManager(
            cache_ttl_seconds,
            stale_ttl_seconds=stale_ttl_seconds,
        )
        self._source = DashboardSourceData(ct_json=lambda args: _ct_json(args))
        self._jobs = JobStore()
        self._runner = JobRunner(self._jobs)
        self._token_efficiency_generation = 0
        self._app_server = CodexAppServerManager(cwd=_repo_root())

    def shutdown(self) -> None:
        self._runner.shutdown(wait=False)
        self._app_server.close()
        close_active_app_servers()
        self._work.shutdown(wait=False)
        self._source.shutdown()

    def clear_caches(self) -> None:
        """Invalidate both dashboard projections and shared source reads."""
        self._source.clear()
        self._work.clear_all()

    def _invalidate_cached_data(self) -> None:
        self.clear_caches()
        self._token_efficiency_generation += 1

    def refresh(self) -> dict[str, Any]:
        self._invalidate_cached_data()
        return {"status": "refreshed"}

    def cache_metrics(self) -> dict[str, Any]:
        """Return observational snapshots; the two layers are not atomic."""
        return {
            "projection": self._work.metrics(),
            "source": self._source.metrics(),
        }

    def overview(self, query: dict[str, list[str]]) -> dict[str, Any]:
        since_days = _int(query, "since_days", 7)
        return self._work.get_or_compute(
            ("overview", since_days),
            lambda: self._overview_uncached(since_days),
        )

    def _overview_uncached(self, since_days: int) -> dict[str, Any]:
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
                                "since_days": since_days,
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
        _overview_attach_session_costs(session_items)
        vendor_counts: dict[str, int] = {}
        for item in project_items.values():
            for vendor in item.get("vendors") or []:
                vendor_counts[vendor] = vendor_counts.get(vendor, 0) + 1
        activity = _overview_activity(session_items)
        return {
            "projects": {"count": len(project_items), "vendors": vendor_counts},
            "sessions": {
                "count": len(session_items),
                "window_days": since_days,
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
        return self._work.get_or_compute(
            ("projects", vendor), lambda: self._projects_uncached(vendor)
        )

    def project_detail(self, query: dict[str, list[str]]) -> dict[str, Any]:
        project_name = _first(query, "project_name")
        if not project_name:
            raise ValueError("project_name is required")
        since_days_raw = _first(query, "since_days")
        since_days = int(since_days_raw) if since_days_raw is not None else None
        return self._work.get_or_compute(
            ("project_detail", project_name, since_days),
            lambda: self._project_detail_uncached(project_name, since_days),
        )

    def _project_detail_uncached(
        self,
        project_name: str,
        since_days: int | None,
    ) -> dict[str, Any]:
        projects = self._source.call(
            "project.list",
            {},
            global_scope=True,
        )
        items = projects.get("items") or {}
        meta = items.get(project_name)
        if not meta:
            raise ValueError(f"project not found: {project_name}")
        sessions_params: dict[str, Any] = {"project_name": project_name}
        if since_days is not None:
            sessions_params["since_days"] = since_days
        sessions = _api_result(
            "project.sessions",
            sessions_params,
            global_scope=True,
            ct_json=_ct_json_expensive,
        )
        return {
            "name": project_name,
            "path": meta.get("path"),
            "vendors": meta.get("vendors") or [],
            "since_days": since_days,
            "sessions": sessions.get("items") or [],
            "session_count": len(sessions.get("items") or []),
        }

    def sessions(self, query: dict[str, list[str]]) -> dict[str, Any]:
        params = _session_query_params(query)
        return self._work.get_or_compute(
            ("sessions", json.dumps(params, sort_keys=True)),
            lambda: self._sessions_uncached(params),
        )

    def session_data(self, query: dict[str, list[str]]) -> dict[str, Any]:
        params = _session_data_query_params(query)
        return self._work.get_or_compute(
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
        turn_id = _first(query, "turn_id")
        return self._work.get_or_compute(
            ("context_window", session_id, turn_id),
            lambda: context_window_mod.build_projection(
                session_id,
                turn_id=turn_id,
                ct_json=_ct_json_expensive,
            ).model_dump(mode="json"),
        )

    def model_usage(self, query: dict[str, list[str]]) -> dict[str, Any]:
        since_days = _int(query, "since_days", 7)
        project_name = _first(query, "project_name")
        model_key = _first(query, "model_key")
        return self._work.get_or_compute(
            ("model_usage", since_days, project_name, model_key),
            lambda: model_usage_mod.build_projection(
                ct_json=self._source.json,
                since_days=since_days,
                project_name=project_name,
                model_key=model_key,
            ),
        )

    def token_efficiency_index(
        self,
        query: dict[str, list[str]],
        *,
        generation: int | None = None,
    ) -> dict[str, Any]:
        since_days = min(_int(query, "since_days", 7), 30)
        generation = (
            self._token_efficiency_generation if generation is None else generation
        )
        return self._work.get_or_compute(
            ("token_efficiency_index", since_days, generation),
            lambda: token_efficiency_mod.build_index_projection(
                ct_json=_ct_json_expensive,
                since_days=since_days,
            ),
            ttl_seconds=3_600,
            stale_ttl_seconds=3_600,
        )

    def start_token_efficiency_index(self, body: dict[str, Any]) -> dict[str, Any]:
        query = _body_query(body)
        since_days = min(_int(query, "since_days", 7), 30)
        generation = self._token_efficiency_generation
        operation_key = f"token-efficiency-index:v1:{since_days}:g{generation}"
        job_id, created = self._runner.submit_once(
            operation_key,
            "token-efficiency-index",
            self.token_efficiency_index,
            query,
            generation=generation,
        )
        return {
            "status": "pending",
            "job_id": job_id,
            "operation_key": operation_key,
            "reused": not created,
        }

    def token_efficiency_project(
        self,
        query: dict[str, list[str]],
        *,
        generation: int | None = None,
    ) -> dict[str, Any]:
        project_name = _first(query, "project_name")
        if not project_name:
            raise ValueError("project_name is required")
        since_days = min(_int(query, "since_days", 7), 30)
        generation = (
            self._token_efficiency_generation if generation is None else generation
        )
        return self._work.get_or_compute(
            (
                "token_efficiency_project",
                project_name,
                since_days,
                generation,
            ),
            lambda: token_efficiency_mod.build_project_projection(
                ct_json=_ct_json_expensive,
                project_name=project_name,
                since_days=since_days,
            ),
            ttl_seconds=3_600,
            stale_ttl_seconds=3_600,
        )

    def start_token_efficiency_project(self, body: dict[str, Any]) -> dict[str, Any]:
        query = _body_query(body)
        project_name = _first(query, "project_name")
        if not project_name:
            raise ValueError("project_name is required")
        since_days = min(_int(query, "since_days", 7), 30)
        generation = self._token_efficiency_generation
        operation_key = (
            "token-efficiency-project:v1:"
            f"{_project_cache_key(project_name)}:{since_days}:"
            f"g{generation}"
        )
        job_id, created = self._runner.submit_once(
            operation_key,
            "token-efficiency-project",
            self.token_efficiency_project,
            query,
            generation=generation,
        )
        return {
            "status": "pending",
            "job_id": job_id,
            "operation_key": operation_key,
            "reused": not created,
        }

    def error_collection(self, query: dict[str, list[str]]) -> dict[str, Any]:
        since_days = _int(query, "since_days", 7)
        project_name = _first(query, "project_name")
        return self._work.get_or_compute(
            ("error_collection", since_days, project_name),
            lambda: error_collection_mod.build_projection(
                ct_json=self._source.json,
                since_days=since_days,
                project_name=project_name,
            ),
        )

    def cache_breaks(self, query: dict[str, list[str]]) -> dict[str, Any]:
        since_days = _int(query, "since_days", 7)
        project_name = _first(query, "project_name")
        # One batched ``ct api call session.usage`` fetches all sessions at
        # once (no per-session subprocess fan-out), so the default cache TTL is
        # enough; concurrent misses coalesce in the work manager.
        return self._work.get_or_compute(
            ("cache_breaks", since_days, project_name),
            lambda: cache_breaks_mod.build_projection(
                ct_json=self._source.json,
                since_days=since_days,
                project_name=project_name,
            ),
        )

    def session_analysis(self, body: dict[str, Any]) -> dict[str, Any]:
        session_id = body.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id is required")
        refresh = bool(body.get("refresh"))
        operation_key = _session_analysis_operation_key(session_id.strip(), refresh)
        if refresh:
            job_id = self._runner.submit(
                "session-analysis",
                session_analysis_mod.build_analysis,
                session_id.strip(),
                ct_json=self._source.json,
                app_server=self._app_server,
            )
            reused = False
        else:
            job_id, created = self._runner.submit_once(
                operation_key,
                "session-analysis",
                session_analysis_mod.build_analysis,
                session_id.strip(),
                ct_json=self._source.json,
                app_server=self._app_server,
            )
            reused = not created
        return {
            "status": "pending",
            "job_id": job_id,
            "operation_key": operation_key,
            "reused": reused,
        }

    def job_status(self, job_id: str) -> dict[str, Any]:
        record = self._jobs.get(job_id)
        if record is None:
            raise ValueError("unknown job_id")
        return record.public()

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
        return self._work.get_or_compute(
            ("cleanup_preview", "project", _query_key(query)),
            lambda: _preview_payload(_project_cleanup_preview(query)),
            ttl_seconds=12,
            stale_ttl_seconds=12,
        )

    def session_cleanup_preview(self, query: dict[str, list[str]]) -> dict[str, Any]:
        return self._work.get_or_compute(
            ("cleanup_preview", "session", _query_key(query)),
            lambda: _preview_payload(_session_cleanup_preview(query)),
            ttl_seconds=12,
            stale_ttl_seconds=12,
        )

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
        self._invalidate_cached_data()
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
        self._invalidate_cached_data()
        return result

    def _projects_uncached(self, vendor: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if vendor:
            params["agent_vendor"] = vendor
        payload = self._source.call(
            "project.list",
            params,
            global_scope=True,
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
        payload = self._source.call(
            "project.sessions",
            params,
            global_scope=True,
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
        result = self._source.call(
            "project.sessions",
            request,
            global_scope=True,
        )
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


def _overview_attach_session_costs(session_items: list[dict[str, Any]]) -> None:
    """Attach per-graph estimated cost from ``graph.usage`` model rows.

    The overview rows represent the graphs returned by ``project.sessions``.
    Reading ``graph.usage`` keeps their cost scope aligned with their runtime and
    usage totals while reusing the request-attributed model rows already present
    in that response.
    """
    session_ids = [
        str(item.get("id") or "") for item in session_items if item.get("id")
    ]
    if not session_ids:
        return
    payloads = _graph_usage_batch(session_ids)
    for session_id, payload in payloads.items():
        if not payload:
            continue
        models = [
            model_usage_mod._priced_model(row)
            for row in payload.get("models") or []
            if isinstance(row, dict)
        ]
        total = sum(_number(row["estimated_cost_usd"]) for row in models)
        confidence = "missing_price"
        if any(
            (row.get("pricing") or {}).get("confidence") == "estimated"
            for row in models
        ):
            confidence = "estimated"
        for item in session_items:
            if str(item.get("id") or "") == session_id:
                item["cost_usd"] = round(float(total), 8)
                item["pricing_confidence"] = confidence
                break


def _graph_usage_batch(graph_ids: list[str]) -> dict[str, dict[str, Any]]:
    requests = [
        {
            "id": graph_id,
            "method": "graph.usage",
            "params": {"session_id": graph_id},
        }
        for graph_id in graph_ids
    ]
    payload = _ct_json(
        [
            "api",
            "batch",
            "--global-scope",
            "--requests",
            json.dumps(requests),
        ]
    )
    results: dict[str, dict[str, Any]] = {}
    for item in payload.get("items") or []:
        if not isinstance(item, dict) or not item.get("ok"):
            continue
        graph_id = item.get("id")
        result = item.get("result")
        if graph_id and isinstance(result, dict):
            results[str(graph_id)] = result
    return results


def _overview_activity(items: list[dict[str, Any]]) -> dict[str, Any]:
    runtime_totals = {
        "execution_seconds": 0,
        "wait_seconds": 0,
        "turns": 0,
        "tool_calls": 0,
        "failed_tool_calls": 0,
    }
    usage_totals = {
        "processed_tokens": 0,
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
        usage_totals["processed_tokens"] += int(_number(usage.get("processed_tokens")))
        cost_usd = item.get("cost_usd")
        if isinstance(cost_usd, int | float):
            usage_totals["cost_usd"] += float(cost_usd)
        if item.get("pricing_confidence") == "estimated":
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
                "processed_tokens": 0,
                "cost_usd": 0.0,
                "known_cost_count": 0,
            },
        )
        stats["count"] += 1
        stats["execution_seconds"] += _number(runtime.get("execution_seconds"))
        stats["processed_tokens"] += int(_number(usage.get("processed_tokens")))
        cost_usd = item.get("cost_usd")
        if isinstance(cost_usd, int | float):
            stats["cost_usd"] += float(cost_usd)
        if item.get("pricing_confidence") == "estimated":
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
            if int(_number((item.get("usage") or {}).get("processed_tokens"))) > 0
        ),
        key=lambda item: item["processed_tokens"],
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


def _api_result(
    method: str,
    params: dict[str, Any],
    *,
    global_scope: bool = False,
    ct_json: Callable[[list[str]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    args = ["api", "call", method, "--params", json.dumps(params)]
    if global_scope:
        args.append("--global-scope")
    payload = (ct_json or _ct_json)(args)
    if not payload.get("ok"):
        error = payload.get("error") or {}
        raise RuntimeError(
            str(error.get("message") or f"ct api request failed: {method}")
        )
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"ct api request returned invalid result: {method}")
    return result


def _dashboard_session_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("root_session_id"),
        "project": item.get("project"),
        "title": item.get("title"),
        "vendors": item.get("vendors") or [],
        "sessions": item.get("session_ids") or [],
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
        "processed_tokens": int(_number(usage.get("processed_tokens"))),
    }


def _number(value: Any) -> int | float:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int | float):
        return value
    return 0


def _project_cleanup_preview(query: dict[str, list[str]]) -> cleanup_mod.CleanupPreview:
    older_than = _first(query, "older_than")
    if older_than is None:
        older_than = f"{_int(query, 'since_days', 30)}d"
    return cleanup_mod.preview_project_cleanup(
        argparse.Namespace(
            older_than=older_than,
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


def _query_key(query: dict[str, list[str]]) -> str:
    normalized = {
        key: sorted(str(value) for value in values)
        for key, values in sorted(query.items())
    }
    return json.dumps(normalized, sort_keys=True)


def _project_cache_key(value: str) -> str:
    return hashlib.sha256(value.casefold().encode()).hexdigest()[:20]


def _ct_json(args: list[str]) -> dict[str, Any]:
    return _run_ct_json(args, timeout_seconds=30)


def _ct_json_expensive(args: list[str]) -> dict[str, Any]:
    return _run_ct_json(args, timeout_seconds=120)


def _run_ct_json(args: list[str], *, timeout_seconds: int) -> dict[str, Any]:
    ct = os.environ.get("CT_COMMAND") or shutil.which("ct")
    if not ct:
        raise RuntimeError(
            "ct executable not found; set CT_COMMAND to the ct command path"
        )
    command = [*shlex.split(ct), *args]
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            cwd=_repo_root(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"ct command timed out after {timeout_seconds}s: {' '.join(command)}"
        ) from exc
    if completed.returncode != 0:
        message = (
            completed.stderr.strip() or completed.stdout.strip() or "ct command failed"
        )
        raise RuntimeError(message)
    return json.loads(completed.stdout)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _session_analysis_operation_key(session_id: str, refresh: bool) -> str:
    refresh_key = "refresh" if refresh else "cached"
    return f"session-analysis:v5:{refresh_key}:{session_id}"


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
