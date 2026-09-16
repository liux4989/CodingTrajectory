#!/usr/bin/env python3
"""Exercise temporary recovery against a real local Worker with synthetic data only."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict

spec = importlib.util.spec_from_file_location(
    "reset_qualification", Path(__file__).with_name("qualify-workspace-reset.py")
)
assert spec and spec.loader
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)
# This identifier is used only in isolated loopback storage, never against production.
q.TARGET = "fbeca960-ce47-4126-ad21-cca95e1855ae"
RECOVERY_TOKEN = "synthetic-local-recovery-token-00000000001"


class RecoveryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = q.TARGET
    agent_id: str = q.AGENT
    token_sha256: str = hashlib.sha256(RECOVERY_TOKEN.encode()).hexdigest()
    not_before_ms: int
    expires_at_ms: int


def record(**changes: object) -> str:
    now = int(time.time() * 1000)
    value = RecoveryRecord(not_before_ms=now - 1000, expires_at_ms=now + 600_000)
    return json.dumps({**value.model_dump(), **changes})


@contextmanager
def worker(recovery: str | None, *, gate: bool = True, registry: str | None = None):
    port = q.available_port()
    q.URL = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="ct-recovery-qualification-") as state:
        command = [
            str(q.WORKER / "node_modules/.bin/wrangler"),
            "dev", "--local", "--ip", "127.0.0.1", "--port", str(port),
            "--persist-to", state,
            "--var", "CT_PRINCIPALS:" + (q.principals() if registry is None else registry),
            "--var", "CT_CURSOR_KEY:synthetic-local-recovery-cursor-key-00001",
        ]
        if recovery is not None:
            command += ["--var", "CT_RESET_RECOVERY:" + recovery]
        if gate:
            command += ["--var", "CT_RESET_WORKSPACE_ID:" + q.TARGET]
        # All command-line values above are public synthetic fixtures.
        child = subprocess.Popen(
            command, cwd=q.WORKER, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
            env={**os.environ, "WRANGLER_SEND_METRICS": "false", "CLOUDFLARE_LOAD_DEV_VARS_FROM_DOT_ENV": "false"},
        )
        try:
            if registry is None:
                q.wait_ready()
            else:
                import httpx

                deadline = time.monotonic() + 30
                while True:
                    try:
                        q.rpc("ct_connection_status", q.TARGET, RECOVERY_TOKEN)
                        break
                    except httpx.TransportError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("local Worker did not become ready") from None
                        time.sleep(0.2)
            yield
        finally:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=10)


def reset(token: str, workspace: str = q.TARGET, confirmation: str | None = None):
    return q.rpc("ct_workspace_reset", workspace, token, {
        "confirmation": confirmation if confirmation is not None else "reset:" + workspace,
    })


def main() -> None:
    # Retain all reviewed reset checks, including the wrong-object RPC boundary.
    q.main()
    checks: list[str] = ["original reset qualification"]
    with worker(record()):
        status = q.expect(q.rpc("ct_connection_status", q.TARGET, RECOVERY_TOKEN), 200)
        assert status["data"]["workspace_id"] == q.TARGET
        assert status["data"]["roles"] == ["owner"]
        q.expect(q.rpc("ct_connection_status", q.OTHER, RECOVERY_TOKEN), 403, "workspace_denied")
        q.expect(reset(q.TOKENS["reader"]), 403, "capability_required")
        q.expect(reset(RECOVERY_TOKEN, confirmation="wrong"), 403, "workspace_reset_confirmation_required")
        q.expect(reset(RECOVERY_TOKEN, q.OTHER), 403, "workspace_denied")
        q.expect(q.rpc("ct_connection_status", q.TARGET, "wrong-recovery-token-00000000000001"), 401)
        for method in (
            "ct_project_register", "ct_collector_register_source", "ct_collector_recover",
            "ct_collector_publish_observation", "ct_collector_missing_fact_rows",
            "ct_collector_stage_fact_rows", "ct_collector_publish_facts", "ct_collector_heartbeat",
            "ct_collector_publish_living_observation", "ct_fact_read", "ct_remote_living",
        ):
            q.expect(q.rpc(method, q.TARGET, RECOVERY_TOKEN), 403, "capability_required")
        for workspace, token in ((q.TARGET, q.TOKENS["owner"]), (q.OTHER, q.TOKENS["other"])):
            q.expect(q.rpc("ct_project_register", workspace, token, {
                "agent_id": q.AGENT, "display_name": "Synthetic recovery isolation control",
            }), 200)
        q.expect(reset(RECOVERY_TOKEN), 200)
        assert q.expect(q.rpc("ct_workspace_snapshot", q.TARGET, RECOVERY_TOKEN), 200)["data"]["snapshot_sequence"] == 0
        inventory = q.expect(q.rpc("ct_project_inventory_snapshot", q.TARGET, RECOVERY_TOKEN), 200)["data"]
        assert inventory["snapshot_sequence"] == 0 and inventory["projects"] == []
        assert q.expect(q.rpc("ct_workspace_snapshot", q.OTHER, q.TOKENS["other"]), 200)["data"]["snapshot_sequence"] == 1
        assert len(q.expect(q.rpc("ct_project_inventory_snapshot", q.OTHER, q.TOKENS["other"]), 200)["data"]["projects"]) == 1
    checks += ["owner recovery", "method restrictions", "workspace restrictions", "confirmation", "snapshot zero", "other object preserved"]

    with worker(record(), gate=False):
        q.expect(q.rpc("ct_connection_status", q.TARGET, RECOVERY_TOKEN), 200)
        q.expect(reset(RECOVERY_TOKEN), 503, "workspace_reset_unavailable")
    checks.append("reset gate removed")

    now = int(time.time() * 1000)
    invalid = {
        "recovery binding removed": None,
        "malformed binding": "{",
        "expired": record(not_before_ms=now - 2000, expires_at_ms=now - 1000),
        "not yet valid": record(not_before_ms=now + 600_000, expires_at_ms=now + 700_000),
        "excessive lifetime": record(not_before_ms=now - 1000, expires_at_ms=now + 3_600_001),
        "different workspace binding": record(workspace_id=q.OTHER),
        "invalid digest": record(token_sha256="not-a-digest"),
        "unexpected roles": record(roles=["owner", "collect"]),
    }
    for name, value in invalid.items():
        with worker(value):
            q.expect(q.rpc("ct_connection_status", q.TARGET, RECOVERY_TOKEN), 401)
            assert q.expect(q.rpc("ct_connection_status", q.TARGET, q.TOKENS["reader"]), 200)["data"]["roles"] == ["read"]
        checks.append(name)

    with worker(record(token_sha256=hashlib.sha256(q.TOKENS["reader"].encode()).hexdigest())):
        assert q.expect(q.rpc("ct_connection_status", q.TARGET, q.TOKENS["reader"]), 200)["data"]["roles"] == ["read"]
        q.expect(reset(q.TOKENS["reader"]), 403, "capability_required")
    checks.append("existing credential cannot be elevated")
    with worker(record(), registry="{"):
        q.expect(q.rpc("ct_connection_status", q.TARGET, RECOVERY_TOKEN), 503, "authentication_unavailable")
    checks.append("broken principal registry fails closed")
    print(json.dumps({"status": "PASS", "scope": "loopback synthetic only", "checks": checks}))


if __name__ == "__main__":
    main()
