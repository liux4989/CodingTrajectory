"""Local, read-only delivery of public Core session metrics."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from coding_trajectory.runtime import ServiceRuntime

WEB = Path(__file__).resolve().parents[1] / "web"


class BreakdownServer(ThreadingHTTPServer):
    def __init__(
        self, address, *, initial_session_id: str | None, allowed_hosts: set[str]
    ):
        self.initial_session_id = initial_session_id
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
        parsed = urlsplit(self.path)
        path = parsed.path
        origin = self.headers.get("Origin")
        if path.startswith("/api/") and (
            (origin and urlsplit(origin).netloc != host)
            or self.headers.get("Sec-Fetch-Site") == "cross-site"
        ):
            self.reply(403, b"Cross-origin request denied", "text/plain; charset=utf-8")
            return
        query = parse_qs(parsed.query)
        if path == "/api/config":
            self.reply(
                200,
                json.dumps(
                    {"initial_session_id": self.server.initial_session_id}
                ).encode(),
                "application/json",
            )
            return
        if path == "/api/sessions":
            cursor = query.get("cursor", [None])[0]
            if cursor is not None and not (0 < len(cursor) <= 4096):
                self.reply(400, b'{"error":"Invalid cursor"}', "application/json")
                return
            try:
                with ServiceRuntime(global_scope=True, current_dir=Path.cwd()) as core:
                    params = {"limit": 100}
                    if cursor:
                        params["cursor"] = cursor
                    page = core.call("project.sessions", params)
                self.reply(200, json.dumps(page).encode(), "application/json")
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                self.reply(
                    500, json.dumps({"error": str(exc)}).encode(), "application/json"
                )
            return
        if path == "/api/breakdown":
            raw_id = query.get("session_id", [self.server.initial_session_id])[0]
            try:
                session_id = str(UUID(raw_id))
            except (ValueError, TypeError, AttributeError):
                self.reply(400, b'{"error":"Invalid session ID"}', "application/json")
                return
            try:
                with ServiceRuntime(global_scope=True, current_dir=Path.cwd()) as core:
                    params = {"session_id": session_id}
                    stats = core.call("session.stats", params)
                    usage = core.call("session.usage", params)
                    requests = core.call("session.request_usage", params)
                    tools = core.call(
                        "session.tool_usage",
                        {**params, "limit": 1000},
                    )
                    items = list(tools["tool_items"])
                    cursor = tools.get("next_cursor")
                    while cursor:
                        page = core.call(
                            "session.tool_usage",
                            {**params, "limit": 1000, "cursor": cursor},
                        )
                        items.extend(page["tool_items"])
                        cursor = page.get("next_cursor")
                    item_details = {}
                    item_ids = [item["item_id"] for item in items]
                    for start in range(0, len(item_ids), 100):
                        page = core.call(
                            "session.items",
                            {**params, "item_ids": item_ids[start : start + 100]},
                        )
                        for item in page["items"]:
                            detail = item.get("detail") or {}
                            evidence = item.get("output_evidence") or {}
                            item_details[item["item_id"]] = {
                                "target": detail.get("target") or detail.get("path"),
                                "duration_ms": evidence.get("duration_ms"),
                            }
                    overview = core.call("session.overview", {**params, "limit": 200})
                    turns = list(overview["turns"])
                    cursor = overview["page"]["next_cursor"]
                    while cursor:
                        page = core.call(
                            "session.overview",
                            {**params, "limit": 200, "cursor": cursor},
                        )
                        turns.extend(page["turns"])
                        cursor = page["page"]["next_cursor"]
                body = json.dumps(
                    {
                        "stats": stats,
                        "usage": usage,
                        "requests": requests["request_count"],
                        "tools": items,
                        "item_details": item_details,
                        "turns": turns,
                        "sessions": overview["sessions"],
                    }
                ).encode()
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
    parser.add_argument(
        "session_id", nargs="?", help="Optional initially selected session UUID"
    )
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument(
        "--allow-host",
        action="append",
        default=[],
        help="Additional hostname (or .domain suffix) for a trusted authenticated proxy",
    )
    args = parser.parse_args()
    session_id = None
    if args.session_id:
        try:
            session_id = str(UUID(args.session_id))
        except ValueError:
            parser.error("session_id must be a UUID")
    with BreakdownServer(
        ("127.0.0.1", args.port),
        initial_session_id=session_id,
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
