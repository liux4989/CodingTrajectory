"""Local, read-only delivery of public Core session metrics."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from coding_trajectory.runtime import ServiceRuntime

WEB = Path(__file__).resolve().parents[1] / "web"


class BreakdownServer(ThreadingHTTPServer):
    def __init__(self, address, *, session_id: str, allowed_hosts: set[str]):
        self.session_id = session_id
        self.allowed_hosts = allowed_hosts
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: BreakdownServer

    def log_message(self, *_args):
        # Session IDs and evidence must not enter access logs.
        pass

    def reply(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        host = self.headers.get("Host", "")
        hostname = urlsplit("//" + host).hostname or ""
        if not any(
            hostname == allowed
            or (allowed.startswith(".") and hostname.endswith(allowed))
            for allowed in self.server.allowed_hosts
        ):
            self.reply(403, b"Host not allowed", "text/plain; charset=utf-8")
            return
        origin = self.headers.get("Origin")
        if (origin and urlsplit(origin).netloc != host) or self.headers.get(
            "Sec-Fetch-Site"
        ) == "cross-site":
            self.reply(403, b"Cross-origin request denied", "text/plain; charset=utf-8")
            return
        path = urlsplit(self.path).path
        if path == "/api/breakdown":
            try:
                with ServiceRuntime(global_scope=True, current_dir=Path.cwd()) as core:
                    stats = core.call(
                        "session.stats", {"session_id": self.server.session_id}
                    )
                    tools = core.call(
                        "session.tool_usage",
                        {"session_id": self.server.session_id, "limit": 1000},
                    )
                    items = list(tools["tool_items"])
                    cursor = tools.get("next_cursor")
                    while cursor:
                        page = core.call(
                            "session.tool_usage",
                            {
                                "session_id": self.server.session_id,
                                "limit": 1000,
                                "cursor": cursor,
                            },
                        )
                        items.extend(page["tool_items"])
                        cursor = page.get("next_cursor")
                body = json.dumps({"stats": stats, "tools": items}).encode()
                self.reply(200, body, "application/json")
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                self.reply(
                    500,
                    json.dumps({"error": str(exc)}).encode(),
                    "application/json",
                )
            return
        files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/style.css": ("style.css", "text/css; charset=utf-8"),
        }
        if path not in files:
            self.reply(404, b"Not found", "text/plain; charset=utf-8")
            return
        filename, content_type = files[path]
        self.reply(200, (WEB / filename).read_bytes(), content_type)


def main():
    parser = argparse.ArgumentParser(description="Read-only local session breakdown")
    parser.add_argument("command", choices=["web"])
    parser.add_argument("session_id", help="Session UUID to inspect")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument(
        "--allow-host",
        action="append",
        default=[],
        help="Additional hostname (or .domain suffix) for a trusted authenticated proxy",
    )
    args = parser.parse_args()
    try:
        session_id = str(UUID(args.session_id))
    except ValueError:
        parser.error("session_id must be a UUID")
    with BreakdownServer(
        ("127.0.0.1", args.port),
        session_id=session_id,
        allowed_hosts={"localhost", "127.0.0.1", *args.allow_host},
    ) as server:
        print(
            f"Breakdown: http://127.0.0.1:{args.port} (local evidence only)", flush=True
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
