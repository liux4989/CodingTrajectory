#!/usr/bin/env python3
"""Benchmark the dashboard incremental read path.

Measures warm-query latency (p50/p95) for every HTTP-backed runtime read
against an existing revisioned SQLite store, and records per-object storage
from the dbstat virtual table.  One unmeasured warm-up call per query absorbs
lazy evidence materialization; timed runs are steady-state reads.

Usage:
    uv run python scripts/benchmark-dashboard.py
    uv run python scripts/benchmark-dashboard.py --repeat 30
    uv run python scripts/benchmark-dashboard.py --database /path/to/read-models-v3.sqlite3

Writes a JSON baseline to benchmarks/results/dashboard-store-baseline.json.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages" / "core" / "src"))
sys.path.insert(0, str(REPO_ROOT / "packages" / "plugins" / "dashboard"))

from incremental_runtime import (  # noqa: E402
    DashboardIncrementalRuntime,
    _default_database_path,
)


def _percentile(ordered_values: list[float], percentile: float) -> float:
    if not ordered_values:
        return 0.0
    if len(ordered_values) == 1:
        return round(ordered_values[0], 3)
    position = (len(ordered_values) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered_values) - 1)
    if lower == upper:
        return round(ordered_values[lower], 3)
    weight = position - lower
    return round(
        ordered_values[lower] * (1 - weight) + ordered_values[upper] * weight, 3
    )


def _time_query(call: Any, *, repeat: int) -> dict[str, Any]:
    call()  # warm-up: absorbs lazy evidence materialization and page cache
    durations: list[float] = []
    response_bytes = 0
    for _ in range(repeat):
        started = time.perf_counter()
        result = call()
        durations.append((time.perf_counter() - started) * 1000)
        response_bytes = len(json.dumps(result, default=str).encode())
    ordered = sorted(durations)
    return {
        "runs_ms": [round(value, 3) for value in durations],
        "min_ms": round(ordered[0], 3),
        "p50_ms": _percentile(ordered, 0.50),
        "p95_ms": _percentile(ordered, 0.95),
        "max_ms": round(ordered[-1], 3),
        "response_bytes": response_bytes,
    }


def _dbstat(database_path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)
    try:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        freelist = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        objects = []
        try:
            rows = connection.execute(
                """
                SELECT name, SUM(pgsize) AS bytes, COUNT(*) AS pages
                  FROM dbstat
                 GROUP BY name
                 ORDER BY bytes DESC
                """
            ).fetchall()
            objects = [
                {"name": row[0], "bytes": int(row[1]), "pages": int(row[2])}
                for row in rows
            ]
        except sqlite3.Error:
            objects = []
    finally:
        connection.close()
    wal_path = database_path.with_suffix(database_path.suffix + "-wal")
    return {
        "page_size": page_size,
        "page_count": page_count,
        "freelist_pages": freelist,
        "database_bytes": page_count * page_size,
        "wal_bytes": wal_path.stat().st_size if wal_path.is_file() else 0,
        "objects": objects,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=_default_database_path())
    parser.add_argument("--repeat", type=int, default=20)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    database_path = args.database.expanduser().resolve()
    if not database_path.is_file():
        raise SystemExit(f"dashboard store not found: {database_path}")

    runtime = DashboardIncrementalRuntime(
        current_dir=REPO_ROOT,
        database_path=database_path,
        autostart=False,
    )
    try:
        if not runtime.is_ready():
            raise SystemExit("dashboard store has no published route models")
        revision = runtime.store.current_revision()
        sessions_page = runtime.sessions(
            since_days=runtime.since_days,
            project_name=None,
            agent_vendor=None,
            limit=50,
            cursor=None,
        )
        session_items = (sessions_page or {}).get("items") or []
        if not session_items:
            raise SystemExit("dashboard store has no session rows to benchmark")
        # Skip actively appended sources: their evidence materialization fence
        # can never settle while this very process is being recorded.
        session_id = None
        for item in session_items:
            candidate = str(item["root_session_id"])
            try:
                if runtime.context_window(session_id=candidate) is not None:
                    session_id = candidate
                    break
            except Exception:
                continue
        if session_id is None:
            raise SystemExit("no stable session available for context_window")
        project_name = str(session_items[0].get("project") or "") or None

        queries: dict[str, Any] = {
            "snapshot": lambda: runtime.snapshot(),
            "changes": lambda: runtime.changes(max(0, revision - 96)),
            "overview": lambda: runtime.overview(since_days=runtime.since_days),
            "projects": lambda: runtime.projects(
                agent_vendor=None, limit=50, cursor=None
            ),
            "sessions": lambda: runtime.sessions(
                since_days=runtime.since_days,
                project_name=None,
                agent_vendor=None,
                limit=50,
                cursor=None,
            ),
            "sessions_project": lambda: runtime.sessions(
                since_days=runtime.since_days,
                project_name=project_name,
                agent_vendor=None,
                limit=50,
                cursor=None,
            ),
            "sessions_second_page": lambda: runtime.sessions(
                since_days=runtime.since_days,
                project_name=None,
                agent_vendor=None,
                limit=50,
                cursor=runtime.sessions(
                    since_days=runtime.since_days,
                    project_name=None,
                    agent_vendor=None,
                    limit=50,
                    cursor=None,
                ).get("next_cursor"),
            ),
            "session_timeline": lambda: runtime.session_timeline(
                since_days=runtime.since_days, limit=50, cursor=None
            ),
            "model_usage": lambda: runtime.model_usage(
                since_days=runtime.since_days,
                project_name=None,
                model_key=None,
                detail="both",
                limit=50,
                cursor=None,
            ),
            "token_efficiency_index": lambda: runtime.token_efficiency_index(
                since_days=runtime.since_days, limit=50, cursor=None
            ),
            "context_window": lambda: runtime.context_window(session_id=session_id),
        }
        results = []
        for name, call in queries.items():
            try:
                entry = {"name": name, **_time_query(call, repeat=args.repeat)}
            except Exception as exc:
                entry = {"name": name, "error": f"{type(exc).__name__}: {exc}"}
            results.append(entry)
            if "error" in entry:
                print(f"{name:24s} error: {entry['error']}")
                continue
            print(
                f"{name:24s} p50={entry['p50_ms']:9.3f}ms "
                f"p95={entry['p95_ms']:9.3f}ms bytes={entry['response_bytes']}"
            )
    finally:
        runtime.shutdown()

    storage = _dbstat(database_path)
    output = {
        "schema_version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "database": str(database_path),
        "revision": revision,
        "repeat": args.repeat,
        "fixture": {"session_id": session_id, "project_name": project_name},
        "results": results,
        "storage": storage,
    }
    out_path = args.output or (
        REPO_ROOT / "benchmarks" / "results" / "dashboard-store-baseline.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2) + "\n")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
