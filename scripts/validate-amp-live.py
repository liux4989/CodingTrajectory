"""Offline integration acceptance: Amp journal -> discovery -> shared APIs.

Synthetic source evidence: one parent turn (10 seconds), one create_thread
call (2 seconds), one failed shell call, and one child turn. No network writes,
provider exports, pricing guesses, or expected-metric regeneration.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from coding_trajectory.control_plane.shareable import (
    build_shareable_graph_artifact,
    shareable_session_graph,
)
from coding_trajectory.datahub import (
    hydrate_retained_session,
    rebuild_affected_session_graphs_with_measurements,
)
from coding_trajectory.discovery import discover_store, stabilize_session
from coding_trajectory.ingestion.adapters.amp import AmpAdapter
from coding_trajectory.ingestion.graph import assemble_project_session_graphs
from coding_trajectory.ingestion.incremental import (
    SourceSnapshot,
    plan_session_graph_components_from_files,
    rebuild_affected_session_graphs_from_files,
)
from coding_trajectory.ingestion.models import Vendor
from coding_trajectory.query import DocumentStore
from coding_trajectory.service.handlers import dispatch
from coding_trajectory.service.store import IndexCache

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
            artifact = build_shareable_graph_artifact(graph)
            assert b"PRIVATE" not in artifact.canonical_bytes()
            replay = artifact.to_session_graph()
            assert replay.sessions[0].vendor == Vendor.AMP and len(replay.edges) == 1
            # Compact ingestion preserves IDs/topology; content measurements are
            # attached separately by datahub, as for the existing adapters.
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
                build_shareable_graph_artifact(
                    assemble_project_session_graphs(graph.project_identifier, full)[0]
                ).digest()
                == artifact.digest()
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
            measured = rebuild_affected_session_graphs_with_measurements(
                sources=snapshots
            )
            assert measured.status == "complete" and len(measured.graphs) == 1
            hydrated = hydrate_retained_session(paths[0], vendor=Vendor.AMP)
            assert hydrated.session_id == parent.session_id
            assert [i.item_id for t in hydrated.turns for i in t.items] == [
                i.item_id for t in parent.turns for i in t.items
            ]
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
                result = dispatch(
                    method,
                    {"agent_vendor": "amp", "include": ["usage", "runtime"]}
                    if method == "project.sessions"
                    else {"session_id": str(parent.session_id)},
                    store=DocumentStore.from_session_graphs(
                        [shareable_session_graph(graph)]
                    ),
                    global_scope=True,
                    current_dir=directory,
                    discovery_note="",
                    cache=IndexCache(),
                )
                if method in {"session.usage", "graph.usage"}:
                    assert result["total_usage"]["availability"] == "unavailable"
                if method == "project.sessions":
                    assert result["items"][0]["usage"]["availability"] == "unavailable"
            print(
                "PASS Amp live: discovery, dedup, observed timing, failed tools, spawn provenance,"
            )
            print(
                "  compact identity parity, full replay parity, body-free artifact, child-seeded rebuild, 13 shared APIs"
            )
        finally:
            if old is None:
                os.environ.pop("CT_AMP_LOG_DIR", None)
            else:
                os.environ["CT_AMP_LOG_DIR"] = old


if __name__ == "__main__":
    main()
