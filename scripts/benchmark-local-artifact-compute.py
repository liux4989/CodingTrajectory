#!/usr/bin/env python3
"""Offline compute experiment using real collector and reader code, not an R2 implementation."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import platform
import resource
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from coding_trajectory.control_plane import collector as collector_module
from coding_trajectory.control_plane import published_facts as facts_module
from coding_trajectory.control_plane.collector import CollectorIdentity, LocalCollector
from coding_trajectory.control_plane.collector_protocol import (
    CollectorRecoveryResponse,
    ObservationReceipt,
    SourceRegistrationResponse,
)
from coding_trajectory.control_plane.fact_protocol import (
    MissingFactRowsResponse,
    StageFactRowsResponse,
)
from coding_trajectory.control_plane.published_facts import FactIndex, PublishedFactSet
from coding_trajectory.ingestion.common import canonical_json
from coding_trajectory.service import handlers as handler_module
from coding_trajectory.service.handlers import dispatch
from coding_trajectory.service.store import IndexCache
from pydantic import TypeAdapter

ROOT = Path(__file__).resolve().parents[1]
PROJECT = Path("/project/synthetic-artifact-compute")
BASELINE = "52dfc9dcead986d1da8b49e4bde204860c4cce87"


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def peak_rss():
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss if sys.platform == "darwin" else rss * 1024


class Measurements:
    def __init__(self):
        self.stages = {}
        self.stack = []

    @contextmanager
    def stage(self, name):
        frame = [time.perf_counter(), time.process_time(), 0.0, 0.0]
        self.stack.append(frame)
        try:
            yield
        finally:
            wall, cpu = time.perf_counter() - frame[0], time.process_time() - frame[1]
            self.stack.pop()
            if self.stack:
                self.stack[-1][2] += wall
                self.stack[-1][3] += cpu
            row = self.stages.setdefault(
                name,
                {
                    "calls": 0,
                    "wall_s": 0.0,
                    "cpu_s": 0.0,
                    "exclusive_wall_s": 0.0,
                    "exclusive_cpu_s": 0.0,
                },
            )
            row["calls"] += 1
            row["wall_s"] += wall
            row["cpu_s"] += cpu
            row["exclusive_wall_s"] += wall - frame[2]
            row["exclusive_cpu_s"] += cpu - frame[3]

    def wrap(self, owner, name, label, capture=None):
        original = getattr(owner, name)

        def measured(*args, **kwargs):
            with self.stage(label):
                result = original(*args, **kwargs)
            if capture is not None:
                capture(result)
            return result

        setattr(owner, name, measured)


class OfflineRemote:
    """Accepting sink: client validation/serialization run, server checks do not."""

    def __init__(self, sequence):
        self.sequence = sequence
        self.calls = Counter()
        self.rows = 0
        self.bytes = 0

    def recover(self, request):
        self.calls["recover"] += 1
        return CollectorRecoveryResponse(next_publication_sequence=self.sequence)

    def register_source(self, request, *, idempotency_key):
        self.calls["register_source"] += 1
        return SourceRegistrationResponse(
            source_id=uuid5(NAMESPACE_URL, request.native_session_id),
            source_epoch=request.source_epoch,
        )

    def publish_observation(self, request, *, idempotency_key):
        self.calls["checkpoint"] += 1
        self.bytes += len(request.model_dump_json().encode())
        return ObservationReceipt(
            receipt_id=uuid5(NAMESPACE_URL, idempotency_key),
            outcome="accepted",
            committed_sequence=1,
        )

    def missing_fact_rows(self, request):
        self.calls["missing"] += 1
        return MissingFactRowsResponse(
            graph_id=request.graph_id,
            fact_set_digest=request.fact_set_digest,
            missing_batches=list(range(request.batch_count)),
        )

    def stage_fact_rows(self, request):
        self.calls["stage"] += 1
        self.rows += len(request.rows)
        self.bytes += len(request.model_dump_json(exclude_none=True).encode())
        return StageFactRowsResponse(
            graph_id=request.graph_id,
            fact_set_digest=request.fact_set_digest,
            staged_batches=request.batch_index + 1,
        )

    def publish_facts(self, request, *, idempotency_key):
        self.calls["publish"] += 1
        self.sequence += 1
        self.bytes += len(request.model_dump_json(exclude_none=True).encode())
        return ObservationReceipt(
            receipt_id=uuid5(NAMESPACE_URL, idempotency_key),
            outcome="accepted",
            committed_sequence=self.sequence,
            details={"publication_outcome": "published"},
        )


def views(index, measurements, prefix, cache=None):
    cache = cache if cache is not None else IndexCache()
    outputs = {}
    sizes = Counter()
    for method in ("graph.stats", "session.summary", "session.overview"):
        rows = []
        with measurements.stage(prefix + ":" + method):
            for graph_id in index.graph_ids:
                params = {"session_id": str(graph_id)}
                if method == "graph.stats":
                    params = {"root_session_id": str(graph_id)}
                if method == "session.overview":
                    params["limit"] = 10
                result = dispatch(
                    method,
                    params,
                    store=index,
                    global_scope=False,
                    current_dir=PROJECT,
                    discovery_note="",
                    cache=cache,
                )
                encoded = canonical_json(result).encode()
                sizes[method] += len(encoded)
                rows.append(sha(encoded))
        outputs[method] = sha(canonical_json(rows).encode())
    return {"digests": outputs, "bytes": dict(sizes)}


def child(args):
    measurements = Measurements()
    report = {"role": args.role, "stages": measurements.stages}
    started, cpu = time.perf_counter(), time.process_time()
    measurements.wrap(
        handler_module, "session_graph_from_fact_index", "graph_reconstruction"
    )
    measurements.wrap(IndexCache, "index_facts", "service_entrypoint_index")
    if args.role == "collector":
        os.environ["CT_AMP_LOG_DIR"] = str(args.logs)
        projected = []
        measurements.wrap(collector_module, "discover_source_candidates", "discovery")
        measurements.wrap(collector_module, "_normalized_segments", "parse_normalize")
        measurements.wrap(
            collector_module, "assemble_project_session_graphs", "graph_assembly"
        )
        measurements.wrap(
            collector_module,
            "build_published_fact_set",
            "fact_projection_inclusive",
            lambda fact: projected.append(
                (str(fact.graph_id), fact.fact_set_digest, len(fact.rows))
            ),
        )
        measurements.wrap(
            facts_module,
            "_assemble_published_fact_set",
            "fact_assembly_hash_validation",
        )
        measurements.wrap(
            facts_module, "_validate_fact_relationships", "fact_relationship_validation"
        )
        measurements.wrap(
            LocalCollector, "_queue_fact_publication", "queue_publication_inclusive"
        )
        measurements.wrap(LocalCollector, "_flush_facts", "flush_client_inclusive")
        identity = CollectorIdentity(
            workspace_id=UUID(int=1),
            agent_id=UUID(int=2),
            agent_instance_id=UUID(int=3),
            project_id=UUID(int=4),
            project_name=PROJECT.name,
        )
        with (
            measurements.stage("collector_end_to_end"),
            LocalCollector(database_path=args.database, identity=identity) as collector,
        ):
            sequence = int(
                collector._get_meta(
                    f"fact_publication:{identity.project_id}:next_sequence", "0"
                )
            )
            remote = OfflineRemote(sequence)
            result = collector.collect(
                current_dir=PROJECT,
                agent_vendor="amp",
                remote=remote,
                heartbeat=False,
            )
            assert result.failed == 0 and collector.pending_count() == 0
            report["sessions_parsed"] = len(collector.prepared_sources)
        report["collector_pipeline_peak_rss_bytes"] = peak_rss()
        assert projected
        report["remote_sink"] = {
            "calls": dict(remote.calls),
            "rows_staged": remote.rows,
            "serialized_bytes": remote.bytes,
        }
        report["graphs_projected"] = len(projected)
        report["rows_projected"] = sum(row[2] for row in projected)
        report["graph_digests"] = {row[0]: row[1] for row in projected}
        with measurements.stage("outbox_decode_for_artifact"):
            with sqlite3.connect(args.database) as connection:
                stored = connection.execute(
                    "SELECT request_json FROM publication_outbox ORDER BY publication_sequence DESC LIMIT 1"
                ).fetchone()[0]
            raw = json.loads(stored)["fact_sets"]
        with measurements.stage("outbox_validation_for_views"):
            projected_facts = TypeAdapter(list[PublishedFactSet]).validate_python(raw)
        assert {str(f.graph_id): f.fact_set_digest for f in projected_facts} == report[
            "graph_digests"
        ]
        with measurements.stage("artifact_model_dump"):
            artifact_view = [
                f.model_dump(mode="json", exclude_none=True) for f in projected_facts
            ]
        assert artifact_view == raw
        with measurements.stage("artifact_canonical_json"):
            encoded = canonical_json(artifact_view).encode()
        with measurements.stage("artifact_sha256"):
            report["artifact_sha256"] = sha(encoded)
        with measurements.stage("producer_fact_index"):
            index = FactIndex.from_fact_sets(projected_facts)
        report["views"] = views(index, measurements, "producer")
        args.artifact.write_bytes(encoded)
        report["artifact_bytes"] = len(encoded)
    else:
        with measurements.stage("artifact_read"):
            encoded = args.artifact.read_bytes()
        with measurements.stage("json_decode"):
            raw = json.loads(encoded)
        with measurements.stage("pydantic_fact_validation"):
            facts = TypeAdapter(list[PublishedFactSet]).validate_python(raw)
        with measurements.stage("fact_index"):
            index = FactIndex.from_fact_sets(facts)
        cache = IndexCache()
        report["views"] = views(index, measurements, "reader_cold", cache)
        report["warm_views"] = views(index, measurements, "reader_warm", cache)
        assert report["views"] == report["warm_views"]
        with measurements.stage("canonical_roundtrip_verification"):
            assert (
                canonical_json(
                    [f.model_dump(mode="json", exclude_none=True) for f in facts]
                ).encode()
                == encoded
            )
        report["compression"] = {}
        for level in (1, 6):
            with measurements.stage(f"candidate_gzip_{level}"):
                compressed = gzip.compress(encoded, compresslevel=level, mtime=0)
            with measurements.stage(f"candidate_gzip_{level}_decode"):
                decoded = gzip.decompress(compressed)
            assert decoded == encoded
            report["compression"][str(level)] = {
                "bytes": len(compressed),
                "sha256": sha(compressed),
            }
        report["artifact_bytes"] = len(encoded)
        report["artifact_sha256"] = sha(encoded)
    report["measured_total_wall_s"] = time.perf_counter() - started
    report["measured_total_cpu_s"] = time.process_time() - cpu
    report["fresh_process_peak_rss_bytes"] = peak_rss()
    args.output.write_text(json.dumps(report, indent=2) + "\n")


def turn_rows(thread, number, text_bytes=128, changed=False):
    stamp = datetime(2026, 9, 15, tzinfo=UTC) + timedelta(seconds=number * 12)

    def row(kind, offset, **fields):
        return {
            "schema_version": 1,
            "type": kind,
            "thread_id": thread,
            "captured_at": (stamp + timedelta(seconds=offset)).isoformat(),
            **fields,
        }

    uid, aid, tool = f"user-{number}", f"assistant-{number}", f"shell-{number}"
    return [
        row("observation", 0, event="agent.start", message_id=uid),
        row(
            "message",
            0,
            message={
                "id": uid,
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Verify synthetic case {number}."}
                ],
            },
        ),
        row(
            "observation",
            1,
            event="tool.call",
            tool_use_id=tool,
            tool_name="shell_command",
            input={"command": "uv run python check.py"},
        ),
        row(
            "observation",
            3,
            event="tool.result",
            tool_use_id=tool,
            tool_name="shell_command",
            status="done",
            output=json.dumps(
                {
                    "exitCode": 23 if changed else 0,
                    "output": (
                        f"synthetic {number} evidence " * (text_bytes // 10 + 1)
                    )[:text_bytes],
                }
            ),
        ),
        row(
            "message",
            5,
            message={
                "id": aid,
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "Validation failed."
                        if changed
                        else "Validation passed.",
                    }
                ],
            },
        ),
        row("observation", 6, event="agent.end", message_id=uid, status="done"),
    ]


def create_logs(directory, graphs, turns, text_bytes):
    directory.mkdir()
    for g in range(graphs):
        thread = "T-" + str(uuid5(NAMESPACE_URL, f"artifact-compute:{g}"))
        rows = [
            {
                "schema_version": 1,
                "type": "thread",
                "captured_at": "2026-09-15T00:00:00Z",
                "payload": {"id": thread, "workspace_root": PROJECT.as_uri()},
            }
        ]
        for number in range(turns):
            rows.extend(turn_rows(thread, number, text_bytes))
        path = directory / f"{g:04}.jsonl"
        path.write_text("".join(canonical_json(row) + "\n" for row in rows))
        os.utime(path, (1789430400, 1789430400))


def input_evidence(directory):
    entries = [
        (p.name, sha(p.read_bytes()), p.stat().st_size)
        for p in sorted(directory.glob("*.jsonl"))
    ]
    return {
        "sha256": sha(canonical_json(entries).encode()),
        "bytes": sum(e[2] for e in entries),
        "files": len(entries),
    }


def run_child(role, folder, logs, database, artifact):
    # A shared checkout can change between subprocesses; never mix source revisions.
    source_check = ["git", "diff", "--exit-code", BASELINE, "--", "packages/core/src"]
    subprocess.run(source_check, cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    output = folder / f"{role}.json"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--role",
        role,
        "--logs",
        str(logs),
        "--database",
        str(database),
        "--artifact",
        str(artifact),
        "--output",
        str(output),
    ]
    start = time.perf_counter()
    subprocess.run(command, check=True, cwd=ROOT, stdout=subprocess.DEVNULL)
    result = json.loads(output.read_text())
    result["subprocess_elapsed_wall_s_including_imports"] = time.perf_counter() - start
    subprocess.run(source_check, cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    return result


def main(args):
    source_paths = subprocess.check_output(
        [
            "git",
            "ls-files",
            "packages/core/src",
            "scripts/qualify-cloudflare-control-plane.py",
        ],
        cwd=ROOT,
        text=True,
    ).splitlines()
    source_hashes = {
        p: sha((ROOT / p).read_bytes()) for p in source_paths if (ROOT / p).is_file()
    }
    report = {
        "baseline": BASELINE,
        "head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "source_tree_sha256": sha(canonical_json(source_hashes).encode()),
        "harness_sha256": sha(Path(__file__).read_bytes()),
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "synthetic": True,
        "network_requests": 0,
        "results": [],
    }
    shapes = [
        ("pilot_scale", 29, 100, 128),
        ("2x", 58, 100, 128),
        ("4x", 116, 100, 128),
        ("few_large", 4, 725, 128),
        ("byte_heavy_logs", 29, 100, 8192),
    ]
    if args.smoke:
        shapes = [("smoke", 2, 3, 128)]
    with tempfile.TemporaryDirectory(prefix="ct-local-compute-") as temporary:
        root = Path(temporary)
        for name, graphs, turns, text_bytes in shapes:
            shape = root / name
            shape.mkdir()
            logs = shape / "logs"
            create_logs(logs, graphs, turns, text_bytes)
            initial = (logs / "0000.jsonl").read_bytes()
            cases = (
                ["cold", "unchanged", "appended_turn", "changed_graph"]
                if name in {"pilot_scale", "smoke"}
                else ["cold"]
            )
            seeded = None
            expected = None
            for case in cases:
                path = logs / "0000.jsonl"
                path.write_bytes(initial)
                if case in {"appended_turn", "changed_graph"}:
                    thread = "T-" + str(uuid5(NAMESPACE_URL, "artifact-compute:0"))
                    if case == "appended_turn":
                        extra = turn_rows(thread, turns, text_bytes)
                        path.write_bytes(
                            initial
                            + "".join(
                                canonical_json(row) + "\n" for row in extra
                            ).encode()
                        )
                    else:
                        rows = [json.loads(line) for line in initial.splitlines()]
                        rows[-6:] = turn_rows(
                            thread, turns - 1, text_bytes, changed=True
                        )
                        path.write_text(
                            "".join(canonical_json(row) + "\n" for row in rows)
                        )
                os.utime(
                    path,
                    (
                        1789430460
                        if case in {"appended_turn", "changed_graph"}
                        else 1789430400,
                    )
                    * 2,
                )
                for repeat in range(args.repeats):
                    folder = shape / f"{case}-{repeat}"
                    folder.mkdir()
                    database, artifact = (
                        folder / "collector.sqlite",
                        folder / "facts.json",
                    )
                    if case != "cold":
                        shutil.copy2(seeded, database)
                    producer = run_child("collector", folder, logs, database, artifact)
                    reader = run_child("reader", folder, logs, database, artifact)
                    assert producer["artifact_sha256"] == reader["artifact_sha256"]
                    assert producer["views"] == reader["views"]
                    if case == "cold":
                        if expected is None:
                            expected = producer
                            seeded = shape / "seed.sqlite"
                            with (
                                sqlite3.connect(database) as source,
                                sqlite3.connect(seeded) as dest,
                            ):
                                source.backup(dest)
                        assert producer["graph_digests"] == expected["graph_digests"]
                    changes = sum(
                        expected["graph_digests"].get(k) != v
                        for k, v in producer["graph_digests"].items()
                    )
                    assert changes == (
                        1 if case in {"appended_turn", "changed_graph"} else 0
                    )
                    if case == "unchanged":
                        assert producer["remote_sink"]["rows_staged"] == 0
                        assert producer["views"] == expected["views"]
                    report["results"].append(
                        {
                            "shape": name,
                            "case": case,
                            "repeat": repeat,
                            "input": input_evidence(logs),
                            "changed_graphs": changes,
                            "collector": producer,
                            "reader": reader,
                        }
                    )
                    print(
                        f"{name}/{case}/{repeat}: {producer['rows_projected']} facts; collect {producer['stages']['collector_end_to_end']['wall_s']:.3f}s; reader validate {reader['stages']['pydantic_fact_validation']['wall_s']:.3f}s",
                        flush=True,
                    )
    args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":

    def refuse_network(event, _args):
        if event == "socket.connect":
            raise RuntimeError("network is prohibited in this benchmark")

    sys.addaudithook(refuse_network)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=["collector", "reader"])
    parser.add_argument("--logs", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--smoke", action="store_true")
    options = parser.parse_args()
    child(options) if options.role else main(options)
