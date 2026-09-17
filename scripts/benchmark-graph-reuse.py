#!/usr/bin/env python3
"""Offline, disposable per-graph preparation/reuse experiment; no runtime changes."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from coding_trajectory.control_plane.fact_projection import build_published_fact_set
from coding_trajectory.control_plane.published_facts import (
    FactIndex,
    PublishedFactSet,
)
from coding_trajectory.discovery import discover_store_from_files
from coding_trajectory.ingestion.common import canonical_json
from coding_trajectory.service import handlers
from coding_trajectory.service.store import IndexCache

ROOT = Path(__file__).resolve().parents[1]
PIN = "368d21be8330163d53ea6884f289205b3ae12226"
spec = importlib.util.spec_from_file_location(
    "prior_compute", ROOT / "scripts/benchmark-local-artifact-compute.py"
)
prior = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prior)
METHODS = ("graph.stats", "session.summary", "session.overview")


def encode(value):
    return canonical_json(value).encode()


def durable_write(path, data):
    if path.exists():
        return 0
    with path.open("wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    return len(data)


def invoke(index, cache, method, graph):
    params = {"root_session_id" if method == "graph.stats" else "session_id": graph}
    if method == "session.overview":
        params["limit"] = 10
    return handlers.dispatch(
        method,
        params,
        store=index,
        global_scope=False,
        current_dir=prior.PROJECT,
        discovery_note="",
        cache=cache,
    )


class OnceIndex(IndexCache):
    def index_facts(self, facts):
        if getattr(self, "indexed", None) is not facts:
            super().index_facts(facts)
            self.indexed = facts


class GraphMemo:
    """Process-local benchmark wrapper, keyed by live FactIndex identity and graph."""

    def __init__(self):
        self.original = handlers.session_graph_from_fact_index
        self.graphs = {}
        self.reconstructed = 0

    def get(self, index, graph):
        key = (id(index), graph)
        if key not in self.graphs:
            self.graphs[key] = (index, self.original(index, graph))
            self.reconstructed += 1
        return self.graphs[key][1]

    def uncached(self, index, graph):
        self.reconstructed += 1
        return self.original(index, graph)


def prepare(args, measurements):
    cache = args.cache
    cache.mkdir(exist_ok=True)
    report = {"projected_graphs": 0, "projected_rows": 0, "written_bytes": 0}
    with measurements.stage("prepare_total"):
        with measurements.stage("manifest_load"):
            old = json.loads(args.seed.read_bytes()) if args.seed else {"graphs": {}}
            if args.seed:
                assert old["contract"] == PIN
        with measurements.stage("discover_parse_assemble"):
            os.environ["CT_AMP_LOG_DIR"] = str(args.logs)
            paths = sorted(args.logs.glob("*.jsonl"))
            graphs = (
                discover_store_from_files(paths).store.session_graphs if paths else {}
            )
        with measurements.stage("graph_content_hash"):
            inputs = {
                str(k): prior.sha(encode(v.model_dump(mode="json", exclude_none=True)))
                for k, v in graphs.items()
            }
        entries = {}
        baseline_facts = []
        if args.variant == "baseline":
            with measurements.stage("fact_projection"):
                baseline_facts = [build_published_fact_set(g) for g in graphs.values()]
            with measurements.stage("full_fact_index"):
                full_index = FactIndex.from_fact_sets(baseline_facts)
            by_id = {str(f.graph_id): f for f in baseline_facts}
        for graph_id in sorted(inputs):
            previous = old["graphs"].get(graph_id)
            if (
                args.variant == "candidate"
                and previous
                and previous["input_sha256"] == inputs[graph_id]
            ):
                entries[graph_id] = previous
                continue
            if args.variant == "candidate":
                with measurements.stage("fact_projection"):
                    fact = build_published_fact_set(graphs[UUID(graph_id)])
                with measurements.stage("per_graph_fact_index"):
                    index = FactIndex.from_fact_sets([fact])
                view_cache = OnceIndex()
                memo = GraphMemo()
                handlers.session_graph_from_fact_index = memo.get
            else:
                fact, index, view_cache = by_id[graph_id], full_index, IndexCache()
            report["projected_graphs"] += 1
            report["projected_rows"] += len(fact.rows)
            with measurements.stage("summary_preparation"):
                summaries = {
                    method: invoke(index, view_cache, method, graph_id)
                    for method in METHODS[:2]
                }
            if args.variant == "candidate":
                handlers.session_graph_from_fact_index = memo.original
            with measurements.stage("canonical_serialization_hash"):
                fact_bytes = encode(fact.model_dump(mode="json", exclude_none=True))
                view_bytes = encode(summaries)
                fact_hash, view_hash = prior.sha(fact_bytes), prior.sha(view_bytes)
            with measurements.stage("artifact_persist_fsync"):
                report["written_bytes"] += durable_write(cache / fact_hash, fact_bytes)
                report["written_bytes"] += durable_write(cache / view_hash, view_bytes)
            entries[graph_id] = {
                "input_sha256": inputs[graph_id],
                "facts": fact_hash,
                "views": view_hash,
                "rows": len(fact.rows),
                "fact_bytes": len(fact_bytes),
                "view_bytes": len(view_bytes),
            }
        with measurements.stage("manifest_persist_fsync"):
            manifest = encode({"contract": PIN, "graphs": entries})
            manifest_path = cache / (prior.sha(manifest) + ".manifest")
            report["written_bytes"] += durable_write(manifest_path, manifest)
    report.update(
        manifest=str(manifest_path),
        manifest_bytes=len(manifest),
        graphs=len(entries),
        rows=sum(e["rows"] for e in entries.values()),
        reused_graphs=len(entries) - report["projected_graphs"],
        removed_graphs=len(set(old["graphs"]) - set(entries)),
    )
    return report


class Reader:
    def __init__(self, cache, variant):
        self.cache, self.variant = cache, variant
        self.snapshots, self.indexes, self.summaries = {}, {}, {}
        self.bytes_read, self.rows_validated = 0, 0
        self.memo = GraphMemo()
        if variant == "candidate":
            handlers.session_graph_from_fact_index = self.memo.get
        else:
            handlers.session_graph_from_fact_index = self.memo.uncached

    def load(self, key):
        data = (self.cache / key).read_bytes()
        self.bytes_read += len(data)
        assert prior.sha(data) == key.split(".")[0]
        return json.loads(data)

    def snapshot(self, path):
        key = path.name
        if key not in self.snapshots:
            manifest = self.load(key)
            assert manifest["contract"] == PIN
            self.snapshots[key] = manifest
            if self.variant == "baseline":
                facts = [
                    PublishedFactSet.model_validate(self.load(e["facts"]))
                    for e in manifest["graphs"].values()
                ]
                self.rows_validated += sum(len(f.rows) for f in facts)
                self.indexes[key] = (FactIndex.from_fact_sets(facts), IndexCache())
        return self.snapshots[key]

    def query(self, path, graph, method):
        snapshot = self.snapshot(path)
        if graph not in snapshot["graphs"]:
            return {"missing": graph}
        entry = snapshot["graphs"][graph]
        if self.variant == "candidate":
            if method in METHODS[:2]:
                if entry["views"] not in self.summaries:
                    data = self.load(entry["views"])
                    self.summaries[entry["views"]] = {
                        m: handlers.service_contract(m).validate_response(data[m])
                        for m in METHODS[:2]
                    }
                return self.summaries[entry["views"]][method]
            key = entry["facts"]
            if key not in self.indexes:
                facts = PublishedFactSet.model_validate(self.load(key))
                self.rows_validated += len(facts.rows)
                self.indexes[key] = (FactIndex.from_fact_sets([facts]), OnceIndex())
        else:
            key = path.name
        index, view_cache = self.indexes[key]
        return invoke(index, view_cache, method, graph)


def read_selected(args, measurements):
    reader = Reader(args.cache, args.variant)
    results, stages = {}, {}
    targets = [
        str(uuid5(NAMESPACE_URL, f"artifact-compute:{g}"))
        for g in (0, 1, args.graphs - 1)
    ]
    for label, snapshot in (
        ("old_cold", args.seed),
        ("current_first", args.manifest),
        ("current_warm", args.manifest),
        ("old_again", args.seed),
    ):
        before = reader.bytes_read, reader.rows_validated, reader.memo.reconstructed
        with measurements.stage(label):
            responses = [
                reader.query(snapshot, graph, method)
                for graph in dict.fromkeys(targets)
                for method in METHODS
            ]
            results[label] = prior.sha(encode(responses))
        stages[label] = {
            "bytes_loaded": reader.bytes_read - before[0],
            "rows_validated": reader.rows_validated - before[1],
            "graphs_reconstructed": reader.memo.reconstructed - before[2],
        }
    assert results["old_cold"] == results["old_again"]
    assert results["current_first"] == results["current_warm"]
    return {"response_hashes": results, "work": stages}


def child(args):
    measurements = prior.Measurements()
    with measurements.stage("total"):
        report = (
            prepare(args, measurements)
            if args.role == "prepare"
            else read_selected(args, measurements)
        )
    report["stages"] = measurements.stages
    report["peak_rss_bytes"] = prior.peak_rss()
    args.output.write_text(json.dumps(report, indent=2) + "\n")


def run_child(folder, role, variant, **options):
    check = ["git", "diff", "--exit-code", PIN, "--", "packages/core/src"]
    subprocess.run(check, cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    output = folder / f"{role}-{variant}.json"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--role",
        role,
        "--variant",
        variant,
        "--output",
        str(output),
    ]
    for key, value in options.items():
        if value is not None:
            command.extend(["--" + key, str(value)])
    started = time.perf_counter()
    subprocess.run(command, check=True, cwd=ROOT)
    elapsed = time.perf_counter() - started
    subprocess.run(check, cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    report = json.loads(output.read_bytes())
    report["process_wall_including_imports_s"] = elapsed
    return report


def mutate(logs, initial, case, turns):
    for name, data in initial.items():
        (logs / name).write_bytes(data)
    path = logs / "0000.jsonl"
    rows = [json.loads(line) for line in path.read_bytes().splitlines()]
    thread = rows[0]["payload"]["id"]
    if case == "append":
        rows.extend(prior.turn_rows(thread, turns))
    elif case == "changed":
        rows[-6:] = prior.turn_rows(thread, turns - 1, changed=True)
    elif case == "merge":
        rows[3]["tool_name"] = "create_thread"
        rows[3]["input"] = {}
        rows[4]["tool_name"] = "create_thread"
        rows[4]["output"] = json.dumps(
            {
                "threadID": "T-" + str(uuid5(NAMESPACE_URL, "artifact-compute:1")),
                "executor": "orb",
                "agentMode": "medium",
            }
        )
    path.write_bytes(b"".join(encode(row) + b"\n" for row in rows))
    if case == "delete":
        path.unlink()
    if case == "empty":
        for path in logs.glob("*.jsonl"):
            path.unlink()
    for path in logs.glob("*.jsonl"):
        os.utime(path, (1789430400, 1789430400))


def main(args):
    sources = subprocess.check_output(
        ["git", "ls-files", "packages/core/src"], cwd=ROOT, text=True
    ).splitlines()
    report = {
        "pin": PIN,
        "head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "source_sha256": prior.sha(
            encode({p: prior.sha((ROOT / p).read_bytes()) for p in sources})
        ),
        "harness_sha256": prior.sha(Path(__file__).read_bytes()),
        "fixture_harness_sha256": prior.sha(
            (ROOT / "scripts/benchmark-local-artifact-compute.py").read_bytes()
        ),
        "environment": {"python": sys.version, "platform": platform.platform()},
        "synthetic": True,
        "network_requests": 0,
        "results": [],
    }
    shapes = (
        [("pilot", 29, 100), ("4x", 116, 100)] if not args.smoke else [("smoke", 3, 3)]
    )
    with tempfile.TemporaryDirectory(prefix="ct-graph-reuse-") as temporary:
        for shape, graphs, turns in shapes:
            base = Path(temporary) / shape
            base.mkdir()
            logs = base / "logs"
            prior.create_logs(logs, graphs, turns, 128)
            initial = {p.name: p.read_bytes() for p in logs.glob("*.jsonl")}
            seeds = {}
            cases = ["cold", "unchanged", "append", "changed"]
            if shape != "4x":
                cases += ["delete", "merge", "split", "empty"]
            for case in cases:
                mutate(logs, initial, case, turns)
                for repeat in range(args.repeats):
                    folder = base / f"{case}-{repeat}"
                    folder.mkdir()
                    results = {}
                    for variant in ("baseline", "candidate"):
                        cache = folder / variant
                        seed = None
                        if case != "cold":
                            seed_source = seeds[
                                (variant, "merge" if case == "split" else "cold")
                            ]
                            shutil.copytree(seed_source.parent, cache)
                            seed = cache / seed_source.name
                        result = run_child(
                            folder,
                            "prepare",
                            variant,
                            logs=logs,
                            cache=cache,
                            seed=seed,
                        )
                        manifest = Path(result.pop("manifest"))
                        current = json.loads(manifest.read_bytes())
                        expected_graphs = (
                            graphs - (case in {"delete", "merge"})
                            if case != "empty"
                            else 0
                        )
                        assert len(current["graphs"]) == expected_graphs
                        if case in {"cold", "merge"} and repeat == 0:
                            seeds[(variant, case)] = manifest
                        selected = run_child(
                            folder,
                            "reader",
                            variant,
                            cache=cache,
                            seed=seed or manifest,
                            manifest=manifest,
                            graphs=graphs,
                        )
                        results[variant] = {
                            "prepare": result,
                            "reader": selected,
                            "manifest_sha256": prior.sha(manifest.read_bytes()),
                        }
                    assert (
                        results["baseline"]["manifest_sha256"]
                        == results["candidate"]["manifest_sha256"]
                    )
                    assert (
                        results["baseline"]["reader"]["response_hashes"]
                        == results["candidate"]["reader"]["response_hashes"]
                    )
                    expected_projected = {
                        "cold": graphs,
                        "unchanged": 0,
                        "append": 1,
                        "changed": 1,
                        "delete": 0,
                        "merge": 1,
                        "split": 2,
                        "empty": 0,
                    }[case]
                    assert (
                        results["candidate"]["prepare"]["projected_graphs"]
                        == expected_projected
                    )
                    hashes = results["candidate"]["reader"]["response_hashes"]
                    assert (hashes["old_cold"] == hashes["current_first"]) == (
                        case in {"cold", "unchanged"}
                    )
                    assert results["candidate"]["reader"]["work"]["current_warm"] == {
                        "bytes_loaded": 0,
                        "rows_validated": 0,
                        "graphs_reconstructed": 0,
                    }
                    report["results"].append(
                        {
                            "shape": shape,
                            "case": case,
                            "repeat": repeat,
                            "input": prior.input_evidence(logs),
                            **results,
                        }
                    )
                    print(
                        f"{shape}/{case}/{repeat}: baseline {results['baseline']['prepare']['stages']['total']['wall_s']:.3f}s candidate {results['candidate']['prepare']['stages']['total']['wall_s']:.3f}s",
                        flush=True,
                    )
    args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":

    def refuse_network(event, _args):
        if event == "socket.connect":
            raise RuntimeError("network is prohibited")

    sys.addaudithook(refuse_network)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=["prepare", "reader"])
    parser.add_argument("--variant", choices=["baseline", "candidate"])
    for flag in ("logs", "cache", "seed", "manifest", "output"):
        parser.add_argument("--" + flag, type=Path, required=flag == "output")
    parser.add_argument("--graphs", type=int)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--smoke", action="store_true")
    options = parser.parse_args()
    child(options) if options.role else main(options)
