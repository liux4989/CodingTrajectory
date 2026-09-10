#!/usr/bin/env python3
"""Exercise the actual snapshot Worker and local assets in workerd, without auth bypass in deployed code."""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "packages/plugins/datahub/web"
DATA = WEB / "dist/_snapshot"


def main():
    manifest = json.loads((DATA / "manifest.json").read_text())
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((DATA / name).read_bytes()).hexdigest() == digest
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="ct-snapshot-qualification-") as tmp:
        directory = Path(tmp)
        # The harness exists only in a temporary local directory; deployment
        # always uses snapshot.ts, whose entrypoint verifies Access first.
        (directory / "worker.ts").write_text(
            f"import worker, {{ dispatch }} from {json.dumps(str(WEB / 'worker/snapshot.ts'))};\n"
            "export default { async fetch(request, env) {\n"
            "if (new URL(request.url).pathname.startsWith('/guard/')) return worker.fetch(request,env);\n"
            "try { return Response.json(await dispatch(await request.json(),env)); }\n"
            "catch(error) { return Response.json({error:true},{status:error.status ?? 503}); }\n"
            "}};\n"
        )
        config = directory / "wrangler.jsonc"
        config.write_text(
            json.dumps(
                {
                    "name": "local-snapshot-qualification",
                    "main": "worker.ts",
                    "compatibility_date": "2026-09-10",
                    "compatibility_flags": ["nodejs_compat"],
                    "assets": {
                        "directory": str(WEB / "dist"),
                        "binding": "ASSETS",
                        "run_worker_first": True,
                        "not_found_handling": "single-page-application",
                    },
                    "vars": {
                        "CF_ACCESS_TEAM_DOMAIN": "https://example.cloudflareaccess.com",
                        "CF_ACCESS_AUD": "qualification",
                    },
                }
            )
        )
        with (directory / "runtime.log").open("w+") as log:
            process = subprocess.Popen(
                [
                    str(WEB / "node_modules/.bin/wrangler"),
                    "dev",
                    "--local",
                    "--config",
                    str(config),
                    "--port",
                    str(port),
                    "--ip",
                    "127.0.0.1",
                    "--show-interactive-dev-session=false",
                ],
                cwd=WEB,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            calls = 0

            def post(method, expected=200, **params):
                nonlocal calls
                url = f"http://127.0.0.1:{port}/api/datahub/query"
                request = Request(
                    url,
                    data=json.dumps(
                        {
                            "protocol": "ct.datahub.v1",
                            "id": None,
                            "method": method,
                            "params": params,
                        }
                    ).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    response = urlopen(request, timeout=20)
                except HTTPError as error:
                    response = error
                with response:
                    assert response.status == expected, (
                        method,
                        response.status,
                        expected,
                    )
                    envelope = json.load(response)
                calls += 1
                if expected != 200:
                    return envelope
                if envelope.get("ok") is False:
                    return envelope
                assert envelope["protocol"] == "ct.datahub.v1"
                assert envelope["error"] is None
                return envelope["data"]

            def signed_out(path: str):
                try:
                    response = urlopen(f"http://127.0.0.1:{port}{path}", timeout=20)
                except HTTPError as error:
                    response = error
                with response:
                    assert response.status == 403

            try:
                deadline = time.monotonic() + 45
                while True:
                    try:
                        snapshot = post("datahub.snapshot")
                        break
                    except URLError:
                        if process.poll() is not None or time.monotonic() > deadline:
                            log.seek(0)
                            raise RuntimeError(log.read()[-3000:]) from None
                        time.sleep(0.2)
                assert snapshot == json.loads((DATA / "snapshot.json").read_text())
                signed_out("/guard/")
                signed_out("/guard/_snapshot/snapshot.json")
                assert post("datahub.refresh")["data"] is None
                assert post("session.events")["data"] is None
                post("sessions", expected=400, since_days=8)
                post("session.graph", expected=400, session_id="../snapshot")
                revision = snapshot["revision"]
                assert not post("datahub.changes", after_revision=revision)[
                    "reset_required"
                ]
                assert post("datahub.changes", after_revision=revision - 1)[
                    "reset_required"
                ]
                for days in range(1, 8):
                    expected = json.loads((DATA / f"sessions-{days}.json").read_text())
                    rows, cursor = [], None
                    while True:
                        params = {"since_days": days, "limit": 3}
                        if cursor:
                            params["cursor"] = cursor
                        payload = post("sessions", **params)
                        rows.extend(payload["items"])
                        cursor = payload["page"]["next_cursor"]
                        if cursor is None:
                            break
                    assert rows == expected
                projects = json.loads((DATA / "projects.json").read_text())
                assert post("projects", limit=200)["items"] == projects
                for project in projects:
                    rows = json.loads((DATA / "sessions-7.json").read_text())
                    for vendor in project["vendors"]:
                        expected = [
                            row
                            for row in rows
                            if row["project"] == project["name"]
                            and vendor in row["vendors"]
                        ]
                        assert (
                            post(
                                "sessions",
                                project_name=project["name"],
                                agent_vendor=vendor,
                                limit=200,
                            )["items"]
                            == expected
                        )
                for kind, route in [("trees", "tree"), ("graphs", "graph")]:
                    for file in (DATA / kind).glob("*.json"):
                        assert post(
                            f"session.{route}", session_id=file.stem
                        ) == json.loads(file.read_text())
                sample = []
                for file in (DATA / "items").glob("*.json"):
                    sample.extend(list(json.loads(file.read_text()).values())[:10])
                post(
                    "session.items",
                    expected=404,
                    item_ids=[sample[0]["item_id"]],
                    include_content=True,
                )
                assert (
                    post(
                        "session.items",
                        item_ids=[row["item_id"] for row in sample],
                    )
                    == sample
                )
                print(
                    json.dumps(
                        {
                            "status": "pass",
                            "runtime": "workerd",
                            "calls": calls,
                            "revision": revision,
                            "asset_digest_checks": len(manifest["files"]),
                        }
                    )
                )
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    main()
