from __future__ import annotations

import argparse
import gzip
import json
import mimetypes
import re
import sys
import webbrowser
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse


_FINGERPRINTED_ASSET = re.compile(r"-[A-Za-z0-9_-]{8,}\.[^.]+$")
_GZIP_CONTENT_TYPES = (
    "application/javascript",
    "application/json",
    "application/manifest+json",
    "application/wasm",
    "image/svg+xml",
    "text/",
)

try:
    from .incremental_runtime import DashboardIncrementalRuntime
    from .web_services import DashboardDataService
except ImportError:
    from incremental_runtime import DashboardIncrementalRuntime
    from web_services import DashboardDataService


_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200
_MAX_CURSOR_LENGTH = 4096


class DashboardBootstrapPending(RuntimeError):
    """A supported revisioned route is waiting for its first snapshot."""


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
            "error: dashboard web assets were not found; run `bun install && bun run build` "
            "in packages/plugins/dashboard/web",
            file=sys.stderr,
        )
        return 2
    return serve(config)


def serve(config: DashboardWebConfig) -> int:
    service = DashboardDataService()
    runtime = DashboardIncrementalRuntime(current_dir=_repo_root())
    handler = _handler_for(config.static_dir, service, runtime)
    ThreadingHTTPServer.allow_reuse_address = True
    try:
        server = ThreadingHTTPServer((config.host, config.port), handler)
    except OSError as exc:
        runtime.shutdown()
        service.shutdown()
        print(
            f"error: could not bind to {config.host}:{config.port} ({exc}); "
            "stop the other process or use --port to pick a different port",
            file=sys.stderr,
        )
        return 1
    url = f"http://{config.host}:{server.server_port}"
    print(f"Dashboard web running at {url}")
    if config.open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard web stopped.")
    finally:
        incremental_runtime = getattr(handler, "dashboard_runtime", None)
        if incremental_runtime is not None:
            incremental_runtime.shutdown()
        service = getattr(handler, "dashboard_service", None)
        if service is not None:
            service.shutdown()
        server.server_close()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ct plugin dashboard web",
        description="Run the dashboard web program.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--open", action="store_true", help="Open the dashboard in a browser."
    )
    parser.add_argument("--static-dir", default=None, help=argparse.SUPPRESS)
    return parser


def _static_dir(raw: str | None) -> Path:
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parent / "web" / "dist"


def _handler_for(
    static_dir: Path,
    service: DashboardDataService,
    runtime: DashboardIncrementalRuntime | None = None,
) -> type[BaseHTTPRequestHandler]:
    class DashboardRequestHandler(BaseHTTPRequestHandler):
        server_version = "CodingTrajectoryDashboard/0.1"
        dashboard_service = service
        dashboard_runtime = runtime

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
                payload, status = self._handle_api_post(parsed.path, body)
            except ValueError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            except RuntimeError as exc:
                self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return
            self._json_response(payload, status=status)

        def log_message(self, format: str, *args: Any) -> None:
            print(f"{self.address_string()} - {format % args}", file=sys.stderr)

        def _handle_api_get(self, path: str, query: dict[str, list[str]]) -> None:
            try:
                self._used_legacy_fallback = False
                limit = _bounded_page_size(query)
                cursor = _cursor(query)
                if path == "/api/dashboard/snapshot":
                    payload = (
                        runtime.snapshot()
                        if runtime is not None
                        else _unavailable_snapshot()
                    )
                elif path == "/api/dashboard/changes":
                    after_revision = _bounded_nonnegative_int(
                        query, "after_revision", 0
                    )
                    payload = (
                        runtime.changes(after_revision)
                        if runtime is not None
                        else _unavailable_changes(after_revision)
                    )
                elif path == "/api/overview":
                    since_days = _bounded_positive_int(query, "since_days", 7)
                    incremental = (
                        runtime.overview(since_days=since_days)
                        if runtime is not None
                        else None
                    )
                    payload = self._with_legacy_fallback(
                        incremental,
                        lambda: service.overview(query),
                        revisioned_scope=(
                            runtime is not None and since_days == runtime.since_days
                        ),
                    )
                elif path == "/api/projects":
                    incremental = (
                        runtime.projects(
                            agent_vendor=_first(query, "agent_vendor"),
                            limit=limit,
                            cursor=cursor,
                        )
                        if runtime is not None
                        else None
                    )
                    payload = self._with_legacy_fallback(
                        incremental,
                        lambda: service.projects(query),
                        revisioned_scope=runtime is not None,
                    )
                elif path == "/api/projects/detail":
                    project_name = _required(query, "project_name")
                    since_days_raw = _first(query, "since_days")
                    since_days = _bounded_positive_int(query, "since_days", 7)
                    incremental = (
                        runtime.project_detail(
                            project_name=project_name,
                            since_days=since_days,
                            limit=limit,
                            cursor=cursor,
                        )
                        if runtime is not None and since_days_raw is not None
                        else None
                    )
                    payload = self._with_legacy_fallback(
                        incremental,
                        lambda: service.project_detail(query),
                        revisioned_scope=(
                            runtime is not None
                            and since_days_raw is not None
                            and since_days == runtime.since_days
                        ),
                    )
                elif path == "/api/sessions":
                    since_days = _bounded_positive_int(query, "since_days", 30)
                    incremental = (
                        runtime.sessions(
                            since_days=since_days,
                            project_name=_first(query, "project_name"),
                            agent_vendor=_first(query, "agent_vendor"),
                            limit=limit,
                            cursor=cursor,
                        )
                        if runtime is not None and not _truthy(query, "all_time")
                        else None
                    )
                    payload = self._with_legacy_fallback(
                        incremental,
                        lambda: service.sessions(query),
                        revisioned_scope=(
                            runtime is not None
                            and since_days == runtime.since_days
                            and not _truthy(query, "all_time")
                        ),
                    )
                elif path == "/api/sessions/timeline":
                    since_days = _bounded_positive_int(query, "since_days", 30)
                    incremental = (
                        runtime.session_timeline(
                            since_days=since_days,
                            limit=limit,
                            cursor=cursor,
                        )
                        if runtime is not None and not _truthy(query, "all_time")
                        else None
                    )
                    payload = self._with_legacy_fallback(
                        incremental,
                        lambda: service.session_timeline(query),
                        revisioned_scope=(
                            runtime is not None
                            and since_days == runtime.since_days
                            and not _truthy(query, "all_time")
                        ),
                    )
                elif path == "/api/sessions/context-window":
                    incremental = (
                        runtime.context_window(
                            session_id=_required(query, "session_id"),
                            turn_id=_first(query, "turn_id"),
                        )
                        if runtime is not None
                        else None
                    )
                    payload = self._with_legacy_fallback(
                        incremental,
                        lambda: service.context_window(query),
                        revisioned_scope=runtime is not None,
                    )
                elif path == "/api/model-usage":
                    since_days = _bounded_positive_int(query, "since_days", 7)
                    incremental = (
                        runtime.model_usage(
                            since_days=since_days,
                            project_name=_first(query, "project_name"),
                            model_key=_first(query, "model_key"),
                            detail=_first(query, "detail") or "both",
                            limit=limit,
                            cursor=cursor,
                            revision=_optional_revision(query),
                        )
                        if runtime is not None
                        else None
                    )
                    payload = self._with_legacy_fallback(
                        incremental,
                        lambda: service.model_usage(query),
                        revisioned_scope=(
                            runtime is not None and since_days == runtime.since_days
                        ),
                    )
                elif path == "/api/token-efficiency":
                    since_days = min(_bounded_positive_int(query, "since_days", 7), 30)
                    incremental = (
                        runtime.token_efficiency_index(
                            since_days=since_days,
                            limit=limit,
                            cursor=cursor,
                        )
                        if runtime is not None
                        else None
                    )
                    payload = self._with_legacy_fallback(
                        incremental,
                        lambda: service.token_efficiency_index(query),
                        revisioned_scope=(
                            runtime is not None and since_days == runtime.since_days
                        ),
                    )
                elif path == "/api/token-efficiency/project":
                    since_days = min(_bounded_positive_int(query, "since_days", 7), 30)
                    detail = _first(query, "detail")
                    grain = _first(query, "grain")
                    incremental = (
                        runtime.token_efficiency_project(
                            project_name=_required(query, "project_name"),
                            since_days=since_days,
                            limit=limit,
                            cursor=cursor,
                            detail=detail,
                            grain=grain,
                        )
                        if runtime is not None
                        else None
                    )
                    payload = self._with_legacy_fallback(
                        incremental,
                        lambda: service.token_efficiency_project(query),
                        revisioned_scope=(
                            runtime is not None and since_days == runtime.since_days
                        ),
                    )
                elif path == "/api/cache-breaks":
                    since_days = _bounded_positive_int(query, "since_days", 7)
                    incremental = (
                        runtime.cache_breaks(
                            since_days=since_days,
                            project_name=_first(query, "project_name"),
                            limit=limit,
                            cursor=cursor,
                        )
                        if runtime is not None
                        else None
                    )
                    payload = self._with_legacy_fallback(
                        incremental,
                        lambda: service.cache_breaks(query),
                        revisioned_scope=(
                            runtime is not None and since_days == runtime.since_days
                        ),
                    )
                elif path == "/api/diagnostics/cache":
                    payload = service.cache_metrics()
                elif path == "/api/vendors":
                    payload = service.vendors(query)
                elif path.startswith("/api/jobs/"):
                    self._handle_job_get(path)
                    return
                else:
                    self._json_error(HTTPStatus.NOT_FOUND, "not found")
                    return
            except DashboardBootstrapPending as exc:
                self._json_error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
                return
            except RuntimeError as exc:
                self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return
            except ValueError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self._json_response(payload)

        def _with_legacy_fallback(
            self,
            incremental: dict[str, Any] | None,
            fallback: Callable[[], dict[str, Any]],
            *,
            revisioned_scope: bool = False,
        ) -> dict[str, Any]:
            if incremental is not None:
                return incremental
            if revisioned_scope and runtime is not None and not runtime.is_ready():
                raise DashboardBootstrapPending(
                    "dashboard read models are catching up; retry shortly"
                )
            self._used_legacy_fallback = True
            return fallback()

        def _handle_api_post(
            self, path: str, body: dict[str, Any]
        ) -> tuple[dict[str, Any], HTTPStatus]:
            if path == "/api/refresh":
                payload = service.refresh()
                if runtime is not None:
                    payload["incremental"] = runtime.request_refresh()
                return payload, HTTPStatus.OK
            if path == "/api/token-efficiency":
                return (
                    service.start_token_efficiency_index(body),
                    HTTPStatus.ACCEPTED,
                )
            if path == "/api/token-efficiency/project":
                return (
                    service.start_token_efficiency_project(body),
                    HTTPStatus.ACCEPTED,
                )
            if path == "/api/sessions/analysis":
                return service.session_analysis(body), HTTPStatus.ACCEPTED
            session_id = _session_analysis_id(path)
            if session_id:
                return (
                    service.session_analysis({**body, "session_id": session_id}),
                    HTTPStatus.ACCEPTED,
                )
            raise ValueError("unknown api endpoint")

        def _handle_job_get(self, path: str) -> None:
            job_id = path[len("/api/jobs/") :].strip("/")
            if not job_id:
                self._json_error(HTTPStatus.NOT_FOUND, "not found")
                return
            try:
                payload = service.job_status(job_id)
            except ValueError as exc:
                self._json_error(HTTPStatus.NOT_FOUND, str(exc))
                return
            self._json_response(payload)

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
            content_type = (
                mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
            )
            data = resolved.read_bytes()
            is_html = resolved.name == "index.html"
            is_fingerprinted_asset = (
                resolved.parent.name == "assets"
                and _FINGERPRINTED_ASSET.search(resolved.name) is not None
            )
            accepts_gzip = "gzip" in self.headers.get("Accept-Encoding", "").lower()
            can_gzip = content_type.startswith(_GZIP_CONTENT_TYPES)
            use_gzip = accepts_gzip and can_gzip and len(data) >= 512
            if use_gzip:
                data = gzip.compress(data)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header(
                "Cache-Control",
                "no-cache"
                if is_html
                else (
                    "public, max-age=31536000, immutable"
                    if is_fingerprinted_asset
                    else "public, max-age=3600"
                ),
            )
            if can_gzip:
                self.send_header("Vary", "Accept-Encoding")
            if use_gzip:
                self.send_header("Content-Encoding", "gzip")
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

        def _json_response(
            self, payload: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK
        ) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            if getattr(self, "_used_legacy_fallback", False):
                self.send_header("X-Dashboard-Delivery", "legacy-fallback")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json_error(self, status: HTTPStatus, message: str) -> None:
            data = json.dumps(
                {"error": {"message": message}}, ensure_ascii=False
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return DashboardRequestHandler


def _session_analysis_id(path: str) -> str | None:
    prefix = "/api/sessions/"
    suffix = "/analysis"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    session_id = path[len(prefix) : -len(suffix)].strip("/")
    return session_id or None


def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key) or []
    value = values[0].strip() if values else ""
    return value or None


def _required(query: dict[str, list[str]], key: str) -> str:
    value = _first(query, key)
    if value is None:
        raise ValueError(f"{key} is required")
    return value


def _truthy(query: dict[str, list[str]], key: str) -> bool:
    value = _first(query, key)
    return value is not None and value.lower() in {"1", "true", "yes", "on"}


def _bounded_page_size(query: dict[str, list[str]]) -> int:
    return _bounded_positive_int(
        query,
        "limit",
        _DEFAULT_PAGE_SIZE,
        maximum=_MAX_PAGE_SIZE,
    )


def _bounded_positive_int(
    query: dict[str, list[str]],
    key: str,
    default: int,
    *,
    maximum: int = 3650,
) -> int:
    value = _first(query, key)
    if value is None:
        return default
    parsed = int(value)
    if not 1 <= parsed <= maximum:
        raise ValueError(f"{key} must be between 1 and {maximum}")
    return parsed


def _bounded_nonnegative_int(
    query: dict[str, list[str]], key: str, default: int
) -> int:
    value = _first(query, key)
    if value is None:
        return default
    parsed = int(value)
    if not 0 <= parsed <= 9_223_372_036_854_775_807:
        raise ValueError(f"{key} must be a non-negative integer")
    return parsed


def _cursor(query: dict[str, list[str]]) -> str | None:
    value = _first(query, "cursor")
    if value is not None and len(value) > _MAX_CURSOR_LENGTH:
        raise ValueError("cursor is too long")
    return value


def _optional_revision(query: dict[str, list[str]]) -> int | None:
    if _first(query, "revision") is None:
        return None
    return _bounded_nonnegative_int(query, "revision", 0)


def _unavailable_snapshot() -> dict[str, Any]:
    return {
        "revision": 0,
        "generated_at": datetime.now(UTC).isoformat(),
        "freshness": {"last_refresh_at": None, "lag_seconds": None},
        "catching_up": False,
        "source_status": {
            "ready": 0,
            "ingesting": 0,
            "failed": 0,
            "incomplete": 0,
        },
        "minimum_available_revision": 0,
        "bootstrap": {
            "ready": False,
            "scan_started_at": None,
            "scan_finished_at": None,
            "error": "incremental runtime unavailable",
            "last_result": None,
        },
    }


def _unavailable_changes(after_revision: int) -> dict[str, Any]:
    snapshot = _unavailable_snapshot()
    return {
        "from_revision": after_revision,
        "to_revision": after_revision,
        "reset_required": False,
        "upserts": [],
        "deletions": [],
        "invalidations": [],
        "freshness": snapshot["freshness"],
        "catching_up": False,
        "source_status": snapshot["source_status"],
    }


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


if __name__ == "__main__":
    raise SystemExit(main())
