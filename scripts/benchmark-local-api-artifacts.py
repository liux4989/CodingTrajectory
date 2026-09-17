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
    stages = {
        "discovery": {"wall_s": 0.0, "cpu_s": 0.0, "calls": 1},
        "fact_projection": {"wall_s": 0.0, "cpu_s": 0.0, "calls": 0},
        "summary_preparation": {"wall_s": 0.0, "cpu_s": 0.0, "calls": 0},
        "object_serialization": {"wall_s": 0.0, "cpu_s": 0.0, "calls": 0},
        "manifest_assembly": {"wall_s": 0.0, "cpu_s": 0.0, "calls": 0},
    }
    wall_start, cpu_start = time.perf_counter(), time.process_time()
    store = discover_store_from_files(paths).store
    stages["discovery"]["wall_s"] += time.perf_counter() - wall_start
    stages["discovery"]["cpu_s"] += time.process_time() - cpu_start
    objects, graphs = {}, []
    for graph in store.session_graphs.values():
        wall_start, cpu_start = time.perf_counter(), time.process_time()
        facts = build_published_fact_set(graph)
        stages["fact_projection"]["wall_s"] += time.perf_counter() - wall_start
        stages["fact_projection"]["cpu_s"] += time.process_time() - cpu_start
        stages["fact_projection"]["calls"] += 1

        wall_start, cpu_start = time.perf_counter(), time.process_time()
        summary = _prepared_graph_summary(facts)
        stages["summary_preparation"]["wall_s"] += time.perf_counter() - wall_start
        stages["summary_preparation"]["cpu_s"] += time.process_time() - cpu_start
        stages["summary_preparation"]["calls"] += 1

        wall_start, cpu_start = time.perf_counter(), time.process_time()
        refs = {}
        for kind, value in (("facts", facts), ("summary", summary)):
            body = encode(value.model_dump(mode="json", exclude_none=True))
            digest = hashlib.sha256(body).hexdigest()
            objects[digest] = body
            refs[kind] = ArtifactObjectReference(
                kind=kind, sha256=digest, bytes=len(body)
            )
        stages["object_serialization"]["wall_s"] += time.perf_counter() - wall_start
        stages["object_serialization"]["cpu_s"] += time.process_time() - cpu_start
        stages["object_serialization"]["calls"] += 1

        wall_start, cpu_start = time.perf_counter(), time.process_time()
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
        stages["manifest_assembly"]["wall_s"] += time.perf_counter() - wall_start
        stages["manifest_assembly"]["cpu_s"] += time.process_time() - cpu_start
        stages["manifest_assembly"]["calls"] += 1

    wall_start, cpu_start = time.perf_counter(), time.process_time()
    manifest = ArtifactManifest(
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
    )
    canonical_vendors = Counter(
        session.vendor.value
        for graph in store.session_graphs.values()
        for session in graph.sessions
    )
    stages["manifest_assembly"]["wall_s"] += time.perf_counter() - wall_start
    stages["manifest_assembly"]["cpu_s"] += time.process_time() - cpu_start
    stages["manifest_assembly"]["calls"] += 1
    return manifest, objects, canonical_vendors, stages


def preparation_fingerprint(manifest, objects, canonical_vendors):
    manifest_body = encode(manifest.model_dump(mode="json", exclude_none=True))
    inventory = sorted(
        (digest, hashlib.sha256(body).hexdigest(), len(body))
        for digest, body in objects.items()
    )
    return {
        "manifest_sha256": hashlib.sha256(manifest_body).hexdigest(),
        "manifest_bytes": len(manifest_body),
        "object_inventory_sha256": hashlib.sha256(encode(inventory)).hexdigest(),
        "objects": len(objects),
        "object_bytes": sum(len(body) for body in objects.values()),
        "canonical_vendors": dict(canonical_vendors),
    }


def summarize_preparation_stages(runs):
    summary = {}
    for stage in runs[0]["stages"]:
        wall = [run["stages"][stage]["wall_s"] for run in runs]
        cpu = [run["stages"][stage]["cpu_s"] for run in runs]
        wall_fractions = [
            run["stages"][stage]["wall_fraction_of_total"] for run in runs
        ]
        cpu_fractions = [run["stages"][stage]["cpu_fraction_of_total"] for run in runs]
        summary[stage] = {
            "wall_median_s": statistics.median(wall),
            "wall_min_s": min(wall),
            "wall_max_s": max(wall),
            "cpu_median_s": statistics.median(cpu),
            "cpu_min_s": min(cpu),
            "cpu_max_s": max(cpu),
            "wall_fraction_median_of_per_run_total": statistics.median(wall_fractions),
            "cpu_fraction_median_of_per_run_total": statistics.median(cpu_fractions),
        }
    return summary


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
            "Stage timers include timer/bookkeeping overhead; unassigned loop and instrumentation work remains explicit as residual.",
            "Stage fraction summaries are medians of per-run stage/total fractions; separately computed medians need not sum to one.",
        ],
        "preparation_fraction_denominators": {
            "wall": "same-run external end-to-end preparation wall time",
            "cpu": "same-run external end-to-end preparation process CPU time",
        },
        "turns_per_graph": args.turns if args.logs is None else None,
        "requested_vendor": args.vendor,
        "preparation_runs_s": [],
        "preparation_runs_cpu_s": [],
        "preparation_stage_runs": [],
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
        expected = None
        if args.expected_preparation_fingerprint:
            expected = json.loads(args.expected_preparation_fingerprint.read_text())
            assert expected["input"] == report["input"], "baseline input mismatch"
            expected = {key: value for key, value in expected.items() if key != "input"}
        for iteration in range(1, args.repeat + 1):
            wall_start, cpu_start = time.perf_counter(), time.process_time()
            manifest, objects, canonical_vendors, stages = prepare(paths)
            total_wall = time.perf_counter() - wall_start
            total_cpu = time.process_time() - cpu_start
            report["preparation_runs_s"].append(total_wall)
            report["preparation_runs_cpu_s"].append(total_cpu)
            fingerprint = preparation_fingerprint(manifest, objects, canonical_vendors)
            if "preparation_fingerprint" not in report:
                report["preparation_fingerprint"] = fingerprint
            assert fingerprint == report["preparation_fingerprint"], (
                "preparation output changed between repeats"
            )
            if expected is not None:
                assert fingerprint == expected, (
                    "instrumented preparation output changed"
                )
            assigned_wall = sum(stage["wall_s"] for stage in stages.values())
            assigned_cpu = sum(stage["cpu_s"] for stage in stages.values())
            stages["residual"] = {
                "wall_s": total_wall - assigned_wall,
                "cpu_s": total_cpu - assigned_cpu,
                "calls": 1,
            }
            for stage in stages.values():
                stage["wall_fraction_of_total"] = stage["wall_s"] / total_wall
                stage["cpu_fraction_of_total"] = stage["cpu_s"] / total_cpu
            report["preparation_stage_runs"].append(
                {
                    "iteration": iteration,
                    "first_iteration": iteration == 1,
                    "total_wall_s": total_wall,
                    "total_cpu_s": total_cpu,
                    "stages": stages,
                }
            )
        report["preparation_stage_summary"] = summarize_preparation_stages(
            report["preparation_stage_runs"]
        )
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
    parser.add_argument("--expected-preparation-fingerprint", type=Path)
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
