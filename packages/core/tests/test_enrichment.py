from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from coding_trajectory.analysis import build_trajectory_structure
from coding_trajectory.ingestion.models import (
    CodexExtensions,
    Event,
    EventType,
    Session,
    Step,
    StepToolItem,
    Trajectory,
    Turn,
    Vendor,
    VendorExtensions,
)
from coding_trajectory.trajectory import build_trajectory


def test_build_trajectory_structure_single_session() -> None:
    timestamp = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
    session = Session(
        session_id=uuid4(),
        trajectory_id=uuid4(),
        vendor=Vendor.CODEX_CLI,
        started_at=timestamp,
        ended_at=timestamp,
        extensions=VendorExtensions(
            codex=CodexExtensions(
                collaboration_mode="default",
                agent_role="cli",
            )
        ),
    )
    trajectory = build_trajectory(
        trajectory_id=session.trajectory_id,
        project_identifier="coding-trajectory",
        sessions=[session],
    )

    structure = build_trajectory_structure(trajectory)

    assert structure.trajectory_id == trajectory.trajectory_id
    assert structure.session_tree.root_session_id == session.session_id
    assert structure.session_tree.root_session_ids == [session.session_id]
    assert structure.session_tree.leaf_session_ids == [session.session_id]
    assert structure.operations == []
    assert structure.multi_agent_mode == "single_session"
    assert structure.topology == "single_session"


def test_build_trajectory_structure_observed_spawn() -> None:
    trajectory_id = uuid4()
    parent_session_id = uuid4()
    child_session_id = uuid4()
    turn_id = uuid4()
    step_id = uuid4()
    timestamp = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
    tool_event = Event(
        session_id=parent_session_id,
        timestamp=timestamp,
        type=EventType.TOOL_CALL_REQUESTED,
        vendor_source=Vendor.CODEX_CLI,
        actor="assistant",
        payload={"tool_name": "spawn_agent", "tool_call_id": "call-1"},
    )
    parent = Session(
        session_id=parent_session_id,
        trajectory_id=trajectory_id,
        vendor=Vendor.CODEX_CLI,
        started_at=timestamp,
        ended_at=timestamp,
        events=[tool_event],
        turns=[
            Turn(
                turn_id=turn_id,
                session_id=parent_session_id,
                sequence=0,
                started_at=timestamp,
                ended_at=timestamp,
                steps=[
                    Step(
                        step_id=step_id,
                        session_id=parent_session_id,
                        turn_id=turn_id,
                        sequence=0,
                        timestamp=timestamp,
                        vendor=Vendor.CODEX_CLI,
                        items=[
                            StepToolItem(
                                tool_name="spawn_agent",
                                tool_call_id="call-1",
                                event_ids=[tool_event.event_id],
                            )
                        ],
                        event_ids=[tool_event.event_id],
                    )
                ],
            )
        ],
    )
    child = Session(
        session_id=child_session_id,
        trajectory_id=trajectory_id,
        vendor=Vendor.CODEX_CLI,
        started_at=timestamp,
        ended_at=timestamp,
        parent_session_id=parent_session_id,
    )
    trajectory = build_trajectory(
        trajectory_id=trajectory_id,
        project_identifier="coding-trajectory",
        sessions=[parent, child],
    )

    structure = build_trajectory_structure(trajectory)

    assert len(structure.operations) == 1
    op = structure.operations[0]
    assert op.edge_type == "spawned_subagent"
    assert op.source_session_id == parent_session_id
    assert op.target_session_id == child_session_id
    assert op.source_turn_id == turn_id
    assert op.source_step_id == step_id
    assert op.source_event_id == tool_event.event_id
    assert op.tool_name == "spawn_agent"
    assert op.provenance == "observed"
    assert structure.session_tree.root_session_id == parent_session_id
    assert structure.session_tree.leaf_session_ids == [child_session_id]
    assert structure.multi_agent_mode == "cross_session"
    assert structure.topology == "linear"
    assert structure.has_observed_spawn is True

    parent_node = structure.session_tree.nodes_by_session_id[parent_session_id]
    child_node = structure.session_tree.nodes_by_session_id[child_session_id]
    assert parent_node.child_session_ids == [child_session_id]
    assert child_node.parent_session_id == parent_session_id
    assert parent_node.is_root is True
    assert child_node.is_leaf is True
