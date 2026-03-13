from __future__ import annotations

from coding_trajectory.ingestion.models import Trajectory


def test_trajectory_supports_project_identifier() -> None:
    trajectory = Trajectory(project_identifier="claude-code-wrapper", task_reference="task-123")

    assert trajectory.project_identifier == "claude-code-wrapper"
    assert trajectory.task_reference == "task-123"
