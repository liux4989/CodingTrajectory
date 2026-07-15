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

from pydantic import ValidationError

from metrics_service import MetricsService


@dataclass(frozen=True, slots=True)
class MetricsWebConfig:
    host: str
    port: int
    open_browser: bool
    static_dir: Path


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    config = MetricsWebConfig(
        host=args.host,
        port=args.port,
        open_browser=args.open,
        static_dir=_static_dir(args.static_dir),
    )
    if not config.static_dir.is_dir():
        print(
            "error: metrics web assets were not found; run `bun install && bun run build` "
            "in packages/plugins/metrics/web",
            file=sys.stderr,
        )
        return 2
    return serve(config)


def serve(config: MetricsWebConfig) -> int:
    handler = _handler_for(config.static_dir, MetricsService())
    ThreadingHTTPServer.allow_reuse_address = True
    try:
        server = ThreadingHTTPServer((config.host, config.port), handler)
    except OSError as exc:
        print(
            f"error: could not bind to {config.host}:{config.port} ({exc}); "
            "stop the other process or choose another port",
            file=sys.stderr,
        )
        return 1

    url = f"http://{config.host}:{server.server_port}"
    print(f"Metrics web running at {url}")
    if config.open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nMetrics web stopped.")
    finally:
        server.server_close()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ct plugin metrics web",
        description="Run the metrics comparison web application.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--open", action="store_true", help="Open in a browser.")
    parser.add_argument("--static-dir", default=None, help=argparse.SUPPRESS)
    return parser


def _static_dir(raw: str | None) -> Path:
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parent / "web" / "dist"


def _handler_for(
    static_dir: Path,
    service: MetricsService,
) -> type[BaseHTTPRequestHandler]:
    class MetricsRequestHandler(BaseHTTPRequestHandler):
        server_version = "CodingTrajectoryMetrics/0.1"

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
            if parsed.path == "/api/refresh":
                self._json_response(service.refresh())
                return
            self._json_error(HTTPStatus.NOT_FOUND, "not found")

        def log_message(self, format: str, *args: Any) -> None:
            print(f"{self.address_string()} - {format % args}", file=sys.stderr)

        def _handle_api_get(
            self,
            path: str,
            query: dict[str, list[str]],
        ) -> None:
            try:
                since_days = int((query.get("since_days") or ["7"])[0])
                chart = (query.get("chart") or [None])[0]
                if path == "/api/options":
                    payload = service.options(since_days)
                elif path == "/api/tokens":
                    payload = service.category("tokens", since_days=since_days, chart=chart)
                elif path == "/api/cost":
                    payload = service.category("cost", since_days=since_days, chart=chart)
                elif path == "/api/execution":
                    payload = service.category("execution", since_days=since_days, chart=chart)
                else:
                    self._json_error(HTTPStatus.NOT_FOUND, "not found")
                    return
            except (ValueError, ValidationError) as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            except RuntimeError as exc:
                self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return
            self._json_response(payload.model_dump(mode="json"))

        def _serve_static(self, raw_path: str, *, include_body: bool) -> None:
            target = static_dir / (raw_path.lstrip("/") or "index.html")
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

            data = resolved.read_bytes()
            content_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if include_body:
                self.wfile.write(data)

        def _json_response(
            self,
            payload: dict[str, Any],
            *,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json_error(self, status: HTTPStatus, message: str) -> None:
            self._json_response({"error": {"message": message}}, status=status)

    return MetricsRequestHandler


if __name__ == "__main__":
    raise SystemExit(main())
