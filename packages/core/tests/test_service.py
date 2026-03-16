from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from coding_trajectory.enrichment import build_default_trajectory_enrichment
from coding_trajectory.ingestion.models import (
    Session,
    Step,
    StepTextItem,
    StepToolItem,
    Trajectory,
    TrajectoryEdge,
    TrajectorySummary,
    Vendor,
)
from coding_trajectory.query import DocumentStore
from coding_trajectory.rpc_server import _dispatch
from coding_trajectory.service import (
    serialize_enriched_trajectory,
    serialize_session_detail,
    serialize_step_detail,
    serialize_trajectory_detail,
)


def test_serialize_trajectory_detail_exposes_only_canonical_trajectory_fields() -> None:
    trajectory_id = uuid4()
    session_a_id = uuid4()
    session_b_id = uuid4()
    turn_id = uuid4()
    step_id = uuid4()
    event_id = uuid4()
    timestamp = datetime(2026, 3, 13, 10, 0, tzinfo=timezone.utc)

    session_a = Session(
        session_id=session_a_id,
        trajectory_id=trajectory_id,
        vendor=Vendor.CODEX_CLI,
        started_at=timestamp,
        ended_at=timestamp,
    )
    session_b = Session(
        session_id=session_b_id,
        trajectory_id=trajectory_id,
        vendor=Vendor.CODEX_CLI,
        started_at=timestamp,
        ended_at=timestamp,
        parent_session_id=session_a_id,
    )
    trajectory = Trajectory(
        trajectory_id=trajectory_id,
        project_identifier="coding-trajectory",
        summary=TrajectorySummary(
            root_session_id=session_a_id,
            started_at=timestamp,
            ended_at=timestamp,
            session_count=2,
            turn_count=1,
            vendors=[Vendor.CODEX_CLI],
        ),
        edges=[
            TrajectoryEdge(
                type="spawned_subagent",
                source_session_id=session_a_id,
                target_session_id=session_b_id,
                source_turn_id=turn_id,
                source_step_id=step_id,
                source_event_id=event_id,
                provenance="observed",
                confidence="high",
                evidence_event_ids=[event_id],
                metadata={"tool_name": "spawn_agent"},
            )
        ],
        sessions=[session_a, session_b],
    )

    payload = serialize_trajectory_detail(trajectory)

    assert set(payload) == {"trajectory_id", "project_identifier", "summary", "session_ids", "edges"}
    assert payload["session_ids"] == [str(session_a_id), str(session_b_id)]
    assert payload["edges"][0]["source_session_id"] == str(session_a_id)
    assert payload["edges"][0]["target_session_id"] == str(session_b_id)
    assert payload["edges"][0]["source_turn_id"] == str(turn_id)
    assert payload["edges"][0]["source_step_id"] == str(step_id)
    assert payload["edges"][0]["source_event_id"] == str(event_id)
    assert payload["edges"][0]["provenance"] == "observed"


def test_serialize_session_detail_exposes_only_canonical_session_fields() -> None:
    session_id = uuid4()
    trajectory_id = uuid4()
    timestamp = datetime(2026, 3, 13, 10, 0, tzinfo=timezone.utc)

    session = Session(
        session_id=session_id,
        trajectory_id=trajectory_id,
        vendor=Vendor.CODEX_CLI,
        started_at=timestamp,
        ended_at=timestamp,
    )

    payload = serialize_session_detail(session)

    assert set(payload) == {
        "session_id",
        "trajectory_id",
        "vendor",
        "started_at",
        "ended_at",
        "turn_ids",
        "event_ids",
    }
    assert payload["session_id"] == str(session_id)
    assert payload["trajectory_id"] == str(trajectory_id)


def test_serialize_step_detail_keeps_tool_items_direct() -> None:
    session_id = uuid4()
    turn_id = uuid4()
    event_id = uuid4()
    timestamp = datetime(2026, 3, 13, 10, 0, tzinfo=timezone.utc)
    step = Step(
        session_id=session_id,
        turn_id=turn_id,
        sequence=0,
        timestamp=timestamp,
        vendor=Vendor.CLAUDE_CODE,
        items=[
            StepTextItem(text="Planning"),
            StepToolItem(
                tool_name="plan_mode",
                output={"status": "ok"},
                event_ids=[event_id],
            ),
        ],
        event_ids=[event_id],
    )

    payload = serialize_step_detail(step)

    assert payload["items"][0] == {"kind": "text", "text": "Planning", "event_ids": []}
    assert payload["items"][1]["tool_name"] == "plan_mode"
    assert payload["items"][1] == {
        "kind": "tool",
        "tool_name": "plan_mode",
        "output": {"status": "ok"},
        "status": "requested",
        "event_ids": [str(event_id)],
    }


def test_serialize_enriched_trajectory_uses_sidecar_wrapper_shape() -> None:
    trajectory_id = uuid4()
    session_id = uuid4()
    timestamp = datetime(2026, 3, 13, 10, 0, tzinfo=timezone.utc)
    trajectory = Trajectory(
        trajectory_id=trajectory_id,
        project_identifier="coding-trajectory",
        summary=TrajectorySummary(
            root_session_id=session_id,
            started_at=timestamp,
            ended_at=timestamp,
            session_count=1,
            turn_count=0,
            vendors=[Vendor.CODEX_CLI],
        ),
        sessions=[
            Session(
                session_id=session_id,
                trajectory_id=trajectory_id,
                vendor=Vendor.CODEX_CLI,
                started_at=timestamp,
                ended_at=timestamp,
            )
        ],
    )

    enriched = build_default_trajectory_enrichment(trajectory)
    payload = serialize_enriched_trajectory(enriched)

    assert payload["trajectory_id"] == str(trajectory_id)
    assert set(payload["enrichment"]) == {"structural", "derived", "agent_specific", "plugins", "notes"}
    assert payload["enrichment"]["structural"]["session_tree"]["root_session_id"] == str(session_id)


def test_rpc_dispatch_trajectory_enrich_returns_enriched_sidecar() -> None:
    trajectory_id = uuid4()
    session_id = uuid4()
    timestamp = datetime(2026, 3, 13, 10, 0, tzinfo=timezone.utc)
    trajectory = Trajectory(
        trajectory_id=trajectory_id,
        project_identifier="coding-trajectory",
        summary=TrajectorySummary(
            root_session_id=session_id,
            started_at=timestamp,
            ended_at=timestamp,
            session_count=1,
            turn_count=0,
            vendors=[Vendor.CODEX_CLI],
        ),
        sessions=[
            Session(
                session_id=session_id,
                trajectory_id=trajectory_id,
                vendor=Vendor.CODEX_CLI,
                started_at=timestamp,
                ended_at=timestamp,
            )
        ],
    )
    store = DocumentStore.from_trajectories([trajectory])

    result = _dispatch(
        "trajectory.enrich",
        {"trajectory_id": str(trajectory_id)},
        store=store,
        global_scope=False,
        current_dir=Path.cwd(),
        discovery_note="",
    )

    assert result["trajectory_id"] == str(trajectory_id)
    assert result["enrichment"]["derived"]["multi_agent_mode"] == "single_session"
    assert result["enrichment"]["structural"]["session_tree"]["roots"] == [str(session_id)]
