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
from coding_trajectory.ingestion.models import (
    SessionEdge,
    SessionGraph,
    SessionGraphSummary,
)
from coding_trajectory.query import DocumentStore
from coding_trajectory.service.handlers import dispatch
from coding_trajectory.service.store import IndexCache, _resolve_session_graph

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


def _collision_fixture(
    fixture: dict[str, Any],
) -> tuple[list[PublishedFactSet], list[SessionGraph], UUID, UUID]:
    first_facts = fixture["synthetic_fact_set"](
        seed="collision-first", project="collision-project"
    )
    parent_facts = fixture["synthetic_fact_set"](
        seed="collision-parent", project="collision-project"
    )
    child_facts = fixture["synthetic_fact_set"](
        seed="collision-child", project="collision-project"
    )
    colliding_facts = fixture["synthetic_fact_set"](
        seed="collision-colliding-child", project="collision-project"
    )
    turn_collision_facts = fixture["synthetic_fact_set"](
        seed="collision-turn-entrypoint", project="collision-project"
    )
    source_index = FactIndex.from_fact_sets(
        [
            first_facts,
            parent_facts,
            child_facts,
            colliding_facts,
            turn_collision_facts,
        ]
    )
    first = session_graph_from_fact_index(source_index, first_facts.graph_id)
    parent = session_graph_from_fact_index(source_index, parent_facts.graph_id)
    child = session_graph_from_fact_index(source_index, child_facts.graph_id)
    colliding = session_graph_from_fact_index(source_index, colliding_facts.graph_id)
    turn_collision = session_graph_from_fact_index(
        source_index, turn_collision_facts.graph_id
    )

    shared_session_id = first.root_session_id
    shared_turn_id = first.sessions[0].turns[0].turn_id
    child_session = colliding.sessions[0]
    colliding_turns = [
        turn.model_copy(
            update={
                "turn_id": shared_turn_id,
                "session_id": shared_session_id,
                "items": [
                    item.model_copy(
                        update={
                            "session_id": shared_session_id,
                            "turn_id": shared_turn_id,
                        }
                    )
                    for item in turn.items
                ],
            }
        )
        for turn in child_session.turns
    ]
    colliding_events = [
        event.model_copy(update={"session_id": shared_session_id})
        for event in child_session.events
    ]
    colliding_child = child_session.model_copy(
        update={
            "session_id": shared_session_id,
            "parent_session_id": parent.root_session_id,
            "events": colliding_events,
            "turns": colliding_turns,
        }
    )
    parent_session = parent.sessions[0]
    normal_child = child.sessions[0].model_copy(
        update={"parent_session_id": parent.root_session_id}
    )
    origin_turn = parent_session.turns[0]
    origin_item = origin_turn.items[0]
    second = SessionGraph(
        root_session_id=parent.root_session_id,
        project_identifier="collision-project",
        summary=SessionGraphSummary(
            root_session_id=parent.root_session_id,
            started_at=parent.summary.started_at,
            ended_at=child.summary.ended_at,
            session_count=3,
            turn_count=3,
            vendors=sorted(
                {
                    parent_session.vendor,
                    normal_child.vendor,
                    colliding_child.vendor,
                },
                key=lambda vendor: vendor.value,
            ),
        ),
        sessions=[parent_session, normal_child, colliding_child],
        edges=[
            SessionEdge(
                type="spawned_subagent",
                source_session_id=parent.root_session_id,
                target_session_id=normal_child.session_id,
                source_turn_id=origin_turn.turn_id,
                source_item_id=origin_item.item_id,
                source_event_id=origin_item.event_ids[0],
                evidence_event_ids=[origin_item.event_ids[0]],
                provenance="observed",
                confidence="high",
            ),
            SessionEdge(
                type="spawned_subagent",
                source_session_id=parent.root_session_id,
                target_session_id=shared_session_id,
                source_turn_id=origin_turn.turn_id,
                source_item_id=origin_item.item_id,
                source_event_id=origin_item.event_ids[0],
                evidence_event_ids=[origin_item.event_ids[0]],
                provenance="observed",
                confidence="high",
            ),
        ],
    )
    second_facts = fixture["build_published_fact_set"](second)
    isolated_second = session_graph_from_fact_index(
        FactIndex.from_fact_sets([second_facts]), second_facts.graph_id
    )
    turn_collision_session = turn_collision.sessions[0]
    turn_collision_turns = [
        turn.model_copy(
            update={
                "turn_id": normal_child.session_id,
                "items": [
                    item.model_copy(update={"turn_id": normal_child.session_id})
                    for item in turn.items
                ],
            }
        )
        for turn in turn_collision_session.turns
    ]
    turn_collision_graph = turn_collision.model_copy(
        update={
            "sessions": [
                turn_collision_session.model_copy(
                    update={"turns": turn_collision_turns}
                )
            ]
        }
    )
    third_facts = fixture["build_published_fact_set"](turn_collision_graph)
    isolated_third = session_graph_from_fact_index(
        FactIndex.from_fact_sets([third_facts]), third_facts.graph_id
    )
    return (
        [first_facts, second_facts, third_facts],
        [first, isolated_second, isolated_third],
        shared_turn_id,
        normal_child.session_id,
    )


def _qualify_collisions(fixture: dict[str, Any]) -> None:
    fact_sets, source_graphs, shared_turn_id, child_session_id = _collision_fixture(
        fixture
    )
    indexed = FactIndex.from_fact_sets(fact_sets)
    ordered_graphs = sorted(source_graphs, key=lambda graph: str(graph.root_session_id))
    baseline = DocumentStore.from_session_graphs(ordered_graphs)

    for source_graph in source_graphs:
        materialized = session_graph_from_fact_index(
            indexed, source_graph.root_session_id
        )
        assert materialized == source_graph
        assert all(
            row.graph_id == source_graph.root_session_id
            for row in indexed.rows_for_parent(
                source_graph.root_session_id, source_graph.root_session_id
            )
        )

    first_root = source_graphs[0].root_session_id
    second_root = source_graphs[1].root_session_id
    assert indexed.graph_id_for_entrypoint(first_root) == first_root
    assert indexed.graph_id_for_entrypoint(second_root) == second_root
    assert indexed.graph_id_for_entrypoint(child_session_id) == second_root
    assert _resolve_session_graph(
        baseline, str(child_session_id)
    ).root_session_id == indexed.graph_id_for_entrypoint(child_session_id)
    assert indexed.graph_id_for_entrypoint(shared_turn_id) == (
        _resolve_session_graph(baseline, str(shared_turn_id)).root_session_id
    )
    for entrypoint in (first_root, second_root, child_session_id):
        params = {"session_id": str(entrypoint)}
        assert _dispatch("session.stats", params, indexed) == _dispatch(
            "session.stats", params, baseline
        )


def main() -> None:
    fixture = runpy.run_path(
        str(ROOT / "scripts" / "qualify-cloudflare-control-plane.py")
    )
    _qualify_collisions(fixture)
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
            assert row in indexed.rows_for_id(row.graph_id, row.fact_id)
            if row.parent_id is not None:
                assert row in indexed.rows_for_parent(row.graph_id, row.parent_id)
            assert indexed.payload(row.graph_id, row.kind, row.fact_id) == row.payload

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
