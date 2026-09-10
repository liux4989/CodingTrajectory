from __future__ import annotations

import argparse
import gzip
import json
import logging
import mimetypes
import re
import shutil
import subprocess
import sys
import time
import traceback
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from coding_trajectory import datahub as _core_datahub  # noqa: F401

import datahub_plugin.cli.code_time_cmd as code_time_mod
from datahub_plugin.api_models import serialize_api_response
from datahub_plugin.runtime.runtime import DatahubIncrementalRuntime
from datahub_plugin.serving.routes import (
    DATAHUB_ENDPOINT,
    DATAHUB_PROTOCOL,
    METHODS,
    METHODS_BY_NAME,
)

_FINGERPRINTED_ASSET = re.compile(r"-[A-Za-z0-9_-]{8,}\.[^.]+$")
_GZIP_CONTENT_TYPES = (
    "application/javascript",
    "application/json",
    "application/manifest+json",
    "application/wasm",
    "image/svg+xml",
    "text/",
)


_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200
_MAX_CURSOR_LENGTH = 4096
_LOGGER = logging.getLogger(__name__)


class DatahubBootstrapPending(RuntimeError):
    """A supported revisioned route is waiting for its first snapshot."""


@dataclass(frozen=True, slots=True)
class DatahubWebConfig:
    host: str
    port: int
    open_browser: bool
    static_dir: Path
    since_days: int | None = None


class DatahubHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    config = DatahubWebConfig(
        host=args.host,
        port=args.port,
        open_browser=args.open,
        static_dir=_static_dir(args.static_dir),
        since_days=args.since_days,
    )
    if (
        args.static_dir is None
        and not args.no_build
        and not _rebuild_if_stale(config.static_dir)
    ):
        return 1
    if not config.static_dir.is_dir():
        print(
            "error: datahub web assets were not found; run `bun install && bun run build` "
            "in packages/plugins/datahub/web",
            file=sys.stderr,
        )
        return 2
    return serve(config)


def serve(config: DatahubWebConfig) -> int:
    runtime_kwargs: dict[str, Any] = {"current_dir": _repo_root()}
    if config.since_days is not None:
        runtime_kwargs["since_days"] = config.since_days
    runtime = DatahubIncrementalRuntime(**runtime_kwargs)
    handler = _handler_for(config.static_dir, runtime)
    try:
        server = DatahubHTTPServer((config.host, config.port), handler)
    except OSError as exc:
        runtime.shutdown()
        print(
            f"error: could not bind to {config.host}:{config.port} ({exc}); "
            "stop the other process or use --port to pick a different port",
            file=sys.stderr,
        )
        return 1
    url = f"http://{config.host}:{server.server_port}"
    print(f"Datahub web running at {url}")
    if config.open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDatahub web stopped.")
    finally:
        incremental_runtime = getattr(handler, "datahub_runtime", None)
        if incremental_runtime is not None:
            incremental_runtime.shutdown()
        server.server_close()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ct plugin datahub web",
        description="Run the datahub web program.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--open", action="store_true", help="Open the datahub in a browser."
    )
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="Serve the current web build without rebuilding stale assets.",
    )
    parser.add_argument("--static-dir", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--since-days",
        type=int,
        default=None,
        help="Materialize the last N days of sessions (default: 7).",
    )
    return parser


def _static_dir(raw: str | None) -> Path:
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "web" / "dist"


_BUILD_INPUT_NAMES = (
    "index.html",
    "package.json",
    "bun.lock",
    "vite.config.ts",
    "tsconfig.json",
)


def _rebuild_if_stale(static_dir: Path) -> bool:
    """Rebuild the web bundle when its sources are newer than the output."""
    web_dir = static_dir.parent
    newest = 0.0
    for name in _BUILD_INPUT_NAMES:
        path = web_dir / name
        if path.is_file():
            newest = max(newest, path.stat().st_mtime)
    src_dir = web_dir / "src"
    if src_dir.is_dir():
        for path in src_dir.rglob("*"):
            if path.is_file():
                newest = max(newest, path.stat().st_mtime)
    marker = static_dir / "index.html"
    if newest == 0.0 or (marker.is_file() and marker.stat().st_mtime >= newest):
        return True
    command = (
        ["bun", "run", "build"] if shutil.which("bun") else ["npm", "run", "build"]
    )
    print(
        f"Datahub web assets are stale; rebuilding with `{' '.join(command)}` "
        "(skip with --no-build)..."
    )
    if subprocess.run(command, cwd=web_dir, check=False).returncode != 0:
        print("error: datahub web build failed", file=sys.stderr)
        return False
    return True


def _handler_for(
    static_dir: Path,
    runtime: DatahubIncrementalRuntime | None = None,
) -> type[BaseHTTPRequestHandler]:
    class DatahubRequestHandler(BaseHTTPRequestHandler):
        server_version = "CodingTrajectoryDatahub/0.1"
        datahub_runtime = runtime

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                self._json_error(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")
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
            if parsed.path != DATAHUB_ENDPOINT or parsed.query:
                self._json_error(HTTPStatus.NOT_FOUND, "not found")
                return
            try:
                body = self._read_json_body()
                payload, status = self._handle_query(body)
            except (TypeError, ValueError) as exc:
                self._protocol_error(
                    HTTPStatus.BAD_REQUEST,
                    body.get("id") if "body" in locals() else None,
                    body.get("method") if "body" in locals() else None,
                    "invalid_params",
                    str(exc),
                )
                return
            except RuntimeError as exc:
                self._protocol_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    body.get("id"),
                    body.get("method"),
                    "query_failed",
                    str(exc),
                )
                return
            self._json_response(payload, status=status)

        def log_message(self, format: str, *args: Any) -> None:
            print(f"{self.address_string()} - {format % args}", file=sys.stderr)

        def _handle_query(
            self, body: dict[str, Any]
        ) -> tuple[dict[str, Any], HTTPStatus]:
            if set(body) - {"protocol", "id", "method", "params"}:
                raise ValueError("request contains unknown fields")
            if body.get("protocol") != DATAHUB_PROTOCOL:
                raise ValueError(f"protocol must be {DATAHUB_PROTOCOL}")
            method_name = body.get("method")
            if not isinstance(method_name, str) or not method_name:
                raise ValueError("method is required")
            params = body.get("params")
            if not isinstance(params, dict):
                raise TypeError("params must be an object")
            if method_name == "datahub.capabilities":
                return self._protocol_success(
                    body.get("id"),
                    method_name,
                    {
                        "supported": [method.name for method in METHODS],
                        "unsupported": [],
                    },
                ), HTTPStatus.OK
            method = METHODS_BY_NAME.get(method_name)
            if method is None:
                return self._protocol_unavailable(
                    body.get("id"), method_name, "unsupported"
                ), HTTPStatus.OK
            unknown = set(params) - set(method.params)
            if unknown:
                raise ValueError(f"unknown params: {', '.join(sorted(unknown))}")
            query = _query_values(params)
            started = time.perf_counter()
            try:
                endpoint = getattr(self, f"_route_{method.handler}")
                if method.handler == "request_refresh":
                    payload, status = endpoint(query)
                else:
                    _bounded_page_size(query)
                    _cursor(query)
                    payload = endpoint(query)
                    status = HTTPStatus.OK
                payload = serialize_api_response(method.handler, payload)
                return self._protocol_success(
                    body.get("id"), method_name, payload
                ), status
            except DatahubBootstrapPending as exc:
                return self._protocol_unavailable(
                    body.get("id"), method_name, "not_materialized", str(exc)
                ), HTTPStatus.SERVICE_UNAVAILABLE
            except (ValueError, RuntimeError):
                raise
            except Exception:  # noqa: BLE001 - API boundary preserves JSON errors
                traceback.print_exc()
                raise RuntimeError("unexpected Datahub API error") from None
            finally:
                _LOGGER.debug(
                    "datahub method=%s duration_ms=%.3f",
                    method.handler,
                    (time.perf_counter() - started) * 1000,
                )

        def _route_snapshot(self, query: dict[str, list[str]]) -> dict[str, Any]:
            return (
                runtime.snapshot() if runtime is not None else _unavailable_snapshot()
            )

        def _route_changes(self, query: dict[str, list[str]]) -> dict[str, Any]:
            after_revision = _bounded_nonnegative_int(query, "after_revision", 0)
            return (
                runtime.changes(after_revision)
                if runtime is not None
                else _unavailable_changes(after_revision)
            )

        def _route_overview(self, query: dict[str, list[str]]) -> dict[str, Any]:
            return self._revisioned(
                lambda: runtime.overview(since_days=self._window_days(query))
            )

        def _route_today(self, query: dict[str, list[str]]) -> dict[str, Any]:
            return self._revisioned(runtime.today)

        def _route_projects(self, query: dict[str, list[str]]) -> dict[str, Any]:
            return self._revisioned(
                lambda: runtime.projects(
                    agent_vendor=_first(query, "agent_vendor"),
                    limit=_bounded_page_size(query),
                    cursor=_cursor(query),
                )
            )

        def _route_project_detail(self, query: dict[str, list[str]]) -> dict[str, Any]:
            return self._revisioned(
                lambda: runtime.project_detail(
                    project_name=_required(query, "project_name"),
                    since_days=self._window_days(query),
                    limit=_bounded_page_size(query),
                    cursor=_cursor(query),
                )
            )

        def _route_sessions(self, query: dict[str, list[str]]) -> dict[str, Any]:
            return self._revisioned(
                lambda: runtime.sessions(
                    since_days=self._window_days(query),
                    project_name=_first(query, "project_name"),
                    agent_vendor=_first(query, "agent_vendor"),
                    limit=_bounded_page_size(query),
                    cursor=_cursor(query),
                )
            )

        def _route_session_timeline(
            self, query: dict[str, list[str]]
        ) -> dict[str, Any]:
            return self._revisioned(
                lambda: runtime.session_timeline(
                    since_days=self._window_days(query),
                    limit=_bounded_page_size(query),
                    cursor=_cursor(query),
                )
            )

        def _route_context_window(self, query: dict[str, list[str]]) -> dict[str, Any]:
            return self._revisioned(
                lambda: runtime.context_window(
                    session_id=_required(query, "session_id"),
                    turn_id=_first(query, "turn_id"),
                )
            )

        def _route_graph_detail(self, query: dict[str, list[str]]) -> dict[str, Any]:
            return self._revisioned(
                lambda: runtime.graph_detail(session_id=_required(query, "session_id"))
            )

        def _route_session_tree(self, query: dict[str, list[str]]) -> dict[str, Any]:
            return self._revisioned(
                lambda: runtime.session_tree(session_id=_required(query, "session_id"))
            )

        def _route_session_evidence_timeline(
            self, query: dict[str, list[str]]
        ) -> dict[str, Any]:
            return self._revisioned(
                lambda: runtime.session_evidence_timeline(
                    session_id=_required(query, "session_id")
                )
            )

        def _route_session_event_details(
            self, query: dict[str, list[str]]
        ) -> dict[str, Any]:
            return self._revisioned(
                lambda: runtime.session_event_details(
                    event_ids=_required(query, "event_ids").split(","),
                    turn_id=_first(query, "turn_id"),
                    event_type=_first(query, "type"),
                )
            )

        def _route_session_item_details(
            self, query: dict[str, list[str]]
        ) -> dict[str, Any]:
            return self._revisioned(
                lambda: runtime.session_item_details(
                    item_ids=_required(query, "item_ids").split(","),
                    include_content=_first(query, "include_content")
                    in {"1", "true", "yes"},
                    turn_id=_first(query, "turn_id"),
                )
            )

        def _route_model_usage(self, query: dict[str, list[str]]) -> dict[str, Any]:
            return self._revisioned(
                lambda: runtime.model_usage(
                    since_days=self._window_days(query),
                    project_name=_first(query, "project_name"),
                    model_key=_first(query, "model_key"),
                    detail=_first(query, "detail") or "both",
                    limit=_bounded_page_size(query),
                    cursor=_cursor(query),
                    revision=_optional_revision(query),
                )
            )

        def _route_token_efficiency_project(
            self, query: dict[str, list[str]]
        ) -> dict[str, Any]:
            return self._revisioned(
                lambda: runtime.token_efficiency_project(
                    project_name=_required(query, "project_name"),
                    since_days=self._window_days(query),
                    limit=_bounded_page_size(query),
                    cursor=_cursor(query),
                    detail=_first(query, "detail"),
                    grain=_first(query, "grain"),
                )
            )

        def _route_code_time_report(
            self, query: dict[str, list[str]]
        ) -> dict[str, Any]:
            window = _code_time_window(query)
            return self._revisioned(
                lambda: runtime.code_time_report(
                    window=window,
                    project_name=_first(query, "project"),
                    agent_vendor=_first(query, "agent_vendor"),
                )
            )

        def _route_code_time_forecasts(
            self, query: dict[str, list[str]]
        ) -> dict[str, Any]:
            return _code_time_forecasts_payload(query)

        def _route_code_time_calibration(
            self, query: dict[str, list[str]]
        ) -> dict[str, Any]:
            return _code_time_calibration_payload(query)


        def _window_days(self, query: dict[str, list[str]]) -> int:
            since_days = _bounded_positive_int(query, "since_days", 7)
            if runtime is not None and since_days != runtime.since_days:
                raise ValueError(
                    f"only the last {runtime.since_days} days are available"
                )
            return since_days

        def _revisioned(
            self, produce: Callable[[], dict[str, Any] | None]
        ) -> dict[str, Any]:
            if runtime is None:
                raise ValueError("revisioned datahub data is unavailable")
            payload = produce()
            if payload is None:
                raise DatahubBootstrapPending(
                    "datahub read model is not available yet; retry shortly"
                )
            return payload

        def _route_request_refresh(
            self, body: dict[str, Any]
        ) -> tuple[dict[str, Any], HTTPStatus]:
            payload: dict[str, Any] = {"status": "refreshed"}
            if runtime is not None:
                payload["incremental"] = runtime.request_refresh()
            return payload, HTTPStatus.OK

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
            if length > 1_000_000:
                raise ValueError("request body exceeds 1 MB")
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError(  # noqa: TRY004 - mapped to HTTP 400 above
                    "request body must be a JSON object"
                )
            return value

        def _json_response(
            self, payload: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK
        ) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
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

        def _protocol_success(
            self, request_id: Any, method: str, data: Any
        ) -> dict[str, Any]:
            return {
                "protocol": DATAHUB_PROTOCOL,
                "id": request_id,
                "method": method,
                "ok": True,
                "data": data,
                "availability": {"state": "complete", "missing": []},
                "error": None,
            }

        def _protocol_unavailable(
            self,
            request_id: Any,
            method: Any,
            reason: str,
            message: str | None = None,
        ) -> dict[str, Any]:
            return {
                "protocol": DATAHUB_PROTOCOL,
                "id": request_id,
                "method": method,
                "ok": False,
                "data": None,
                "availability": {
                    "state": "unsupported"
                    if reason == "unsupported"
                    else "unavailable",
                    "missing": [{"field": "$", "reason": reason}],
                },
                "error": {
                    "code": reason,
                    "message": message or "Datahub method is unavailable",
                },
            }

        def _protocol_error(
            self,
            status: HTTPStatus,
            request_id: Any,
            method: Any,
            code: str,
            message: str,
        ) -> None:
            self._json_response(
                self._protocol_unavailable(request_id, method, code, message),
                status=status,
            )

    return DatahubRequestHandler


def _query_values(params: dict[str, Any]) -> dict[str, list[str]]:
    query: dict[str, list[str]] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            query[key] = ["true" if value else "false"]
        elif isinstance(value, list):
            query[key] = [",".join(str(item) for item in value)]
        elif isinstance(value, (str, int, float)):
            query[key] = [str(value)]
        else:
            raise TypeError(f"{key} must be a scalar, array, or null")
    return query


def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key) or []
    value = values[0].strip() if values else ""
    return value or None


def _required(query: dict[str, list[str]], key: str) -> str:
    value = _first(query, key)
    if value is None:
        raise ValueError(f"{key} is required")
    return value


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


# ---------------------------------------------------------------------------
# code-time — computed on demand from the service API, not the revisioned store
# ---------------------------------------------------------------------------


def _code_time_window(
    query: dict[str, list[str]],
) -> Literal["today", "72h", "7d", "30d"]:
    window = _first(query, "window") or "today"
    if window not in code_time_mod.WINDOW_SINCE_DAYS:
        choices = ", ".join(sorted(code_time_mod.WINDOW_SINCE_DAYS))
        raise ValueError(f"window must be one of: {choices}")
    return window  # type: ignore[return-value]


def _code_time_forecasts_payload(query: dict[str, list[str]]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for query_key, param_key in (
        ("kind", "forecast_kind"),
        ("project", "project_name"),
        ("target_harness_name", "target_harness_name"),
        ("status", "status"),
    ):
        value = _first(query, query_key)
        if value:
            params[param_key] = value
    params["limit"] = _bounded_positive_int(query, "limit", 50, maximum=500)
    return _estimate_call("estimate.list", params)


def _code_time_calibration_payload(query: dict[str, list[str]]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for query_key, param_key in (
        ("kind", "forecast_kind"),
        ("project", "project_name"),
        ("target_harness_name", "target_harness_name"),
        ("target_model", "target_model"),
        ("estimator_model", "estimator_model"),
    ):
        value = _first(query, query_key)
        if value:
            params[param_key] = value
    return _estimate_call("estimate.calibration", params)


def _estimate_call(method: str, params: dict[str, Any]) -> dict[str, Any]:
    from coding_trajectory.runtime import PluginApiError, default_plugin_client

    try:
        result = default_plugin_client().call(method, params)
    except PluginApiError as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(result, dict):
        raise RuntimeError(  # noqa: TRY004 - upstream wire contract violation
            f"ct api call {method} returned a non-object result"
        )
    return result


def _unavailable_snapshot() -> dict[str, Any]:
    return {
        "revision": 0,
        "generated_at": datetime.now(UTC).isoformat(),
        "transport": None,
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
        "transport": snapshot["transport"],
        "freshness": snapshot["freshness"],
        "catching_up": False,
        "source_status": snapshot["source_status"],
    }


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[5]


if __name__ == "__main__":
    raise SystemExit(main())
