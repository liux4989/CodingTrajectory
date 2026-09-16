#!/usr/bin/env python3
"""Qualify typed current and cumulative usage through Chronicle facts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from coding_trajectory.control_plane.fact_protocol import FactReadResponse
from coding_trajectory.control_plane.fact_repository import (
    _fact_sets_from_rows,
    published_fact_set_for_store,
)
from coding_trajectory.control_plane.published_facts import FactIndex
from coding_trajectory.ingestion.models import (
    ContextUsageObservation,
    Event,
    EventType,
    Session,
    SessionGraph,
    Turn,
    Vendor,
)
from coding_trajectory.query import DocumentStore


def main() -> None:
    started = datetime(2026, 9, 15, 12, tzinfo=UTC)
    session_id = uuid4()
    turn_id = uuid4()
    current_event_id = uuid4()
    missing_event_id = uuid4()
    current_usage = {
        "input_tokens": 101,
        "cached_input_tokens": 11,
        "cache_creation_input_tokens": 3,
        "output_tokens": 17,
        "reasoning_output_tokens": 5,
        "total_tokens": 118,
        "uncached_input_tokens": 87,
        "cost_usd": 0.0125,
    }
    cumulative_usage = {
        "inputTokens": 1001,
        "cachedInputTokens": 111,
        "cacheCreationInputTokens": 33,
        "outputTokens": 177,
        "reasoningOutputTokens": 55,
        "totalTokens": 1178,
        "uncachedInputTokens": 857,
        "costUsd": 0.125,
        "raw_provider_payload": 999999,
    }
    normalized_cumulative = {
        "input_tokens": 1001,
        "cached_input_tokens": 111,
        "cache_creation_input_tokens": 33,
        "output_tokens": 177,
        "reasoning_output_tokens": 55,
        "total_tokens": 1178,
        "uncached_input_tokens": 857,
        "cost_usd": 0.125,
    }
    graph = SessionGraph(
        root_session_id=session_id,
        project_identifier="chronicle-usage-qualification",
        sessions=[
            Session(
                session_id=session_id,
                vendor=Vendor.AMP,
                started_at=started,
                ended_at=started + timedelta(seconds=2),
                events=[
                    Event(
                        event_id=event_id,
                        session_id=session_id,
                        timestamp=started + timedelta(seconds=offset),
                        type=EventType.LLM_RESPONSE,
                        vendor_source=Vendor.AMP,
                    )
                    for event_id, offset in (
                        (current_event_id, 1),
                        (missing_event_id, 2),
                    )
                ],
                turns=[
                    Turn(
                        turn_id=turn_id,
                        session_id=session_id,
                        sequence=0,
                        started_at=started,
                        ended_at=started + timedelta(seconds=2),
                        event_ids=[current_event_id, missing_event_id],
                    )
                ],
                context_usage=[
                    ContextUsageObservation(
                        source_event_id=current_event_id,
                        timestamp=started + timedelta(seconds=1),
                        source="usage",
                        used_input_tokens=101,
                        usage=current_usage,
                        cumulative_usage=cumulative_usage,
                    ),
                    ContextUsageObservation(
                        source_event_id=missing_event_id,
                        timestamp=started + timedelta(seconds=2),
                        source="usage",
                        used_input_tokens=7,
                        usage={"input_tokens": 7, "total_tokens": 9},
                        cumulative_usage=None,
                    ),
                ],
            )
        ],
    )
    source_store = DocumentStore.from_session_graphs([graph])
    fact_sets = published_fact_set_for_store(source_store)
    assert len(fact_sets) == 1
    request_rows = {
        row.fact_id: row for row in fact_sets[0].rows if row.kind == "request"
    }
    current_row = request_rows[current_event_id]
    missing_row = request_rows[missing_event_id]
    assert current_row.payload.usage.model_dump(mode="json", exclude_none=True) == {
        **current_usage,
        "cost_usd": "0.0125",
    }
    assert current_row.payload.cumulative_usage.model_dump(
        mode="json", exclude_none=True
    ) == {**normalized_cumulative, "cost_usd": "0.125"}
    assert missing_row.payload.cumulative_usage is None

    local_store = FactIndex.from_fact_sets(fact_sets)
    assert_usage(local_store, current_usage, normalized_cumulative)

    response = FactReadResponse(
        workspace_id=UUID("00000000-0000-0000-0000-000000000001"),
        snapshot_sequence=1,
        rows=fact_sets[0].rows,
        graph_digests={str(graph.root_session_id): fact_sets[0].fact_set_digest},
        graph_fact_counts={str(graph.root_session_id): len(fact_sets[0].rows)},
    )
    remote_response = FactReadResponse.model_validate_json(response.model_dump_json())
    remote_sets = _fact_sets_from_rows(
        remote_response.rows,
        digests=remote_response.graph_digests,
        counts=remote_response.graph_fact_counts,
    )
    remote_store = FactIndex.from_fact_sets(remote_sets)
    assert_usage(remote_store, current_usage, normalized_cumulative)
    print(
        "Chronicle cumulative usage round trip: PASS (local facts and remote response)"
    )


def assert_usage(
    store: FactIndex,
    current_usage: dict[str, int | float],
    cumulative_usage: dict[str, int | float],
) -> None:
    session_id = store.graph_ids[0]
    from coding_trajectory.control_plane.published_facts import (
        session_graph_from_fact_index,
    )

    observations = (
        session_graph_from_fact_index(store, session_id).sessions[0].context_usage
    )
    assert len(observations) == 2
    by_event_id = {
        observation.source_event_id: observation for observation in observations
    }
    current = next(
        observation
        for observation in by_event_id.values()
        if observation.cumulative_usage is not None
    )
    missing = next(
        observation
        for observation in by_event_id.values()
        if observation.cumulative_usage is None
    )
    assert current.usage == current_usage
    assert current.cumulative_usage == cumulative_usage
    assert current.usage != current.cumulative_usage
    assert missing.cumulative_usage is None


if __name__ == "__main__":
    main()
