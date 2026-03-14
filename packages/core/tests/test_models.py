from __future__ import annotations

from coding_trajectory.ingestion.models import Trajectory


def test_trajectory_supports_project_identifier() -> None:
    trajectory = Trajectory(project_identifier="claude-code-wrapper", task_reference="task-123")

    assert trajectory.project_identifier == "claude-code-wrapper"
    assert trajectory.task_reference == "task-123"


def test_trajectory_defaults_include_graph_fields() -> None:
    trajectory = Trajectory(project_identifier="coding-trajectory")

    assert trajectory.multi_agent_mode is None
    assert trajectory.summary is None
    assert trajectory.session_refs == []
    assert trajectory.edges == []
    assert trajectory.operations == []
    assert trajectory.sections == []
    assert trajectory.inference_notes == []
