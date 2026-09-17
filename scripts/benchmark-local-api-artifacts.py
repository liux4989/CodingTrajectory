#!/usr/bin/env python3
"""Compare current local API and production artifact reads on frozen Amp logs.

The artifact transport is in-memory, not Cloudflare or a new local backend.
Preparation is measured separately; cold means a fresh runtime, not cold OS caches.
Run with a disposable HOME to isolate provider discovery and the locator cache.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import UUID

from coding_trajectory.control_plane.artifact_protocol import (
    ARTIFACT_MANIFEST_SCHEMA_VERSION,
    ARTIFACT_PREPARATION_VERSION,
    ArtifactManifest,
    ArtifactManifestGraph,
    ArtifactObjectReference,
)
from coding_trajectory.control_plane.collector import _prepared_graph_summary
from coding_trajectory.control_plane.fact_projection import build_published_fact_set
from coding_trajectory.control_plane.fact_repository import (
    ArtifactReadCache,
    CloudflareArtifactRepository,
)
from coding_trajectory.discovery import discover_store_from_files
from coding_trajectory.ingestion.common import canonical_json
from coding_trajectory.runtime import ServiceRuntime

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "fixture", ROOT / "scripts/benchmark-local-artifact-compute.py"
)
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)
WORKSPACE = UUID(int=1)


def encode(value):
    return canonical_json(value).encode()


class OfflineArtifacts:
    def __init__(self, manifest, objects):
        self.manifest = manifest
        self.objects = objects
        self.reads = {"manifest": 0, "facts": 0, "summary": 0}

    def call(self, method, params):
        if method == "ct_artifact_manifest":
            self.reads["manifest"] += 1
            return {
                "workspace_id": str(WORKSPACE),
                "snapshot_sequence": 1,
                "manifests": [self.manifest.model_dump(mode="json")],
            }
        assert method == "ct_artifact_read"
        self.reads[params["kind"]] += 1
        return json.loads(self.objects[params["sha256"]])


def prepare(logs):
    store = discover_store_from_files(sorted(logs.glob("*.jsonl"))).store
    objects, graphs = {}, []
    for graph in store.session_graphs.values():
        facts = build_published_fact_set(graph)
        summary = _prepared_graph_summary(facts)
        refs = {}
        for kind, value in (("facts", facts), ("summary", summary)):
            body = encode(value.model_dump(mode="json", exclude_none=True))
            digest = hashlib.sha256(body).hexdigest()
            objects[digest] = body
            refs[kind] = ArtifactObjectReference(
                kind=kind, sha256=digest, bytes=len(body)
            )
        graphs.append(
            ArtifactManifestGraph(
                graph_id=facts.graph_id,
                fact_set_digest=facts.fact_set_digest,
                fact_count=len(facts.rows),
                observed_at="2026-09-15T00:00:00Z",
                vendors=["amp"],
                **refs,
            )
        )
    return ArtifactManifest(
        schema_version=ARTIFACT_MANIFEST_SCHEMA_VERSION,
        preparation_version=ARTIFACT_PREPARATION_VERSION,
        workspace_id=WORKSPACE,
        project_id=UUID(int=2),
        publisher_agent_id=UUID(int=3),
        publication_sequence=0,
        snapshot_sequence=1,
        published_at="2026-09-15T00:00:00Z",
        inventory_state="complete",
        graphs=graphs,
    ), objects


def main(args):
    report = {
        "head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "python": sys.version,
        "platform": platform.platform(),
        "synthetic": args.logs is None,
        "network_requests": 0,
        "limitations": [
            "In-process API, no HTTP or process-startup cost; OS caches not evicted.",
            "Artifact transport decodes in-memory bytes; no disk, R2 or network latency.",
            "Preparation includes parsing, facts, summaries and serialization, not durable collector caching/publication.",
            "Artifact path is not wired into the ordinary local API.",
        ],
        "turns_per_graph": args.turns if args.logs is None else None,
        "preparation_runs_s": [],
        "results": [],
    }
    with tempfile.TemporaryDirectory(prefix="ct-local-api-bench-") as temporary:
        logs = args.logs.resolve() if args.logs else Path(temporary) / "logs"
        if args.logs is None:
            fixture.create_logs(logs, args.graphs, args.turns, 128)
        os.environ["CT_AMP_LOG_DIR"] = str(logs)
        report["input"] = fixture.input_evidence(logs)
        for _ in range(args.repeat):
            start = time.perf_counter()
            manifest, objects = prepare(logs)
            report["preparation_runs_s"].append(time.perf_counter() - start)
        report["artifact_bytes"] = sum(map(len, objects.values()))
        report["graphs"] = len(manifest.graphs)
        report["facts"] = sum(graph.fact_count for graph in manifest.graphs)
        report["sessions"] = sum(
            row["kind"] == "session"
            for graph in manifest.graphs
            for row in json.loads(objects[graph.facts.sha256])["rows"]
        )
        report["turns"] = sum(
            row["kind"] == "turn"
            for graph in manifest.graphs
            for row in json.loads(objects[graph.facts.sha256])["rows"]
        )
        selected = str(manifest.graphs[0].graph_id)
        for method, params in (
            ("project.sessions", {}),
            ("graph.stats", {"root_session_id": selected}),
            ("session.overview", {"session_id": selected, "limit": 10}),
        ):
            expected = None
            for variant in ("local", "artifact"):
                cold, warm, reads = [], [], []
                for _ in range(args.repeat):
                    client = OfflineArtifacts(manifest, objects)
                    start = time.perf_counter()
                    repository = (
                        None
                        if variant == "local"
                        else CloudflareArtifactRepository(
                            client=client,
                            workspace_id=WORKSPACE,
                            snapshot_sequence=1,
                            cache=ArtifactReadCache(),
                            fallback=None,
                        )
                    )
                    runtime = ServiceRuntime(
                        global_scope=args.logs is not None,
                        current_dir=ROOT if args.logs else fixture.PROJECT,
                        historical_repository=repository,
                    )
                    response = runtime.call(method, params)
                    cold.append(time.perf_counter() - start)
                    if method == "project.sessions":
                        assert response["items"], (
                            "benchmark requires visible list cards"
                        )
                    response_hash = hashlib.sha256(encode(response)).hexdigest()
                    expected = expected or response_hash
                    assert response_hash == expected, (
                        f"response mismatch: {method}/{variant}"
                    )
                    before = client.reads.copy()
                    start = time.perf_counter()
                    replay = runtime.call(method, params)
                    warm.append(time.perf_counter() - start)
                    assert hashlib.sha256(encode(replay)).hexdigest() == expected
                    assert client.reads == before, "warm read fetched more artifacts"
                    if variant == "artifact":
                        assert before == {
                            "manifest": 1,
                            "summary": report["graphs"],
                            "facts": 0 if method == "project.sessions" else 1,
                        }
                    reads.append(before)
                row = {
                    "method": method,
                    "variant": variant,
                    "cold_runs_s": cold,
                    "warm_runs_s": warm,
                    "cold_median_s": statistics.median(cold),
                    "warm_median_s": statistics.median(warm),
                    "response_sha256": expected,
                    "object_reads": reads,
                }
                report["results"].append(row)
                print(
                    f"{method}/{variant}: cold {row['cold_median_s']:.6f}s warm {row['warm_median_s']:.6f}s",
                    flush=True,
                )
        assert fixture.input_evidence(logs) == report["input"], "inputs changed"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":

    def refuse_network(event, _args):
        if event == "socket.connect":
            raise RuntimeError("network is prohibited in this benchmark")

    sys.addaudithook(refuse_network)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graphs", type=int, default=29)
    parser.add_argument("--turns", type=int, default=100)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument(
        "--logs",
        type=Path,
        help="Frozen private Amp JSONL directory; omit to generate synthetic inputs",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.graphs, args.turns, args.repeat) < 1:
        parser.error("graphs, turns and repeat must be positive")
    main(args)
