"""Compatibility facade for analysis projections."""

from coding_trajectory.analysis.event_scan import build_event_scan
from coding_trajectory.analysis.item_details import build_item_details
from coding_trajectory.analysis.session_graph_views import (
    build_session_graph_narrative,
    build_session_graph_overview,
)

__all__ = [
    "build_event_scan",
    "build_item_details",
    "build_session_graph_narrative",
    "build_session_graph_overview",
]
