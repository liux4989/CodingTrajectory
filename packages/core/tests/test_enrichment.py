from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from coding_trajectory.enrichment import (
    CodexWorkflowPlugin,
    EnrichmentNote,
    EnrichmentOverlay,
    EnrichmentPlugin,
    build_default_trajectory_enrichment,
    build_enriched_session,
    build_enriched_trajectory,
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


class _TrajectoryPlugin(EnrichmentPlugin):
    namespace = "codex.spawn"

    def enrich_trajectory(self, trajectory: Trajectory, *, store=None) -> EnrichmentOverlay | None:
        return EnrichmentOverlay(
            structural={"operations": [{"kind": "subagent"}]},
            derived={"multi_agent_mode": "cross_session"},
            agent_specific={"codex": {"collaboration_mode": "default"}},
            notes=[
                EnrichmentNote(
                    subject="trajectory",
                    message="derived for replay",
                    provenance="derived",
                    confidence="medium",
                )
            ],
        )


def test_build_enriched_session_wraps_canonical_and_sidecar_separately() -> None:
    timestamp = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
    session = Session(
        session_id=uuid4(),
        trajectory_id=uuid4(),
        vendor=Vendor.CODEX_CLI,
        started_at=timestamp,
        ended_at=timestamp,
    )

    enriched = build_enriched_session(
        session,
        derived={"session_kind": "primary"},
        agent_specific={"codex": {"approval_policy": "never"}},
    )

    assert enriched.session_id == session.session_id
    assert enriched.enrichment.derived == {"session_kind": "primary"}
    assert enriched.enrichment.agent_specific == {"codex": {"approval_policy": "never"}}


def test_build_enriched_trajectory_collects_plugin_output_under_namespace() -> None:
    timestamp = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
    trajectory = Trajectory(
        trajectory_id=uuid4(),
        project_identifier="coding-trajectory",
        sessions=[
            Session(
                session_id=uuid4(),
                trajectory_id=uuid4(),
                vendor=Vendor.CODEX_CLI,
                started_at=timestamp,
                ended_at=timestamp,
            )
        ],
    )

    enriched = build_enriched_trajectory(
        trajectory,
        structural={"session_graph": {"session_count": 1}},
        plugins=[_TrajectoryPlugin()],
    )

    assert enriched.trajectory_id == trajectory.trajectory_id
    assert enriched.enrichment.structural["session_graph"] == {"session_count": 1}
    assert enriched.enrichment.derived["multi_agent_mode"] == "cross_session"
    assert enriched.enrichment.agent_specific["codex"] == {"collaboration_mode": "default"}
    assert enriched.enrichment.plugins["codex.spawn"]["derived"] == {"multi_agent_mode": "cross_session"}
    assert enriched.enrichment.notes[0].subject == "trajectory"


def test_build_default_trajectory_enrichment_builds_single_session_projection() -> None:
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

    enriched = build_default_trajectory_enrichment(trajectory)

    assert enriched.trajectory_id == trajectory.trajectory_id
    assert enriched.enrichment.structural["session_tree"]["root_session_id"] == session.session_id
    assert enriched.enrichment.structural["session_tree"]["roots"] == [session.session_id]
    assert enriched.enrichment.structural["session_tree"]["leaves"] == [session.session_id]
    assert enriched.enrichment.structural["operations"] == []
    assert enriched.enrichment.derived["multi_agent_mode"] == "single_session"
    assert enriched.enrichment.derived["topology"] == "single_session"
    assert enriched.enrichment.agent_specific["codex"]["collaboration_modes"] == ["default"]
    assert enriched.enrichment.agent_specific["codex"]["agent_roles"] == ["cli"]


def test_build_default_trajectory_enrichment_projects_observed_spawn_operation() -> None:
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

    enriched = build_default_trajectory_enrichment(trajectory)

    operation = enriched.enrichment.structural["operations"][0]
    session_tree = enriched.enrichment.structural["session_tree"]
    assert operation["edge_type"] == "spawned_subagent"
    assert operation["source_session_id"] == parent_session_id
    assert operation["target_session_id"] == child_session_id
    assert operation["source_turn_id"] == turn_id
    assert operation["source_step_id"] == step_id
    assert operation["source_event_id"] == tool_event.event_id
    assert operation["tool_name"] == "spawn_agent"
    assert operation["provenance"] == "observed"
    assert session_tree["root_session_id"] == parent_session_id
    assert session_tree["leaves"] == [child_session_id]
    assert enriched.enrichment.derived["multi_agent_mode"] == "cross_session"
    assert enriched.enrichment.derived["topology"] == "linear"
    assert enriched.enrichment.derived["has_observed_spawn"] is True
    assert enriched.enrichment.agent_specific["codex"]["multi_agent"] == {
        "spawned_subagent_count": 1,
        "spawn_links": [
            {
                "source_session_id": parent_session_id,
                "target_session_id": child_session_id,
                "source_step_id": step_id,
                "source_event_id": tool_event.event_id,
            }
        ],
    }
    assert enriched.enrichment.notes == []


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

    enriched = build_enriched_session(session, plugins=[CodexWorkflowPlugin()])

    codex = enriched.enrichment.agent_specific["codex"]
    assert codex["plans"] == [
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
    assert enriched.enrichment.derived == {}
    assert enriched.enrichment.notes[0].message == "Codex plan snapshots derived from update_plan tool calls."


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

    enriched_parent = build_enriched_session(parent, plugins=[CodexWorkflowPlugin()])
    enriched_child = build_enriched_session(child, plugins=[CodexWorkflowPlugin()])

    parent_codex = enriched_parent.enrichment.agent_specific["codex"]
    assert parent_codex["spawned_agents"] == [
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
    assert "multi_agent" not in parent_codex
    assert enriched_parent.enrichment.derived == {}

    child_codex = enriched_child.enrichment.agent_specific["codex"]
    assert child_codex["spawned_from"] == {
        "parent_session_id": str(parent_session_id),
        "spawn_origin": "subagent",
        "agent_role": "explorer",
        "agent_nickname": "Singer",
    }
    assert enriched_child.enrichment.derived == {}
