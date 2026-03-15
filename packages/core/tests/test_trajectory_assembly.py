from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from coding_trajectory.ingestion.models import (
    ClaudeCodeExtensions,
    CodexExtensions,
    Event,
    EventType,
    Session,
    Vendor,
    VendorExtensions,
)
from coding_trajectory.trajectory import assemble_project_trajectories, build_trajectory


def test_build_trajectory_single_session() -> None:
    session_id = uuid4()
    trajectory_id = uuid4()
    timestamp = datetime(2026, 3, 13, 10, 0, tzinfo=timezone.utc)
    session = Session(
        session_id=session_id,
        trajectory_id=trajectory_id,
        vendor=Vendor.CODEX_CLI,
        started_at=timestamp,
        ended_at=timestamp,
        events=[
            Event(
                session_id=session_id,
                timestamp=timestamp,
                type=EventType.TOOL_CALL_REQUESTED,
                vendor_source=Vendor.CODEX_CLI,
                actor="assistant",
                payload={"tool_name": "spawn_agent", "tool_call_id": "call-1"},
            ),
        ],
        extensions=VendorExtensions(
            codex=CodexExtensions(
                collaboration_mode="default",
                agent_role="cli",
            )
        ),
    )

    trajectory = build_trajectory(
        trajectory_id=trajectory_id,
        project_identifier="coding-trajectory",
        sessions=[session],
    )

    assert trajectory.edges == []
    assert len(trajectory.sessions) == 1
    assert trajectory.summary is not None
    assert trajectory.summary.session_count == 1


def test_build_trajectory_detects_cross_session_claude_sidechain() -> None:
    trajectory_id = uuid4()
    parent_session_id = uuid4()
    child_session_id = uuid4()
    timestamp = datetime(2026, 3, 13, 10, 0, tzinfo=timezone.utc)

    parent = Session(
        session_id=parent_session_id,
        trajectory_id=trajectory_id,
        vendor=Vendor.CLAUDE_CODE,
        started_at=timestamp,
        ended_at=timestamp,
        extensions=VendorExtensions(
            claude_code=ClaudeCodeExtensions(
                team_name="alpha",
                agent_name="root-agent",
                is_sidechain=False,
            )
        ),
    )
    child = Session(
        session_id=child_session_id,
        trajectory_id=trajectory_id,
        vendor=Vendor.CLAUDE_CODE,
        started_at=timestamp,
        ended_at=timestamp,
        parent_session_id=parent_session_id,
        extensions=VendorExtensions(
            claude_code=ClaudeCodeExtensions(
                team_name="alpha",
                agent_name="sidechain-agent",
                is_sidechain=True,
            )
        ),
    )

    trajectory = build_trajectory(
        trajectory_id=trajectory_id,
        project_identifier="coding-trajectory",
        sessions=[parent, child],
    )

    assert len(trajectory.edges) == 1
    assert trajectory.edges[0].type == "sidechain_of"
    assert trajectory.edges[0].source_session_id == child_session_id
    assert trajectory.edges[0].target_session_id == parent_session_id


def test_build_trajectory_summary_counts() -> None:
    trajectory_id = uuid4()
    timestamp = datetime(2026, 3, 13, 10, 0, tzinfo=timezone.utc)
    session = Session(
        session_id=uuid4(),
        trajectory_id=trajectory_id,
        vendor=Vendor.CLAUDE_CODE,
        started_at=timestamp,
        ended_at=timestamp,
    )

    trajectory = build_trajectory(
        trajectory_id=trajectory_id,
        project_identifier="test",
        sessions=[session],
    )

    summary = trajectory.summary
    assert summary is not None
    assert summary.session_count == 1
    assert summary.turn_count == 0
    assert Vendor.CLAUDE_CODE in summary.vendors


def test_assemble_multilevel_chain_stays_in_one_trajectory() -> None:
    """root -> child -> grandchild must all land in a single trajectory."""
    root_id, child_id, grandchild_id = uuid4(), uuid4(), uuid4()
    trajectory_id = uuid4()
    t0 = datetime(2026, 3, 13, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 3, 13, 10, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 3, 13, 10, 2, tzinfo=timezone.utc)

    root = Session(
        session_id=root_id, trajectory_id=trajectory_id,
        vendor=Vendor.CODEX_CLI, started_at=t0, ended_at=t0,
    )
    child = Session(
        session_id=child_id, trajectory_id=trajectory_id,
        vendor=Vendor.CODEX_CLI, started_at=t1, ended_at=t1,
        parent_session_id=root_id,
    )
    grandchild = Session(
        session_id=grandchild_id, trajectory_id=trajectory_id,
        vendor=Vendor.CODEX_CLI, started_at=t2, ended_at=t2,
        parent_session_id=child_id,
    )

    trajectories = assemble_project_trajectories("test-proj", [grandchild, root, child])

    assert len(trajectories) == 1
    traj = trajectories[0]
    assert len(traj.sessions) == 3
    session_ids = {s.session_id for s in traj.sessions}
    assert session_ids == {root_id, child_id, grandchild_id}


def test_assemble_unrelated_sessions_become_separate_trajectories() -> None:
    """Sessions with no parent links should each become their own trajectory."""
    t0 = datetime(2026, 3, 13, 10, 0, tzinfo=timezone.utc)
    s1 = Session(
        session_id=uuid4(), trajectory_id=uuid4(),
        vendor=Vendor.CODEX_CLI, started_at=t0, ended_at=t0,
    )
    s2 = Session(
        session_id=uuid4(), trajectory_id=uuid4(),
        vendor=Vendor.CODEX_CLI, started_at=t0, ended_at=t0,
    )

    trajectories = assemble_project_trajectories("test-proj", [s1, s2])

    assert len(trajectories) == 2
