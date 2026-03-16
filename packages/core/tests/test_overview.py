from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from coding_trajectory.ingestion.models import (
    Event,
    EventType,
    Session,
    Step,
    StepToolItem,
    StepTextItem,
    ToolStatus,
    Trajectory,
    TrajectoryEdge,
    TrajectorySummary,
    Turn,
    Vendor,
)
from coding_trajectory.overview import build_session_overview, build_step_overview, build_trajectory_overview
from coding_trajectory.query import DocumentStore
from coding_trajectory.rpc_server import _dispatch, _handle_request


def _fixture_store() -> tuple[DocumentStore, dict[str, object]]:
    trajectory_id = uuid4()
    session_id = uuid4()
    child_session_id = uuid4()
    turn_id = uuid4()
    step_id = uuid4()
    prompt_event_id = uuid4()
    tool_event_id = uuid4()
    ts = datetime(2026, 3, 10, 8, 20, tzinfo=timezone.utc)

    prompt_event = Event(
        event_id=prompt_event_id,
        session_id=session_id,
        timestamp=ts,
        type=EventType.USER_PROMPT_SUBMITTED,
        vendor_source=Vendor.CLAUDE_CODE,
        actor="user",
        payload={"text": "analyze the schema design"},
    )
    tool_event = Event(
        event_id=tool_event_id,
        session_id=session_id,
        timestamp=ts,
        type=EventType.TOOL_CALL_REQUESTED,
        vendor_source=Vendor.CLAUDE_CODE,
        actor="assistant",
        payload={"tool_name": "spawn_agent", "tool_call_id": "call-1"},
    )
    step = Step(
        step_id=step_id,
        session_id=session_id,
        turn_id=turn_id,
        sequence=0,
        timestamp=ts,
        vendor=Vendor.CLAUDE_CODE,
        items=[
            StepTextItem(text="Reading files", event_ids=[tool_event_id]),
            StepToolItem(
                tool_name="spawn_agent",
                tool_call_id="call-1",
                status=ToolStatus.REQUESTED,
                event_ids=[tool_event_id],
            ),
        ],
        event_ids=[prompt_event_id, tool_event_id],
    )
    turn = Turn(
        turn_id=turn_id,
        session_id=session_id,
        sequence=0,
        started_at=ts,
        ended_at=ts,
        user_request_event_id=prompt_event_id,
        event_ids=[prompt_event_id, tool_event_id],
        steps=[step],
    )
    session = Session(
        session_id=session_id,
        trajectory_id=trajectory_id,
        vendor=Vendor.CLAUDE_CODE,
        agent_name="main",
        started_at=ts,
        ended_at=ts,
        events=[prompt_event, tool_event],
        turns=[turn],
    )
    child_session = Session(
        session_id=child_session_id,
        trajectory_id=trajectory_id,
        vendor=Vendor.CLAUDE_CODE,
        agent_name="child",
        started_at=ts,
        ended_at=ts,
        parent_session_id=session_id,
    )
    trajectory = Trajectory(
        trajectory_id=trajectory_id,
        project_identifier="legion",
        summary=TrajectorySummary(
            root_session_id=session_id,
            started_at=ts,
            ended_at=ts,
            session_count=2,
            turn_count=1,
            vendors=[Vendor.CLAUDE_CODE],
        ),
        edges=[
            TrajectoryEdge(
                type="spawned_subagent",
                source_session_id=session_id,
                target_session_id=child_session_id,
                source_turn_id=turn_id,
                source_step_id=step_id,
                source_event_id=tool_event_id,
                provenance="observed",
                confidence="high",
                evidence_event_ids=[tool_event_id],
                metadata={"tool_name": "spawn_agent"},
            )
        ],
        sessions=[session, child_session],
    )
    store = DocumentStore.from_trajectories([trajectory])
    return store, {
        "trajectory_id": trajectory_id,
        "session_id": session_id,
        "child_session_id": child_session_id,
        "turn_id": turn_id,
        "step_id": step_id,
        "prompt_event_id": prompt_event_id,
        "tool_event_id": tool_event_id,
    }


def test_build_session_overview_returns_canonical_session_and_turn_overviews() -> None:
    store, ids = _fixture_store()

    overview = build_session_overview(store.get_session(ids["session_id"]), store=store)

    assert overview["session"]["session_id"] == str(ids["session_id"])
    assert overview["operations"][0]["kind"] == "spawned_subagent"
    assert overview["operations"][0]["related_session_ids"] == [str(ids["child_session_id"])]
    assert overview["turns"][0]["turn_id"] == str(ids["turn_id"])
    assert overview["turns"][0]["user_request"] == "analyze the schema design"
    assert overview["turns"][0]["steps"][0]["event_refs"] == [
        {
            "event_id": str(ids["prompt_event_id"]),
            "type": "user.prompt.submitted",
            "category": "user_interaction",
        },
        {
            "event_id": str(ids["tool_event_id"]),
            "type": "tool.call.requested",
            "category": "tool_call",
        },
    ]
    assert overview["turns"][0]["steps"][0]["operations"][0]["kind"] == "spawn_subsession"
    assert overview["turns"][0]["steps"][0]["operations"][0]["target_session_ids"] == [str(ids["child_session_id"])]


def test_build_step_overview_includes_navigation_ids_and_operations() -> None:
    store, ids = _fixture_store()

    overview = build_step_overview(store.get_step(ids["step_id"]), store=store)

    assert overview["step"]["step_id"] == str(ids["step_id"])
    assert overview["step"]["session_id"] == str(ids["session_id"])
    assert overview["step"]["turn_id"] == str(ids["turn_id"])
    assert overview["step"]["operations"][0]["kind"] == "spawn_subsession"
    assert overview["step"]["operations"][0]["event_ids"] == [str(ids["tool_event_id"])]


def test_rpc_dispatch_trajectory_overview_returns_tree_and_context() -> None:
    store, ids = _fixture_store()

    result = _dispatch(
        "trajectory.overview",
        {"trajectory_id": str(ids["trajectory_id"])},
        store=store,
        global_scope=False,
        current_dir=Path.cwd(),
        discovery_note="",
    )

    assert result["trajectory"]["trajectory_id"] == str(ids["trajectory_id"])
    assert result["trajectory"]["context"]["multi_agent_mode"] == "cross_session"
    assert result["tree"][0]["session"]["session_id"] == str(ids["session_id"])
    assert result["tree"][0]["children"][0]["session"]["session_id"] != str(ids["session_id"])


def test_non_atomic_get_methods_are_removed() -> None:
    store, ids = _fixture_store()
    requests = [
        ("trajectory.get", {"trajectory_id": str(ids["trajectory_id"])}),
        ("session.get", {"session_id": str(ids["session_id"])}),
        ("turn.get", {"turn_id": str(ids["turn_id"])}),
        ("step.get", {"step_id": str(ids["step_id"])}),
    ]

    for method, params in requests:
        response = _handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            store=store,
            global_scope=False,
            current_dir=Path.cwd(),
            discovery_note="",
        )
        assert response["error"]["code"] == -32601
        assert response["error"]["message"] == f"unknown method: {method}"
