#!/usr/bin/env python3
"""Compare current local API and production artifact reads on frozen logs.

The artifact transport is in-memory, not Cloudflare or a new local backend.
Preparation is measured separately; cold means a fresh runtime, not cold OS caches.
Run with a disposable HOME to isolate provider discovery and the locator cache.

For Codex, place the frozen source tree under ``$HOME/.codex/sessions`` and run
with ``--vendor codex_cli --logs "$HOME/.codex/sessions"``. For Amp, pass the
frozen flat journal directory with ``--vendor amp --logs <directory>``.
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
from collections import Counter
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
from coding_trajectory.discovery import (
    discover_source_candidates,
    discover_store_from_files,
)
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


def input_evidence(paths, root):
    resolved_root = root.resolve()
    entries = [
        (
            str(path.resolve().relative_to(resolved_root)),
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_size,
        )
        for path in sorted(paths)
    ]
    return {
        "sha256": hashlib.sha256(encode(entries)).hexdigest(),
        "bytes": sum(entry[2] for entry in entries),
        "files": len(entries),
    }


def source_paths(logs, vendor):
    if vendor == "amp":
        os.environ["CT_AMP_LOG_DIR"] = str(logs)
    else:
        os.environ.pop("CT_AMP_LOG_DIR", None)
        expected = Path.home() / ".codex" / "sessions"
        assert logs == expected.resolve(), (
            "Codex logs must be frozen under the disposable HOME's .codex/sessions"
        )
    candidates = discover_source_candidates(
        current_dir=ROOT,
        global_scope=True,
        agent_vendor=vendor,
    )
    assert candidates, f"no {vendor} sources discovered"
    assert {candidate.vendor.value for candidate in candidates} == {vendor}
    return candidates, [candidate.path for candidate in candidates]


def prepare(paths):
    store = discover_store_from_files(paths).store
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
                vendors=sorted({session.vendor.value for session in graph.sessions}),
                **refs,
            )
        )
    return (
        ArtifactManifest(
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
        ),
        objects,
        Counter(
            session.vendor.value
            for graph in store.session_graphs.values()
            for session in graph.sessions
        ),
    )


def main(args):
    os.environ["CT_DISABLE_LIVE_PRICING"] = "1"
    report = {
        "head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "python": sys.version,
        "platform": platform.platform(),
        "synthetic": args.logs is None,
        "network_requests": 0,
        "live_pricing_disabled": True,
        "limitations": [
            "In-process API, no HTTP or process-startup cost; OS caches not evicted.",
            "Artifact transport decodes in-memory bytes; no disk, R2 or network latency.",
            "Preparation includes parsing, facts, summaries and serialization, not durable collector caching/publication.",
            "Artifact path is not wired into the ordinary local API.",
            "Live pricing is disabled; prices absent from offline sources remain unavailable.",
        ],
        "turns_per_graph": args.turns if args.logs is None else None,
        "requested_vendor": args.vendor,
        "preparation_runs_s": [],
        "results": [],
    }
    with tempfile.TemporaryDirectory(prefix="ct-local-api-bench-") as temporary:
        logs = args.logs.resolve() if args.logs else Path(temporary) / "logs"
        if args.logs is None:
            assert args.vendor == "amp", "synthetic fixtures are Amp journals"
            fixture.create_logs(logs, args.graphs, args.turns, 128)
        candidates, paths = source_paths(logs, args.vendor)
        report["input"] = input_evidence(paths, logs)
        report["detected_source_vendors"] = dict(
            Counter(candidate.vendor.value for candidate in candidates)
        )
        for _ in range(args.repeat):
            start = time.perf_counter()
            manifest, objects, canonical_vendors = prepare(paths)
            report["preparation_runs_s"].append(time.perf_counter() - start)
        report["canonical_session_vendors"] = dict(canonical_vendors)
        report["manifest_graph_vendors"] = dict(
            Counter(vendor for graph in manifest.graphs for vendor in graph.vendors)
        )
        assert set(report["canonical_session_vendors"]) == {args.vendor}
        assert set(report["manifest_graph_vendors"]) == {args.vendor}
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
        graph_characteristics = []
        for graph in manifest.graphs:
            rows = json.loads(objects[graph.facts.sha256])["rows"]
            graph_characteristics.append(
                {
                    "graph": graph,
                    "facts": graph.fact_count,
                    "sessions": sum(row["kind"] == "session" for row in rows),
                    "turns": sum(row["kind"] == "turn" for row in rows),
                    "vendors": graph.vendors,
                }
            )
        ordered = sorted(graph_characteristics, key=lambda row: row["facts"])
        selected_graphs = [("small", ordered[0])]
        if ordered[-1]["graph"].graph_id != ordered[0]["graph"].graph_id:
            selected_graphs.append(("large", ordered[-1]))
        report["selected_graphs"] = [
            {
                "label": label,
                **{key: value for key, value in row.items() if key != "graph"},
            }
            for label, row in selected_graphs
        ]
        calls = [("project.sessions", None, {})]
        for label, row in selected_graphs:
            selected = str(row["graph"].graph_id)
            calls.extend(
                (
                    ("graph.stats", label, {"root_session_id": selected}),
                    ("session.overview", label, {"session_id": selected, "limit": 10}),
                )
            )
        for method, graph_label, params in calls:
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
                        f"response mismatch: {method}/{graph_label}/{variant}"
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
                    "graph_label": graph_label,
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
                    f"{method}/{graph_label or 'all'}/{variant}: cold {row['cold_median_s']:.6f}s warm {row['warm_median_s']:.6f}s",
                    flush=True,
                )
        replay_candidates, replay_paths = source_paths(logs, args.vendor)
        assert len(replay_candidates) == len(candidates), "source inventory changed"
        assert input_evidence(replay_paths, logs) == report["input"], "inputs changed"
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
    parser.add_argument("--vendor", choices=("amp", "codex_cli"), default="amp")
    parser.add_argument(
        "--logs",
        type=Path,
        help="Frozen provider source root; omit to generate synthetic Amp inputs",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.graphs, args.turns, args.repeat) < 1:
        parser.error("graphs, turns and repeat must be positive")
    main(args)
