"""Synthetic CLI/HTTP qualification for shared connections; no production access."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WORKSPACE = "11111111-1111-4111-8111-111111111111"
AGENT = "22222222-2222-4222-8222-222222222222"
TOKEN = "synthetic_connection_token_0000000000000000"
requests: list[str] = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        requests.append(body["method"])
        valid = self.headers.get("Authorization") == f"Bearer {TOKEN}"
        self.send_response(200 if valid else 401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps(
                {
                    "ok": valid,
                    "data": {
                        "workspace_id": WORKSPACE,
                        "agent_id": AGENT,
                        "roles": ["read"],
                        "protocol": "ct.core.v1",
                    },
                }
            ).encode()
        )


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="ct-connections-") as temporary:
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("CT_")
            }
            env.update(
                HOME=temporary, CT_CONNECTION_DIR=temporary, CT_FIXTURE_TOKEN=TOKEN
            )
            passed = 0

            def run(*args, success=True, overrides=None):
                nonlocal passed
                result = subprocess.run(
                    [sys.executable, "-m", "coding_trajectory_cli.cli", *args],
                    env={**env, **(overrides or {})},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                assert (result.returncode == 0) == success, (args, result.stderr)
                assert TOKEN not in result.stdout + result.stderr
                passed += 1
                return result

            origin = f"http://127.0.0.1:{server.server_port}"
            run(
                "connection",
                "configure",
                "reader",
                "--url",
                origin,
                "--workspace-id",
                WORKSPACE,
                "--token-env",
                "CT_FIXTURE_TOKEN",
            )
            profile = json.loads((Path(temporary) / "reader.json").read_text())
            assert profile["agent_id"] is None and "access_token" not in profile
            run("connection", "status", "reader", overrides={"CT_FIXTURE_TOKEN": ""})
            assert requests == []
            run("connection", "check", "reader")
            run(
                "connection",
                "check",
                "reader",
                success=False,
                overrides={"CT_FIXTURE_TOKEN": "revoked"},
            )
            run("connection", "rotate", "reader", "--token-env", "CT_ROTATED_TOKEN")
            run("connection", "check", "reader", overrides={"CT_ROTATED_TOKEN": TOKEN})
            run("connection", "configure", "local", "--role", "local")
            run("connection", "status", "local")
            before = len(requests)
            run(
                "--profile",
                "reader",
                "--source",
                "local",
                "api",
                "call",
                "project.list",
                overrides={"CT_AUTO_PUBLISH": "1"},
            )
            assert len(requests) == before
            run(
                "--profile",
                "reader",
                "--source",
                "local",
                "api",
                "call",
                "project.list",
                "--snapshot-sequence",
                "1",
                success=False,
            )
            legacy = {
                key: profile[key]
                for key in ("cloudflare_url", "workspace_id", "token_env", "project_id")
            }
            legacy.update(version=2, agent_id=AGENT)
            (Path(temporary) / "legacy.json").write_text(json.dumps(legacy))
            run("connection", "status", "legacy")
            run(
                "connection", "check", "legacy", success=False
            )  # read credential is not collect authorization
            state = Path(temporary) / "collector.sqlite3"
            run(
                "connection",
                "configure",
                "collector",
                "--role",
                "collector",
                "--url",
                origin,
                "--workspace-id",
                WORKSPACE,
                "--agent-id",
                AGENT,
                "--project-name",
                "Synthetic",
                "--state-path",
                str(state),
                "--token-env",
                "CT_FIXTURE_TOKEN",
                "--default-source",
                "local",
            )
            # The managed `collector sync` service and CanonicalRepository
            # capture boundary were deleted by the fact-refactor clean break;
            # fact staging/publication is qualified separately against workerd.
            assert len(requests) == before + 1  # only the legacy capability check
            run("connection", "forget", "reader")
            run("connection", "status", "reader", success=False)
            assert all(method == "ct_connection_status" for method in requests)
            print(
                json.dumps(
                    {
                        "passed": passed,
                        "network_requests": len(requests),
                        "publication_requests": 0,
                    }
                )
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()
