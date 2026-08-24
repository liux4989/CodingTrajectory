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


@dataclass(frozen=True, slots=True)
class CodeTimeWebConfig:
    host: str
    port: int
    open_browser: bool
    static_dir: Path


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    config = CodeTimeWebConfig(
        host=args.host,
        port=args.port,
        open_browser=args.open,
        static_dir=_static_dir(args.static_dir),
    )
    if not config.static_dir.is_dir():
        print(
            "error: code-time web assets not found; run `bun install && bun run build` "
            "in packages/plugins/code_time/web",
            file=sys.stderr,
        )
        return 2
    return serve(config)


def serve(config: CodeTimeWebConfig) -> int:
    handler = _handler_for(config.static_dir)
    ThreadingHTTPServer.allow_reuse_address = True
    server: ThreadingHTTPServer | None = None
    last_exc: OSError | None = None
    for port in range(config.port, config.port + 20):
        try:
            server = ThreadingHTTPServer((config.host, port), handler)
            break
        except OSError as exc:
            last_exc = exc
    if server is None:
        print(
            f"error: could not bind to any port on {config.host} in "
            f"{config.port}-{config.port + 19} ({last_exc}); "
            "stop the other processes or use --port to pick a different range",
            file=sys.stderr,
        )
        return 1
    if server.server_port != config.port:
        print(f"port {config.port} in use; using {server.server_port} instead")
    url = f"http://{config.host}:{server.server_port}"
    print(f"Code Time web running at {url}")
    if config.open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nCode Time web stopped.")
    finally:
        server.server_close()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ct plugin code-time web",
        description="Run the code-time web dashboard.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--open", action="store_true", help="Open in a browser.")
    parser.add_argument("--static-dir", default=None, help=argparse.SUPPRESS)
    return parser


def _static_dir(raw: str | None) -> Path:
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parent / "web" / "dist"


def _handler_for(static_dir: Path) -> type[BaseHTTPRequestHandler]:
    class CodeTimeRequestHandler(BaseHTTPRequestHandler):
        server_version = "CodingTrajectoryCodeTime/0.1"

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

        def log_message(self, format: str, *args: Any) -> None:
            print(f"{self.address_string()} - {format % args}", file=sys.stderr)

        def _handle_api_get(self, path: str, query: dict[str, list[str]]) -> None:
            try:
                if path == "/api/today":
                    payload = _code_time_payload(query)
                elif path == "/api/summary":
                    payload = _code_time_payload(query)
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

        def _json_response(self, payload: Any) -> None:
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json_error(self, status: HTTPStatus, message: str) -> None:
            body = json.dumps({"error": {"message": message}}).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return CodeTimeRequestHandler


def _code_time_payload(query: dict[str, list[str]]) -> dict[str, Any]:
    from code_time import build_report

    window = (query.get("window") or ["today"])[0]
    project = (query.get("project") or [None])[0]
    agent_vendor = (query.get("agent_vendor") or [None])[0]

    return build_report(
        window=window,
        project_filter=project,
        agent_vendor=agent_vendor,
    )


if __name__ == "__main__":
    raise SystemExit(main())
