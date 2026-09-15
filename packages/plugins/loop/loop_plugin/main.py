"""Local-only HTTP delivery; Core retains its own methods and envelopes."""

from __future__ import annotations

import argparse
import json
import mimetypes
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from coding_trajectory.contracts import SERVICE_CONTRACTS
from coding_trajectory.runtime import ServiceRuntime
from pydantic import ValidationError

from loop_plugin.models import PROTOCOL, CoreQuery, Investigation
from loop_plugin.monitor.models import (
    FindingStatusEvent,
    FindingStatusRequest,
    RunRequest,
    Watch,
    WatchCreateRequest,
    WatchRevision,
    WatchUpdateRequest,
)
from loop_plugin.monitor.runtime import (
    MonitorCoreError,
    dry_run,
    refresh,
)
from loop_plugin.monitor.store import MonitorStore
from loop_plugin.monitor.strategies import catalog

WEB = Path(__file__).resolve().parents[1] / "web" / "dist"


def _parse_uuid(raw: str) -> UUID | None:
    try:
        return UUID(raw)
    except (ValueError, AttributeError, TypeError):
        return None


def _path_uuid(path: str, prefix: str) -> UUID | None:
    rest = path.removeprefix(prefix)
    if "/" in rest:
        return None
    return _parse_uuid(rest)


def _query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _query_uuid(query: dict[str, list[str]], key: str) -> UUID | None:
    value = _query_value(query, key)
    return _parse_uuid(value) if value else None


def _query_limit(query: dict[str, list[str]], *, default: int, maximum: int) -> int:
    raw = _query_value(query, "limit")
    if raw is None:
        return default
    try:
        return max(1, min(int(raw), maximum))
    except ValueError:
        return default


def _apply_watch_update(watch: Watch, update: WatchUpdateRequest) -> Watch:
    """Apply a human edit. Scope/config changes create a new effective
    revision so historical evaluations stay pinned to their configuration,
    and reset the refresh cursor so the new policy re-observes the scope."""
    now = datetime.now(UTC)
    if update.name is not None:
        watch.name = update.name
    if update.enabled is not None:
        watch.enabled = update.enabled
    scope_changed = update.scope is not None and update.scope != watch.scope
    config_changed = update.config is not None and update.config != watch.config
    if scope_changed or config_changed:
        if update.scope is not None:
            watch.scope = update.scope
        if update.config is not None:
            watch.config = update.config
        watch.config_revision += 1
        watch.revisions.append(
            WatchRevision(
                revision=watch.config_revision,
                scope=watch.scope,
                config=watch.config,
                changed_at=now,
            )
        )
        watch.refresh = watch.refresh.model_copy(
            update={"cursor": None, "watermark": None, "caught_up": False}
        )
    watch.updated_at = now
    return watch


class LoopServer(ThreadingHTTPServer):
    def __init__(
        self, address, *, state: Path, monitor_state: Path, allowed_hosts: set[str]
    ):
        self.allowed_hosts = allowed_hosts
        self.state = state
        self.monitor = MonitorStore(monitor_state)
        self.core_lock = Lock()
        state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(state)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS investigations (id TEXT PRIMARY KEY, state TEXT NOT NULL)"
            )
        state.chmod(0o600)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: LoopServer
    timeout = 30

    def log_message(self, *_args):
        # Request paths may contain evidence references; do not log them.
        pass

    def reply(self, status, value, *, content_type="application/json"):
        body = (
            json.dumps(value).encode() if content_type == "application/json" else value
        )
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def fail(self, status, message):
        self.reply(status, {"protocol": PROTOCOL, "error": message})

    def trusted(self):
        host = self.headers.get("Host", "")
        if urlsplit("//" + host).hostname not in self.server.allowed_hosts:
            self.fail(
                403,
                "Host is not allowed. Configure --allow-host for an authenticated local proxy.",
            )
            return False
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != host:
            self.fail(403, "Cross-origin requests are not allowed.")
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.fail(403, "Cross-site requests are not allowed.")
            return False
        return True

    def do_GET(self):
        if not self.trusted():
            return
        split = urlsplit(self.path)
        path = split.path
        query = parse_qs(split.query)
        if path == "/api/investigations":
            with closing(sqlite3.connect(self.server.state)) as db:
                rows = db.execute(
                    "SELECT state FROM investigations ORDER BY rowid DESC LIMIT 100"
                ).fetchall()
            self.reply(
                200,
                {"protocol": PROTOCOL, "items": [json.loads(row[0]) for row in rows]},
            )
        elif path == "/api/status":
            self.reply(
                200,
                {
                    "protocol": PROTOCOL,
                    "source": "host_local",
                    "revision": "latest",
                    "remote": False,
                },
            )
        elif path == "/api/monitor/strategies":
            self.reply(
                200,
                {
                    "protocol": PROTOCOL,
                    "items": [item.model_dump(mode="json") for item in catalog()],
                },
            )
        elif path == "/api/monitor/watches":
            watches = self.server.monitor.list_watches()
            self.reply(
                200,
                {
                    "protocol": PROTOCOL,
                    "items": [watch.model_dump(mode="json") for watch in watches],
                },
            )
        elif path.startswith("/api/monitor/watches/"):
            watch_id = _path_uuid(path, "/api/monitor/watches/")
            watch = watch_id and self.server.monitor.get_watch(watch_id)
            if watch is None:
                self.fail(404, "Unknown watch")
                return
            self.reply(
                200, {"protocol": PROTOCOL, "watch": watch.model_dump(mode="json")}
            )
        elif path == "/api/monitor/evaluations":
            evaluations = self.server.monitor.list_evaluations(
                watch_id=_query_uuid(query, "watch_id"),
                trigger=_query_value(query, "trigger"),
                state=_query_value(query, "state"),
                result=_query_value(query, "result"),
                include_superseded=_query_value(query, "include_superseded") == "true",
                limit=_query_limit(query, default=100, maximum=500),
            )
            self.reply(
                200,
                {
                    "protocol": PROTOCOL,
                    "items": [item.model_dump(mode="json") for item in evaluations],
                },
            )
        elif path == "/api/monitor/findings":
            findings = self.server.monitor.list_findings(
                status=_query_value(query, "status"),
                watch_id=_query_uuid(query, "watch_id"),
                limit=_query_limit(query, default=200, maximum=500),
            )
            self.reply(
                200,
                {
                    "protocol": PROTOCOL,
                    "items": [item.model_dump(mode="json") for item in findings],
                },
            )
        elif path.startswith("/api/"):
            self.fail(404, "Unknown Loop route")
        else:
            asset = (WEB / (path.lstrip("/") or "index.html")).resolve()
            if not asset.is_relative_to(WEB.resolve()) or not asset.is_file():
                self.fail(404, "Asset not found. Build Loop with bun run build.")
                return
            self.reply(
                200,
                asset.read_bytes(),
                content_type=mimetypes.guess_type(asset.name)[0]
                or "application/octet-stream",
            )

    def do_POST(self):
        if not self.trusted():
            return
        if self.headers.get_content_type() != "application/json":
            self.fail(415, "Use application/json")
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 65536:
                self.fail(413, "Request must be between 1 and 65536 bytes")
                return
            body = json.loads(self.rfile.read(size))
            path = urlsplit(self.path).path
            if path == "/api/core":
                query = CoreQuery.model_validate(body)
                if query.method not in SERVICE_CONTRACTS:
                    self.fail(400, "Unknown Core method")
                    return
                # Core validates parameters, resolves evidence, and owns provenance.
                # Per-request lifetime avoids freezing mutable local evidence in a
                # long-lived runtime cache. There is no remote fallback factory.
                with (
                    self.server.core_lock,
                    ServiceRuntime(
                        global_scope=True,
                        current_dir=Path.cwd(),
                        connection_profile="local",
                    ) as core,
                ):
                    self.reply(200, core.execute(query.model_dump()))
            elif path == "/api/investigations":
                investigation = Investigation.model_validate(body)
                with closing(sqlite3.connect(self.server.state)) as db, db:
                    db.execute(
                        "INSERT INTO investigations VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET state=excluded.state",
                        (str(investigation.id), investigation.model_dump_json()),
                    )
                self.reply(
                    200,
                    {
                        "protocol": PROTOCOL,
                        "investigation": investigation.model_dump(mode="json"),
                    },
                )
            elif path == "/api/monitor/watches":
                created = self._create_watch(body)
                self.reply(200, {"protocol": PROTOCOL, "watch": created})
            elif path.startswith("/api/monitor/watches/"):
                self._watch_command(path, body)
            elif path.startswith("/api/monitor/findings/"):
                self._finding_command(path, body)
            else:
                self.fail(404, "Unknown Loop route")
        except (ValueError, ValidationError) as exc:
            self.fail(400, str(exc))
        except MonitorCoreError as exc:
            self.fail(400, str(exc))
        except (OSError, sqlite3.Error, RuntimeError):
            self.fail(500, "Local query failed. Check source availability and retry.")

    def _create_watch(self, body):
        request = WatchCreateRequest.model_validate(body)
        manifest = next(
            (item for item in catalog() if item.strategy_id == request.strategy_id),
            None,
        )
        if manifest is None:
            raise ValueError(f"Unknown strategy: {request.strategy_id}")
        now = datetime.now(UTC)
        watch = Watch(
            watch_id=uuid4(),
            strategy_id=manifest.strategy_id,
            strategy_version=manifest.version,
            name=request.name,
            scope=request.scope,
            config=request.config,
            created_at=now,
            updated_at=now,
        )
        watch.revisions.append(
            WatchRevision(
                revision=1,
                scope=watch.scope,
                config=watch.config,
                changed_at=now,
            )
        )
        self.server.monitor.save_watch(watch)
        return watch.model_dump(mode="json")

    def _watch_command(self, path, body):
        rest = path.removeprefix("/api/monitor/watches/")
        parts = rest.split("/")
        watch_id = _parse_uuid(parts[0])
        watch = watch_id and self.server.monitor.get_watch(watch_id)
        if watch is None:
            self.fail(404, "Unknown watch")
            return
        if len(parts) == 1:
            update = WatchUpdateRequest.model_validate(body)
            watch = _apply_watch_update(watch, update)
            self.server.monitor.save_watch(watch)
            self.reply(
                200, {"protocol": PROTOCOL, "watch": watch.model_dump(mode="json")}
            )
        elif parts[1] == "dry-run":
            run = RunRequest.model_validate(body)
            result = dry_run(self.server.monitor, watch, max_sessions=run.max_sessions)
            self.reply(
                200, {"protocol": PROTOCOL, "run": result.model_dump(mode="json")}
            )
        elif parts[1] == "refresh":
            if not watch.enabled:
                self.fail(
                    409,
                    "Enable the watch to evaluate newly observed evidence; "
                    "use dry-run for a historical preview.",
                )
                return
            run = RunRequest.model_validate(body)
            result = refresh(self.server.monitor, watch, max_sessions=run.max_sessions)
            self.reply(
                200, {"protocol": PROTOCOL, "run": result.model_dump(mode="json")}
            )
        else:
            self.fail(404, "Unknown Loop route")

    def _finding_command(self, path, body):
        rest = path.removeprefix("/api/monitor/findings/")
        parts = rest.split("/")
        finding_id = _parse_uuid(parts[0])
        finding = finding_id and self.server.monitor.get_finding(finding_id)
        if finding is None:
            self.fail(404, "Unknown finding")
            return
        if len(parts) == 2 and parts[1] == "status":
            request = FindingStatusRequest.model_validate(body)
            now = datetime.now(UTC)
            finding.status = request.status
            finding.updated_at = now
            finding.status_history.append(
                FindingStatusEvent(status=request.status, at=now)
            )
            self.server.monitor.save_finding(finding)
            self.reply(
                200, {"protocol": PROTOCOL, "finding": finding.model_dump(mode="json")}
            )
        else:
            self.fail(404, "Unknown Loop route")


def main():
    parser = argparse.ArgumentParser(
        description="CodingTrajectory Loop — local Analytics and Monitor"
    )
    parser.add_argument("command", choices=["web"])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--state",
        type=Path,
        default=Path.home() / ".coding-trajectory" / "loop" / "investigations.sqlite3",
    )
    parser.add_argument(
        "--monitor-state",
        type=Path,
        default=Path.home() / ".coding-trajectory" / "loop" / "monitor.sqlite3",
    )
    parser.add_argument(
        "--allow-host",
        action="append",
        default=[],
        help="Additional hostname for a trusted authenticated proxy",
    )
    args = parser.parse_args()
    if not (WEB / "index.html").is_file():
        parser.error(
            "Build packages/plugins/loop/web with bun install && bun run build first"
        )
    with LoopServer(
        ("127.0.0.1", args.port),
        state=args.state,
        monitor_state=args.monitor_state,
        allowed_hosts={"localhost", "127.0.0.1", *args.allow_host},
    ) as server:
        print(f"Loop: http://127.0.0.1:{args.port} (local evidence only)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
