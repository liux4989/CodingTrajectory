from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from coding_trajectory.ingestion.models import (
    Event,
    EventType,
    Session,
    Step,
    StepToolItem,
    ToolStatus,
    Trajectory,
    TrajectoryEdge,
    TrajectorySummary,
    Turn,
    Vendor,
    VendorExtensions,
    CodexExtensions,
)
from coding_trajectory.service import serialize_session_detail, serialize_trajectory_detail


def test_serialize_trajectory_detail_includes_session_refs_and_edge_provenance() -> None:
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
        extensions=VendorExtensions(codex=CodexExtensions(collaboration_mode="default", agent_nickname="root")),
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

    assert payload["multi_agent_mode"] == "cross_session"
    assert payload["operations"] == []
    assert payload["sections"] == []
    assert payload["inference_notes"] == []
    assert payload["session_refs"][0]["role"] == "primary"
    assert payload["session_refs"][0]["agent_name"] == "root"
    assert payload["session_refs"][1]["role"] == "subagent"
    assert payload["edges"][0]["source_session_id"] == str(session_a_id)
    assert payload["edges"][0]["target_session_id"] == str(session_b_id)
    assert payload["edges"][0]["source_turn_id"] == str(turn_id)
    assert payload["edges"][0]["source_step_id"] == str(step_id)
    assert payload["edges"][0]["source_event_id"] == str(event_id)
    assert payload["edges"][0]["provenance"] == "observed"
    assert payload["edges"][0]["metadata"] == {"tool_name": "spawn_agent"}


def test_serialize_session_detail_includes_turn_timeline() -> None:
    session_id = uuid4()
    turn_id = uuid4()
    step_id = uuid4()
    user_event_id = uuid4()
    timestamp = datetime(2026, 3, 13, 10, 0, tzinfo=timezone.utc)

    session = Session(
        session_id=session_id,
        trajectory_id=uuid4(),
        vendor=Vendor.CODEX_CLI,
        started_at=timestamp,
        ended_at=timestamp,
        events=[
            Event(
                event_id=user_event_id,
                session_id=session_id,
                timestamp=timestamp,
                type=EventType.USER_PROMPT_SUBMITTED,
                vendor_source=Vendor.CODEX_CLI,
                actor="user",
                payload={"text": "spawn a worker"},
            )
        ],
        turns=[
            Turn(
                turn_id=turn_id,
                session_id=session_id,
                sequence=0,
                started_at=timestamp,
                ended_at=timestamp,
                user_request_event_id=user_event_id,
                steps=[
                    Step(
                        step_id=step_id,
                        session_id=session_id,
                        turn_id=turn_id,
                        sequence=0,
                        timestamp=timestamp,
                        vendor=Vendor.CODEX_CLI,
                        items=[StepToolItem(tool_name="spawn_agent", status=ToolStatus.REQUESTED)],
                    )
                ],
            )
        ],
    )

    payload = serialize_session_detail(session)

    assert payload["timeline"][0]["kind"] == "turn"
    assert payload["timeline"][0]["turn_id"] == str(turn_id)
    assert payload["timeline"][0]["user_request_event_id"] == str(user_event_id)
    assert payload["timeline"][0]["step_ids"] == [str(step_id)]
