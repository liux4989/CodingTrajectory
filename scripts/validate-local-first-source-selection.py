"""Offline filesystem -> CLI/runtime qualification of local-only selection.

Uses the existing Amp journal evidence, an isolated home/cache, and rejects
socket connection attempts. No credential profiles, remote stubs or unit tests.
"""

from __future__ import annotations

import base64
import json
import os
import runpy
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
META = {"source": "local", "freshness": "live", "content_scope": "retained"}


def qualify() -> None:
    from coding_trajectory.contracts import service_contract
    from coding_trajectory.query import DocumentError, ResourceNotFoundError
    from coding_trajectory.runtime import ServiceRuntime

    failures = []

    def check(condition, description):
        if not condition:
            failures.append(description)

    def forbid_network(*args, **kwargs):
        raise AssertionError("local-only qualification attempted network access")

    socket.socket.connect = forbid_network
    socket.create_connection = forbid_network
    fixtures = runpy.run_path(str(ROOT / "scripts/validate-amp-live.py"))
    parent, child = fixtures["PARENT"], fixtures["CHILD"]
    second = "T-00000000-0000-4000-8000-000000000003"
    unrelated = "T-00000000-0000-4000-8000-000000000004"
    logs = Path(os.environ["CT_AMP_LOG_DIR"])
    logs.mkdir()
    project_dir = Path.home() / "amp-example"
    project_dir.mkdir()
    for thread in (parent, child, second, unrelated):
        rows = fixtures["journal"](thread, parent=thread == parent)
        (logs / f"{thread}.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows)
        )

    def request(method, **params):
        return {"method": method, "params": params}

    def success(runtime, method, **params):
        reply = runtime.execute(request(method, **params))
        assert reply["ok"] is True, reply
        assert reply["meta"] == META, reply
        assert reply["error"] is None
        assert reply["availability"] == {"state": "complete", "missing": []}
        return reply["result"]

    def error(runtime, req, code):
        try:
            reply = runtime.execute(req)
        except (DocumentError, ResourceNotFoundError, ValueError, KeyError) as exc:
            failures.append(
                f"{req}: expected {code}, raised {type(exc).__name__}: {exc}"
            )
            return None
        check(
            reply["ok"] is False
            and reply["result"] is None
            and reply["error"]["code"] == code,
            f"{req}: expected {code}, got {reply}",
        )
        return reply

    for source in ("local", "auto"):
        with ServiceRuntime(
            global_scope=True, current_dir=logs, source=source
        ) as runtime:
            assert success(runtime, "project.list")["items"]
            empty = success(runtime, "project.sessions", project_name="absent-project")
            assert empty["items"] == [] and empty["total"] == 0
            overview = success(runtime, "session.overview", session_id=parent[2:])
            assert overview["root_session_id"] == parent[2:]
            first = success(runtime, "session.items", session_id=parent[2:], limit=1)
            assert first["next_cursor"]
            continuation = success(
                runtime,
                "session.items",
                session_id=parent[2:],
                limit=2,
                cursor=first["next_cursor"],
            )
            assert first["items"][0]["item_id"] not in {
                item["item_id"] for item in continuation["items"]
            }
            error(
                runtime,
                request(
                    "session.overview",
                    session_id="00000000-0000-4000-8000-000000000099",
                ),
                "resource_not_found",
            )
            error(runtime, request("session.overview"), "invalid_request")
            error(
                runtime,
                request("session.items", session_id=parent[2:], cursor="malformed"),
                "invalid_cursor",
            )
            error(
                runtime,
                request(
                    "session.items", session_id=child[2:], cursor=first["next_cursor"]
                ),
                "invalid_cursor",
            )
            version = service_contract("session.items").version
            error(
                runtime,
                {
                    **request("session.items", session_id=parent[2:]),
                    "method_version": version + 1,
                },
                "unsupported_version",
            )
            error(
                runtime,
                {
                    **request("session.items", session_id=parent[2:]),
                    "protocol": "ct.api.retired",
                },
                "unsupported_protocol",
            )
            payload = json.loads(
                base64.urlsafe_b64decode(
                    first["next_cursor"] + "=" * (-len(first["next_cursor"]) % 4)
                )
            )
            payload["version"] = version + 1
            stale_cursor = (
                base64.urlsafe_b64encode(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                )
                .decode()
                .rstrip("=")
            )
            error(
                runtime,
                request("session.items", session_id=parent[2:], cursor=stale_cursor),
                "invalid_cursor",
            )
            error(runtime, request("retired.method"), "unknown_method")

    for source in ("shared", "remote"):
        with ServiceRuntime(
            global_scope=True, current_dir=logs, source=source
        ) as runtime:
            reply = error(runtime, request("project.list"), "method_unavailable")
            if reply is not None:
                check(reply["availability"]["state"] == "unsupported", str(reply))

    # A fresh detail cache makes union loading observable; inventory does not
    # materialize every graph. Invalid items must not widen that union.
    cache_path = Path.home() / ".coding-trajectory" / "local.sqlite"
    cache_path.unlink()
    requests = [
        request("session.overview", session_id=parent[2:]),
        request("session.stats", session_id=child[2:]),
        request("session.overview", session_id=second[2:]),
        request("session.items"),
        request("retired.method"),
        {
            **request("session.overview", session_id=unrelated[2:]),
            "method_version": 999,
        },
    ]
    with ServiceRuntime(global_scope=True, current_dir=logs) as runtime:
        batch = runtime.batch(requests)
        assert batch["meta"] == META
        assert [item["ok"] for item in batch["items"]] == [
            True,
            True,
            True,
            False,
            False,
            False,
        ], batch
        assert [item["error"]["code"] for item in batch["items"][3:]] == [
            "invalid_request",
            "unknown_method",
            "unsupported_version",
        ]
        check(
            runtime.cache.counters["graph_builds"] == 2,
            f"batch union expected two graph builds: {runtime.cache.counters}",
        )
        check(
            runtime.cache.counters["ingested_sources"] == 3,
            f"batch union expected three ingested sources: {runtime.cache.counters}",
        )
        with sqlite3.connect(cache_path) as db:
            roots = {row[0] for row in db.execute("SELECT root FROM graphs")}
            check(
                roots == {parent[2:], second[2:]},
                f"invalid version item widened batch graph union: {sorted(roots)}",
            )
            graph_bytes = "".join(
                row[0] for row in db.execute("SELECT graph FROM graphs")
            )
            assert "PRIVATE output" not in graph_bytes

    # CLI subprocesses inherit the same isolated filesystem and are independently
    # denied network access via Python's socket audit events.
    code = """import sys, runpy
def offline(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        raise AssertionError('CLI attempted network access')
sys.addaudithook(offline)
sys.argv = ['ct', *sys.argv[1:]]
runpy.run_module('coding_trajectory_cli.cli', run_name='__main__')
"""
    for source in ("local", "auto", "shared", "remote"):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                "--source",
                source,
                "project",
                "list",
                "--output",
                "json",
            ],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if source in {"local", "auto"}:
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout)["items"]
        else:
            assert result.returncode != 0, result.stdout
            assert json.loads(result.stderr)["error"]["code"] == "method_unavailable", (
                result.stderr
            )

    for path in logs.glob("*.jsonl"):
        path.unlink()
    with ServiceRuntime(global_scope=True, current_dir=logs) as runtime:
        error(runtime, request("project.list"), "local_source_unavailable")
    assert not failures, "local-only qualification failures:\n" + "\n".join(failures)
    print(
        "PASS local-only: offline filesystem/CLI/runtime, local/auto, unavailable shared/remote, empty filters, typed errors, cursor/version binding, isolated batch union, SQLite privacy"
    )


def main() -> None:
    if sys.argv[1:] == ["--offline-worker"]:
        qualify()
        return
    with TemporaryDirectory(prefix="ct-local-only-") as root:
        env = {
            key: value for key, value in os.environ.items() if not key.startswith("CT_")
        }
        env.update(
            HOME=root,
            CT_AMP_LOG_DIR=str(Path(root) / "amp-logs"),
            CT_QUERY_SOURCE="shared",
            CT_CLOUDFLARE_URL="https://invalid.invalid",
            CT_TELEMETRY="off",
        )
        subprocess.run(
            [sys.executable, __file__, "--offline-worker"],
            cwd=ROOT,
            env=env,
            check=True,
        )


if __name__ == "__main__":
    main()
