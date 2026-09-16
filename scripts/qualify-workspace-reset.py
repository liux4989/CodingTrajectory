#!/usr/bin/env python3
"""Qualify the temporary single-workspace reset boundary on loopback only."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).parents[1]
WORKER = ROOT / "cloudflare" / "control-plane"
URL = ""
TARGET = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-000000000002"
AGENT = "00000000-0000-0000-0000-000000000003"
TOKENS = {
    "owner": "reset-qualification-owner-token-00000001",
    "reader": "reset-qualification-reader-token-0000001",
    "other": "reset-qualification-other-owner-token-00001",
}


def principals() -> str:
    claims = {
        TOKENS["owner"]: (TARGET, ["owner"]),
        TOKENS["reader"]: (TARGET, ["read"]),
        TOKENS["other"]: (OTHER, ["owner"]),
    }
    return json.dumps(
        {
            hashlib.sha256(token.encode()).hexdigest(): {
                "workspace_id": workspace,
                "agent_id": AGENT,
                "roles": roles,
            }
            for token, (workspace, roles) in claims.items()
        },
        separators=(",", ":"),
    )


def rpc(
    method: str,
    workspace: str,
    token: str,
    params: dict[str, object] | None = None,
) -> httpx.Response:
    return httpx.post(
        URL + "/v1/core",
        headers={"Authorization": "Bearer " + token},
        json={
            "protocol": "ct.core.v1",
            "id": None,
            "method": method,
            "params": {"workspace_id": workspace, **(params or {})},
        },
        timeout=10,
    )


def expect(response: httpx.Response, status: int, code: str | None = None) -> dict:
    assert response.status_code == status, (response.status_code, response.text[:300])
    body = response.json()
    if code is not None:
        assert body["error"]["code"] == code, body
    return body


def wait_ready() -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            response = rpc("ct_workspace_snapshot", TARGET, TOKENS["owner"])
            if response.status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.2)
    raise RuntimeError("local Worker did not become ready")


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def qualify_wrong_stub() -> None:
    port = available_port()
    with tempfile.TemporaryDirectory(prefix="ct-reset-wrong-stub-") as state:
        state_path = Path(state)
        harness = state_path / "harness.ts"
        harness.write_text(
            f"""import {{ Workspace }} from {json.dumps(str(WORKER / "src" / "workspace.ts"))};
export {{ Workspace }};
export default {{
  async fetch(_request: Request, env: Env): Promise<Response> {{
    const other = env.WORKSPACES.getByName({json.dumps(OTHER)});
    const agent = {json.dumps(AGENT)};
    const otherPrincipal = {{ workspace_id: {json.dumps(OTHER)}, agent_id: agent, roles: ["owner"] }};
    await other.invoke("ct_project_register", JSON.stringify({{ request: {{ workspace_id: {json.dumps(OTHER)}, agent_id: agent, display_name: "Isolation control" }} }}), JSON.stringify(otherPrincipal));
    const targetPrincipal = {{ workspace_id: {json.dumps(TARGET)}, agent_id: agent, roles: ["owner"] }};
    const reset = JSON.parse(await other.invoke("ct_workspace_reset", JSON.stringify({{ request: {{ workspace_id: {json.dumps(TARGET)}, confirmation: "reset:{TARGET}" }} }}), JSON.stringify(targetPrincipal)));
    const snapshot = JSON.parse(await other.invoke("ct_workspace_snapshot", JSON.stringify({{ request: {{ workspace_id: {json.dumps(OTHER)} }} }}), JSON.stringify(otherPrincipal)));
    return Response.json({{ reset, snapshot }});
  }}
}} satisfies ExportedHandler<Env>;
""",
            encoding="utf-8",
        )
        config = state_path / "wrangler.jsonc"
        config.write_text(
            json.dumps(
                {
                    "name": "ct-reset-wrong-stub-qualification",
                    "main": str(harness),
                    "compatibility_date": "2026-09-10",
                    "compatibility_flags": ["nodejs_compat"],
                    "durable_objects": {
                        "bindings": [{"name": "WORKSPACES", "class_name": "Workspace"}]
                    },
                    "migrations": [{"tag": "v1", "new_sqlite_classes": ["Workspace"]}],
                }
            ),
            encoding="utf-8",
        )
        command = [
            str(WORKER / "node_modules" / ".bin" / "wrangler"),
            "dev",
            "--local",
            "--config",
            str(config),
            "--port",
            str(port),
            "--persist-to",
            str(state_path / "storage"),
            "--var",
            "CT_PRINCIPALS:{}",
            "--var",
            "CT_CURSOR_KEY:local-reset-qualification-cursor-key-0001",
            "--var",
            "CT_RESET_WORKSPACE_ID:" + TARGET,
        ]
        worker = subprocess.Popen(
            command,
            cwd=WORKER,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    response = httpx.get(f"http://127.0.0.1:{port}", timeout=10)
                    break
                except httpx.TransportError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("wrong-stub Worker did not become ready")
                    time.sleep(0.2)
            assert response.status_code == 200, response.text[:300]
            result = response.json()
            assert result["reset"] == {
                "status": 403,
                "body": {"error": {"code": "workspace_reset_target_denied"}},
            }
            assert result["snapshot"]["status"] == 200
            assert result["snapshot"]["body"]["snapshot_sequence"] == 1
        finally:
            os.killpg(worker.pid, signal.SIGTERM)
            worker.wait(timeout=10)


def main() -> None:
    global URL
    port = available_port()
    URL = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="ct-reset-qualification-") as state:
        command = [
            str(WORKER / "node_modules" / ".bin" / "wrangler"),
            "dev",
            "--local",
            "--port",
            str(port),
            "--persist-to",
            state,
            "--var",
            "CT_PRINCIPALS:" + principals(),
            "--var",
            "CT_CURSOR_KEY:local-reset-qualification-cursor-key-0001",
            "--var",
            "CT_RESET_WORKSPACE_ID:" + TARGET,
        ]
        worker = subprocess.Popen(
            command,
            cwd=WORKER,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            wait_ready()
            expect(
                rpc(
                    "ct_project_register",
                    TARGET,
                    TOKENS["owner"],
                    {"agent_id": AGENT, "display_name": "Reset target"},
                ),
                200,
            )
            expect(
                rpc(
                    "ct_project_register",
                    OTHER,
                    TOKENS["other"],
                    {"agent_id": AGENT, "display_name": "Isolation control"},
                ),
                200,
            )
            assert (
                expect(rpc("ct_workspace_snapshot", TARGET, TOKENS["owner"]), 200)[
                    "data"
                ]["snapshot_sequence"]
                == 1
            )
            assert (
                expect(rpc("ct_workspace_snapshot", OTHER, TOKENS["other"]), 200)[
                    "data"
                ]["snapshot_sequence"]
                == 1
            )

            expect(
                rpc(
                    "ct_workspace_reset",
                    TARGET,
                    TOKENS["reader"],
                    {"confirmation": "reset:" + TARGET},
                ),
                403,
                "capability_required",
            )
            expect(
                rpc(
                    "ct_workspace_reset",
                    TARGET,
                    TOKENS["other"],
                    {"confirmation": "reset:" + TARGET},
                ),
                403,
                "workspace_denied",
            )
            expect(
                rpc(
                    "ct_workspace_reset",
                    OTHER,
                    TOKENS["other"],
                    {"confirmation": "reset:" + OTHER},
                ),
                403,
                "workspace_reset_target_denied",
            )
            expect(
                rpc(
                    "ct_workspace_reset",
                    TARGET,
                    TOKENS["owner"],
                    {"confirmation": "wrong"},
                ),
                403,
                "workspace_reset_confirmation_required",
            )
            expect(
                rpc(
                    "ct_workspace_reset",
                    TARGET,
                    TOKENS["owner"],
                    {"confirmation": "reset:" + TARGET},
                ),
                200,
            )
            assert (
                expect(rpc("ct_workspace_snapshot", TARGET, TOKENS["owner"]), 200)[
                    "data"
                ]["snapshot_sequence"]
                == 0
            )
            assert (
                expect(rpc("ct_workspace_snapshot", OTHER, TOKENS["other"]), 200)[
                    "data"
                ]["snapshot_sequence"]
                == 1
            )
        finally:
            os.killpg(worker.pid, signal.SIGTERM)
            worker.wait(timeout=10)

    qualify_wrong_stub()
    print("Targeted workspace reset qualification: PASS")


if __name__ == "__main__":
    main()
