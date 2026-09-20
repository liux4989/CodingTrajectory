"""Offline producer -> Worker -> page -> detail integration; disposable data only."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import UUID, uuid4

from coding_trajectory.analysis.activity_flow import build_overview_flows
from coding_trajectory.contracts import service_contract
from coding_trajectory.control_plane.artifact_protocol import (
    compact_graph,
    expand_graph,
)
from coding_trajectory.control_plane.fact_projection import build_published_fact_set
from coding_trajectory.control_plane.graph_preparation import prepare_graph
from coding_trajectory.control_plane.prepared_api import (
    PreparedApi,
    PreparedApiError,
    encoded,
)
from coding_trajectory.control_plane.prepared_api_reader import (
    read_prepared,
    save_local_view,
)
from coding_trajectory.discovery import _ingest_sessions
from coding_trajectory.ingestion import PiAdapter
from coding_trajectory.ingestion.graph import assemble_project_session_graphs
from coding_trajectory.ingestion.models import (
    CommandExecutionItem,
    Event,
    EventType,
    Vendor,
)

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark-output", type=Path)
    parser.add_argument("--fixture-output", type=Path)
    parser.add_argument(
        "--shape",
        choices=("representative", "near-budget", "index-heavy"),
        default="near-budget",
    )
    args = parser.parse_args()
    benchmark_shape = (
        args.benchmark_output is not None or args.fixture_output is not None
    )
    with tempfile.TemporaryDirectory(prefix="ct-direct-api-") as directory:
        os.environ["HOME"] = directory
        source = Path(directory) / "new-session.jsonl"
        stamp = "2026-09-19T10:00:00Z"
        source.write_text(
            "\n".join(
                json.dumps(row)
                for row in [
                    {
                        "type": "session",
                        "id": str(uuid4()),
                        "version": 3,
                        "timestamp": stamp,
                        "cwd": "/qualification/new-session",
                    },
                    {
                        "type": "message",
                        "id": "request",
                        "parentId": None,
                        "timestamp": stamp,
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Inspect this new synthetic session.",
                                }
                            ],
                        },
                    },
                    {
                        "type": "message",
                        "id": "answer",
                        "parentId": "request",
                        "timestamp": stamp,
                        "message": {
                            "role": "assistant",
                            "stopReason": "stop",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Synthetic inspection complete.",
                                }
                            ],
                        },
                    },
                ]
            )
            + "\n"
        )
        ingested, _ = _ingest_sessions([(Vendor.PI, PiAdapter, source)])
        graph = assemble_project_session_graphs(
            "DirectApi", [item.session for item in ingested]
        )[0]
        session = graph.sessions[0]
        session.cwd = "/full/" + "工程/" * 250
        if benchmark_shape:
            session.cwd = (
                "/project/fresh"
                if args.shape == "representative"
                else "/full/" + "p" * 120000
            )
        source_turn = session.turns[0]
        session.turns = []
        session.events = []
        session.context_usage = []
        session.context_sources = []
        session.runtime_observations = []
        session.measurements = None
        for ordinal in range(
            3 if benchmark_shape and args.shape == "representative" else 97
        ):
            turn = source_turn.model_copy(deep=True)
            turn.turn_id = UUID(int=1000 + ordinal)
            turn.sequence = ordinal
            turn.user_request_event_id = None
            turn.event_ids = []
            turn.team_state = None
            turn.items = [
                CommandExecutionItem(
                    item_id=UUID(int=10000 + ordinal * 8 + offset),
                    session_id=session.session_id,
                    turn_id=turn.turn_id,
                    sequence=offset,
                    tool_name=("shell_command", "Bash", "bash", "provider_exec")[
                        offset % 4
                    ],
                    status="failed" if offset == 3 else "completed",
                    command=f"python3 inspect.py --ordinal {ordinal} --target /source/{'segment/' * 24}{offset}.json",
                    exit_code=7 if offset == 3 else 0,
                    started_at=turn.started_at,
                )
                for offset in range(8)
            ]
            event = Event(
                event_id=UUID(int=30000 + ordinal),
                session_id=session.session_id,
                timestamp=turn.started_at,
                type=EventType.TOOL_CALL_FAILED,
                vendor_source=session.vendor,
            )
            turn.items[3].event_ids = [event.event_id]
            turn.event_ids = [event.event_id]
            session.events.append(event)
            if args.shape == "index-heavy":
                for offset in range(60):
                    extra = event.model_copy(
                        update={"event_id": UUID(int=50000 + ordinal * 60 + offset)}
                    )
                    session.events.append(extra)
                    turn.event_ids.append(extra.event_id)
                    turn.items[offset % 3].event_ids.append(extra.event_id)
            session.turns.append(turn)
        build_published_fact_set(
            graph
        )  # Reject invalid synthetic identities before preparing every method.
        started, cpu = time.perf_counter(), time.process_time()
        prepared = prepare_graph(graph)
        print(
            f"cold preparation: wall={time.perf_counter() - started:.3f}s cpu={time.process_time() - cpu:.3f}s",
            flush=True,
        )
        assert prepare_graph(graph) == prepared
        api = prepared.api
        usage_cases = []
        if args.shape == "index-heavy":
            # Asymmetric independent collections expose accidental ID joins,
            # dropped cost-only rows and ordering changes in both readers.
            scope = str(UUID(int=900000))
            for method, data in [
                (
                    "session.request_usage",
                    {
                        "root_session_id": scope,
                        "request_count": 701,
                        "usage": {"input_tokens": 245350},
                        "warnings": [],
                        "requests": [
                            {
                                "sequence": i,
                                "usage": {"input_tokens": i},
                                "detail": "雪" * 300,
                            }
                            for i in range(701)
                        ],
                    },
                ),
                (
                    "session.tool_usage",
                    {
                        "root_session_id": scope,
                        "tool_item_count": 531,
                        "tool_output_chars": 159300,
                        "tool_output_original_tokens": 0,
                        "warnings": [],
                        "attribution_policy": {},
                        "tool_items": [
                            {"item_id": str(i), "detail": "雪" * 300}
                            for i in range(531)
                        ],
                        "item_real_token_costs": [
                            {"item_id": str(1000 - i), "cost": i, "detail": "x" * 600}
                            for i in range(877)
                        ],
                    },
                ),
            ]:
                api.prepare(
                    method,
                    scope,
                    base=data,
                    source_digest=prepared.summary.fact_set_digest,
                )
                assert not api.methods[-1].error
                usage_cases.append(
                    {"method": method, "params": {"session_id": scope}, "data": data}
                )
        identity = save_local_view(api, prepared.summary.fact_set_digest)
        fixture = {
            "root": str(session.session_id),
            "cwd": session.cwd,
            "source": prepared.summary.fact_set_digest,
            "api": api.model_dump(mode="json"),
            "summary": prepared.summary.model_dump(mode="json"),
            "facts": prepared.publication().model_dump(mode="json"),
            "overview_activities": {
                str(turn.turn_id): [
                    flow
                    for flow in build_overview_flows(turn.items, flatten_commands=True)
                    if "tool" in flow
                ][-8:]
                for turn in session.turns
            },
            "versions": {
                m.method: service_contract(m.method).version for m in api.methods
            },
        }
        from coding_trajectory.control_plane.published_facts import FactIndex
        from coding_trajectory.service.handlers import SERVICE_HANDLERS, ServiceContext
        from coding_trajectory.service.store import IndexCache

        context = ServiceContext(
            FactIndex.from_rows(prepared.rows),
            True,
            Path.cwd(),
            "qualification",
            IndexCache(),
        )
        if args.shape == "index-heavy":
            fixture["column_queries"] = []
            descriptor = next(m for m in api.methods if m.method == "session.events")
            assert (
                json.loads(api.objects[descriptor.index.sha256])["mode"]
                == "page_columns"
            )
            for filters in [
                {},
                {"turn_id": str(session.turns[0].turn_id)},
                {
                    "event_ids": [
                        str(UUID(int=30000)),
                        str(UUID(int=50000)),
                        str(UUID(int=50061)),
                        str(UUID(int=999999)),
                    ]
                },
                {"item_id": str(session.turns[0].items[0].item_id)},
                {
                    "turn_id": str(session.turns[0].turn_id),
                    "types": ["tool.call.failed"],
                    "tool_name": "shell_command",
                },
            ]:
                params = {"session_id": str(session.session_id), **filters}
                expected = json.loads(
                    encoded(
                        SERVICE_HANDLERS["session.events"](
                            {**params, "limit": 2**31 - 1}, context
                        )
                    )
                )["events"]
                cursor, collected = None, []
                while True:
                    fetched = []

                    def fetch_column(key, fetched=fetched):
                        body = api.objects[key].encode()
                        fetched.append(len(body))
                        return body

                    page = read_prepared(
                        descriptor,
                        {**params, "limit": 41, "cursor": cursor},
                        identity=identity,
                        fetch=fetch_column,
                        signing_key=b"k" * 32,
                    )
                    assert (
                        len(fetched) <= (10 if filters else 4)
                        and sum(fetched) <= 768 * 1024
                    )
                    collected.extend(page["events"])
                    assert page["total"] == len(expected)
                    cursor = page["next_cursor"]
                    if cursor is None:
                        break
                assert collected == expected
                fixture["column_queries"].append({"params": params, "events": expected})
        fixture["usage_expected"] = usage_cases
        for method in ("session.request_usage", "session.tool_usage"):
            for turn_id in (None, str(session.turns[-1].turn_id)):
                params = {
                    "session_id": str(session.session_id),
                    **({"turn_id": turn_id} if turn_id else {}),
                }
                expected = json.loads(
                    encoded(SERVICE_HANDLERS[method](params, context))
                )
                method_descriptor = next(
                    m
                    for m in api.methods
                    if m.method == method and m.turn_id == turn_id
                )
                fields = (
                    ["requests"]
                    if method == "session.request_usage"
                    else ["tool_items", "item_real_token_costs"]
                )
                collected = {
                    field: [] for field in fields if expected.get(field) is not None
                }
                cursor = None
                while True:
                    result = read_prepared(
                        method_descriptor,
                        {**params, "limit": 37, "cursor": cursor},
                        identity=identity,
                        fetch=lambda key: api.objects[key].encode(),
                        signing_key=b"k" * 32,
                    )
                    service_contract(method).validate_response(result)
                    for field in collected:
                        collected[field].extend(result[field])
                    assert {
                        k: v
                        for k, v in result.items()
                        if k
                        not in fields
                        + ["total", "returned", "next_cursor", "unresolved_ids"]
                    } == {k: v for k, v in expected.items() if k not in fields}
                    cursor = result["next_cursor"]
                    if cursor is None:
                        break
                for field, rows in collected.items():
                    assert rows == expected[field]
                fixture["usage_expected"].append(
                    {"method": method, "params": params, "data": expected}
                )
        expanded_api = {
            "api_methods": fixture["api"]["methods"],
            "api_objects": [
                {"kind": "api", "sha256": hash_, "bytes": len(body.encode())}
                for hash_, body in sorted(fixture["api"]["objects"].items())
            ],
        }
        fixture["compact_api"] = compact_graph(expanded_api)["api"]
        assert expand_graph({"api": fixture["compact_api"]}) == expanded_api
        # Distinguish object position zero from the null size-error sentinel,
        # preserve repeated methods across scopes and non-ASCII turn IDs.
        corner = {
            "api_objects": expanded_api["api_objects"][:2],
            "api_methods": [
                {
                    "method": "session.summary",
                    "method_version": 3,
                    "scope": scope,
                    "turn_id": turn,
                    "index": ref,
                    "error": error,
                }
                for scope, turn, ref, error in [
                    ("scope-b", "雪", expanded_api["api_objects"][0], None),
                    ("scope-a", None, None, "remote_result_too_large"),
                    ("scope-b", None, expanded_api["api_objects"][1], None),
                ]
            ],
        }
        fixture["compact_corner"] = {
            "expanded": corner,
            "compact": compact_graph(corner),
        }
        assert expand_graph(fixture["compact_corner"]["compact"]) == corner
        descriptor = next(m for m in api.methods if m.method == "session.overview")
        reads = []

        def fetch(key):
            reads.append(key)
            return api.objects[key].encode()

        params = {"session_id": str(session.session_id), "limit": 200}
        started, cpu = time.perf_counter(), time.process_time()
        page = read_prepared(
            descriptor, params, identity=identity, fetch=fetch, signing_key=b"k" * 32
        )
        print(
            f"local prepared page: wall={time.perf_counter() - started:.4f}s cpu={time.process_time() - cpu:.4f}s reads={len(reads)} fetched={sum(len(api.objects[key].encode()) for key in reads)} result={len(encoded(page))} turns={len(page['turns'])}",
            flush=True,
        )
        assert (
            len(reads) <= 4
            and sum(len(api.objects[key].encode()) for key in reads) <= 768 * 1024
        )
        if benchmark_shape:
            fixture["benchmark"] = {
                "shape": args.shape,
                "expected_reads": len(reads),
                "expected_fetched_bytes": sum(
                    len(api.objects[key].encode()) for key in reads
                ),
                "expected_data": page,
            }
            if args.fixture_output is not None:
                args.fixture_output.parent.mkdir(parents=True, exist_ok=True)
                args.fixture_output.write_bytes(encoded(fixture))
            if args.benchmark_output is None:
                return
            path = Path(directory) / "fixture.json"
            path.write_bytes(encoded(fixture))
            subprocess.run(
                [
                    "node",
                    str(ROOT / "scripts/qualify-prepared-api.mjs"),
                    str(path),
                    "--benchmark",
                    str(args.benchmark_output.resolve()),
                ],
                cwd=ROOT,
                check=True,
            )
            return
        assert page["sessions"][0]["cwd"] == session.cwd
        assert page["turns"][-1]["global_ordinal"] == 96 and page["page"]["has_more"]
        activities = page["turns"][-1]["activities"]
        assert (
            activities == fixture["overview_activities"][str(session.turns[-1].turn_id)]
        )
        assert [cell["item_ids"] for cell in activities] == [
            [str(item.item_id)] for item in session.turns[-1].items
        ]
        assert activities[3]["outcome"] == "failed"
        seen = [row["global_ordinal"] for row in page["turns"]]
        while page["page"]["next_cursor"]:
            page = read_prepared(
                descriptor,
                {**params, "cursor": page["page"]["next_cursor"]},
                identity=identity,
                fetch=fetch,
                signing_key=b"k" * 32,
            )
            assert [row["global_ordinal"] for row in page["turns"]] == sorted(
                row["global_ordinal"] for row in page["turns"]
            )
            seen.extend(row["global_ordinal"] for row in page["turns"])
        assert sorted(seen) == list(range(97))
        for method, field, known_id, result_field in (
            ("session.items", "item_ids", session.turns[0].items[0].item_id, "items"),
            ("session.events", "event_ids", session.events[0].event_id, "events"),
        ):
            detail = read_prepared(
                next(m for m in api.methods if m.method == method),
                {
                    "session_id": str(session.session_id),
                    "turn_id": str(session.turns[-1].turn_id),
                    "limit": 10,
                    field: [str(known_id), "not-present"],
                },
                identity=identity,
                fetch=fetch,
                signing_key=b"k" * 32,
            )
            assert detail[result_field] == []
            assert detail["unresolved_ids"] == ["not-present"]
        overflow = PreparedApi()
        overflow.prepare(
            "session.summary",
            "oversize",
            base={"value": "x" * (448 * 1024)},
            source_digest="a" * 64,
        )
        assert overflow.methods[0].error == "remote_result_too_large"
        try:
            read_prepared(
                descriptor,
                params,
                identity=identity,
                fetch=lambda key: api.objects[key].encode() + b"!",
                signing_key=b"k" * 32,
            )
        except PreparedApiError as error:
            assert error.code == "prepared_object_corrupt"
        else:
            raise AssertionError("corrupt object accepted")
        path = Path(directory) / "fixture.json"
        path.write_bytes(encoded(fixture))
        subprocess.run(
            ["node", str(ROOT / "scripts/qualify-prepared-api.mjs"), str(path)],
            cwd=ROOT,
            check=True,
        )
        print(
            "PASS local deterministic preparation, full paths, semantic arguments/outcomes, bounded pages, complete ordinals, size and corruption rejection"
        )


if __name__ == "__main__":
    main()
