"""Compatibility facade for analysis projections."""

from coding_trajectory.analysis.event_scan import build_event_scan
from coding_trajectory.analysis.step_details import build_step_details
from coding_trajectory.analysis.trajectory_views import (
    build_trajectory_narrative,
    build_trajectory_overview,
)

__all__ = [
    "build_event_scan",
    "build_step_details",
    "build_trajectory_narrative",
    "build_trajectory_overview",
]