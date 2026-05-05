from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from coding_trajectory.ingestion.models import Session, Trajectory, Vendor
from coding_trajectory.query import DocumentStore
from coding_trajectory.service import _resolve_trajectory


def test_resolve_trajectory_accepts_member_session_id() -> None:
    trajectory_id = uuid4()
    parent_session_id = uuid4()
    child_session_id = uuid4()
    trajectory = Trajectory(
        trajectory_id=trajectory_id,
        sessions=[
            Session(
                session_id=parent_session_id,
                trajectory_id=trajectory_id,
                vendor=Vendor.CODEX_CLI,
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            Session(
                session_id=child_session_id,
                trajectory_id=trajectory_id,
                vendor=Vendor.CODEX_CLI,
                started_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
                parent_session_id=parent_session_id,
            ),
        ],
    )
    store = DocumentStore.from_trajectories([trajectory])

    assert _resolve_trajectory(store, str(child_session_id)) is trajectory
