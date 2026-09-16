#!/usr/bin/env python3
"""Offline asymmetric qualification for indexed historical fact reads."""

from __future__ import annotations

import runpy
from pathlib import Path
from typing import Any
from uuid import UUID

from coding_trajectory.contracts import service_contract
from coding_trajectory.control_plane.fact_repository import (
    CloudflareFactRepository,
    LocalPublishedFactRepository,
)
from coding_trajectory.control_plane.published_facts import (
    FactIndex,
    PublishedFactSet,
    session_graph_from_fact_index,
)
from coding_trajectory.query import DocumentStore
from coding_trajectory.service.handlers import dispatch
from coding_trajectory.service.store import IndexCache

ROOT = Path(__file__).resolve().parents[1]
METHODS = (
    "project.sessions",
    "session.overview",
    "session.summary",
    "session.search",
    "session.tree",
    "graph.overview",
    "session.stats",
    "graph.stats",
    "session.usage",
    "graph.usage",
    "session.model_usage",
    "session.request_usage",
    "session.tool_usage",
    "session.events",
    "session.items",
)


class FactClient:
    def __init__(self, fact_sets: list[PublishedFactSet]) -> None:
        self.fact_sets = fact_sets

    def call(self, method: str, _params: dict[str, Any]) -> dict[str, Any]:
        assert method == "ct_fact_read"
        return {
            "workspace_id": str(UUID(int=1)),
            "snapshot_sequence": 9,
            "rows": [
                row.model_dump(mode="json", exclude_none=True)
                for fact_set in self.fact_sets
                for row in fact_set.rows
            ],
            "graph_digests": {
                str(fact_set.graph_id): fact_set.fact_set_digest
                for fact_set in self.fact_sets
            },
            "graph_fact_counts": {
                str(fact_set.graph_id): len(fact_set.rows)
                for fact_set in self.fact_sets
            },
            "next_cursor": None,
        }

    def close(self) -> None:
        return None


def _params(method: str, *, project: str, root: str) -> dict[str, Any]:
    if method == "project.sessions":
        return {"project_name": project}
    if method == "graph.overview":
        return {"root_session_id": root, "limit": 100}
    if method.startswith("graph."):
        return {"root_session_id": root}
    if method == "session.search":
        return {"session_id": root, "query": "command", "limit": 100}
    if method in {"session.items", "session.events", "session.overview"}:
        return {"session_id": root, "limit": 100}
    return {"session_id": root}


def _dispatch(method: str, params: dict[str, Any], store: Any) -> Any:
    return dispatch(
        method,
        service_contract(method).validate_request(params),
        store=store,
        global_scope=True,
        current_dir=ROOT,
        discovery_note="synthetic facts",
        cache=IndexCache(),
    )


def main() -> None:
    fixture = runpy.run_path(
        str(ROOT / "scripts" / "qualify-cloudflare-control-plane.py")
    )
    project = "fact-index-asymmetric"
    primary = fixture["synthetic_edge_fact_set"](seed="index-primary", project=project)
    secondary = fixture["synthetic_fact_set"](
        seed="index-secondary", project="other-project"
    )
    fact_sets = [secondary, primary]
    indexed = FactIndex.from_fact_sets(fact_sets)
    assert indexed.graph_ids == tuple(sorted(indexed.graph_ids, key=str))
    for fact_set in fact_sets:
        assert all(
            row.graph_id == fact_set.graph_id
            for row in indexed.rows_for_graph(fact_set.graph_id)
        )
        for row in fact_set.rows:
            assert row in indexed.rows_for_id(row.fact_id)
            if row.parent_id is not None:
                assert row in indexed.rows_for_parent(row.parent_id)
            assert indexed.payload(row.kind, row.fact_id) == row.payload

    canonical = DocumentStore.from_session_graphs(
        [
            session_graph_from_fact_index(indexed, graph_id)
            for graph_id in indexed.graph_ids
        ]
    )
    local = LocalPublishedFactRepository(
        global_scope=True,
        current_dir=ROOT,
        cache=IndexCache(),
        resolve=lambda *_args, **_kwargs: (canonical, "synthetic facts"),
    )
    remote = CloudflareFactRepository(
        client=FactClient(fact_sets),
        workspace_id=UUID(int=1),
        snapshot_sequence=9,
    )
    root = str(primary.graph_id)
    checked = 0
    try:
        for method in METHODS:
            params = _params(method, project=project, root=root)
            baseline = _dispatch(method, params, canonical)
            optimized = _dispatch(method, params, indexed)
            local_index, _ = local.store_for(method, params)
            remote_index, _ = remote.store_for(method, params)
            assert optimized == baseline
            assert _dispatch(method, params, local_index) == baseline
            assert _dispatch(method, params, remote_index) == baseline
            checked += 1

        for kind, key in (("item", "items"), ("event", "events")):
            method = f"session.{key}"
            full_params = {"session_id": root, "limit": 100}
            full = _dispatch(method, full_params, indexed)
            first = _dispatch(method, {**full_params, "limit": 1}, indexed)
            combined = list(first[key])
            cursor = first["next_cursor"]
            while cursor is not None:
                page = _dispatch(
                    method, {**full_params, "limit": 1, "cursor": cursor}, indexed
                )
                combined.extend(page[key])
                cursor = page["next_cursor"]
            assert combined == full[key]
            selected_id = full[key][-1][f"{kind}_id"]
            filtered = _dispatch(
                method,
                {
                    **full_params,
                    f"{kind}_ids": [selected_id],
                },
                indexed,
            )
            assert [row[f"{kind}_id"] for row in filtered[key]] == [selected_id]
            checked += 2
    finally:
        local.close()
        remote.close()

    print(
        "fact index: PASS "
        f"({checked} asymmetric parity, paging, filtering, order, and index checks)"
    )


if __name__ == "__main__":
    main()
