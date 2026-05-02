from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from coding_trajectory.analysis.views import build_trajectory_overview
from coding_trajectory.ingestion.models import Session, Step, StepTextItem, StepToolItem, Trajectory, Turn, Vendor
from coding_trajectory.query import DocumentStore


def _ts(second: int) -> datetime:
    return datetime(2026, 1, 1, 0, 0, second, tzinfo=UTC)


def test_overview_activity_flattens_interleaved_tool_and_text_items() -> None:
    trajectory_id = uuid4()
    session_id = uuid4()
    turn_id = uuid4()

    step = Step(
        session_id=session_id,
        turn_id=turn_id,
        sequence=0,
        timestamp=_ts(1),
        vendor=Vendor.CLAUDE_CODE,
        items=[
            StepTextItem(text="I’ll inspect the config."),
            StepToolItem(
                tool_name="Read",
                input={"file_path": "/tmp/config.py", "offset": 0, "limit": 1},
                output="setting = true\n",
            ),
            StepTextItem(text="The config looks good."),
        ],
    )
    turn = Turn(
        session_id=session_id,
        turn_id=turn_id,
        sequence=0,
        started_at=_ts(0),
        steps=[step],
    )
    session = Session(
        session_id=session_id,
        trajectory_id=trajectory_id,
        vendor=Vendor.CLAUDE_CODE,
        started_at=_ts(0),
        turns=[turn],
    )
    trajectory = Trajectory(trajectory_id=trajectory_id, sessions=[session])

    result = build_trajectory_overview(trajectory, store=DocumentStore.from_trajectories([trajectory]))

    turn = result["sessions"][0]["turns"][0]

    assert "work_summary" not in turn
    assert result["sessions"][0]["session_id"] == str(session_id)
    assert result["sessions"][0]["status"] == "completed"
    assert turn["status"] == "completed"
    assert turn["activity"] == [
        {"type": "assistant_response", "text": "I’ll inspect the config."},
        {
            "type": "tool_call",
            "name": "ReadFile",
            "description": "/tmp/config.py",
            "summary": "Read lines 1-1",
        },
        {"type": "assistant_response", "text": "The config looks good."},
    ]


def test_overview_activity_keeps_full_assistant_text() -> None:
    trajectory_id = uuid4()
    session_id = uuid4()
    turn_id = uuid4()
    long_text = "x" * 350

    step = Step(
        session_id=session_id,
        turn_id=turn_id,
        sequence=0,
        timestamp=_ts(1),
        vendor=Vendor.CLAUDE_CODE,
        items=[StepTextItem(text=long_text)],
    )
    turn = Turn(
        session_id=session_id,
        turn_id=turn_id,
        sequence=0,
        started_at=_ts(0),
        steps=[step],
    )
    session = Session(
        session_id=session_id,
        trajectory_id=trajectory_id,
        vendor=Vendor.CLAUDE_CODE,
        started_at=_ts(0),
        turns=[turn],
    )
    trajectory = Trajectory(trajectory_id=trajectory_id, sessions=[session])

    result = build_trajectory_overview(trajectory, store=DocumentStore.from_trajectories([trajectory]))

    assert result["sessions"][0]["turns"][0]["activity"] == [
        {"type": "assistant_response", "text": long_text}
    ]
