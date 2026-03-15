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
from coding_trajectory.trajectory import build_trajectory


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
