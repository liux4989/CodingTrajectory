#!/usr/bin/env python3
"""Offline query-cost experiments on a private reviewed export; aggregates only.

No credentials, network clients, or persistent database are used. SQLite VM steps
are local work measurements, not Cloudflare metered rows. Candidate changes below
are experiments, not modifications to the deployed Worker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import statistics
import subprocess
import time
from collections import Counter
from pathlib import Path
from uuid import UUID

from coding_trajectory.control_plane.collector import _fact_row_batches
from coding_trajectory.control_plane.fact_repository import CloudflareFactRepository
from coding_trajectory.control_plane.published_facts import PublishedFactSet
from coding_trajectory.ingestion.common import canonical_json
from pydantic import TypeAdapter

ROOT = Path(__file__).resolve().parents[1]
AGENT = "local-benchmark"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.export.read_bytes()
    export = json.loads(raw)
    facts = TypeAdapter(list[PublishedFactSet]).validate_python(export["fact_sets"])
    source = (ROOT / "cloudflare/control-plane/src/facts.ts").read_text()
    schema = source.split("state.sql.exec(`", 1)[1].split("`);", 1)[0]
    sql = sqlite3.connect(":memory:")
    sql.executescript(schema)
    batches = [(f, _fact_row_batches(f)) for f in facts]

    def measure(fn, repeats=3):
        results = []
        for _ in range(repeats):
            steps = 0

            def progress():
                nonlocal steps
                steps += 100
                return 0

            sql.set_progress_handler(progress, 100)
            started = time.perf_counter()
            value = fn()
            results.append((time.perf_counter() - started, steps))
            sql.set_progress_handler(None, 0)
        return value, {
            "median_seconds": round(statistics.median(r[0] for r in results), 6),
            "median_vm_steps_approx": statistics.median(r[1] for r in results),
            "runs": repeats,
        }

    def insert_batch(f, index, rows, count):
        for row in rows:
            d = row.model_dump(mode="json", exclude_none=True)
            sql.execute(
                "INSERT INTO staged_fact_items VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    AGENT,
                    str(f.graph_id),
                    f.fact_set_digest,
                    index,
                    row.kind,
                    str(row.fact_id),
                    str(row.parent_id) if row.parent_id else None,
                    row.order_index,
                    row.row_hash,
                    canonical_json(d),
                ),
            )
        sql.execute(
            "INSERT INTO staged_fact_rows VALUES(?,?,?,?,?,?,?)",
            (
                AGENT,
                str(f.graph_id),
                f.fact_set_digest,
                index,
                count,
                len(rows),
                canonical_json(
                    [r.model_dump(mode="json", exclude_none=True) for r in rows]
                ),
            ),
        )

    for f, groups in batches:
        for index, rows in enumerate(groups):
            insert_batch(f, index, rows, len(groups))
    counts = Counter(r.kind for f in facts for r in f.rows)
    report = {
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "sqlite_version": sqlite3.sqlite_version,
        "export_sha256": hashlib.sha256(raw).hexdigest(),
        "scope": {
            "graphs": len(facts),
            "rows": sum(counts.values()),
            "kind_counts": dict(counts),
            "batches": sum(len(b) for _, b in batches),
        },
        "units": "local wall time and approximate SQLite VM steps; not billing counters",
    }
    # Extract the current production query verbatim, resolving its SQL scope.
    ordering = re.search(
        r"`(SELECT 1 FROM staged_fact_items row WHERE \$\{where\} AND row.kind=\? AND.*?)`",
        source,
        re.DOTALL,
    ).group(1)
    ordering = ordering.replace(
        "${where}", "agent_id=? AND graph_id=? AND fact_set_digest=?"
    )

    def validation(kind):
        return [
            sql.execute(
                ordering, (AGENT, str(f.graph_id), f.fact_set_digest, kind)
            ).fetchall()
            for f in facts
        ]

    report["ordering_validation"] = {}
    for kind in ("turn", "item"):
        result, metrics = measure(lambda kind=kind: validation(kind))
        assert not any(result)
        report["ordering_validation"][kind] = {"current": metrics}
    sql.execute(
        "CREATE INDEX benchmark_parent_sequence ON staged_fact_items(agent_id,graph_id,fact_set_digest,kind,parent_id,json_extract(payload,'$.payload.sequence'))"
    )
    for kind in ("turn", "item"):
        result, metrics = measure(lambda kind=kind: validation(kind))
        assert not any(result)
        report["ordering_validation"][kind]["candidate_index"] = metrics
    sql.execute("DROP INDEX benchmark_parent_sequence")

    missing = re.search(
        r"`(SELECT batch.batch_index FROM staged_fact_rows batch.*?)`",
        source,
        re.DOTALL,
    ).group(1)
    metadata = "SELECT batch_index FROM staged_fact_rows WHERE agent_id=? AND graph_id=? AND fact_set_digest=?"
    sql.execute("DELETE FROM staged_fact_items")
    sql.execute("DELETE FROM staged_fact_rows")
    totals = {
        "current": {"seconds": 0.0, "vm_steps_approx": 0},
        "metadata_candidate": {"seconds": 0.0, "vm_steps_approx": 0},
    }
    recount_rows = 0
    metadata_rows = 0
    for f, groups in batches:
        cumulative = 0
        for index, rows in enumerate(groups):
            insert_batch(f, index, rows, len(groups))
            params = (AGENT, str(f.graph_id), f.fact_set_digest)
            current, a = measure(
                lambda params=params: sorted(sql.execute(missing, params).fetchall())
            )
            candidate, b = measure(
                lambda params=params: sorted(sql.execute(metadata, params).fetchall())
            )
            assert current == candidate == [(i,) for i in range(index + 1)]
            for key, result in (("current", a), ("metadata_candidate", b)):
                totals[key]["seconds"] += result["median_seconds"]
                totals[key]["vm_steps_approx"] += result["median_vm_steps_approx"]
            cumulative += len(rows)
            recount_rows += cumulative
            metadata_rows += index + 1
    report["staging_acknowledgments"] = {
        **totals,
        "normalized_rows_recounted": recount_rows,
        "batch_metadata_rows_returned": metadata_rows,
        "candidate_condition": "Valid transactional stage data only; full integrity verification must remain before publication",
    }

    # Exercise the real repository and its cache against an in-memory page source.
    # This isolates download/materialization amplification from HTTP and SQL costs.
    all_rows = [
        r.model_dump(mode="json", exclude_none=True) for f in facts for r in f.rows
    ]
    all_rows.sort(key=lambda r: (r["graph_id"], r["kind"], r["fact_id"]))
    pages = []
    page = []
    size = 2
    for row in all_rows:
        width = len(canonical_json(row).encode()) + bool(page)
        if page and (len(page) == 2048 or size + width > 1024 * 1024):
            pages.append(page)
            page = []
            size = 2
            width = len(canonical_json(row).encode())
        page.append(row)
        size += width
    if page:
        pages.append(page)
    workspace = UUID(int=1)

    class LocalPages:
        def __init__(self):
            self.calls = self.rows = self.bytes = 0

        def call(self, method, params):
            assert method == "ct_fact_read"
            index = int(params.get("cursor", 0))
            rows = pages[index]
            self.calls += 1
            self.rows += len(rows)
            self.bytes += len(canonical_json(rows).encode())
            return {
                "workspace_id": str(workspace),
                "snapshot_sequence": 63,
                "rows": rows,
                "graph_digests": {str(f.graph_id): f.fact_set_digest for f in facts},
                "graph_fact_counts": {str(f.graph_id): len(f.rows) for f in facts},
                "next_cursor": str(index + 1) if index + 1 < len(pages) else None,
            }

        def close(self):
            pass

    report["repository_repeated_reads"] = {}
    for shared in (False, True):
        client = LocalPages()
        repo = None
        start = time.perf_counter()
        indexes = []
        for _ in range(3):
            if repo is None or not shared:
                repo = CloudflareFactRepository(
                    client=client, workspace_id=workspace, snapshot_sequence=63
                )
            store, _ = repo.store_for(
                "project.sessions", {"project_name": export["project_name"]}
            )
            indexes.append(set(store.graph_ids))
        assert all(ids == indexes[0] for ids in indexes) and len(indexes[0]) == len(
            facts
        )
        report["repository_repeated_reads"][
            "shared_repository" if shared else "separate_repositories"
        ] = {
            "calls": client.calls,
            "fact_rows_delivered": client.rows,
            "row_array_bytes_delivered": client.bytes,
            "seconds": round(time.perf_counter() - start, 6),
            "requests": 3,
        }

    # Count table mutations through SQLite itself, excluding index accounting.
    before = sql.total_changes
    sql.execute(
        "INSERT INTO fact_rows SELECT graph_id,kind,fact_id,parent_id,order_index,row_hash,payload,63,NULL FROM staged_fact_items"
    )
    published = sql.total_changes - before
    before = sql.total_changes
    sql.execute("DELETE FROM staged_fact_items")
    deleted = sql.total_changes - before
    report["write_amplification"] = {
        "staged_fact_inserts": sum(counts.values()),
        "published_fact_inserts": published,
        "staged_fact_deletes": deleted,
        "logical_fact_mutations": sum(counts.values()) + published + deleted,
        "excluded": "batch/generation/receipt metadata, retries, canary, and all index maintenance; not actual Cloudflare writes",
        "candidate_index_extra_entries": sum(counts.values()),
        "candidate_index_note": "One additional full index entry per staged fact; creation, subsequent inserts and deletes add work",
    }
    # Reproduce the pre-fix page SQL and current indexed traversal on these facts.
    old_source = subprocess.check_output(
        ["git", "show", "aec71b0:cloudflare/control-plane/src/facts.ts"],
        cwd=ROOT,
        text=True,
    )
    old_page = re.search(r"`(WITH ordered AS \(.*?)`", old_source, re.DOTALL).group(1)
    graph_ids = sorted(str(f.graph_id) for f in facts)
    kinds = sorted(counts)

    def paginate(indexed):
        after = None
        returned = []
        page_count = 0
        matching_candidates = 0
        while True:
            page = []
            more = False
            if indexed:
                size = 2
                for graph_id in graph_ids:
                    if after and graph_id < after[0]:
                        continue
                    for kind in kinds:
                        if after and graph_id == after[0] and kind < after[1]:
                            continue
                        after_id = (
                            after[2]
                            if after and (graph_id, kind) == after[:2]
                            else None
                        )
                        query = "SELECT graph_id,kind,fact_id,payload,length(CAST(payload AS BLOB)) FROM fact_rows WHERE graph_id=? AND kind=? AND valid_from_sequence<=? AND (valid_to_sequence IS NULL OR valid_to_sequence>=?)"
                        params = [graph_id, kind, 63, 63]
                        if after_id is not None:
                            query += " AND fact_id > ?"
                            params.append(after_id)
                        query += " ORDER BY fact_id LIMIT ?"
                        params.append(2048 - len(page) + 1)
                        for row in sql.execute(query, params):
                            matching_candidates += 1
                            width = row[4] + bool(page)
                            if len(page) == 2048 or size + width > 1024 * 1024:
                                more = True
                                break
                            page.append(row[:4])
                            size += width
                        if more:
                            break
                    if more:
                        break
            else:
                where = "WHERE graph_id IN (SELECT value FROM json_each(?)) AND valid_from_sequence <= ? AND (valid_to_sequence IS NULL OR valid_to_sequence >= ?)"
                params = [json.dumps(graph_ids), 63, 63]
                if after:
                    where += " AND (graph_id > ? OR (graph_id = ? AND (kind > ? OR (kind = ? AND fact_id > ?))))"
                    params += [after[0], after[0], after[1], after[1], after[2]]
                page = sql.execute(
                    old_page.replace("${where}", where),
                    params + [2048, 1024 * 1024 - 1],
                ).fetchall()
                if page:
                    last = page[-1]
                    more = bool(
                        sql.execute(
                            "SELECT 1 FROM fact_rows "
                            + where
                            + " AND (graph_id > ? OR (graph_id = ? AND (kind > ? OR (kind = ? AND fact_id > ?)))) LIMIT 1",
                            params + [last[0], last[0], last[1], last[1], last[2]],
                        ).fetchone()
                    )
            page_count += 1
            returned.append(page)
            if not more:
                return returned, page_count, matching_candidates
            after = page[-1][:3]

    prior, old_metrics = measure(lambda: paginate(False))
    current, new_metrics = measure(lambda: paginate(True))
    assert prior[0] == current[0]
    assert sum(len(page) for page in current[0]) == sum(counts.values())
    report["pagination"] = {
        "before": old_metrics,
        "after": new_metrics,
        "pages": current[1],
        "new_matching_candidates": current[2],
        "exact_page_equality": True,
        "excludes": "graph discovery, HTTP, cursor signing; uses only present fact kinds",
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
