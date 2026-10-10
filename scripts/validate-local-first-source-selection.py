"""Offline filesystem -> CLI/runtime qualification of local-only selection.

Uses the existing Amp journal evidence, an isolated home, and rejects
socket connection attempts. No credential profiles, remote stubs or unit tests.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import runpy
import socket
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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

    def filesystem_state():
        return {
            str(path.relative_to(Path.home())): (
                path.is_dir(),
                path.stat().st_size,
                path.stat().st_mtime_ns,
            )
            for path in Path.home().rglob("*")
        }

    def success(runtime, method, **params):
        before = filesystem_state()
        reply = runtime.execute(request(method, **params))
        assert filesystem_state() == before, "Core read changed the filesystem"
        assert reply["ok"] is True, reply
        assert reply["meta"] == META, reply
        assert reply["error"] is None
        assert reply["availability"] == {"state": "complete", "missing": []}
        return reply["result"]

    def error(runtime, req, code):
        before = filesystem_state()
        try:
            reply = runtime.execute(req)
        except (DocumentError, ResourceNotFoundError, ValueError, KeyError) as exc:
            failures.append(
                f"{req}: expected {code}, raised {type(exc).__name__}: {exc}"
            )
            return None
        assert filesystem_state() == before, "Core error changed the filesystem"
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

    # Instrument canonical source loading in this qualification run, rather
    # than maintaining production counters or a persisted graph store.
    from coding_trajectory.service import store as store_module

    original_ingest = store_module._ingest_sessions
    ingested_paths = []

    def observe_ingestion(candidates, **kwargs):
        ingested_paths.append([str(path) for _vendor, _adapter, path in candidates])
        return original_ingest(candidates, **kwargs)

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
    before = filesystem_state()
    with (
        patch.object(store_module, "_ingest_sessions", observe_ingestion),
        ServiceRuntime(global_scope=True, current_dir=logs) as runtime,
    ):
        batch = runtime.batch(requests)
        assert filesystem_state() == before, "Core batch changed the filesystem"
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
        expected_paths = {
            str((logs / f"{sid}.jsonl").resolve()) for sid in (parent, child, second)
        }
        check(
            len(ingested_paths) == 1 and set(ingested_paths[0]) == expected_paths,
            f"batch must ingest only the valid run union once: {ingested_paths}",
        )
        assert "PRIVATE output" not in json.dumps(batch)

    # A long-lived runtime must discard request state, including after a batch.
    # Inventory must never invoke canonical ingestion.
    ingested_paths.clear()
    with (
        patch.object(store_module, "_ingest_sessions", observe_ingestion),
        ServiceRuntime(global_scope=True, current_dir=logs) as runtime,
    ):
        initial_inventory = success(runtime, "project.sessions", limit=200)
        assert not ingested_paths, "inventory ingested transcripts"
        initial_items = success(runtime, "session.items", session_id=second[2:])
        source = logs / f"{second}.jsonl"
        original = source.read_text()
        updated = fixtures["journal"](second, parent=False)[-2]
        updated["message"]["content"] = [
            {"type": "text", "text": "fresh request evidence"}
        ]
        updated["captured_at"] = "2026-09-05T00:00:11Z"
        with source.open("a") as stream:
            stream.write(json.dumps(updated) + "\n")
        changed = success(runtime, "session.items", session_id=second[2:])
        assert changed["items"][0]["preview"] == "fresh request evidence", changed
        source.write_text(original)
        restored = success(runtime, "session.items", session_id=second[2:])
        assert restored == initial_items
        runtime.batch([request("session.stats", session_id=parent[2:])])
        child_source = logs / f"{child}.jsonl"
        child_contents = child_source.read_text()
        child_source.unlink()
        removed = success(runtime, "project.sessions", limit=200)
        assert all(child[2:] not in row["session_ids"] for row in removed["items"])
        child_source.write_text(child_contents)
        returned = success(runtime, "project.sessions", limit=200)
        assert returned["items"] and returned["total"] == initial_inventory["total"]
        assert any(child[2:] in row["session_ids"] for row in returned["items"])
    print(
        "PASS request freshness: append, rewrite, delete, restore; header-only inventory"
    )

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
        before = filesystem_state()
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
        assert filesystem_state() == before, "CLI query changed the filesystem"
        if source in {"local", "auto"}:
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout)["items"]
        else:
            assert result.returncode != 0, result.stdout
            assert json.loads(result.stderr)["error"]["code"] == "method_unavailable", (
                result.stderr
            )

    # Inventory must reject metadata-only sources without rejecting runtime-only
    # sessions that canonical ingestion accepts. Compare both directions, not
    # just membership of roots that happen to exist in both projections.
    from coding_trajectory.analysis.orchestration_runs import orchestration_runs
    from coding_trajectory.discovery import discover_store

    timestamp = "2026-10-01T12:00:00Z"
    claude_dir = Path.home() / ".claude/projects" / str(project_dir).replace("/", "-")
    pi_dir = Path.home() / ".pi/agent/sessions/qualification"
    extra_paths = []

    def write_source(path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        extra_paths.append(path)

    claude_records = [
        {"type": "user", "message": {"content": "acceptance"}},
        {"type": "assistant", "message": {"content": []}},
        *(
            {"type": "system", "subtype": value}
            for value in ("compact_boundary", "turn_duration", "local_command")
        ),
        {"type": "attachment", "attachment": {}},
        {"type": "queue-operation", "operation": "enqueue"},
        {"type": "file-history-snapshot", "snapshot": {"timestamp": timestamp}},
        *(
            {"type": value}
            for value in (
                "mode",
                "permission-mode",
                "system",
                "cost-state",
                "ai-title",
                "agent-name",
            )
        ),
        {"type": "user", "isMeta": True, "message": {"content": "metadata"}},
        {"type": "assistant", "timestamp": "invalid", "message": {}},
    ]
    for ordinal, record in enumerate(claude_records, 100):
        sid = f"00000000-0000-4000-8000-{ordinal:012d}"
        write_source(
            claude_dir / f"{sid}.jsonl",
            [
                *(
                    [
                        {
                            "type": "queue-operation",
                            "operation": "enqueue",
                            "sessionId": sid,
                            "timestamp": timestamp,
                        }
                    ]
                    if ordinal == 100
                    else []
                ),
                {
                    "sessionId": sid,
                    "cwd": str(project_dir),
                    "timestamp": timestamp,
                    **record,
                },
            ],
        )
    for ordinal, role in enumerate(
        ("user", "assistant", "toolResult", "bashExecution"), 200
    ):
        sid = f"00000000-0000-4000-8000-{ordinal:012d}"
        write_source(
            pi_dir / f"{sid}.jsonl",
            [
                {"type": "session", "id": sid, "cwd": str(project_dir)},
                {
                    "type": "message",
                    "timestamp": timestamp,
                    "message": {"role": role, "content": []},
                },
            ],
        )
    write_source(pi_dir / "state-only.jsonl", [{"type": "session", "id": "state-only"}])
    write_source(pi_dir / "parent.jsonl.subagents/manifest.jsonl", [{"agent": "child"}])

    codex_dir = Path.home() / ".codex/sessions/qualification"
    codex_parent = "00000000-0000-4000-8000-000000000300"
    for ordinal in (300, 301, 302):
        sid = f"00000000-0000-4000-8000-{ordinal:012d}"
        relationship = (
            {
                "source": {
                    "subagent": {"thread_spawn": {"parent_thread_id": codex_parent}}
                }
            }
            if ordinal == 301
            else {"forked_from_id": codex_parent}
            if ordinal == 302
            else {}
        )
        write_source(
            codex_dir / f"rollout-{sid}.jsonl",
            [
                {
                    "type": "session_meta",
                    "timestamp": timestamp,
                    "payload": {"id": sid, "cwd": str(project_dir), **relationship},
                },
                {
                    "type": "event_msg",
                    "timestamp": timestamp,
                    "payload": {"type": "task_started", "turn_id": f"turn-{ordinal}"},
                },
                {
                    "type": "response_item",
                    "timestamp": timestamp,
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "accepted"}],
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": timestamp,
                    "payload": {"type": "task_complete", "turn_id": f"turn-{ordinal}"},
                },
            ],
        )

    for ordinal, activity in (
        (303, {}),
        (304, {"base_instructions": {"text": "context"}}),
    ):
        sid = f"00000000-0000-4000-8000-{ordinal:012d}"
        write_source(
            codex_dir / f"rollout-{sid}.jsonl",
            [
                {
                    "type": "session_meta",
                    "timestamp": timestamp,
                    "payload": {"id": sid, "cwd": str(project_dir), **activity},
                }
            ],
        )

    for ordinal, activity in (
        (400, []),
        (401, [{"type": "observation", "event": "agent.start", "message_id": "u1"}]),
        (
            402,
            [
                {
                    "type": "message",
                    "message": {"id": "u1", "role": "user", "content": []},
                }
            ],
        ),
        (
            403,
            [
                {
                    "type": "message",
                    "message": {
                        "id": "a1",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "activity"}],
                    },
                }
            ],
        ),
        (
            404,
            [
                {
                    "type": "message",
                    "message": {
                        "id": "a1",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "replaced"}],
                    },
                },
                {
                    "type": "message",
                    "message": {"id": "a1", "role": "assistant", "content": []},
                },
            ],
        ),
        (
            405,
            [
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "missing identity"}],
                    },
                }
            ],
        ),
    ):
        thread = f"T-00000000-0000-4000-8000-{ordinal:012d}"
        header = fixtures["journal"](thread, parent=False)[0]
        write_source(
            logs / f"{thread}.jsonl",
            [
                header,
                *(
                    {
                        "schema_version": 1,
                        "captured_at": timestamp,
                        "thread_id": thread,
                        **row,
                    }
                    for row in activity
                ),
            ],
        )

    canonical = discover_store(current_dir=project_dir, global_scope=True).store
    expected = {
        str(run.root_session_id): (
            str(graph.root_session_id),
            tuple(sorted(str(s.session_id) for s in run.sessions)),
        )
        for graph in canonical.session_graphs.values()
        for run in orchestration_runs(graph)
    }
    with ServiceRuntime(global_scope=True, current_dir=project_dir) as runtime:
        inventory = success(runtime, "project.sessions", limit=200)
        actual = {
            row["root_session_id"]: (
                row["lineage_root_session_id"],
                tuple(sorted(row["session_ids"])),
            )
            for row in inventory["items"]
        }
        assert actual == expected, {
            "inventory_only": actual.keys() - expected.keys(),
            "canonical_only": expected.keys() - actual.keys(),
        }
        for root in actual:
            assert success(runtime, "session.stats", session_id=root)
        claude = success(
            runtime,
            "living.sessions",
            session_id="00000000-0000-4000-8000-000000000100",
        )
        assert claude["items"][0]["cwd"] == str(project_dir), (
            "Claude cwd must come from available record metadata, even when the "
            "first session record has none"
        )
    print(
        f"PASS source acceptance: {len(extra_paths) + 4} sources, {len(expected)} runs, zero root/member/lineage mismatches; every inventory root resolves"
    )

    timings = []
    for _ in range(2):
        before = filesystem_state()
        started = time.perf_counter()
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                "project",
                "sessions",
                "--global-scope",
                "--output",
                "json",
            ],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        timings.append(time.perf_counter() - started)
        assert filesystem_state() == before, "CLI inventory changed the filesystem"
        assert json.loads(result.stdout)["total"] == len(expected)
    print(
        f"Inventory timing (fixture CLI, separate processes): cold={timings[0]:.3f}s repeat={timings[1]:.3f}s; no persistent cache"
    )

    # Parent-aware admission must agree with full ingestion, including the
    # boundaries where a simple non-parent-turn-id check would be incorrect.
    inherited_rows = [
        json.loads(line)
        for line in (codex_dir / f"rollout-{codex_parent}.jsonl")
        .read_text()
        .splitlines()
    ]

    def lifecycle(kind, turn_id):
        return {
            "type": "event_msg",
            "timestamp": timestamp,
            "payload": {"type": kind, "turn_id": turn_id},
        }

    segment = [
        inherited_rows[0],
        lifecycle("task_started", "parent-segment"),
        lifecycle("task_complete", "parent-segment"),
    ]
    write_source(codex_dir / f"rollout-{codex_parent}-segment.jsonl", segment)
    fork_cases = (
        ("inherited-only", {}, [], False),
        ("base-instructions", {"base_instructions": {"text": "own context"}}, [], True),
        (
            "compaction",
            {},
            [{"type": "compacted", "timestamp": timestamp, "payload": {}}],
            True,
        ),
        (
            "completed-owned",
            {},
            [lifecycle("task_started", "own"), lifecycle("task_complete", "own")],
            True,
        ),
        ("in-progress-owned", {}, [lifecycle("task_started", "own")], True),
        (
            "nonfinal-incomplete",
            {},
            [lifecycle("task_started", "own"), *inherited_rows[1:]],
            False,
        ),
        (
            "interleaved-parent",
            {},
            [
                lifecycle("task_started", "own"),
                *inherited_rows[1:],
                lifecycle("task_complete", "own"),
            ],
            True,
        ),
        (
            "inherited-other-segment",
            {},
            segment[1:],
            False,
        ),
        (
            "missing-parent",
            {"forked_from_id": "00000000-0000-4000-8000-000000000999"},
            [],
            True,
        ),
    )
    for ordinal, (label, metadata, activity, admitted) in enumerate(fork_cases, 305):
        sid = f"00000000-0000-4000-8000-{ordinal:012d}"
        rows = [
            {
                **inherited_rows[0],
                "payload": {
                    **inherited_rows[0]["payload"],
                    "id": sid,
                    "forked_from_id": codex_parent,
                    **metadata,
                },
            },
            *inherited_rows[1:],
            *activity,
        ]
        write_source(codex_dir / f"rollout-{sid}.jsonl", rows)
        canonical = discover_store(current_dir=project_dir, global_scope=True).store
        assert (sid in {str(value) for value in canonical.sessions}) == admitted, label
        expected = {
            str(run.root_session_id): (
                str(graph.root_session_id),
                tuple(sorted(str(session.session_id) for session in run.sessions)),
            )
            for graph in canonical.session_graphs.values()
            for run in orchestration_runs(graph)
        }
        with ServiceRuntime(global_scope=True, current_dir=project_dir) as runtime:
            with patch.object(
                store_module,
                "_ingest_sessions",
                side_effect=AssertionError("parent-aware inventory built transcripts"),
            ):
                inventory = success(runtime, "project.sessions", limit=200)["items"]
            actual = {
                row["root_session_id"]: (
                    row["lineage_root_session_id"],
                    tuple(sorted(row["session_ids"])),
                )
                for row in inventory
            }
            assert actual == expected, label
            if admitted:
                assert success(runtime, "session.stats", session_id=sid)
            else:
                error(
                    runtime,
                    request("session.stats", session_id=sid),
                    "resource_not_found",
                )
    print(
        f"PASS parent-aware Codex admission: {len(fork_cases)} ownership/metadata cases, segmented parent union, strict root/member/lineage parity, no transcript ingestion"
    )
    for path in extra_paths:
        path.unlink()

    for path in logs.glob("*.jsonl"):
        path.unlink()
    with ServiceRuntime(global_scope=True, current_dir=logs) as runtime:
        error(runtime, request("project.list"), "local_source_unavailable")
    assert not (Path.home() / ".coding-trajectory").exists()
    assert not failures, "local-only qualification failures:\n" + "\n".join(failures)
    print(
        "PASS local-only: offline filesystem/CLI/runtime, local/auto, unavailable shared/remote, empty filters, typed errors, cursor/version binding, isolated batch union, no filesystem writes"
    )


def qualify_living() -> None:
    """Exercise stateless living reads against mutable, disposable source logs."""
    from coding_trajectory.runtime import ServiceRuntime
    from coding_trajectory.service import store as store_module

    fixtures = runpy.run_path(str(ROOT / "scripts/validate-amp-live.py"))
    parent, child = fixtures["PARENT"], fixtures["CHILD"]
    old = "T-00000000-0000-4000-8000-000000000003"
    logs = Path(os.environ["CT_AMP_LOG_DIR"])
    paths = {sid[2:]: logs / f"{sid}.jsonl" for sid in (parent, child, old)}
    for sid in (parent, child, old):
        paths[sid[2:]].write_text(
            "".join(
                json.dumps(row) + "\n"
                for row in fixtures["journal"](sid, parent=sid == parent)
            )
        )
    old_time = time.time() - 4 * 86400
    os.utime(paths[old[2:]], (old_time, old_time))

    def filesystem_state():
        return {
            str(path.relative_to(Path.home())): (
                path.is_dir(),
                path.stat().st_size,
                path.stat().st_mtime_ns,
            )
            for path in Path.home().rglob("*")
        }

    def digest(payload):
        # Derive expectations from the public wire format, not Core helpers.
        return hashlib.sha256(
            json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()

    with ServiceRuntime(global_scope=True, current_dir=logs) as runtime:

        def read(method, **params):
            before = filesystem_state()
            reply = runtime.execute({"method": method, "params": params})
            assert filesystem_state() == before, "living read wrote derived state"
            assert reply["ok"], reply
            return reply["result"]

        def rejected(method, code, **params):
            before = filesystem_state()
            reply = runtime.execute({"method": method, "params": params})
            assert filesystem_state() == before
            assert not reply["ok"] and reply["error"]["code"] == code, reply

        def complete(method, field, **params):
            result = read(method, limit=1, **params)
            rows = list(result[field])
            cursor = result["next_cursor"]
            cursors = set()
            while cursor:
                assert cursor not in cursors, "living pagination did not advance"
                cursors.add(cursor)
                page = read(method, limit=2, cursor=cursor, **params)
                assert page["returned"] == len(page[field])
                rows.extend(page[field])
                cursor = page["next_cursor"]
            assert len(rows) == result["total"]
            return rows

        # Header-level inventory must not invoke transcript ingestion at all.
        with patch.object(
            store_module,
            "_ingest_sessions",
            side_effect=AssertionError("living.sessions ingested transcripts"),
        ):
            initial = complete("living.sessions", "items")
            repeated = complete("living.sessions", "items")
            assert initial == repeated
            assert [row["session_id"] for row in initial] == [parent[2:], child[2:]]
            assert all(row["state"] == "living" for row in initial)
            assert all(
                row["digest"] == digest({k: v for k, v in row.items() if k != "digest"})
                for row in initial
            )
            assert not any(
                {"model", "turn_count", "source_readiness"} & row.keys()
                for row in initial
            )
            assert {
                row["session_id"]
                for row in complete("living.sessions", "items", horizon_days=5)
            } == {parent[2:], child[2:], old[2:]}
            assert [
                row["session_id"]
                for row in complete("living.sessions", "items", root_session_id=old[2:])
            ] == [old[2:]]
            assert {
                row["session_id"]
                for row in complete("living.sessions", "items", session_id=child[2:])
            } == {parent[2:], child[2:]}
            assert not read("living.sessions", project_name="absent-project")["items"]

        first = read("living.sessions", limit=1)
        rejected(
            "living.sessions",
            "invalid_cursor",
            root_session_id=parent[2:],
            cursor=first["next_cursor"],
        )
        rejected(
            "living.sessions",
            "invalid_request",
            session_id=child[2:],
            project_name="amp-example",
        )
        rejected("living.sessions", "invalid_request", horizon_days=31)
        rejected("living.sessions", "invalid_request", after="retired")
        rejected("living.events", "invalid_request")
        rejected("living.events", "invalid_request", scope={})
        rejected(
            "living.events",
            "invalid_request",
            scope={"root_session_id": parent[2:], "session_id": child[2:]},
        )

        scope = {"root_session_id": parent[2:]}
        initial_events = complete(
            "living.events", "resources", scope=scope, mode="details"
        )
        assert initial_events == complete(
            "living.events", "resources", scope=scope, mode="details"
        )
        assert all(row["digest"] == digest(row["resource"]) for row in initial_events)
        event_page = read("living.events", scope=scope, limit=1)
        rejected(
            "living.events",
            "invalid_cursor",
            scope={"session_id": child[2:]},
            cursor=event_page["next_cursor"],
        )
        rejected(
            "living.events", "invalid_cursor", scope=scope, cursor=first["next_cursor"]
        )

        def append(event, second, **fields):
            with paths[parent[2:]].open("a") as stream:
                stream.write(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "type": "observation",
                            "thread_id": parent,
                            "captured_at": f"2026-09-05T00:00:{second:02d}Z",
                            "event": event,
                            **fields,
                        }
                    )
                    + "\n"
                )

        append("agent.start", 11, message_id="u2")
        append(
            "tool.call",
            12,
            tool_use_id="empty-result",
            tool_name="read_file",
            input={"path": "α" * 600},
        )
        changed_sessions = complete("living.sessions", "items")
        assert changed_sessions[0]["digest"] != initial[0]["digest"]
        assert changed_sessions[1] == initial[1], "append changed an unrelated session"
        pending = complete("living.events", "resources", scope=scope, mode="details")
        tool = next(
            row for row in pending if row["resource"].get("operations") == ["read_file"]
        )
        assert tool["resource"].get("status") != "completed"
        append(
            "tool.result",
            13,
            tool_use_id="empty-result",
            tool_name="read_file",
            status="done",
            output="",
        )
        completed = complete("living.events", "resources", scope=scope, mode="details")
        done = next(row for row in completed if row["path"] == tool["path"])
        assert done["resource"]["status"] == "completed"
        assert done["digest"] != tool["digest"]
        assert done["digest"] == digest(done["resource"])
        view = complete("living.events", "resources", scope=scope)
        compact = next(row for row in view if row["path"] == done["path"])
        assert compact["digest"] == done["digest"]
        # Raw inputs are not retained. Exercise view truncation using a public
        # tool name below retention's 512-character cap, above view's 500 cap.
        name = "qualified_tool_" + "α" * 490
        append("tool.call", 14, tool_use_id="view-boundary", tool_name=name, input={})
        details = complete("living.events", "resources", scope=scope, mode="details")
        large = next(
            row for row in details if row["resource"].get("operations") == [name]
        )
        compact = next(
            row
            for row in complete("living.events", "resources", scope=scope)
            if row["path"] == large["path"]
        )
        assert large["resource"]["shape"]["tool_name"] == name
        assert compact["resource"]["shape"]["tool_name"]["$type"] == "content_ref"
        assert compact["digest"] == large["digest"] == digest(large["resource"])
        assert "PRIVATE output" not in json.dumps(completed)
        for field in ("item_id", "turn_id"):
            rejected(
                "living.events", "invalid_request", scope={field: done["path"][field]}
            )
            selected = complete(
                "living.events",
                "resources",
                scope={**scope, field: done["path"][field]},
                mode="details",
            )
            assert selected and all(
                row["path"][field] == done["path"][field] for row in selected
            )

        ingested = []
        original_ingest = store_module._ingest_sessions

        def observe_ingestion(candidates, **kwargs):
            ingested.append(
                {str(path.resolve()) for _vendor, _adapter, path in candidates}
            )
            return original_ingest(candidates, **kwargs)

        other_run = complete(
            "living.events",
            "resources",
            scope={"root_session_id": old[2:]},
            mode="details",
        )
        other_item = next(row for row in other_run if row["resource_kind"] == "item")
        with patch.object(store_module, "_ingest_sessions", observe_ingestion):
            for field in ("item_id", "turn_id"):
                rejected(
                    "living.events",
                    "resource_not_found",
                    scope={**scope, field: other_item["path"][field]},
                )
        required = {str(paths[sid].resolve()) for sid in (parent[2:], child[2:])}
        assert ingested == [required, required], "narrowing searched another run"
        ingested.clear()
        before = filesystem_state()
        with patch.object(store_module, "_ingest_sessions", observe_ingestion):
            batch = runtime.batch(
                [
                    {"method": "living.events", "params": {"scope": scope}},
                    {"method": "session.stats", "params": {"session_id": child[2:]}},
                    {
                        "method": "living.sessions",
                        "params": {"root_session_id": parent[2:]},
                    },
                ]
            )
        assert filesystem_state() == before
        assert all(row["ok"] for row in batch["items"]), batch
        assert ingested == [
            {str(paths[sid].resolve()) for sid in (parent[2:], child[2:])}
        ], ingested

        # Let only the parent's source cross the real 300-second boundary.
        # No log append, timestamp rewrite, or fake per-run liveness is involved.
        expires = time.time() + 5
        parent_time = expires - 300
        os.utime(paths[parent[2:]], (parent_time, parent_time))
        os.utime(paths[child[2:]], None)
        before_expiry = complete("living.sessions", "items", root_session_id=parent[2:])
        assert [row["state"] for row in before_expiry] == ["living", "living"]
        time.sleep(max(0, expires - time.time()) + 0.1)
        after_expiry = complete("living.sessions", "items", root_session_id=parent[2:])
        assert after_expiry[0]["state"] == "inactive"
        assert after_expiry[0]["digest"] != before_expiry[0]["digest"]
        assert {
            k: v for k, v in after_expiry[0].items() if k not in {"state", "digest"}
        } == {k: v for k, v in before_expiry[0].items() if k not in {"state", "digest"}}
        assert after_expiry[1] == before_expiry[1], "expiry leaked across the run"
        os.utime(paths[parent[2:]], (old_time, old_time))
        recent_child = complete("living.sessions", "items")
        assert {row["session_id"] for row in recent_child} == {parent[2:], child[2:]}
        paths[child[2:]].unlink()
        assert not read("living.sessions")["items"], (
            "deleted child kept the old run in horizon"
        )
        remaining = complete("living.events", "resources", scope=scope)
        assert all(row["path"].get("session_id") != child[2:] for row in remaining)
        assert not any(row["resource_kind"] == "session_edge" for row in remaining)
    assert not (Path.home() / ".coding-trajectory").exists()
    print(
        "PASS stateless living: stable wire digests, header-only inventory, horizon, live paging/scopes, empty-output tool completion, retained views, directly routed narrowing, mixed batch, own-source expiry, removals, no writes"
    )


def main() -> None:
    if sys.argv[1:] == ["--offline-worker"]:
        qualify()
        qualify_living()
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
