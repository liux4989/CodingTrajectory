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
        {"text": "I’ll inspect the config."},
        {"tool": "ReadFile", "path": "/tmp/config.py"},
        {"text": "The config looks good."},
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
        {"text": long_text}
    ]


def test_narrative_keeps_full_assistant_text() -> None:
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

    result = build_session_graph_narrative(session_graph)

    assert result["sessions"][0]["turns"][0]["assistant_responses"] == [long_text]


def test_overview_activity_keeps_busy_assistant_items() -> None:
    root_session_id = uuid4()
    session_id = uuid4()
    turn_id = uuid4()

    step = Step(
        session_id=session_id,
        turn_id=turn_id,
        sequence=0,
        timestamp=_ts(1),
        vendor=Vendor.CLAUDE_CODE,
        items=[StepTextItem(text=f"assistant {idx}") for idx in range(30)],
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

    activity = result["sessions"][0]["turns"][0]["activity"]
    assert len(activity) == 30
    assert activity[0] == {"text": "assistant 0"}
    assert activity[-1] == {"text": "assistant 29"}


def test_overview_groups_repeated_consecutive_tool_calls_without_losing_descriptions() -> None:
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
            StepTextItem(text="I’ll inspect related files."),
            StepToolItem(
                tool_name="Read",
                input={"file_path": "/tmp/a.py"},
                output="a",
            ),
            StepToolItem(
                tool_name="Read",
                input={"file_path": "/tmp/b.py"},
                output="b",
            ),
            StepToolItem(
                tool_name="Read",
                input={"file_path": "/tmp/c.py"},
                output="c",
            ),
            StepTextItem(text="Now I have the context."),
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

    assert result["sessions"][0]["turns"][0]["activity"] == [
        {"text": "I’ll inspect related files."},
        {
            "tool": "ReadFile",
            "count": 3,
            "paths": ["/tmp/a.py", "/tmp/b.py", "/tmp/c.py"],
        },
        {"text": "Now I have the context."},
    ]


def test_overview_dedupes_repeated_grouped_read_paths_with_counts() -> None:
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
            StepToolItem(
                tool_name="Bash",
                input={"command": "sed -n '1,20p' /tmp/a.py"},
                output="a1",
            ),
            StepToolItem(
                tool_name="Bash",
                input={"command": "sed -n '20,40p' /tmp/a.py"},
                output="a2",
            ),
            StepToolItem(
                tool_name="Bash",
                input={"command": "sed -n '1,20p' /tmp/b.py"},
                output="b",
            ),
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

    assert result["sessions"][0]["turns"][0]["activity"] == [
        {
            "tool": "ReadFile",
            "count": 3,
            "paths": ["/tmp/a.py", "/tmp/b.py"],
            "path_counts": {"/tmp/a.py": 2},
        },
    ]


def test_overview_does_not_group_repeated_mutating_tool_calls() -> None:
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
            StepToolItem(
                tool_name="Edit",
                input={"file_path": "/tmp/a.py"},
                output="edited a",
            ),
            StepToolItem(
                tool_name="Edit",
                input={"file_path": "/tmp/b.py"},
                output="edited b",
            ),
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

    assert result["sessions"][0]["turns"][0]["activity"] == [
        {"tool": "EditFile", "path": "/tmp/a.py"},
        {"tool": "EditFile", "path": "/tmp/b.py"},
    ]


def test_overview_does_not_group_repeated_shell_commands() -> None:
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
            StepToolItem(
                tool_name="Bash",
                input={"command": "uv run pytest tests/test_overview.py"},
                output="passed",
            ),
            StepToolItem(
                tool_name="Bash",
                input={"command": "uv run pytest tests/test_service.py"},
                output="passed",
            ),
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

    assert result["sessions"][0]["turns"][0]["activity"] == [
        {"tool": "RunCommand", "cmd": "uv run pytest tests/test_overview.py"},
        {"tool": "RunCommand", "cmd": "uv run pytest tests/test_service.py"},
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
    assert overview_turns[0]["activity"] == [{"text": "assistant 2"}]
    assert overview_turns[1]["activity"] == [{"text": "assistant 3"}]


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
