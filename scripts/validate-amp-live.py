"""Offline integration acceptance: Amp journal -> discovery -> shared APIs.

Synthetic source evidence: one parent turn (10 seconds), one create_thread
call (2 seconds), one failed shell call, and one child turn. No network writes,
provider exports, pricing guesses, or expected-metric regeneration.
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from coding_trajectory.analysis.measurements import attach_measurements
from coding_trajectory.discovery import discover_store, stabilize_session
from coding_trajectory.ingestion.adapters.amp import AmpAdapter
from coding_trajectory.ingestion.graph import assemble_project_session_graphs
from coding_trajectory.ingestion.incremental import (
    SourceSnapshot,
    plan_session_graph_components_from_files,
    rebuild_affected_session_graphs_from_files,
)
from coding_trajectory.ingestion.models import Vendor
from coding_trajectory.ingestion.retained import retain_session_graph
from coding_trajectory.query import DocumentStore
from coding_trajectory.service.handlers import dispatch
from coding_trajectory.service.store import IndexCache, resolve_store
from coding_trajectory_cli._shared import compact_payload

PARENT = "T-00000000-0000-4000-8000-000000000001"
CHILD = "T-00000000-0000-4000-8000-000000000002"


def journal(thread: str, *, parent: bool) -> list[dict]:
    def row(kind: str, second: int, **fields) -> dict:
        return {
            "schema_version": 1,
            "type": kind,
            "captured_at": f"2026-09-05T00:00:{second:02d}Z",
            **fields,
        }

    def observe(event: str, second: int, **fields) -> dict:
        return row("observation", second, thread_id=thread, event=event, **fields)

    def message(mid: str, role: str, second: int, blocks: list) -> dict:
        return row(
            "message",
            second,
            thread_id=thread,
            message={"id": mid, "role": role, "content": blocks},
        )

    rows = [
        row(
            "thread",
            0,
            payload={
                "id": thread,
                "parent_thread_id": None,
                "workspace_root": "file:///project/amp-example",
            },
        ),
        observe("agent.start", 0, message_id="u1"),
        message("u1", "user", 0, [{"type": "text", "text": "PRIVATE task"}]),
    ]
    if parent:
        output = json.dumps(
            {"threadID": CHILD, "executor": "orb", "agentMode": "medium"}
        )
        rows += [
            observe(
                "tool.call",
                1,
                tool_use_id="spawn1",
                tool_name="create_thread",
                input={},
            ),
            observe(
                "tool.result",
                3,
                tool_use_id="spawn1",
                tool_name="create_thread",
                status="done",
                output=output,
            ),
            observe(
                "tool.call",
                4,
                tool_use_id="shell1",
                tool_name="shell_command",
                input={"command": "exit 23"},
            ),
            observe(
                "tool.result",
                5,
                tool_use_id="shell1",
                tool_name="shell_command",
                status="done",
                output=json.dumps({"exitCode": 23, "output": "PRIVATE output"}),
            ),
            message(
                "a1",
                "assistant",
                6,
                [
                    {
                        "type": "tool_use",
                        "id": "spawn1",
                        "name": "create_thread",
                        "input": {},
                    }
                ],
            ),
            message(
                "r1",
                "user",
                6,
                [
                    {
                        "type": "tool_result",
                        "toolUseID": "spawn1",
                        "status": "done",
                        "output": output,
                    }
                ],
            ),
        ]
    rows += [
        message("a2", "assistant", 10, [{"type": "text", "text": "PRIVATE final"}]),
        observe("agent.end", 10, message_id="u1", status="done"),
    ]
    return rows


def qualify_result_formats(source: Path) -> None:
    # Current PluginToolResult permits strings or text/image arrays; live hooks
    # also expose objects. All variants use the same explicit synthetic evidence:
    # one child ID and exit 23, never expected values derived from parser output.
    image = {
        "type": "image",
        "mimeType": "image/png",
        "url": "https://invalid.example/private",
    }
    formats = {
        "string": (lambda text: text, True),
        "object": (json.loads, True),
        "text-block": (lambda text: [{"type": "text", "text": text}], True),
        "mixed-image": (lambda text: [image, {"type": "text", "text": text}], True),
        "ambiguous": (lambda text: [{"type": "text", "text": text}] * 2, False),
        "image-only": (lambda text: [image], False),
        "non-object": (lambda text: [{"type": "text", "text": "[]"}], False),
    }
    for name, (encode, recognized) in formats.items():
        rows = journal(PARENT, parent=True)
        for row in rows:
            if row.get("event") == "tool.result":
                row["output"] = encode(row["output"])
            for block in row.get("message", {}).get("content", []):
                if block["type"] == "tool_result":
                    block["output"] = encode(block["output"])
        session = AmpAdapter().build_canonical_session(source, rows + rows)
        tools = [
            i
            for t in session.turns
            for i in t.items
            if getattr(i, "tool_call_id", None)
        ]
        assert len(tools) == 2, name
        shell = next(i for i in tools if i.tool_call_id == "shell1")
        assert (shell.status == "failed") == recognized, name
        assert session.extensions.amp.spawn_links == (
            {CHILD[2:]: "spawn1"} if recognized else {}
        ), name
        replay = AmpAdapter().build_canonical_session(
            source, [r for r in rows if r["type"] != "observation"]
        )
        assert not replay.extensions.amp.spawn_links, name
        graph = assemble_project_session_graphs("amp-example", [session])[0]
        retained = retain_session_graph(graph)
        assert "PRIVATE output" not in retained.model_dump_json(), name
        assert retain_session_graph(retained) == retained, name
    print(
        "PASS Amp result formats: 7 legacy/structured/ambiguous variants, dedup, live-only edges, no raw tool output"
    )


def qualify_throughput(source: Path) -> None:
    # Independent cl100k_base source arithmetic (not metric-output-derived):
    # {} = 1; {"command": "exit 23"} = 8; hello world = 2;
    # Let us reason. = 4; a b c d e f = 6.
    # Turn 1: 1 + 8 + 2 + 4 + 2 = 17 tokens. Parallel tool windows
    # [1,3] and [2,5] have union 4s: 17/(10-4) = 2.833.
    # Turn 2: 6/4 = 1.5. Session: (17+6)/(6+4) = 2.3,
    # NOT the average of the rates, and NOT including 20s of user idle time.
    field = "estimated_output_tokens_per_second"
    rows = journal(PARENT, parent=True)
    for row in rows:
        if row.get("tool_use_id") == "shell1" and row.get("event") == "tool.call":
            row["observed_at"] = "2026-09-05T00:00:02Z"
        if row.get("message", {}).get("id") == "a2":
            row["message"]["content"] = [
                {"type": "text", "text": "hello world"},
                {"type": "thinking", "thinking": "Let us reason."},
            ]
    final = next(row for row in rows if row.get("message", {}).get("id") == "a2")
    revision = copy.deepcopy(final)
    revision["captured_at"] = "2026-09-05T00:00:08Z"
    revision["message"]["content"] = [{"type": "text", "text": "obsolete revision"}]
    repeated_text = copy.deepcopy(final)
    repeated_text["captured_at"] = "2026-09-05T00:00:09Z"
    repeated_text["message"] = {
        "id": "a3",
        "role": "assistant",
        "content": [{"type": "text", "text": "hello world"}],
    }
    rows.insert(rows.index(final), revision)
    rows.insert(rows.index(final), repeated_text)
    first_turn_rows = copy.deepcopy(rows)
    second_turn = journal(PARENT, parent=False)[1:]
    for row in second_turn:
        second = int(row["captured_at"][17:19])
        row["captured_at"] = f"2026-09-05T00:00:{30 if second == 0 else 34}Z"
        if "message_id" in row:
            row["message_id"] = "u2"
        if "message" in row:
            row["message"]["id"] = "u2" if row["message"]["role"] == "user" else "a4"
            if row["message"]["role"] == "assistant":
                row["message"]["content"] = [{"type": "text", "text": "a b c d e f"}]
    rows += second_turn

    def graph_for(evidence):
        session = stabilize_session(
            AmpAdapter().build_canonical_session(source, evidence),
            vendor=Vendor.AMP,
            source=source,
        )
        return assemble_project_session_graphs("amp-example", [session])[0]

    def call(graph, method, **params):
        return dispatch(
            method,
            {"session_id": PARENT[2:], **params},
            store=DocumentStore.from_session_graphs([graph]),
            global_scope=True,
            current_dir=source.parent,
            discovery_note="",
            cache=IndexCache(),
        )

    graph = graph_for(rows + rows)
    measured = AmpAdapter().build_canonical_session(
        source, rows, retention="measurements"
    )
    attach_measurements(measured, graph.sessions[0])
    measured_graph = assemble_project_session_graphs("amp-example", [measured])[0]
    replay = retain_session_graph(graph)
    assert "Let us reason." not in replay.model_dump_json()
    assert retain_session_graph(replay) == replay
    for variant in (graph, measured_graph, replay):
        for method in ("session.stats", "session.usage", "session.model_usage"):
            result = call(variant, method)
            runtime = result if method == "session.model_usage" else result["runtime"]
            assert runtime[field] == 2.3, (method, runtime)
            assert runtime.get("output_tokens_per_second") is None
            assert runtime.get("processed_tokens_per_second") is None
            assert "decode_tokens_per_second" not in runtime
            compact = compact_payload(method, result)
            compact_runtime = (
                compact if method == "session.model_usage" else compact["runtime"]
            )
            assert compact_runtime[field] == 2.3
            if method != "session.stats":
                assert (
                    result[
                        "usage" if method == "session.model_usage" else "total_usage"
                    ]["availability"]
                    == "unavailable"
                )
                rates = [
                    turn[field]
                    if method == "session.model_usage"
                    else turn["runtime"][field]
                    for turn in result["turns"]
                ]
                assert rates == [2.833, 1.5], rates
                assert not result["models"]  # No guessed model attribution.
                selected = call(
                    variant, method, turn_id=str(variant.sessions[0].turns[0].turn_id)
                )
                if method == "session.model_usage":
                    assert selected[field] == 2.833
                assert len(selected["turns"]) == 1

    # Exercise the real disposable SQLite path, including a fresh cache instance.
    throughput_source = source.parent / "throughput" / source.name
    throughput_source.parent.mkdir()
    throughput_source.write_text("".join(json.dumps(row) + "\n" for row in rows))
    previous_logs = os.environ["CT_AMP_LOG_DIR"]
    os.environ["CT_AMP_LOG_DIR"] = str(throughput_source.parent)
    try:
        cache_path = source.parent / "throughput.sqlite"
        for cache in (IndexCache(db_path=cache_path), IndexCache(db_path=cache_path)):
            store, _ = resolve_store(
                {"session_id": PARENT[2:]},
                global_scope=True,
                current_dir=source.parent,
                cache=cache,
            )
            cached_graph = store.get_session_graph_for_session(replay.root_session_id)
            for method in ("session.stats", "session.usage", "session.model_usage"):
                actual, expected = call(cached_graph, method), call(replay, method)
                # Turn UUIDs include the source path; the separate cache fixture
                # has the same evidence, not the same filesystem identity.
                actual_runtime = (
                    actual if method == "session.model_usage" else actual["runtime"]
                )
                expected_runtime = (
                    expected if method == "session.model_usage" else expected["runtime"]
                )
                assert actual_runtime[field] == expected_runtime[field] == 2.3
                if method != "session.stats":
                    usage_key = (
                        "usage" if method == "session.model_usage" else "total_usage"
                    )
                    assert actual[usage_key] == expected[usage_key]
                    assert len(actual["turns"]) == len(expected["turns"]) == 2
                    for actual_turn, expected_turn in zip(
                        actual["turns"], expected["turns"]
                    ):
                        assert actual_turn["usage"] == expected_turn["usage"]
                        actual_rate = (
                            actual_turn
                            if method == "session.model_usage"
                            else actual_turn["runtime"]
                        )
                        expected_rate = (
                            expected_turn
                            if method == "session.model_usage"
                            else expected_turn["runtime"]
                        )
                        assert actual_rate[field] == expected_rate[field]
        assert cache.counters["graph_hits"] == 1
        assert cache.counters["graph_builds"] == 0
        with sqlite3.connect(cache_path) as db:
            retained_json = db.execute("SELECT graph FROM graphs").fetchone()[0]
            assert "Let us reason." not in retained_json
            assert "PRIVATE output" not in retained_json
    finally:
        os.environ["CT_AMP_LOG_DIR"] = previous_logs
        throughput_source.unlink()
        throughput_source.parent.rmdir()

    scenarios = {
        "replay": [row for row in first_turn_rows if row["type"] != "observation"],
        "missing-start": [
            row for row in first_turn_rows if row.get("event") != "agent.start"
        ],
        "running": [row for row in first_turn_rows if row.get("event") != "agent.end"],
        "missing-tool-call": [
            row
            for row in first_turn_rows
            if not (
                row.get("event") == "tool.call" and row.get("tool_use_id") == "spawn1"
            )
        ],
        "missing-tool-result": [
            row
            for row in first_turn_rows
            if not (
                row.get("event") == "tool.result" and row.get("tool_use_id") == "spawn1"
            )
        ],
    }
    for name in (
        "interrupted",
        "orphan-end",
        "late-revision",
        "reversed-tool",
        "zero-window",
    ):
        evidence = copy.deepcopy(first_turn_rows)
        for row in evidence:
            if name == "interrupted" and row.get("event") == "agent.end":
                row["status"] = "cancelled"
            if name == "orphan-end" and row.get("event") == "agent.end":
                row["message_id"] = "different-turn"
            if (
                name == "late-revision"
                and row.get("message", {}).get("id") == "a2"
                and row["captured_at"].endswith("10Z")
            ):
                row["captured_at"] = "2026-09-05T00:00:11Z"
            if (
                name == "reversed-tool"
                and row.get("event") == "tool.call"
                and row.get("tool_use_id") == "shell1"
            ):
                row["observed_at"] = "2026-09-05T00:00:06Z"
            if name == "zero-window":
                row["captured_at"] = "2026-09-05T00:00:00Z"
                row.pop("observed_at", None)
        scenarios[name] = evidence
    for name, evidence in scenarios.items():
        variant = graph_for(evidence)
        for method in ("session.stats", "session.usage", "session.model_usage"):
            result = call(variant, method)
            runtime = result if method == "session.model_usage" else result["runtime"]
            assert field not in runtime, (name, method, runtime)
        if name != "zero-window":
            # A healthy later turn must not hide a partial historical selection.
            mixed = call(graph_for(evidence + second_turn), "session.model_usage")
            assert field not in mixed, name
            assert mixed["turns"][-1][field] == 1.5, name
    print(
        "PASS Amp throughput: source-derived rates, parallel-window union, revisions, thinking, idle exclusion, weighted aggregation, CLI, retained idempotence/SQLite cache, 10 unavailable cases"
    )


def main() -> None:
    with TemporaryDirectory(prefix="ct-amp-acceptance-") as root:
        directory = Path(root)
        old = os.environ.get("CT_AMP_LOG_DIR")
        os.environ["CT_AMP_LOG_DIR"] = root
        try:
            paths = []
            for thread, parent in ((PARENT, True), (CHILD, False)):
                path = directory / f"{thread}.jsonl"
                rows = journal(thread, parent=parent)
                # Repeated observations and message revisions must not count twice.
                path.write_text("".join(json.dumps(r) + "\n" for r in rows + rows))
                paths.append(path)
            qualify_result_formats(paths[0])
            qualify_throughput(paths[0])
            discovery = discover_store(
                current_dir=Path("/project/amp-example"), agent_vendor="amp"
            )
            graphs = list(discovery.store.session_graphs.values())
            assert len(graphs) == 1 and len(graphs[0].sessions) == 2
            graph = graphs[0]
            assert len(graph.edges) == 1 and graph.edges[0].type == "spawned_subagent"
            assert graph.edges[0].source_item_id is not None
            parent = next(s for s in graph.sessions if str(s.session_id) == PARENT[2:])
            assert (
                len(parent.turns) == 1 and parent.turns[0].status.value == "completed"
            )
            assert (
                parent.turns[0].ended_at - parent.turns[0].started_at
            ).total_seconds() == 10
            tools = [
                i for i in parent.turns[0].items if getattr(i, "tool_call_id", None)
            ]
            assert len(tools) == 2 and sum(i.status == "failed" for i in tools) == 1
            replay_only = AmpAdapter().build_canonical_session(
                paths[0],
                [r for r in journal(PARENT, parent=True) if r["type"] != "observation"],
            )
            assert not replay_only.extensions.amp.spawn_links
            # A live start is activity even before its prompt snapshot arrives.
            start_only = AmpAdapter().build_canonical_session(
                paths[0], journal(PARENT, parent=True)[:2]
            )
            assert len(start_only.turns) == 1
            assert start_only.turns[0].status.value == "running"
            replay = retain_session_graph(graph)
            retained_bytes = replay.model_dump_json(exclude_none=True).encode()
            assert b"PRIVATE task" in retained_bytes
            assert b"PRIVATE final" in retained_bytes
            assert b"PRIVATE output" not in retained_bytes
            assert len(replay.sessions) == 2
            assert retain_session_graph(replay) == replay
            assert all(
                not getattr(item, "text", None) and not getattr(item, "output", None)
                for session in replay.sessions
                for turn in session.turns
                for item in turn.items
            )
            assert replay.edges == graph.edges
            assert replay.sessions[0].vendor == Vendor.AMP and len(replay.edges) == 1
            # Compact ingestion preserves IDs/topology independently of content.
            compact = [
                AmpAdapter().ingest_file(p, retention="measurements") for p in paths
            ]
            compact_graph = assemble_project_session_graphs(
                graph.project_identifier, compact
            )[0]
            assert [
                i.item_id
                for s in compact_graph.sessions
                for t in s.turns
                for i in t.items
            ] == [i.item_id for s in graph.sessions for t in s.turns for i in t.items]
            assert compact_graph.edges == graph.edges
            full = [
                stabilize_session(
                    AmpAdapter().ingest_file(p), vendor=Vendor.AMP, source=p
                )
                for p in paths
            ]
            assert (
                retain_session_graph(
                    assemble_project_session_graphs(graph.project_identifier, full)[0]
                )
                == replay
            )
            snapshots = [
                SourceSnapshot(
                    path=str(p),
                    file_identity=None,
                    size=p.stat().st_size,
                    mtime_ns=p.stat().st_mtime_ns,
                    committed_offset=p.stat().st_size,
                    prefix_checksum=None,
                    tail_checksum=None,
                    parser_version="1",
                    schema_version="1",
                    status="ready",
                    error=None,
                    last_success_revision=1,
                    revision=1,
                    deleted=False,
                    root_link=None,
                    parent_link=None,
                    metadata={"vendor": "amp"},
                )
                for p in paths
            ]
            plan = plan_session_graph_components_from_files(sources=snapshots)
            assert len(plan.components) == 1
            rebuilt = rebuild_affected_session_graphs_from_files(
                sources=snapshots, seed_paths=[paths[1]]
            )
            assert (
                rebuilt.status == "complete" and len(rebuilt.selected_source_paths) == 2
            )
            methods = [
                "session.overview",
                "session.summary",
                "session.tree",
                "graph.overview",
                "session.stats",
                "graph.stats",
                "session.usage",
                "graph.usage",
                "session.model_usage",
                "session.request_usage",
                "session.tool_usage",
                "session.items",
                "project.sessions",
            ]
            for method in methods:
                if method == "project.sessions":
                    params = {"agent_vendor": "amp"}
                elif method.startswith("graph."):
                    params = {"root_session_id": str(graph.root_session_id)}
                else:
                    params = {"session_id": str(parent.session_id)}
                result = dispatch(
                    method,
                    params,
                    store=DocumentStore.from_session_graphs([replay]),
                    global_scope=True,
                    current_dir=directory,
                    discovery_note="",
                    cache=IndexCache(),
                )
                if method in {"session.usage", "graph.usage"}:
                    assert result["total_usage"]["availability"] == "unavailable"
                if method in {
                    "session.stats",
                    "graph.stats",
                    "session.usage",
                    "graph.usage",
                }:
                    # Parent: 1 + 8 + 2 content tokens over 10 - (2 + 1)s.
                    # The overlapping child's 2/10 rate does not affect root runtime.
                    assert (
                        result["runtime"]["estimated_output_tokens_per_second"] == 1.571
                    )
                    assert (
                        compact_payload(method, result)["runtime"][
                            "estimated_output_tokens_per_second"
                        ]
                        == 1.571
                    )
                if method == "graph.usage":
                    sections = compact_payload(method, result)["sessions"]
                    assert sorted(
                        section["runtime"]["estimated_output_tokens_per_second"]
                        for section in sections
                    ) == [0.2, 1.571]
                if method == "project.sessions":
                    # Inventory is source metadata, never eager metric materialization.
                    assert result["items"][0]["root_session_id"] == str(
                        graph.root_session_id
                    )
                    assert result["items"][0]["vendors"] == ["amp"]
                    assert "usage" not in result["items"][0]
            print(
                "PASS Amp live: discovery, dedup, observed timing, failed tools, spawn provenance,"
            )
            print(
                "  compact identity parity, retained replay parity/privacy, local narrative, child-seeded rebuild, 13 public APIs"
            )
        finally:
            if old is None:
                os.environ.pop("CT_AMP_LOG_DIR", None)
            else:
                os.environ["CT_AMP_LOG_DIR"] = old


if __name__ == "__main__":
    main()
