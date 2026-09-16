#!/usr/bin/env python3
"""Offline qualification for direct published-fact projection and inversion."""

from __future__ import annotations

import copy
import runpy
from pathlib import Path
from uuid import UUID

from coding_trajectory.control_plane.fact_repository import (
    CloudflareFactRepository,
    LocalPublishedFactRepository,
)
from coding_trajectory.control_plane.published_facts import (
    FactIndex,
    FactRow,
    PublishedFactSet,
    compute_fact_set_digest,
    compute_row_hash,
    session_graph_from_fact_index,
)
from coding_trajectory.query import DocumentStore
from coding_trajectory.service.handlers import dispatch
from coding_trajectory.service.store import IndexCache
from pydantic import TypeAdapter, ValidationError

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    qualification = runpy.run_path(
        str(ROOT / "scripts" / "qualify-cloudflare-control-plane.py")
    )
    build = qualification["build_published_fact_set"]
    facts = qualification["synthetic_edge_fact_set"](
        seed="direct-published-facts", project="synthetic"
    )
    expected_kinds = {
        "graph",
        "session",
        "turn",
        "item",
        "event",
        "edge",
        "request",
        "model",
        "runtime",
        "measurement",
        "output_evidence",
    }
    assert set(facts.kind_counts) == expected_kinds
    indexed = FactIndex.from_fact_sets([facts])
    replay = build(session_graph_from_fact_index(indexed, facts.graph_id))
    assert replay.model_dump_json(exclude_none=True) == facts.model_dump_json(
        exclude_none=True
    )

    raw = facts.model_dump(mode="json", exclude_none=True)
    adapter = TypeAdapter(FactRow)

    def rejected(mutate) -> None:
        candidate = copy.deepcopy(raw)
        mutate(candidate)
        for row in candidate["rows"]:
            view = {key: value for key, value in row.items() if key != "row_hash"}
            row["row_hash"] = compute_row_hash(view)
        typed = [adapter.validate_python(row) for row in candidate["rows"]]
        candidate["fact_set_digest"] = compute_fact_set_digest(facts.graph_id, typed)
        try:
            PublishedFactSet.model_validate(candidate)
        except (ValidationError, ValueError):
            return
        raise AssertionError("adversarial fact mutation was accepted")

    rejected(
        lambda value: next(
            row for row in value["rows"] if row["kind"] == "item"
        ).__setitem__("parent_id", value["graph_id"])
    )

    def change_order(value) -> None:
        row = next(row for row in value["rows"] if row["kind"] == "item")
        row["payload"]["sequence"] += 1

    rejected(change_order)
    rejected(
        lambda value: next(row for row in value["rows"] if row["kind"] == "graph")[
            "payload"
        ]["summary"].__setitem__("project", "data:text/plain," + "A" * 200)
    )

    def oversize(value) -> None:
        row = next(row for row in value["rows"] if row["kind"] == "measurement")
        row["payload"]["context_sources"] = [
            {
                "timestamp": "2026-09-15T12:00:00Z",
                "key": f"k{index}",
                "label": "x" * 500,
                "chars": 1,
                "tokens": 1,
            }
            for index in range(1200)
        ]

    rejected(oversize)
    secret = "synthetic-bearer-secret-value"
    secret_facts = qualification["synthetic_fact_set"](
        seed="secret",
        project="synthetic",
        command_description=(
            f"curl -H 'Authorization: Bearer {secret}' https://example.test"
        ),
    )
    assert secret not in secret_facts.model_dump_json(exclude_none=True)

    canonical = DocumentStore.from_session_graphs(
        [session_graph_from_fact_index(indexed, facts.graph_id)]
    )

    class Client:
        def call(self, method, _params):
            assert method == "ct_fact_read"
            return {
                "workspace_id": str(UUID(int=1)),
                "snapshot_sequence": 7,
                "rows": [
                    row.model_dump(mode="json", exclude_none=True) for row in facts.rows
                ],
                "graph_digests": {str(facts.graph_id): facts.fact_set_digest},
                "graph_fact_counts": {str(facts.graph_id): len(facts.rows)},
                "next_cursor": None,
            }

        def close(self):
            return None

    local = LocalPublishedFactRepository(
        global_scope=True,
        current_dir=ROOT,
        cache=IndexCache(),
        resolve=lambda *_args, **_kwargs: (canonical, "facts"),
    )
    remote = CloudflareFactRepository(
        client=Client(), workspace_id=UUID(int=1), snapshot_sequence=7
    )
    params = {"root_session_id": str(facts.graph_id)}
    local_store, _ = local.store_for("graph.overview", params)
    remote_store, _ = remote.store_for("graph.overview", params)
    arguments = {
        "global_scope": True,
        "current_dir": ROOT,
        "discovery_note": "facts",
        "cache": IndexCache(),
    }
    assert dispatch(
        "graph.overview", params, store=local_store, **arguments
    ) == dispatch("graph.overview", params, store=remote_store, **arguments)
    print(
        "direct published facts: PASS "
        f"({len(facts.rows)} rows, {len(expected_kinds)} kinds, digest "
        f"{facts.fact_set_digest})"
    )


if __name__ == "__main__":
    main()
