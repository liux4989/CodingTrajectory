#!/usr/bin/env python3
"""Build deterministic, synthetic fixtures from existing qualification tooling."""

import argparse
import runpy
from collections import Counter
from pathlib import Path

from coding_trajectory.control_plane.published_facts import PublishedFactSet
from coding_trajectory.ingestion.common import canonical_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--graphs", type=int, default=1)
    parser.add_argument("--sessions", type=int, default=1)
    parser.add_argument("--graph-bytes", type=int)
    args = parser.parse_args()
    if not 1 <= args.graphs <= 64 or not 1 <= args.sessions <= 128:
        parser.error("require 1..64 graphs and 1..128 sessions per graph")
    qualification = runpy.run_path(
        str(Path(__file__).with_name("qualify-cloudflare-control-plane.py"))
    )
    project = "Synthetic-Free-Plan-Benchmark"
    facts = []
    for graph_index in range(args.graphs):
        if args.graph_bytes:
            fact = qualification["exact_graph_fact_set"](
                seed=f"free-plan:{graph_index}",
                project=project,
                target_bytes=args.graph_bytes,
            )
        else:
            rows = []
            for session_index in range(args.sessions):
                source = qualification["synthetic_fact_set"](
                    seed=f"free-plan:{graph_index}:{session_index}", project=project
                )
                if session_index == 0:
                    graph_id = str(source.graph_id)
                for row in qualification["raw_rows"](source):
                    if row["kind"] == "graph" and session_index:
                        continue
                    if (
                        row["kind"] in {"session", "model", "edge"}
                        and row.get("parent_id") == row["graph_id"]
                    ):
                        row["parent_id"] = graph_id
                    row["graph_id"] = graph_id
                    rows.append(row)
            graph = next(row for row in rows if row["kind"] == "graph")
            graph["payload"]["summary"].update(
                session_count=args.sessions,
                turn_count=args.sessions,
                item_count=args.sessions * 2,
            )
            rows.sort(key=lambda row: (row["kind"], row["fact_id"]))
            digest = qualification["recalculate"](rows)
            fact = PublishedFactSet.model_validate(
                qualification["raw_fact_set"](
                    rows,
                    digest=digest,
                    kind_counts=dict(Counter(r["kind"] for r in rows)),
                )
            )
        facts.append(fact.model_dump(mode="json", exclude_none=True))
    args.output.write_text(
        canonical_json({"project_name": project, "synthetic": True, "fact_sets": facts})
    )
    print(f"synthetic: {len(facts)} graphs, {sum(len(f['rows']) for f in facts)} rows")


if __name__ == "__main__":
    main()
