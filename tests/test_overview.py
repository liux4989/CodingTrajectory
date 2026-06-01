from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from coding_trajectory.analysis.projections import build_step_details, build_session_graph_narrative, build_session_graph_overview
from coding_trajectory.ingestion.models import (
    Session,
    Step,
    StepTextItem,
    StepToolItem,
    SessionGraph,
    SessionEdge,
    Turn,
    Vendor,
)


def _ts(second: int) -> datetime:
    return datetime(2026, 1, 1, 0, 0, second, tzinfo=UTC)


def _session_graph_with_text_turns(count: int) -> tuple[SessionGraph, list[Turn]]:
    root_session_id = uuid4()
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
        vendor=Vendor.CLAUDE_CODE,
        started_at=_ts(0),
        turns=turns,
    )
    return SessionGraph(root_session_id=root_session_id, sessions=[session]), turns


def test_overview_activity_flattens_interleaved_tool_and_text_items() -> None:
    root_session_id = uuid4()
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
        vendor=Vendor.CLAUDE_CODE,
        started_at=_ts(0),
        turns=[turn],
    )
    session_graph = SessionGraph(root_session_id=root_session_id, sessions=[session])

    result = build_session_graph_overview(session_graph)

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
        },
        {"type": "assistant_response", "text": "The config looks good."},
    ]


def test_overview_activity_keeps_full_assistant_text() -> None:
    root_session_id = uuid4()
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
        vendor=Vendor.CLAUDE_CODE,
        started_at=_ts(0),
        turns=[turn],
    )
    session_graph = SessionGraph(root_session_id=root_session_id, sessions=[session])

    result = build_session_graph_overview(session_graph)

    assert result["sessions"][0]["turns"][0]["activity"] == [
        {"type": "assistant_response", "text": long_text}
    ]


def test_overview_can_limit_to_latest_visible_turns() -> None:
    session_graph, turns = _session_graph_with_text_turns(4)

    result = build_session_graph_overview(
        session_graph,
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
    session_graph, turns = _session_graph_with_text_turns(4)

    result = build_session_graph_narrative(
        session_graph,
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
    session_graph, turns = _session_graph_with_text_turns(4)

    result = build_session_graph_overview(
        session_graph,
        drop_turns=2,
    )

    overview_turns = result["sessions"][0]["turns"]
    assert [turn["turn_id"] for turn in overview_turns] == [
        str(turns[0].turn_id),
        str(turns[1].turn_id),
    ]


def test_narrative_applies_drop_before_limit() -> None:
    session_graph, turns = _session_graph_with_text_turns(4)

    result = build_session_graph_narrative(
        session_graph,
        num_turns=3,
        drop_turns=1,
    )

    narrative_turns = result["sessions"][0]["turns"]
    assert [turn["turn_id"] for turn in narrative_turns] == [
        str(turns[0].turn_id),
        str(turns[1].turn_id),
        str(turns[2].turn_id),
    ]


def test_fork_relationship_is_visible_from_parent_and_child_sessions() -> None:
    root_session_id = uuid4()
    parent_session_id = uuid4()
    child_session_id = uuid4()
    parent = Session(
        session_id=parent_session_id,
        vendor=Vendor.CODEX_CLI,
        started_at=_ts(0),
    )
    child = Session(
        session_id=child_session_id,
        vendor=Vendor.CODEX_CLI,
        started_at=_ts(1),
        parent_session_id=parent_session_id,
    )
    session_graph = SessionGraph(
        root_session_id=root_session_id,
        sessions=[parent, child],
        edges=[
            SessionEdge(
                type="forked_from",
                source_session_id=parent_session_id,
                target_session_id=child_session_id,
            )
        ],
    )

    overview = build_session_graph_overview(session_graph)
    narrative = build_session_graph_narrative(session_graph)

    for result in (overview, narrative):
        parent_node, child_node = result["sessions"]
        assert parent_node["relationship"] == {
            "role": "main",
            "forked_session_ids": [str(child_session_id)],
        }
        assert child_node["relationship"] == {
            "relationship": "forked_from",
            "parent_session_id": str(parent_session_id),
        }


def test_step_details_resolves_spawned_session_from_session_graph_edges() -> None:
    root_session_id = uuid4()
    parent_session_id = uuid4()
    child_session_id = uuid4()
    turn_id = uuid4()

    step = Step(
        session_id=parent_session_id,
        turn_id=turn_id,
        sequence=0,
        timestamp=_ts(1),
        vendor=Vendor.CLAUDE_CODE,
        items=[
            StepToolItem(
                tool_name="Task",
                input={"subagent_type": "worker", "description": "Inspect code"},
                output="done",
            )
        ],
    )
    turn = Turn(
        session_id=parent_session_id,
        turn_id=turn_id,
        sequence=0,
        started_at=_ts(0),
        steps=[step],
    )
    parent = Session(
        session_id=parent_session_id,
        vendor=Vendor.CLAUDE_CODE,
        started_at=_ts(0),
        turns=[turn],
    )
    child = Session(
        session_id=child_session_id,
        vendor=Vendor.CLAUDE_CODE,
        started_at=_ts(2),
    )
    session_graph = SessionGraph(
        root_session_id=root_session_id,
        sessions=[parent, child],
        edges=[
            SessionEdge(
                type="spawned_subagent",
                source_session_id=parent_session_id,
                target_session_id=child_session_id,
                source_turn_id=turn_id,
                source_step_id=step.step_id,
            )
        ],
    )

    result = build_step_details(step, session_graph=session_graph)

    assert result["type"] == "plan_subagent"
    assert result["shape"]["agent_session_id"] == str(child_session_id)
