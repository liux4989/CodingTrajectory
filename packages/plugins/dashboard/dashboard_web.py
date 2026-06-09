from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shlex
import shutil
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import cleanup as cleanup_mod


@dataclass(frozen=True, slots=True)
class DashboardWebConfig:
    host: str
    port: int
    open_browser: bool
    static_dir: Path


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    config = DashboardWebConfig(
        host=args.host,
        port=args.port,
        open_browser=args.open,
        static_dir=_static_dir(args.static_dir),
    )
    if not config.static_dir.is_dir():
        print(
            "error: dashboard web assets were not found; run `npm install && npm run build` "
            "in packages/plugins/dashboard/web",
            file=sys.stderr,
        )
        return 2
    return serve(config)


def serve(config: DashboardWebConfig) -> int:
    handler = _handler_for(config.static_dir)
    server = ThreadingHTTPServer((config.host, config.port), handler)
    url = f"http://{config.host}:{server.server_port}"
    print(f"Dashboard web running at {url}")
    if config.open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard web stopped.")
    finally:
        server.server_close()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ct plugin dashboard web",
        description="Run the dashboard web program.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="Open the dashboard in a browser.")
    parser.add_argument("--static-dir", default=None, help=argparse.SUPPRESS)
    return parser


def _static_dir(raw: str | None) -> Path:
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parent / "web" / "dist"


def _handler_for(static_dir: Path) -> type[BaseHTTPRequestHandler]:
    class DashboardRequestHandler(BaseHTTPRequestHandler):
        server_version = "CodingTrajectoryDashboard/0.1"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                self._handle_api_get(parsed.path, parse_qs(parsed.query))
                return
            self._serve_static(parsed.path, include_body=True)

        def do_HEAD(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                self._json_error(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")
                return
            self._serve_static(parsed.path, include_body=False)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/api/"):
                self._json_error(HTTPStatus.NOT_FOUND, "not found")
                return
            try:
                body = self._read_json_body()
                payload = self._handle_api_post(parsed.path, body)
            except ValueError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            except RuntimeError as exc:
                self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return
            self._json_response(payload)

        def log_message(self, format: str, *args: Any) -> None:
            print(f"{self.address_string()} - {format % args}", file=sys.stderr)

        def _handle_api_get(self, path: str, query: dict[str, list[str]]) -> None:
            try:
                if path == "/api/overview":
                    payload = _overview_payload()
                elif path == "/api/projects":
                    payload = _project_payload(query)
                elif path == "/api/projects/detail":
                    payload = _project_detail_payload(query)
                elif path == "/api/sessions":
                    payload = _session_payload(query)
                elif path == "/api/sessions/timeline":
                    payload = _session_timeline_payload(query)
                elif path == "/api/vendors":
                    payload = _vendor_payload(query)
                elif path == "/api/cleanup/project/preview":
                    payload = _preview_payload(_project_cleanup_preview(query))
                elif path == "/api/cleanup/session/preview":
                    payload = _preview_payload(_session_cleanup_preview(query))
                else:
                    self._json_error(HTTPStatus.NOT_FOUND, "not found")
                    return
            except RuntimeError as exc:
                self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return
            except ValueError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self._json_response(payload)

        def _handle_api_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
            if path == "/api/cleanup/project/apply":
                return _apply_project_cleanup(body)
            if path == "/api/cleanup/session/apply":
                return _apply_session_cleanup(body)
            raise ValueError("unknown api endpoint")

        def _serve_static(self, raw_path: str, *, include_body: bool) -> None:
            relative = raw_path.lstrip("/")
            target = static_dir / (relative or "index.html")
            try:
                resolved = target.resolve()
                resolved.relative_to(static_dir.resolve())
            except ValueError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not resolved.is_file():
                resolved = static_dir / "index.html"
            if not resolved.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            content_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
            data = resolved.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if include_body:
                self.wfile.write(data)

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("request body must be a JSON object")
            return value

        def _json_response(self, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json_error(self, status: HTTPStatus, message: str) -> None:
            data = json.dumps({"error": {"message": message}}, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return DashboardRequestHandler


def _overview_payload() -> dict[str, Any]:
    projects = _project_payload({})
    sessions = _session_payload({})
    project_cleanup = _preview_payload(_project_cleanup_preview({}))
    session_cleanup = _preview_payload(_session_cleanup_preview({}))
    vendors = _vendor_payload({})
    project_items = projects.get("items", [])
    vendor_counts: dict[str, int] = {}
    for item in project_items:
        for vendor in item.get("vendors") or []:
            vendor_counts[vendor] = vendor_counts.get(vendor, 0) + 1
    return {
        "projects": {"count": len(project_items), "vendors": vendor_counts},
        "sessions": {"count": len(sessions.get("items", []))},
        "vendors": vendors.get("vendors", {}),
        "cleanup": {
            "projects": project_cleanup["summary"],
            "sessions": session_cleanup["summary"],
        },
    }


def _project_detail_payload(query: dict[str, list[str]]) -> dict[str, Any]:
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
        "path": meta.get("p"),
        "vendors": meta.get("v") or [],
        "sessions": sessions.get("items") or [],
        "session_count": len(sessions.get("items") or []),
    }


def _session_timeline_payload(query: dict[str, list[str]]) -> dict[str, Any]:
    params: dict[str, Any] = {
        "since_days": None if _bool(query, "all_time") else _int(query, "since_days", 30)
    }
    project_name = _first(query, "project_name")
    vendor = _first(query, "agent_vendor")
    if project_name:
        params["project_name"] = project_name
    if vendor:
        params["agent_vendor"] = vendor
    payload = _ct_json(["project", "sessions", "--params", json.dumps(params), "--output", "json"])
    items = payload.get("items") or []
    by_date: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        date_key = str(item.get("date") or item.get("created_at") or "unknown")[:10]
        by_date.setdefault(date_key, []).append(
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "v": item.get("v") or [],
            }
        )
    timeline = [
        {"date": date, "count": len(entries), "sessions": entries}
        for date, entries in sorted(by_date.items(), reverse=True)
    ]
    return {"timeline": timeline, "total": len(items)}


def _vendor_payload(query: dict[str, list[str]]) -> dict[str, Any]:
    projects = _ct_json(["project", "list", "--params", json.dumps({}), "--output", "json"])
    items = projects.get("items") or {}
    vendor_stats: dict[str, dict[str, Any]] = {}
    for name, meta in items.items():
        for vendor in meta.get("v") or []:
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


def _project_payload(query: dict[str, list[str]]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    vendor = _first(query, "agent_vendor")
    if vendor:
        params["agent_vendor"] = vendor
    payload = _ct_json(["project", "list", "--params", json.dumps(params), "--output", "json"])
    items = payload.get("items") or {}
    return {
        "items": [
            {
                "name": name,
                "path": item.get("p"),
                "vendors": item.get("v") or [],
            }
            for name, item in sorted(items.items())
        ]
    }


def _session_payload(query: dict[str, list[str]]) -> dict[str, Any]:
    params: dict[str, Any] = {
        "since_days": None if _bool(query, "all_time") else _int(query, "since_days", 30)
    }
    project_name = _first(query, "project_name")
    vendor = _first(query, "agent_vendor")
    if project_name:
        params["project_name"] = project_name
    if vendor:
        params["agent_vendor"] = vendor
    payload = _ct_json(["project", "sessions", "--params", json.dumps(params), "--output", "json"])
    return {"items": payload.get("items") or []}


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


def _apply_project_cleanup(body: dict[str, Any]) -> dict[str, Any]:
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
    return cleanup_mod.apply_project_selection(
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


def _apply_session_cleanup(body: dict[str, Any]) -> dict[str, Any]:
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
    return cleanup_mod.apply_session_selection(
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


if __name__ == "__main__":
    raise SystemExit(main())
