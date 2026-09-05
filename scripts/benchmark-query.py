#!/usr/bin/env python3
"""Benchmark the @packages/core query API.

Surveys two cost layers of the query path:

1. Store build (discovery + ingestion -> DocumentStore)
   - targeted: ingest only the files for one session_graph (warm path-index cache)
   - full:     ingest every matching log in the selected project/global scope
2. Projection (warm store): each ServiceRuntime method run in isolation

For the slowest methods, an optional cProfile pass pinpoints hot lines.

Usage:
    uv run python scripts/benchmark-query.py                       # auto-pick largest graph
    uv run python scripts/benchmark-query.py --graph-id <uuid>      # target a specific graph
    uv run python scripts/benchmark-query.py --global-scope         # search all known logs
    uv run python scripts/benchmark-query.py --profile stats,tool_usage
    uv run python scripts/benchmark-query.py --repeat 3
    uv run python scripts/benchmark-query.py --small                # use smallest graph (sanity)

Writes a JSON baseline to .artifacts/benchmarks/query-baseline.json.
"""

from __future__ import annotations

import argparse
import cProfile
import io
import json
import pstats
import statistics
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages" / "core" / "src"))

from coding_trajectory.service import (  # noqa: E402
    IndexCache,
    dispatch,
    resolve_store,
)
from coding_trajectory.query import DocumentStore  # noqa: E402


# Methods that take a session entry point (session_id / root_session_id).
ENTRYPOINT_METHODS: list[tuple[str, dict[str, Any]]] = [
    ("session.overview", {}),
    ("graph.overview", {}),
    ("session.usage", {}),
    ("graph.usage", {}),
    ("session.stats", {}),
    ("graph.stats", {}),
    ("session.model_usage", {}),
    ("session.request_usage", {}),
    ("session.tool_usage", {}),
    ("session.events", {}),
    ("session.items", {"types": ["tool_call"]}),
]

COLLECTION_METHODS: list[tuple[str, dict[str, Any]]] = [
    ("project.sessions", {}),
    ("project.sessions", {"include": ["usage"]}),
    ("project.sessions", {"include": ["runtime"]}),
]


def _fmt_ms(seconds: float) -> str:
    if seconds >= 1.0:
        return f"{seconds:8.2f} s"
    return f"{seconds * 1000:8.1f}ms"


def _fmt_size(byte_count: int) -> str:
    return f"{byte_count / 1024:8.1f}KB"


def _store_stats(store: DocumentStore) -> dict[str, int]:
    return {
        "graphs": len(store.session_graphs),
        "sessions": len(store.sessions),
        "turns": len(store.turns),
        "items": len(store.items),
        "events": len(store.events),
    }


def bench_store_build(
    *,
    global_scope: bool,
    current_dir: Path,
    cache: IndexCache,
    params: dict[str, Any],
    repeat: int,
) -> dict[str, Any]:
    """Time resolve_store (discovery + ingestion -> DocumentStore)."""
    runs: list[float] = []
    last_store: DocumentStore | None = None
    note = ""
    for _ in range(repeat):
        cache2 = IndexCache.load()  # fresh cache each run to measure cold path
        t0 = time.perf_counter()
        last_store, note = resolve_store(
            params,
            global_scope=global_scope,
            current_dir=current_dir,
            cache=cache2,
        )
        runs.append(time.perf_counter() - t0)
    assert last_store is not None
    return {
        "runs_s": runs,
        "median_s": statistics.median(runs),
        "min_s": min(runs),
        "max_s": max(runs),
        "note": note,
        "store": _store_stats(last_store),
    }


def bench_projection(
    *,
    method: str,
    params: dict[str, Any],
    store: DocumentStore,
    current_dir: Path,
    cache: IndexCache,
    global_scope: bool,
    repeat: int,
    discovery_note: str,
) -> dict[str, Any]:
    """Time a single dispatch call on a warm store."""
    full = {**params}
    runs: list[float] = []
    resp_size = 0
    for _ in range(repeat):
        t0 = time.perf_counter()
        result = dispatch(
            method,
            full,
            store=store,
            global_scope=global_scope,
            current_dir=current_dir,
            discovery_note=discovery_note,
            cache=cache,
        )
        runs.append(time.perf_counter() - t0)
        resp_size = len(json.dumps(result, default=str, separators=(",", ":")))
    return {
        "runs_s": runs,
        "median_s": statistics.median(runs),
        "min_s": min(runs),
        "max_s": max(runs),
        "resp_bytes": resp_size,
    }


def profile_projection(
    *,
    method: str,
    params: dict[str, Any],
    store: DocumentStore,
    current_dir: Path,
    cache: IndexCache,
    global_scope: bool,
    discovery_note: str,
    top: int = 25,
) -> str:
    """cProfile a single dispatch call; return top-N cumulative lines."""
    profiler = cProfile.Profile()
    profiler.enable()
    dispatch(
        method,
        params,
        store=store,
        global_scope=global_scope,
        current_dir=current_dir,
        discovery_note=discovery_note,
        cache=cache,
    )
    profiler.disable()
    buf = io.StringIO()
    stats = pstats.Stats(profiler, stream=buf).sort_stats("cumulative")
    stats.print_stats(top)
    return buf.getvalue()


def pick_graph(
    *, global_scope: bool, current_dir: Path, smallest: bool
) -> tuple[str, dict[str, int]]:
    """Pick a graph id; return (graph_id, {sessions,turns,items})."""
    # This is an offline ingestion/projection benchmark, not a public API read.
    cache = IndexCache.load()
    store, note = resolve_store(
        {},
        global_scope=global_scope,
        current_dir=current_dir,
        cache=cache,
    )
    res = dispatch(
        "project.sessions",
        {},
        store=store,
        global_scope=global_scope,
        current_dir=current_dir,
        discovery_note=note,
        cache=cache,
    )
    items = res["items"]
    items.sort(key=lambda i: len(i.get("session_ids", [])), reverse=not smallest)
    top = items[0]
    return top["root_session_id"], {
        "sessions": len(top.get("session_ids", [])),
        "title": (top.get("title") or "")[:60],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph-id", help="target a specific session_graph root id")
    parser.add_argument(
        "--global-scope",
        action="store_true",
        help="search all known log files instead of the current project",
    )
    parser.add_argument("--small", action="store_true", help="use the smallest graph (sanity check)")
    parser.add_argument("--repeat", type=int, default=1, help="repetitions per measurement")
    parser.add_argument("--profile", default="", help="comma-separated methods to cProfile (e.g. stats,tool_usage)")
    parser.add_argument("--no-store-build", action="store_true", help="skip the (slow) store-build benchmarks")
    parser.add_argument("--no-projection", action="store_true", help="skip per-method projection benchmarks")
    parser.add_argument(
        "--methods",
        default="",
        help="comma-separated method names to include in projection (default: all)",
    )
    parser.add_argument(
        "--skip-methods",
        default="",
        help="comma-separated method names to exclude from projection",
    )
    args = parser.parse_args()

    current_dir = REPO_ROOT
    global_scope = args.global_scope
    cache = IndexCache.load()

    # --- pick a target graph -------------------------------------------------
    if args.graph_id:
        graph_id = args.graph_id
        meta = {"sessions": "?", "title": "(user-specified)"}
    else:
        graph_id, meta = pick_graph(
            global_scope=global_scope, current_dir=current_dir, smallest=args.small
        )
    print("=" * 78)
    print(f"target graph: {graph_id}")
    print(f"  {meta}")
    print(f"scope: {'global' if global_scope else 'project'}  repeat={args.repeat}")
    print("=" * 78)

    baseline: dict[str, Any] = {
        "graph_id": graph_id,
        "graph_meta": meta,
        "scope": "global" if global_scope else "project",
        "repeat": args.repeat,
        "store_build": {},
        "projection": {},
        "profiles": {},
    }

    # --- layer 1: store build ------------------------------------------------
    if not args.no_store_build:
        print("\n## Layer 1: store build (discovery + ingestion)\n")
        print(f"{'case':28}{'median':>11}{'min':>11}{'max':>11}  store")
        print("-" * 90)
        full_scope_label = (
            "global (all logs)" if global_scope else "project (current project)"
        )
        for label, params in [
            ("targeted (warm cache)", {"session_id": graph_id}),
            (full_scope_label, {}),
        ]:
            r = bench_store_build(
                global_scope=global_scope,
                current_dir=current_dir,
                cache=cache,
                params=params,
                repeat=args.repeat,
            )
            baseline["store_build"][label] = r
            s = r["store"]
            store_str = f"g={s['graphs']} s={s['sessions']} t={s['turns']} i={s['items']} e={s['events']}"
            print(
                f"{label:28}{_fmt_ms(r['median_s']):>11}{_fmt_ms(r['min_s']):>11}"
                f"{_fmt_ms(r['max_s']):>11}  {store_str}"
            )

    # --- warm store for projections -----------------------------------------
    if not args.no_projection:
        print("\n## Layer 2: projection (warm store, single dispatch)\n")
        warm_cache = IndexCache.load()
        t0 = time.perf_counter()
        store, discovery_note = resolve_store(
            {"session_id": graph_id},
            global_scope=global_scope,
            current_dir=current_dir,
            cache=warm_cache,
        )
        warm_build_s = time.perf_counter() - t0
        s = _store_stats(store)
        print(f"(warm store built once in {_fmt_ms(warm_build_s)}: "
              f"g={s['graphs']} s={s['sessions']} t={s['turns']} i={s['items']} e={s['events']})\n")

        print(f"{'method':22}{'params':22}{'median':>11}{'min':>11}{'max':>11}  resp")
        print("-" * 100)
        include = {m.strip() for m in args.methods.split(",") if m.strip()}
        exclude = {m.strip() for m in args.skip_methods.split(",") if m.strip()}
        for method, params in ENTRYPOINT_METHODS:
            if include and method not in include:
                continue
            if method in exclude:
                continue
            full = {"session_id": graph_id, **params}
            label = json.dumps(params, sort_keys=True) if params else "{}"
            try:
                r = bench_projection(
                    method=method,
                    params=full,
                    store=store,
                    current_dir=current_dir,
                    cache=warm_cache,
                    global_scope=global_scope,
                    repeat=args.repeat,
                    discovery_note=discovery_note,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"{method:22}{label:22}  ERROR: {exc}")
                continue
            baseline["projection"][f"{method} {label}"] = r
            print(
                f"{method:22}{label:22}{_fmt_ms(r['median_s']):>11}"
                f"{_fmt_ms(r['min_s']):>11}{_fmt_ms(r['max_s']):>11}  "
                f"{_fmt_size(r['resp_bytes'])}"
            )

        # --- cProfile the requested slow methods -----------------------------
        if args.profile:
            print("\n## cProfile passes\n")
            want = {m.strip() for m in args.profile.split(",") if m.strip()}
            for method, params in ENTRYPOINT_METHODS:
                if method not in want:
                    continue
                full = {"session_id": graph_id, **params}
                print(f"\n### {method} {json.dumps(params)}\n")
                out = profile_projection(
                    method=method,
                    params=full,
                    store=store,
                    current_dir=current_dir,
                    cache=warm_cache,
                    global_scope=global_scope,
                    discovery_note=discovery_note,
                )
                baseline["profiles"][method] = out
                # print the top-N table only (skip the pstats header lines)
                for line in out.splitlines():
                    print(line)

    # --- persist baseline ----------------------------------------------------
    out_dir = REPO_ROOT / ".artifacts" / "benchmarks"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "query-baseline.json"
    out_path.write_text(json.dumps(baseline, indent=2, default=str) + "\n")
    print(f"\nbaseline written: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
