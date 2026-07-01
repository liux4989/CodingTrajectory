from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    from .web_services import DashboardDataService
except ImportError:
    from web_services import DashboardDataService


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
    handler = _handler_for(config.static_dir, DashboardDataService())
    ThreadingHTTPServer.allow_reuse_address = True
    try:
        server = ThreadingHTTPServer((config.host, config.port), handler)
    except OSError as exc:
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
    static_dir: Path, service: DashboardDataService
) -> type[BaseHTTPRequestHandler]:
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
                    payload = service.overview()
                elif path == "/api/projects":
                    payload = service.projects(query)
                elif path == "/api/projects/detail":
                    payload = service.project_detail(query)
                elif path == "/api/sessions":
                    payload = service.sessions(query)
                elif path == "/api/sessions/timeline":
                    payload = service.session_timeline(query)
                elif path == "/api/sessions/context-window":
                    payload = service.context_window(query)
                elif path == "/api/model-usage":
                    payload = service.model_usage(query)
                elif path == "/api/error-collection":
                    payload = service.error_collection(query)
                elif path == "/api/vendors":
                    payload = service.vendors(query)
                elif path == "/api/cleanup/project/preview":
                    payload = service.project_cleanup_preview(query)
                elif path == "/api/cleanup/session/preview":
                    payload = service.session_cleanup_preview(query)
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
                return service.apply_project_cleanup(body)
            if path == "/api/cleanup/session/apply":
                return service.apply_session_cleanup(body)
            if path == "/api/sessions/analysis":
                return service.session_analysis(body)
            session_id = _session_analysis_id(path)
            if session_id:
                return service.session_analysis({**body, "session_id": session_id})
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
            content_type = (
                mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
            )
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
            data = json.dumps(
                {"error": {"message": message}}, ensure_ascii=False
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
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


if __name__ == "__main__":
    raise SystemExit(main())
