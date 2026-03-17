from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from coding_trajectory.discovery import DiscoveryResult, DiscoverySource
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
from coding_trajectory.analysis.views import build_step_details, build_trajectory_overview, build_trajectory_scan, _SCAN_STRING_PREVIEW_LEN
from coding_trajectory.query import DocumentStore
from coding_trajectory.rpc_server import _dispatch, _resolve_store, IndexCache


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
                input={"prompt": "analyze schema"},
                output={"result": "done"},
                status=ToolStatus.COMPLETED,
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


def test_build_trajectory_overview_returns_navigation_tree() -> None:
    store, ids = _fixture_store()
    trajectory = store.get_trajectory(ids["trajectory_id"])

    result = build_trajectory_overview(trajectory, store=store)

    assert result["trajectory_id"] == str(ids["trajectory_id"])
    sessions = result["sessions"]
    main_session = sessions[0]
    assert main_session["session_id"] == str(ids["session_id"])
    assert main_session["connection"] == {"role": "main"}
    assert main_session["turns"][0]["user_request"] == {"text": "analyze the schema design", "type": "message"}
    steps_node = main_session["turns"][0]["steps"]
    assert str(ids["step_id"]) in steps_node["step_ids"]


def test_build_trajectory_overview_child_session_has_connection_context() -> None:
    store, ids = _fixture_store()
    trajectory = store.get_trajectory(ids["trajectory_id"])

    result = build_trajectory_overview(trajectory, store=store)

    sessions_by_id = {s["session_id"]: s for s in result["sessions"]}
    child = sessions_by_id[str(ids["child_session_id"])]
    assert child["connection"]["relationship"] == "spawned_subagent"
    assert child["connection"]["parent_session_id"] == str(ids["session_id"])


def test_build_step_details_plan_subagent() -> None:
    store, ids = _fixture_store()
    step = store.get_step(ids["step_id"])

    result = build_step_details(step, store=store)

    assert result["step_id"] == str(ids["step_id"])
    assert result["type"] == "plan_subagent"
    assert result["operations"] == ["spawn", "collect_result"]
    assert result["shape"]["agent_input"] == {"prompt": "analyze schema"}
    assert result["shape"]["agent_output"] == {"result": "done"}
    assert result["shape"]["agent_session_id"] == str(ids["child_session_id"])
    assert str(ids["tool_event_id"]) in result["event_ids"]


def test_rpc_dispatch_trajectory_overview_returns_navigation_tree() -> None:
    store, ids = _fixture_store()

    result = _dispatch(
        "trajectory.overview",
        {"trajectory_id": str(ids["trajectory_id"])},
        store=store,
        global_scope=False,
        current_dir=Path.cwd(),
        discovery_note="",
        cache=IndexCache(),
    )

    assert result["trajectory_id"] == str(ids["trajectory_id"])
    assert result["sessions"][0]["session_id"] == str(ids["session_id"])
    assert result["sessions"][0]["connection"] == {"role": "main"}


def test_rpc_dispatch_step_details_returns_evidence() -> None:
    store, ids = _fixture_store()

    result = _dispatch(
        "step.details",
        {"trajectory_id": str(ids["trajectory_id"]), "step_id": str(ids["step_id"])},
        store=store,
        global_scope=False,
        current_dir=Path.cwd(),
        discovery_note="",
        cache=IndexCache(),
    )

    assert result["step_id"] == str(ids["step_id"])
    assert result["type"] == "plan_subagent"
    assert result["shape"]["agent_session_id"] == str(ids["child_session_id"])


def test_resolve_store_full_discovery_maps_source_to_own_trajectory(monkeypatch) -> None:
    store_a, ids_a = _fixture_store()
    store_b, ids_b = _fixture_store()
    trajectory_a = store_a.get_trajectory(ids_a["trajectory_id"])
    trajectory_b = store_b.get_trajectory(ids_b["trajectory_id"])
    combined_store = DocumentStore.from_trajectories([trajectory_a, trajectory_b])
    source_a = Path("/tmp/trajectory-a.jsonl")
    source_b = Path("/tmp/trajectory-b.jsonl")

    discovery = DiscoveryResult(
        store=combined_store,
        sources=[
            DiscoverySource(vendor=Vendor.CLAUDE_CODE, path=source_a, trajectory_id=trajectory_a.trajectory_id),
            DiscoverySource(vendor=Vendor.CLAUDE_CODE, path=source_b, trajectory_id=trajectory_b.trajectory_id),
        ],
    )

    def _fake_discover_store(*, current_dir: Path, global_scope: bool = False) -> DiscoveryResult:
        return discovery

    monkeypatch.setattr("coding_trajectory.rpc_server.discover_store", _fake_discover_store)

    cache = IndexCache()
    _resolve_store(
        {"trajectory_id": str(trajectory_a.trajectory_id)},
        log_file=None,
        global_scope=False,
        current_dir=Path.cwd(),
        cache=cache,
    )

    assert cache.paths_for_trajectory(str(trajectory_a.trajectory_id)) == [str(source_a)]
    assert cache.paths_for_trajectory(str(trajectory_b.trajectory_id)) == [str(source_b)]


def _fixture_store_with_long_output() -> tuple[DocumentStore, dict[str, object]]:
    """Fixture with a tool_call step whose tool_output contains a long string."""
    trajectory_id = uuid4()
    session_id = uuid4()
    turn_id = uuid4()
    step_id = uuid4()
    prompt_event_id = uuid4()
    tool_event_id = uuid4()
    ts = datetime(2026, 3, 10, 8, 20, tzinfo=timezone.utc)

    long_content = "x" * (_SCAN_STRING_PREVIEW_LEN + 100)

    prompt_event = Event(
        event_id=prompt_event_id,
        session_id=session_id,
        timestamp=ts,
        type=EventType.USER_PROMPT_SUBMITTED,
        vendor_source=Vendor.CLAUDE_CODE,
        actor="user",
        payload={"text": "read a file"},
    )
    tool_event = Event(
        event_id=tool_event_id,
        session_id=session_id,
        timestamp=ts,
        type=EventType.TOOL_CALL_REQUESTED,
        vendor_source=Vendor.CLAUDE_CODE,
        actor="assistant",
        payload={"tool_name": "Read", "tool_call_id": "call-read"},
    )
    step = Step(
        step_id=step_id,
        session_id=session_id,
        turn_id=turn_id,
        sequence=0,
        timestamp=ts,
        vendor=Vendor.CLAUDE_CODE,
        items=[
            StepToolItem(
                tool_name="Read",
                tool_call_id="call-read",
                input={"file_path": "/src/foo.py"},
                output={"content": long_content},
                status=ToolStatus.COMPLETED,
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
    trajectory = Trajectory(
        trajectory_id=trajectory_id,
        project_identifier="legion",
        summary=TrajectorySummary(
            root_session_id=session_id,
            started_at=ts,
            ended_at=ts,
            session_count=1,
            turn_count=1,
            vendors=[Vendor.CLAUDE_CODE],
        ),
        edges=[],
        sessions=[session],
    )
    store = DocumentStore.from_trajectories([trajectory])
    return store, {
        "trajectory_id": trajectory_id,
        "step_id": step_id,
        "long_content": long_content,
    }


def test_trajectory_scan_truncates_long_output_strings() -> None:
    store, ids = _fixture_store_with_long_output()
    trajectory = store.get_trajectory(ids["trajectory_id"])

    result = build_trajectory_scan(trajectory, store=store, step_type="tool_call")

    assert len(result["matches"]) == 1
    content = result["matches"][0]["shape"]["tool_output"]["content"]
    assert len(content) == _SCAN_STRING_PREVIEW_LEN + 1  # truncated + ellipsis char
    assert content.endswith("…")


def test_step_details_returns_full_output_without_truncation() -> None:
    store, ids = _fixture_store_with_long_output()
    step = store.get_step(ids["step_id"])

    result = build_step_details(step, store=store)

    content = result["shape"]["tool_output"]["content"]
    assert content == ids["long_content"]


def test_removed_methods_return_method_not_found() -> None:
    store, ids = _fixture_store()
    removed_methods = [
        ("session.overview", {"session_id": str(ids["session_id"])}),
        ("turn.overview", {"turn_id": str(ids["turn_id"])}),
        ("step.overview", {"step_id": str(ids["step_id"])}),
        ("event.get", {"event_id": str(ids["prompt_event_id"])}),
        ("trajectory.enrich", {"trajectory_id": str(ids["trajectory_id"])}),
    ]

    for method, params in removed_methods:
        try:
            _dispatch(
                method,
                params,
                store=store,
                global_scope=False,
                current_dir=Path.cwd(),
                discovery_note="",
                cache=IndexCache(),
            )
            raise AssertionError(f"{method} should raise KeyError")
        except KeyError:
            pass  # expected — unknown method
