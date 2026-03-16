from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from coding_trajectory.enrichment import (
    CodexWorkflowPlugin,
    EnrichmentNote,
    SidecarPlugin,
    build_trajectory_structure,
    collect_sidecars,
)
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
    assert structure.notes == []

    # Verify tree node relationships
    parent_node = structure.session_tree.nodes_by_session_id[parent_session_id]
    child_node = structure.session_tree.nodes_by_session_id[child_session_id]
    assert parent_node.child_session_ids == [child_session_id]
    assert child_node.parent_session_id == parent_session_id
    assert parent_node.is_root is True
    assert child_node.is_leaf is True


def test_codex_workflow_plugin_extracts_plan_snapshots_from_update_plan_tools() -> None:
    timestamp = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
    session_id = uuid4()
    turn_id = uuid4()
    step_id = uuid4()
    session = Session(
        session_id=session_id,
        trajectory_id=uuid4(),
        vendor=Vendor.CODEX_CLI,
        started_at=timestamp,
        ended_at=timestamp,
        turns=[
            Turn(
                turn_id=turn_id,
                session_id=session_id,
                sequence=0,
                started_at=timestamp,
                steps=[
                    Step(
                        step_id=step_id,
                        session_id=session_id,
                        turn_id=turn_id,
                        sequence=0,
                        timestamp=timestamp,
                        vendor=Vendor.CODEX_CLI,
                        items=[
                            StepToolItem(
                                tool_name="update_plan",
                                tool_call_id="call-plan-1",
                                input={
                                    "explanation": "Need to refactor in small safe slices.",
                                    "plan": [
                                        {"step": "Refactor models", "status": "in_progress"},
                                        {"step": "Update tests", "status": "pending"},
                                    ],
                                },
                                output="Plan updated",
                            )
                        ],
                    )
                ],
            )
        ],
    )

    sidecars = collect_sidecars([CodexWorkflowPlugin()], session)

    payload = sidecars.namespaces["codex.workflow"]
    assert payload.data["plans"] == [
        {
            "source_step_id": str(step_id),
            "tool_name": "update_plan",
            "tool_call_id": "call-plan-1",
            "explanation": "Need to refactor in small safe slices.",
            "items": [
                {"step": "Refactor models", "status": "in_progress"},
                {"step": "Update tests", "status": "pending"},
            ],
        }
    ]
    assert payload.notes[0].message == "Codex plan snapshots derived from update_plan tool calls."


def test_codex_workflow_plugin_keeps_spawn_references_at_session_level() -> None:
    timestamp = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
    parent_session_id = uuid4()
    turn_id = uuid4()
    step_id = uuid4()

    parent = Session(
        session_id=parent_session_id,
        trajectory_id=uuid4(),
        vendor=Vendor.CODEX_CLI,
        started_at=timestamp,
        ended_at=timestamp,
        turns=[
            Turn(
                turn_id=turn_id,
                session_id=parent_session_id,
                sequence=0,
                started_at=timestamp,
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
                                tool_call_id="call-spawn-1",
                                input={"agent_type": "explorer", "role": "explorer", "nickname": "Singer"},
                                output={"agent_id": "019cf10a-9142-73d3-b05c-3d37fdfcbe4e"},
                            )
                        ],
                    )
                ],
            )
        ],
    )
    child = Session(
        session_id=uuid4(),
        trajectory_id=parent.trajectory_id,
        vendor=Vendor.CODEX_CLI,
        started_at=timestamp,
        ended_at=timestamp,
        parent_session_id=parent_session_id,
        extensions=VendorExtensions(
            codex=CodexExtensions(
                spawn_parent_thread_id=str(parent_session_id),
                spawn_agent_nickname="Singer",
                spawn_agent_role="explorer",
            )
        ),
    )

    parent_sidecars = collect_sidecars([CodexWorkflowPlugin()], parent)
    child_sidecars = collect_sidecars([CodexWorkflowPlugin()], child)

    parent_data = parent_sidecars.namespaces["codex.workflow"].data
    assert parent_data["spawned_agents"] == [
        {
            "source_step_id": str(step_id),
            "tool_name": "spawn_agent",
            "tool_call_id": "call-spawn-1",
            "agent_type": "explorer",
            "role": "explorer",
            "nickname": "Singer",
            "agent_id": "019cf10a-9142-73d3-b05c-3d37fdfcbe4e",
        }
    ]

    child_data = child_sidecars.namespaces["codex.workflow"].data
    assert child_data["spawned_from"] == {
        "parent_session_id": str(parent_session_id),
        "spawn_origin": "subagent",
        "agent_role": "explorer",
        "agent_nickname": "Singer",
    }
