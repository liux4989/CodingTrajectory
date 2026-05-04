from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from coding_trajectory.analysis.projections import build_trajectory_narrative, build_trajectory_overview
from coding_trajectory.ingestion.models import Session, Step, StepTextItem, StepToolItem, Trajectory, Turn, Vendor
from coding_trajectory.query import DocumentStore


def _ts(second: int) -> datetime:
    return datetime(2026, 1, 1, 0, 0, second, tzinfo=UTC)


def _trajectory_with_text_turns(count: int) -> tuple[Trajectory, list[Turn]]:
    trajectory_id = uuid4()
    session_id = uuid4()

    turns = []
    for sequence in range(count):
        turn_id = uuid4()
        step = Step(
            session_id=session_id,
            turn_id=turn_id,
            sequence=0,
            timestamp=_ts(sequence + 1),
            vendor=Vendor.CLAUDE_CODE,
            items=[StepTextItem(text=f"assistant {sequence}")],
        )
        turns.append(
            Turn(
                session_id=session_id,
                turn_id=turn_id,
                sequence=sequence,
                started_at=_ts(sequence),
                steps=[step],
            )
        )
    session = Session(
        session_id=session_id,
        trajectory_id=trajectory_id,
        vendor=Vendor.CLAUDE_CODE,
        started_at=_ts(0),
        turns=turns,
    )
    return Trajectory(trajectory_id=trajectory_id, sessions=[session]), turns


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


def test_overview_can_limit_to_latest_visible_turns() -> None:
    trajectory, turns = _trajectory_with_text_turns(4)

    result = build_trajectory_overview(
        trajectory,
        store=DocumentStore.from_trajectories([trajectory]),
        num_turns=2,
    )

    overview_turns = result["sessions"][0]["turns"]
    assert [turn["turn_id"] for turn in overview_turns] == [
        str(turns[2].turn_id),
        str(turns[3].turn_id),
    ]
    assert overview_turns[0]["activity"] == [{"type": "assistant_response", "text": "assistant 2"}]
    assert overview_turns[1]["activity"] == [{"type": "assistant_response", "text": "assistant 3"}]


def test_narrative_can_limit_to_latest_visible_turns() -> None:
    trajectory, turns = _trajectory_with_text_turns(4)

    result = build_trajectory_narrative(
        trajectory,
        store=DocumentStore.from_trajectories([trajectory]),
        num_turns=2,
    )

    narrative_turns = result["sessions"][0]["turns"]
    assert [turn["turn_id"] for turn in narrative_turns] == [
        str(turns[2].turn_id),
        str(turns[3].turn_id),
    ]
    assert narrative_turns[0]["assistant_responses"] == ["assistant 2"]
    assert narrative_turns[1]["assistant_responses"] == ["assistant 3"]


def test_overview_can_drop_latest_turns_like_thread_rollback() -> None:
    trajectory, turns = _trajectory_with_text_turns(4)

    result = build_trajectory_overview(
        trajectory,
        store=DocumentStore.from_trajectories([trajectory]),
        drop_turns=2,
    )

    overview_turns = result["sessions"][0]["turns"]
    assert [turn["turn_id"] for turn in overview_turns] == [
        str(turns[0].turn_id),
        str(turns[1].turn_id),
    ]


def test_narrative_applies_drop_before_limit() -> None:
    trajectory, turns = _trajectory_with_text_turns(4)

    result = build_trajectory_narrative(
        trajectory,
        store=DocumentStore.from_trajectories([trajectory]),
        num_turns=3,
        drop_turns=1,
    )

    narrative_turns = result["sessions"][0]["turns"]
    assert [turn["turn_id"] for turn in narrative_turns] == [
        str(turns[0].turn_id),
        str(turns[1].turn_id),
        str(turns[2].turn_id),
    ]
