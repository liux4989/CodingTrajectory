"""Local-only HTTP delivery; Core retains its own methods and envelopes."""

from __future__ import annotations

import argparse
import json
import mimetypes
import select
import socket
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from coding_trajectory.contracts import SERVICE_CONTRACTS
from coding_trajectory.control_plane.prepared_api import prepare_inventory_api
from coding_trajectory.control_plane.prepared_api_reader import (
    decode_cursor,
    load_local_view,
    local_signing_key,
    read_prepared,
    save_local_view,
)
from coding_trajectory.service.store import session_browser_metadata
from pydantic import ValidationError

from loop_plugin.models import PROTOCOL, CoreQuery, Investigation
from loop_plugin.monitor.models import (
    FindingStatusEvent,
    FindingStatusRequest,
    MonitorRun,
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
        self.monitor.interrupt_running_runs()
        self.core_lock = Lock()
        self.run_lock = Lock()
        state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(state)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS investigations (id TEXT PRIMARY KEY, state TEXT NOT NULL)"
            )
        state.chmod(0o600)
        super().__init__(address, Handler)

    def start_run(
        self, watch: Watch, kind: str, max_sessions: int, *, resumed_from=None
    ) -> MonitorRun | None:
        with self.run_lock:
            if resumed_from is not None:
                existing = self.monitor.resumed_run(resumed_from, watch.watch_id)
                if existing is not None:
                    return existing
            if self.monitor.running_run(watch.watch_id) is not None:
                return None
            current = self.monitor.get_watch(watch.watch_id)
            if current is None or current.config_revision != watch.config_revision:
                return None
            if kind == "refresh" and not current.enabled:
                return None
            watch = current
            now = datetime.now(UTC)
            run = MonitorRun(
                run_id=uuid4(),
                watch_id=watch.watch_id,
                kind=kind,
                state="running",
                config_revision=watch.config_revision,
                max_sessions=max_sessions,
                started_at=now,
                updated_at=now,
                resumed_from=resumed_from,
            )
            self.monitor.save_run(run)
            Thread(target=self.execute_run, args=(run, watch), daemon=True).start()
            return run

    def execute_run(self, run: MonitorRun, watch: Watch) -> None:
        try:
            result = (
                dry_run(self.monitor, watch, max_sessions=run.max_sessions)
                if run.kind == "dry_run"
                else refresh(self.monitor, watch, max_sessions=run.max_sessions)
            )
            run.result = result.model_dump(mode="json")
            run.state = "completed"
        except Exception as exc:  # noqa: BLE001 - durable operation boundary
            run.error = (
                str(exc)[:512]
                if isinstance(exc, MonitorCoreError)
                else "Local Monitor run failed; review partial results before resuming."
            )
            run.state = "failed"
        run.updated_at = datetime.now(UTC)
        self.monitor.save_run(run)


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
        try:
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
        except (BrokenPipeError, ConnectionResetError):
            # A read may finish just as its browser leaves the page.
            pass

    def disconnected(self) -> bool:
        try:
            if not select.select([self.connection], [], [], 0)[0]:
                return False
            return not self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
        except (BlockingIOError, ConnectionResetError, OSError):
            return True

    def core_read(self, query: CoreQuery):
        while not self.server.core_lock.acquire(timeout=0.1):
            if self.disconnected():
                return None
        try:
            if self.disconnected():
                return None
            worker = subprocess.Popen(
                [sys.executable, "-m", "loop_plugin.core_worker"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=Path.cwd(),
            )
            payload = query.model_dump_json().encode()
            pending = payload
            try:
                while True:
                    try:
                        output, _ = worker.communicate(input=pending, timeout=0.1)
                        if worker.returncode != 0:
                            raise RuntimeError("Core read failed")
                        return json.loads(output)
                    except subprocess.TimeoutExpired:
                        pending = None
                        if self.disconnected():
                            worker.terminate()
                            try:
                                worker.communicate(timeout=1)
                            except subprocess.TimeoutExpired:
                                worker.kill()
                                worker.communicate()
                            return None
            finally:
                if worker.poll() is None:
                    worker.kill()
                    worker.communicate()
        finally:
            self.server.core_lock.release()

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
        elif path == "/api/monitor/runs":
            watch_id = _query_uuid(query, "watch_id")
            if watch_id is None:
                self.fail(400, "watch_id is required")
                return
            self.reply(
                200,
                {
                    "protocol": PROTOCOL,
                    "items": [
                        run.model_dump(mode="json")
                        for run in self.server.monitor.list_runs(watch_id)
                    ],
                },
            )
        elif path.startswith("/api/monitor/runs/"):
            run_id = _path_uuid(path, "/api/monitor/runs/")
            run = run_id and self.server.monitor.get_run(run_id)
            if run is None:
                self.fail(404, "Unknown run")
                return
            self.reply(200, {"protocol": PROTOCOL, "run": run.model_dump(mode="json")})
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
                result = self.core_read(query)
                if result is not None:
                    self.reply(200, result)
            elif path == "/api/session-browser":
                from coding_trajectory.contracts import service_contract

                if not isinstance(body, dict) or not body.get("project_id"):
                    raise ValueError("project_id is required")
                params = service_contract("project.sessions").validate_request(body)
                signing_key = local_signing_key()
                if params.get("cursor"):
                    cursor = decode_cursor(params["cursor"], signing_key)
                    api, identity = load_local_view(cursor["view_manifest_sha256"])
                else:
                    cards = session_browser_metadata(
                        current_dir=Path.cwd(),
                        project_id=params["project_id"],
                        cancelled=self.disconnected,
                    )
                    api, source = prepare_inventory_api([], cards)
                    identity = save_local_view(api, source)
                descriptor = next(
                    item for item in api.methods if item.method == "project.sessions"
                )
                result = read_prepared(
                    descriptor,
                    params,
                    identity=identity,
                    fetch=lambda digest: api.objects[digest].encode(),
                    signing_key=signing_key,
                )
                self.reply(200, {"protocol": PROTOCOL, "result": result})
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
            elif path.startswith("/api/monitor/runs/"):
                self._resume_run(path, body)
            elif path.startswith("/api/monitor/findings/"):
                self._finding_command(path, body)
            else:
                self.fail(404, "Unknown Loop route")
        except (ValueError, ValidationError) as exc:
            self.fail(400, str(exc))
        except InterruptedError:
            return
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
            with self.server.run_lock:
                if self.server.monitor.running_run(watch.watch_id):
                    self.fail(409, "Wait for the active run before changing this watch")
                    return
                watch = self.server.monitor.get_watch(watch.watch_id) or watch
                watch = _apply_watch_update(watch, update)
                self.server.monitor.save_watch(watch)
            self.reply(
                200, {"protocol": PROTOCOL, "watch": watch.model_dump(mode="json")}
            )
        elif parts[1] == "dry-run":
            request = RunRequest.model_validate(body)
            run = self.server.start_run(watch, "dry_run", request.max_sessions)
            if run is None:
                self.fail(409, "A run is already active for this watch")
                return
            self.reply(202, {"protocol": PROTOCOL, "run": run.model_dump(mode="json")})
        elif parts[1] == "refresh":
            if not watch.enabled:
                self.fail(
                    409,
                    "Enable the watch to evaluate newly observed evidence; "
                    "use dry-run for a historical preview.",
                )
                return
            request = RunRequest.model_validate(body)
            run = self.server.start_run(watch, "refresh", request.max_sessions)
            if run is None:
                self.fail(409, "A run is already active for this watch")
                return
            self.reply(202, {"protocol": PROTOCOL, "run": run.model_dump(mode="json")})
        else:
            self.fail(404, "Unknown Loop route")

    def _resume_run(self, path, body):
        parts = path.removeprefix("/api/monitor/runs/").split("/")
        run_id = _parse_uuid(parts[0])
        previous = run_id and self.server.monitor.get_run(run_id)
        if len(parts) != 2 or parts[1] != "resume" or previous is None:
            self.fail(404, "Unknown run")
            return
        if body:
            self.fail(400, "Resume uses the original run parameters")
            return
        if previous.state not in {"interrupted", "failed"}:
            self.fail(409, "Only interrupted or failed runs can resume")
            return
        watch = self.server.monitor.get_watch(previous.watch_id)
        if watch is None or watch.config_revision != previous.config_revision:
            self.fail(409, "Watch configuration changed; start a new run")
            return
        if previous.kind == "refresh" and not watch.enabled:
            self.fail(409, "Enable the watch before resuming refresh")
            return
        run = self.server.start_run(
            watch, previous.kind, previous.max_sessions, resumed_from=previous.run_id
        )
        if run is None:
            self.fail(409, "A run is already active for this watch")
            return
        self.reply(202, {"protocol": PROTOCOL, "run": run.model_dump(mode="json")})

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
