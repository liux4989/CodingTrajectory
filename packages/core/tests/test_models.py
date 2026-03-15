from __future__ import annotations

from coding_trajectory.ingestion.models import Trajectory, Session, Turn, Step, Event, EventType, Vendor
from datetime import datetime, timezone
from uuid import uuid4


def test_trajectory_supports_project_identifier() -> None:
    trajectory = Trajectory(project_identifier="claude-code-wrapper")

    assert trajectory.project_identifier == "claude-code-wrapper"


def test_trajectory_defaults_include_graph_fields() -> None:
    trajectory = Trajectory(project_identifier="coding-trajectory")

    assert trajectory.summary is None
    assert trajectory.edges == []
    assert trajectory.sessions == []


def test_step_model_has_required_fields() -> None:
    session_id = uuid4()
    turn_id = uuid4()
    ts = datetime(2026, 3, 13, 10, 0, tzinfo=timezone.utc)

    step = Step(
        session_id=session_id,
        turn_id=turn_id,
        sequence=0,
        timestamp=ts,
        vendor=Vendor.CLAUDE_CODE,
    )

    assert step.step_id is not None
    assert step.text is None
    assert step.vendor_data == {}
    assert step.event_ids == []


def test_turn_model_new_schema() -> None:
    session_id = uuid4()
    ts = datetime(2026, 3, 13, 10, 0, tzinfo=timezone.utc)

    turn = Turn(
        session_id=session_id,
        sequence=0,
        started_at=ts,
    )

    assert turn.turn_id is not None
    assert turn.user_request_event_id is None
    assert turn.steps == []


def test_event_model_simplified() -> None:
    session_id = uuid4()
    ts = datetime(2026, 3, 13, 10, 0, tzinfo=timezone.utc)

    event = Event(
        session_id=session_id,
        timestamp=ts,
        type=EventType.USER_PROMPT_SUBMITTED,
        vendor_source=Vendor.CLAUDE_CODE,
    )

    assert event.event_id is not None
    assert event.payload == {}
    assert event.actor is None
