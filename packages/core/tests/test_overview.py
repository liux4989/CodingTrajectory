from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from coding_trajectory.discovery import DiscoveryResult, DiscoverySource
from coding_trajectory.ingestion.models import (
    ClaudeCodeExtensions,
    Event,
    EventType,
    Session,
    Step,
    TeamMemberState,
    TeamTaskState,
    TeamTurnState,
    StepToolItem,
    StepTextItem,
    ToolStatus,
    Trajectory,
    TrajectoryEdge,
    TrajectorySummary,
    Turn,
    Vendor,
    VendorExtensions,
)
from coding_trajectory.analysis.views import build_step_details, build_trajectory_overview, build_event_scan, _EVENT_SCAN_PAYLOAD_PREVIEW_LEN
from coding_trajectory.query import DocumentStore
from coding_trajectory.service import dispatch, resolve_store, IndexCache


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
    assert set(main_session) == {"turns"}
    assert main_session["turns"][0]["user_request"] == {
        "content": "analyze the schema design",
        "type": "message",
        "source": "human_user",
    }
    steps_node = main_session["turns"][0]["work_summary"]
    assert str(ids["step_id"]) in steps_node["step_ids"]


def test_build_trajectory_overview_collapses_spawned_subagent_sessions() -> None:
    store, ids = _fixture_store()
    trajectory = store.get_trajectory(ids["trajectory_id"])

    result = build_trajectory_overview(trajectory, store=store)

    assert len(result["sessions"]) == 1


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

    result = dispatch(
        "trajectory.overview",
        {"trajectory_id": str(ids["trajectory_id"])},
        store=store,
        global_scope=False,
        current_dir=Path.cwd(),
        discovery_note="",
        cache=IndexCache(),
    )

    assert result["trajectory_id"] == str(ids["trajectory_id"])
    assert set(result["sessions"][0]) == {"turns"}


def test_rpc_dispatch_step_details_returns_evidence() -> None:
    store, ids = _fixture_store()

    result = dispatch(
        "step.details",
        {"trajectory_id": str(ids["trajectory_id"]), "step_ids": [str(ids["step_id"])]},
        store=store,
        global_scope=False,
        current_dir=Path.cwd(),
        discovery_note="",
        cache=IndexCache(),
    )

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["step_id"] == str(ids["step_id"])
    assert result[0]["type"] == "plan_subagent"
    assert result[0]["shape"]["agent_session_id"] == str(ids["child_session_id"])


def test_rpc_dispatch_step_details_batch_returns_array() -> None:
    store, ids = _fixture_store()
    store2, ids2 = _fixture_store_with_long_output()
    # Merge stores
    combined = DocumentStore.from_trajectories(
        list(store.trajectories.values()) + list(store2.trajectories.values())
    )

    result = dispatch(
        "step.details",
        {"step_ids": [str(ids["step_id"]), str(ids2["step_id"])]},
        store=combined,
        global_scope=False,
        current_dir=Path.cwd(),
        discovery_note="",
        cache=IndexCache(),
    )

    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["step_id"] == str(ids["step_id"])
    assert result[1]["step_id"] == str(ids2["step_id"])


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

    monkeypatch.setattr("coding_trajectory.service.discover_store", _fake_discover_store)

    cache = IndexCache()
    resolve_store(
        {"trajectory_id": str(trajectory_a.trajectory_id)},
        log_file=None,
        global_scope=False,
        current_dir=Path.cwd(),
        cache=cache,
    )

    assert cache.paths_for_trajectory(str(trajectory_a.trajectory_id)) == [str(source_a)]
    assert cache.paths_for_trajectory(str(trajectory_b.trajectory_id)) == [str(source_b)]


def test_resolve_store_targeted_reassembles_multi_file_trajectory(tmp_path) -> None:
    project_dir = tmp_path / "project"
    root_session_id = "05e58bcb-fe0a-4324-b311-2568aa901c9c"
    main_path = project_dir / f"{root_session_id}.jsonl"
    subagent_path = project_dir / root_session_id / "subagents" / "agent-123.jsonl"
    subagent_path.parent.mkdir(parents=True)

    cwd = str(project_dir)
    main_records = [
        {
            "type": "user",
            "sessionId": root_session_id,
            "timestamp": "2026-03-13T10:00:00Z",
            "cwd": cwd,
            "message": {"role": "user", "content": "create a teammate"},
            "uuid": "u-1",
        },
        {
            "type": "assistant",
            "sessionId": root_session_id,
            "timestamp": "2026-03-13T10:00:01Z",
            "cwd": cwd,
            "uuid": "a-1",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tool-1", "name": "Agent", "input": {"prompt": "Do work"}},
                ],
                "stop_reason": "tool_use",
            },
        },
        {
            "type": "user",
            "sessionId": root_session_id,
            "timestamp": "2026-03-13T10:00:02Z",
            "cwd": cwd,
            "uuid": "u-2",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "spawned"}],
            },
            "toolUseResult": {
                "status": "teammate_spawned",
                "teammate_id": "agent-123",
                "name": "agent-123",
                "team_name": "alpha",
                "agent_type": "general-purpose",
            },
        },
    ]
    subagent_records = [
        {
            "type": "user",
            "sessionId": root_session_id,
            "timestamp": "2026-03-13T10:00:03Z",
            "cwd": cwd,
            "agentId": "agent-123",
            "isSidechain": True,
            "message": {"role": "user", "content": "do work"},
            "uuid": "u-3",
        },
        {
            "type": "assistant",
            "sessionId": root_session_id,
            "timestamp": "2026-03-13T10:00:04Z",
            "cwd": cwd,
            "agentId": "agent-123",
            "isSidechain": True,
            "uuid": "a-2",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "working"}],
                "stop_reason": "end_turn",
            },
        },
    ]
    main_path.write_text("\n".join(json.dumps(record) for record in main_records), encoding="utf-8")
    subagent_path.write_text("\n".join(json.dumps(record) for record in subagent_records), encoding="utf-8")

    cache = IndexCache(
        path_to_trajectory={
            str(main_path): root_session_id,
            str(subagent_path): root_session_id,
        }
    )
    store, _ = resolve_store(
        {"trajectory_id": root_session_id},
        log_file=None,
        global_scope=False,
        current_dir=tmp_path,
        cache=cache,
    )

    trajectory = store.get_trajectory(UUID(root_session_id))
    assert len(trajectory.sessions) == 2
    session_ids = {str(session.session_id) for session in trajectory.sessions}
    assert root_session_id in session_ids


def test_resolve_store_targeted_expands_claude_subagent_directory(tmp_path) -> None:
    project_dir = tmp_path / "project"
    root_session_id = "05e58bcb-fe0a-4324-b311-2568aa901c9c"
    main_path = project_dir / f"{root_session_id}.jsonl"
    subagent_path = project_dir / root_session_id / "subagents" / "agent-123.jsonl"
    subagent_path.parent.mkdir(parents=True)

    cwd = str(project_dir)
    main_records = [
        {
            "type": "user",
            "sessionId": root_session_id,
            "timestamp": "2026-03-13T10:00:00Z",
            "cwd": cwd,
            "message": {"role": "user", "content": "create a teammate"},
            "uuid": "u-1",
        }
    ]
    subagent_records = [
        {
            "type": "user",
            "sessionId": root_session_id,
            "timestamp": "2026-03-13T10:00:03Z",
            "cwd": cwd,
            "agentId": "agent-123",
            "isSidechain": True,
            "message": {"role": "user", "content": "do work"},
            "uuid": "u-3",
        }
    ]
    main_path.write_text("\n".join(json.dumps(record) for record in main_records), encoding="utf-8")
    subagent_path.write_text("\n".join(json.dumps(record) for record in subagent_records), encoding="utf-8")

    cache = IndexCache(path_to_trajectory={str(main_path): root_session_id})
    store, _ = resolve_store(
        {"trajectory_id": root_session_id},
        log_file=None,
        global_scope=False,
        current_dir=tmp_path,
        cache=cache,
    )

    trajectory = store.get_trajectory(UUID(root_session_id))
    assert len(trajectory.sessions) == 2


def _fixture_store_with_long_output() -> tuple[DocumentStore, dict[str, object]]:
    """Fixture with a tool_call step whose tool_output contains a long string."""
    trajectory_id = uuid4()
    session_id = uuid4()
    turn_id = uuid4()
    step_id = uuid4()
    prompt_event_id = uuid4()
    tool_event_id = uuid4()
    ts = datetime(2026, 3, 10, 8, 20, tzinfo=timezone.utc)

    long_content = "x" * (_EVENT_SCAN_PAYLOAD_PREVIEW_LEN + 100)

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


def test_event_scan_filters_by_type_and_truncates() -> None:
    store, ids = _fixture_store_with_long_output()
    trajectory = store.get_trajectory(ids["trajectory_id"])

    result = build_event_scan(trajectory, event_type="tool.call.requested")

    assert len(result["matches"]) >= 1
    assert result["type"] == "tool.call.requested"
    for match in result["matches"]:
        assert match["type"] == "tool.call.requested"
        assert "event_id" in match
        assert "session_id" in match


def test_step_details_returns_full_output_without_truncation() -> None:
    store, ids = _fixture_store_with_long_output()
    step = store.get_step(ids["step_id"])

    result = build_step_details(step, store=store)

    content = result["shape"]["tool_output"]["content"]
    assert content == ids["long_content"]


def test_build_trajectory_overview_includes_files_in_flows() -> None:
    store, ids = _fixture_store_with_long_output()
    trajectory = store.get_trajectory(ids["trajectory_id"])

    result = build_trajectory_overview(trajectory, store=store)

    session = result["sessions"][0]
    flows = session["turns"][0]["work_summary"]["flows"]
    tool_flow = next(f for f in flows if "tool_calls" in f)
    assert tool_flow["files"] == ["foo.py"]


def test_build_trajectory_overview_uses_teammate_summary_for_team_turns() -> None:
    trajectory_id = uuid4()
    session_id = uuid4()
    turn_id = uuid4()
    step_id = uuid4()
    prompt_event_id = uuid4()
    tool_event_id = uuid4()
    ts = datetime(2026, 4, 7, 10, 56, tzinfo=timezone.utc)

    prompt_event = Event(
        event_id=prompt_event_id,
        session_id=session_id,
        timestamp=ts,
        type=EventType.USER_PROMPT_SUBMITTED,
        vendor_source=Vendor.CLAUDE_CODE,
        actor="user",
        payload={"text": "team update"},
    )
    tool_event = Event(
        event_id=tool_event_id,
        session_id=session_id,
        timestamp=ts,
        type=EventType.TOOL_CALL_REQUESTED,
        vendor_source=Vendor.CLAUDE_CODE,
        actor="assistant",
        payload={"tool_name": "TaskUpdate", "tool_call_id": "call-task-update"},
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
                tool_name="TaskUpdate",
                tool_call_id="call-task-update",
                input={"taskId": "1", "addBlockedBy": []},
                output={"taskId": "1", "updatedFields": ["status"]},
                status=ToolStatus.COMPLETED,
                event_ids=[tool_event_id],
            ),
        ],
        event_ids=[tool_event_id],
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
        team_state=TeamTurnState(
            members=[
                TeamMemberState(
                    member_id="infra",
                    color="blue",
                    summary="Task 1 done, 2 and 3 unblocked",
                )
            ],
            tasks=[
                TeamTaskState(
                    task_id="1",
                    member_id="infra",
                    summary="Task 1 done, 2 and 3 unblocked",
                    status="completed",
                    blocked_by=[],
                    updated_fields=["status"],
                )
            ],
        ),
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

    result = build_trajectory_overview(trajectory, store=store)

    turn_node = result["sessions"][0]["turns"][0]
    assert "work_summary" not in turn_node
    assert turn_node["user_request"] == {
        "content": "team update",
        "type": "message",
        "source": "human_user",
    }
    assert turn_node["teammate_summary"]["step_ids"] == [str(step_id)]
    assert turn_node["teammate_summary"]["members"] == [
        {
            "member_id": "infra",
            "color": "blue",
            "summary": "Task 1 done, 2 and 3 unblocked",
        }
    ]
    assert turn_node["teammate_summary"]["task"] == [
        {
            "task_id": "1",
            "member_id": "infra",
            "summary": "Task 1 done, 2 and 3 unblocked",
            "status": "completed",
            "updated_fields": ["status"],
        }
    ]


def test_build_trajectory_overview_filters_team_lead_user_request_content() -> None:
    trajectory_id = uuid4()
    session_id = uuid4()
    turn_id = uuid4()
    step_id = uuid4()
    prompt_event_id = uuid4()
    ts = datetime(2026, 4, 7, 10, 56, tzinfo=timezone.utc)

    teammate_payload = (
        '<teammate-message teammate_id="yaml-fix" color="purple" summary="Task 4 done: YAMLs wrapped in execution">\n'
        "Task #4 complete.\n"
        "</teammate-message>\n\n"
        '<teammate-message teammate_id="yaml-fix" color="purple">\n'
        '{"type":"idle_notification","from":"yaml-fix"}\n'
        "</teammate-message>\n\n"
        '<teammate-message teammate_id="infra" color="blue" summary="Task 1 done, 2 and 3 unblocked">\n'
        "Task #1 complete.\n"
        "</teammate-message>"
    )
    prompt_event = Event(
        event_id=prompt_event_id,
        session_id=session_id,
        timestamp=ts,
        type=EventType.USER_PROMPT_SUBMITTED,
        vendor_source=Vendor.CLAUDE_CODE,
        actor="user",
        payload={"text": teammate_payload},
    )
    turn = Turn(
        turn_id=turn_id,
        session_id=session_id,
        sequence=0,
        started_at=ts,
        ended_at=ts,
        user_request_event_id=prompt_event_id,
        event_ids=[prompt_event_id],
        steps=[
            Step(
                step_id=step_id,
                session_id=session_id,
                turn_id=turn_id,
                sequence=0,
                timestamp=ts,
                vendor=Vendor.CLAUDE_CODE,
                items=[StepTextItem(text="received update", event_ids=[prompt_event_id])],
                event_ids=[prompt_event_id],
            )
        ],
        team_state=TeamTurnState(
            members=[
                TeamMemberState(member_id="yaml-fix", color="purple", summary="Task 4 done: YAMLs wrapped in execution"),
                TeamMemberState(member_id="infra", color="blue", summary="Task 1 done, 2 and 3 unblocked"),
            ],
            tasks=[],
        ),
    )
    session = Session(
        session_id=session_id,
        trajectory_id=trajectory_id,
        vendor=Vendor.CLAUDE_CODE,
        agent_name="main",
        started_at=ts,
        ended_at=ts,
        events=[prompt_event],
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

    result = build_trajectory_overview(trajectory, store=store)

    user_request = result["sessions"][0]["turns"][0]["user_request"]
    assert user_request == {
        "type": "message",
        "source": "team_lead",
        "content": "yaml-fix: Task 4 done: YAMLs wrapped in execution\ninfra: Task 1 done, 2 and 3 unblocked",
    }


def test_build_trajectory_overview_resolves_teammate_member_session_id() -> None:
    trajectory_id = uuid4()
    main_session_id = uuid4()
    child_session_id = uuid4()
    turn_id = uuid4()
    prompt_event_id = uuid4()
    ts = datetime(2026, 4, 7, 10, 56, tzinfo=timezone.utc)

    prompt_event = Event(
        event_id=prompt_event_id,
        session_id=main_session_id,
        timestamp=ts,
        type=EventType.USER_PROMPT_SUBMITTED,
        vendor_source=Vendor.CLAUDE_CODE,
        actor="user",
        payload={"text": "team update"},
    )
    turn = Turn(
        turn_id=turn_id,
        session_id=main_session_id,
        sequence=0,
        started_at=ts,
        ended_at=ts,
        user_request_event_id=prompt_event_id,
        event_ids=[prompt_event_id],
        steps=[
            Step(
                step_id=uuid4(),
                session_id=main_session_id,
                turn_id=turn_id,
                sequence=0,
                timestamp=ts,
                vendor=Vendor.CLAUDE_CODE,
                items=[StepTextItem(text="team update", event_ids=[prompt_event_id])],
                event_ids=[prompt_event_id],
            )
        ],
        team_state=TeamTurnState(
            members=[TeamMemberState(member_id="infra@alpha", name="infra")],
            tasks=[],
        ),
    )
    main_session = Session(
        session_id=main_session_id,
        trajectory_id=trajectory_id,
        vendor=Vendor.CLAUDE_CODE,
        agent_name="main",
        started_at=ts,
        ended_at=ts,
        events=[prompt_event],
        turns=[turn],
    )
    child_session = Session(
        session_id=child_session_id,
        trajectory_id=trajectory_id,
        vendor=Vendor.CLAUDE_CODE,
        agent_name="infra@alpha",
        started_at=ts,
        ended_at=ts,
        parent_session_id=main_session_id,
        turns=[],
        events=[],
    )
    trajectory = Trajectory(
        trajectory_id=trajectory_id,
        project_identifier="legion",
        summary=TrajectorySummary(
            root_session_id=main_session_id,
            started_at=ts,
            ended_at=ts,
            session_count=2,
            turn_count=1,
            vendors=[Vendor.CLAUDE_CODE],
        ),
        edges=[],
        sessions=[main_session, child_session],
    )
    store = DocumentStore.from_trajectories([trajectory])

    result = build_trajectory_overview(trajectory, store=store)

    members = result["sessions"][0]["turns"][0]["teammate_summary"]["members"]
    assert members == [{"member_id": "infra@alpha", "name": "infra", "session_id": str(child_session_id)}]


def test_build_trajectory_overview_resolves_teammate_member_session_id_from_role_and_timing() -> None:
    trajectory_id = uuid4()
    main_session_id = uuid4()
    old_child_session_id = uuid4()
    new_child_session_id = uuid4()
    turn_id = uuid4()
    turn_start = datetime(2026, 4, 7, 10, 56, 43, tzinfo=timezone.utc)
    prompt_event_id = uuid4()

    prompt_event = Event(
        event_id=prompt_event_id,
        session_id=main_session_id,
        timestamp=turn_start,
        type=EventType.USER_PROMPT_SUBMITTED,
        vendor_source=Vendor.CLAUDE_CODE,
        actor="user",
        payload={"text": "team update"},
    )
    turn = Turn(
        turn_id=turn_id,
        session_id=main_session_id,
        sequence=0,
        started_at=turn_start,
        ended_at=turn_start,
        user_request_event_id=prompt_event_id,
        event_ids=[prompt_event_id],
        steps=[
            Step(
                step_id=uuid4(),
                session_id=main_session_id,
                turn_id=turn_id,
                sequence=0,
                timestamp=turn_start,
                vendor=Vendor.CLAUDE_CODE,
                items=[StepTextItem(text="team update", event_ids=[prompt_event_id])],
                event_ids=[prompt_event_id],
            )
        ],
        team_state=TeamTurnState(
            members=[TeamMemberState(member_id="infra")],
            tasks=[],
        ),
    )
    main_session = Session(
        session_id=main_session_id,
        trajectory_id=trajectory_id,
        vendor=Vendor.CLAUDE_CODE,
        agent_name="main",
        started_at=turn_start,
        ended_at=turn_start,
        events=[prompt_event],
        turns=[turn],
    )
    old_child_session = Session(
        session_id=old_child_session_id,
        trajectory_id=trajectory_id,
        vendor=Vendor.CLAUDE_CODE,
        agent_name="agent-old",
        started_at=datetime(2026, 4, 7, 10, 52, 35, tzinfo=timezone.utc),
        ended_at=datetime(2026, 4, 7, 10, 54, 24, tzinfo=timezone.utc),
        parent_session_id=main_session_id,
        turns=[],
        events=[],
        extensions=VendorExtensions(
            claude_code=ClaudeCodeExtensions(agent_name="agent-old", agent_role="infra")
        ),
    )
    new_child_session = Session(
        session_id=new_child_session_id,
        trajectory_id=trajectory_id,
        vendor=Vendor.CLAUDE_CODE,
        agent_name="agent-new",
        started_at=datetime(2026, 4, 7, 10, 52, 35, tzinfo=timezone.utc),
        ended_at=datetime(2026, 4, 7, 10, 54, 29, tzinfo=timezone.utc),
        parent_session_id=main_session_id,
        turns=[],
        events=[],
        extensions=VendorExtensions(
            claude_code=ClaudeCodeExtensions(agent_name="agent-new", agent_role="infra")
        ),
    )
    trajectory = Trajectory(
        trajectory_id=trajectory_id,
        project_identifier="legion",
        summary=TrajectorySummary(
            root_session_id=main_session_id,
            started_at=old_child_session.started_at,
            ended_at=turn_start,
            session_count=3,
            turn_count=1,
            vendors=[Vendor.CLAUDE_CODE],
        ),
        edges=[],
        sessions=[main_session, old_child_session, new_child_session],
    )
    store = DocumentStore.from_trajectories([trajectory])

    result = build_trajectory_overview(trajectory, store=store)

    members = result["sessions"][0]["turns"][0]["teammate_summary"]["members"]
    assert members == [{"member_id": "infra", "session_id": str(new_child_session_id)}]


def test_build_trajectory_overview_omits_spawned_child_sessions() -> None:
    trajectory_id = uuid4()
    main_session_id = uuid4()
    child_session_id = uuid4()
    turn_id = uuid4()
    step_id = uuid4()
    prompt_event_id = uuid4()
    ts = datetime(2026, 4, 7, 10, 56, tzinfo=timezone.utc)

    teammate_payload = (
        '<teammate-message teammate_id="team-lead" summary="infra agent - router and auth context">\n'
        "You are the infra agent.\n"
        "</teammate-message>"
    )
    prompt_event = Event(
        event_id=prompt_event_id,
        session_id=child_session_id,
        timestamp=ts,
        type=EventType.USER_PROMPT_SUBMITTED,
        vendor_source=Vendor.CLAUDE_CODE,
        actor="user",
        payload={"text": teammate_payload},
    )
    child_turn = Turn(
        turn_id=turn_id,
        session_id=child_session_id,
        sequence=0,
        started_at=ts,
        ended_at=ts,
        user_request_event_id=prompt_event_id,
        event_ids=[prompt_event_id],
        steps=[
            Step(
                step_id=step_id,
                session_id=child_session_id,
                turn_id=turn_id,
                sequence=0,
                timestamp=ts,
                vendor=Vendor.CLAUDE_CODE,
                items=[StepTextItem(text="implementing auth context", event_ids=[prompt_event_id])],
                event_ids=[prompt_event_id],
            )
        ],
        team_state=TeamTurnState(
            members=[TeamMemberState(member_id="team-lead", summary="infra agent - router and auth context")],
            tasks=[],
        ),
    )
    parent_prompt_event_id = uuid4()
    parent_turn_id = uuid4()
    parent_step_id = uuid4()
    parent_prompt_event = Event(
        event_id=parent_prompt_event_id,
        session_id=main_session_id,
        timestamp=ts,
        type=EventType.USER_PROMPT_SUBMITTED,
        vendor_source=Vendor.CLAUDE_CODE,
        actor="user",
        payload={"text": "create a team to implement these"},
    )
    parent_turn = Turn(
        turn_id=parent_turn_id,
        session_id=main_session_id,
        sequence=0,
        started_at=ts,
        ended_at=ts,
        user_request_event_id=parent_prompt_event_id,
        event_ids=[parent_prompt_event_id],
        steps=[
            Step(
                step_id=parent_step_id,
                session_id=main_session_id,
                turn_id=parent_turn_id,
                sequence=0,
                timestamp=ts,
                vendor=Vendor.CLAUDE_CODE,
                items=[StepTextItem(text="spawning infra agent", event_ids=[parent_prompt_event_id])],
                event_ids=[parent_prompt_event_id],
            )
        ],
    )
    main_session = Session(
        session_id=main_session_id,
        trajectory_id=trajectory_id,
        vendor=Vendor.CLAUDE_CODE,
        agent_name="main",
        started_at=ts,
        ended_at=ts,
        turns=[parent_turn],
        events=[parent_prompt_event],
    )
    child_session = Session(
        session_id=child_session_id,
        trajectory_id=trajectory_id,
        vendor=Vendor.CLAUDE_CODE,
        agent_name="agent-child",
        started_at=ts,
        ended_at=ts,
        parent_session_id=main_session_id,
        turns=[child_turn],
        events=[prompt_event],
    )
    trajectory = Trajectory(
        trajectory_id=trajectory_id,
        project_identifier="legion",
        summary=TrajectorySummary(
            root_session_id=main_session_id,
            started_at=ts,
            ended_at=ts,
            session_count=2,
            turn_count=1,
            vendors=[Vendor.CLAUDE_CODE],
        ),
        edges=[
            TrajectoryEdge(
                type="spawned_subagent",
                source_session_id=main_session_id,
                target_session_id=child_session_id,
                source_turn_id=parent_turn_id,
                source_step_id=parent_step_id,
                provenance="observed",
                confidence="high",
            )
        ],
        sessions=[main_session, child_session],
    )
    store = DocumentStore.from_trajectories([trajectory])

    result = build_trajectory_overview(trajectory, store=store)

    assert len(result["sessions"]) == 1
    main_turn_node = result["sessions"][0]["turns"][0]
    assert main_turn_node["user_request"] == {
        "type": "message",
        "source": "human_user",
        "content": "create a team to implement these",
    }


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
            dispatch(
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
