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


def test_build_trajectory_detects_in_session_codex_orchestration() -> None:
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
            )
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

    assert trajectory.multi_agent_mode == "in_session"
    assert len(trajectory.operations) == 1
    assert trajectory.operations[0].scope == "session_span"
    assert trajectory.edges == []


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
        events=[
            Event(
                session_id=child_session_id,
                timestamp=timestamp,
                type=EventType.BACKGROUND_TASK_STARTED,
                vendor_source=Vendor.CLAUDE_CODE,
                actor="assistant",
                payload={"task_id": "task-1"},
            )
        ],
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

    assert trajectory.multi_agent_mode == "cross_session"
    assert len(trajectory.edges) == 1
    assert trajectory.edges[0].type == "sidechain_of"
    assert trajectory.operations[0].scope == "session_graph"
