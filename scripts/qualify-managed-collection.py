"""Synthetic CLI, real SQLite, HTTP outage and process-restart qualification.

Never registers a launchd/systemd service and never reads provider source files.
"""

from __future__ import annotations

import json
import os
import plistlib
import runpy
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from coding_trajectory.control_plane.canonical_repository import CanonicalRepository
from coding_trajectory_cli.collector_service import HostService

WORKSPACE = "11111111-1111-4111-8111-111111111111"
AGENT = "22222222-2222-4222-8222-222222222222"
TOKEN = "synthetic_managed_collector_token_0000000000"
ROTATED = "synthetic_managed_rotated_token_000000000000"
observed: list[str] = []


class Outage(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        observed.append(self.headers.get("Authorization", ""))
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":false,"error":{"code":"synthetic_outage"}}')


def eventually(predicate, *, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("bounded recovery condition did not arrive")


def main():
    checks = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), Outage)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    child = None
    old_env = dict(os.environ)
    try:
        with tempfile.TemporaryDirectory(prefix="ct-managed-collection-") as directory:
            root = Path(directory)
            projects = [root / "one", root / "two"]
            for project in projects:
                project.mkdir()
            journals = root / "empty-journals"
            journals.mkdir()
            env = {k: v for k, v in os.environ.items() if not k.startswith("CT_")}
            env.update(
                CT_CONNECTION_DIR=str(root / "connections"),
                CT_COLLECTOR_SERVICE_DIR=str(root / "service"),
                CT_AMP_LOG_DIR=str(journals),
            )
            os.environ.clear()
            os.environ.update(env)
            host = HostService()

            def cli(*args, success=True):
                result = subprocess.run(
                    [sys.executable, "-m", "coding_trajectory_cli.cli", *args],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                assert (result.returncode == 0) == success, result.stderr
                assert TOKEN not in result.stdout + result.stderr
                assert ROTATED not in result.stdout + result.stderr
                return json.loads(result.stdout) if success else None

            cli(
                "connection",
                "configure",
                "fixture",
                "--role",
                "collector",
                "--url",
                f"http://127.0.0.1:{server.server_port}",
                "--workspace-id",
                WORKSPACE,
                "--agent-id",
                AGENT,
                "--token-env",
                "CT_FIXTURE_TOKEN",
            )
            for project in projects:
                cli(
                    "collector",
                    "service",
                    "add",
                    project.name,
                    "--profile",
                    "fixture",
                    "--directory",
                    str(project),
                    "--project-name",
                    project.name,
                    "--state-path",
                    str(root / f"{project.name}.sqlite3"),
                    "--agent-vendor",
                    "amp",
                )
            initial = cli("collector", "service", "status")
            assert len(initial["projects"]) == 2 and not initial["running"]
            assert all(p["policy"]["mode"] == "manual" for p in initial["projects"])
            assert not observed
            checks += 3
            fixture = runpy.run_path(
                str(Path(__file__).with_name("qualify-canonical-repository.py"))
            )
            for project in host.config().projects:
                repository = CanonicalRepository(
                    project.state_path.with_suffix(".canonical.sqlite3"),
                    project.identity(),
                )
                repository.commit(captures=[fixture["capture"]()], snapshots={})
                repository.close()
            config = host.config()
            config.poll_seconds = 1
            host.save(config)
            installed = cli("collector", "service", "install")
            assert installed["schedule_activated"] is False and not host.running()
            for system in ("Darwin", "Linux"):
                _kind, template = host.supervisor_template(system)
                assert (
                    TOKEN.encode() not in template and ROTATED.encode() not in template
                )
                if system == "Darwin":
                    parsed = plistlib.loads(template)
                    assert parsed["ProgramArguments"][-1] == "run"
                    assert parsed["EnvironmentVariables"][
                        "CT_COLLECTOR_SERVICE_DIR"
                    ] == str(host.root)
                else:
                    assert (
                        b"Restart=always" in template
                        and b"KillMode=control-group" in template
                    )
            checks += 3

            def start():
                return subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "coding_trajectory_cli.cli",
                        "collector",
                        "service",
                        "run",
                    ],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

            child = start()
            eventually(
                lambda: all(p["pending_bytes"] > 0 for p in host.status()["projects"])
            )
            assert not observed
            frozen = [p["consumed_cursor"] for p in host.status()["projects"]]
            cli("collector", "service", "run", success=False)
            cli("collector", "service", "remove", "one", success=False)
            child.kill()
            child.wait(timeout=10)
            assert not host.running()
            child = start()
            eventually(
                lambda: host.running() and host.status()["heartbeat_at"] is not None
            )
            assert [p["consumed_cursor"] for p in host.status()["projects"]] == frozen
            assert not observed
            checks += 6
            # A bad directory cannot block another project's preparation.
            projects[0].rmdir()
            eventually(
                lambda: (
                    host.status()["projects"][0]["error_code"]
                    == "project_directory_unavailable"
                )
            )
            eventually(
                lambda: host.status()["projects"][1]["progress"].get("last_prepare_at")
            )
            checks += 2
            # Automatic mode reads a private reference; changing its value takes
            # effect without changing project identity or recreating the outbox.
            secrets = root / "secrets.json"
            secrets.write_text(json.dumps({"CT_FIXTURE_TOKEN": TOKEN}))
            secrets.chmod(0o600)
            cli("collector", "service", "install", "--secret-file", str(secrets))
            cli("collector", "service", "policy", "--mode", "automatic")
            for project in host.config().projects:
                from coding_trajectory.control_plane.upload_service import UploadService

                state = UploadService(project.state_path, project.identity())
                state.configure(batch_seconds=1)
                state.close()
            eventually(lambda: f"Bearer {TOKEN}" in observed)
            secrets.write_text(json.dumps({"CT_FIXTURE_TOKEN": ROTATED}))
            eventually(lambda: f"Bearer {ROTATED}" in observed)
            assert [p["consumed_cursor"] for p in host.status()["projects"]] == frozen
            assert all(p["pending_bytes"] > 0 for p in host.status()["projects"])
            checks += 4
            cli("collector", "service", "pause")
            child.terminate()
            child.wait(timeout=35)
            assert child.returncode == 0
            child = start()
            eventually(lambda: host.running())
            assert all(
                p["policy"]["paused"] and p["policy"]["mode"] == "automatic"
                for p in host.status()["projects"]
            )
            child.terminate()
            child.wait(timeout=35)
            child = None
            cli("collector", "service", "remove", "one")
            assert (root / "one.sqlite3").exists()
            assert (root / "connections/fixture.json").exists()
            checks += 4
            print(
                json.dumps(
                    {
                        "passed": checks,
                        "hard_restarts": 1,
                        "outage_attempts": len(observed),
                        "schedule_activated": False,
                    }
                )
            )
    finally:
        if child and child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        server.shutdown()
        server.server_close()
        os.environ.clear()
        os.environ.update(old_env)


if __name__ == "__main__":
    main()
