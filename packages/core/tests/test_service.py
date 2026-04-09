from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from coding_trajectory.analysis import build_trajectory_structure
from coding_trajectory.ingestion.models import (
    Event,
    EventType,
    Session,
    Step,
    StepTextItem,
    StepToolItem,
    Trajectory,
    TrajectoryEdge,
    TrajectorySummary,
    Turn,
    Vendor,
)
from coding_trajectory.query import DocumentStore
from coding_trajectory.service import dispatch, IndexCache, resolve_store
from coding_trajectory.service import (
    serialize_event_detail,
    serialize_session_detail,
    serialize_step_detail,
    serialize_trajectory_detail,
    serialize_turn_detail,
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

    assert set(payload) == {"trajectory_id", "session_ids"}
    assert payload["session_ids"] == [str(session_a_id), str(session_b_id)]


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
        "turn_ids",
        "event_ids",
    }
    assert payload["session_id"] == str(session_id)
    assert payload["trajectory_id"] == str(trajectory_id)


def test_serialize_turn_detail_exposes_only_analysis_join_fields() -> None:
    session_id = uuid4()
    turn_id = uuid4()
    step_id = uuid4()
    event_id = uuid4()
    turn = Turn(
        turn_id=turn_id,
        session_id=session_id,
        sequence=0,
        started_at=datetime(2026, 3, 13, 10, 0, tzinfo=timezone.utc),
        event_ids=[event_id],
        steps=[
            Step(
                step_id=step_id,
                session_id=session_id,
                turn_id=turn_id,
                sequence=0,
                timestamp=datetime(2026, 3, 13, 10, 0, tzinfo=timezone.utc),
                vendor=Vendor.CODEX_CLI,
            )
        ],
    )

    payload = serialize_turn_detail(turn)

    assert payload == {
        "turn_id": str(turn_id),
        "session_id": str(session_id),
        "event_ids": [str(event_id)],
        "step_ids": [str(step_id)],
    }


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

    assert set(payload) == {"step_id", "session_id", "turn_id", "items", "event_ids"}
    assert payload["items"][0] == {"kind": "text", "text": "Planning", "event_ids": []}
    assert payload["items"][1]["tool_name"] == "plan_mode"
    assert payload["items"][1] == {
        "kind": "tool",
        "tool_name": "plan_mode",
        "output": {"status": "ok"},
        "status": "requested",
        "event_ids": [str(event_id)],
    }


def test_serialize_event_detail_keeps_analysis_fields_and_typed_subdetails() -> None:
    session_id = uuid4()
    event = Event(
        session_id=session_id,
        timestamp=datetime(2026, 3, 13, 10, 0, tzinfo=timezone.utc),
        type=EventType.TOOL_CALL_SUCCEEDED,
        vendor_source=Vendor.CODEX_CLI,
        payload={
            "tool_call_id": "call-1",
            "tool_name": "spawn_agent",
            "tool_args": {"role": "explorer"},
            "result": {"agent_id": "a1"},
            "text": "spawned agent",
        },
    )

    payload = serialize_event_detail(event)

    assert payload == {
        "event_id": str(event.event_id),
        "session_id": str(session_id),
        "timestamp": "2026-03-13T10:00:00Z",
        "type": "tool.call.succeeded",
        "tool_call": {
            "tool_call_id": "call-1",
            "tool_name": "spawn_agent",
            "input": {"role": "explorer"},
            "result": {"agent_id": "a1"},
            "status": "done",
        },
        "text": {"text": "spawned agent"},
    }


def test_build_trajectory_structure_serializes_correctly() -> None:
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

    structure = build_trajectory_structure(trajectory)
    payload = structure.model_dump(mode="json")

    assert payload["trajectory_id"] == str(trajectory_id)
    assert payload["session_tree"]["root_session_id"] == str(session_id)
    assert payload["multi_agent_mode"] == "single_session"


def test_rpc_dispatch_trajectory_enrich_is_removed() -> None:
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

    try:
        dispatch(
            "trajectory.enrich",
            {"trajectory_id": str(trajectory_id)},
            store=store,
            global_scope=False,
            current_dir=Path.cwd(),
            discovery_note="",
            cache=IndexCache(),
        )
        raise AssertionError("trajectory.enrich should raise KeyError")
    except KeyError:
        pass  # expected — unknown method


def test_resolve_store_uses_global_discovery_for_explicit_project_name(monkeypatch, tmp_path) -> None:
    cache = IndexCache()
    fake_store = DocumentStore.from_trajectories([])
    observed: dict[str, object] = {}

    def fake_build_store_full(*, global_scope: bool, current_dir: Path, cache: IndexCache):
        observed["global_scope"] = global_scope
        observed["current_dir"] = current_dir
        return fake_store, ""

    monkeypatch.setattr("coding_trajectory.service._build_store_full", fake_build_store_full)

    store, note = resolve_store(
        {"project_name": "TaskBucket"},
        log_file=None,
        global_scope=False,
        current_dir=tmp_path,
        cache=cache,
    )

    assert store is fake_store
    assert note == ""
    assert observed["global_scope"] is True
    assert observed["current_dir"] == tmp_path
