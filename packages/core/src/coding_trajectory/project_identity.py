"""Opaque local project identities, independent of display-name spelling.

Local IDs are location-scoped; remote IDs are assigned by the workspace registry.
Callers must use IDs returned by their selected authority, not derive them from names.
"""

from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from coding_trajectory.ingestion.models import SessionGraph


def local_project_id(path: Path | None, *, fallback: str) -> str:
    identity = (
        f"path:{path.expanduser().resolve()}" if path else f"unlocated:{fallback}"
    )
    return str(uuid5(NAMESPACE_URL, f"codingtrajectory:local-project:{identity}"))


def graph_project_id(graph: SessionGraph) -> str:
    root = next(
        session
        for session in graph.sessions
        if session.session_id == graph.root_session_id
    )
    return local_project_id(
        Path(root.cwd) if root.cwd else None,
        fallback=graph.project_identifier or str(graph.root_session_id),
    )
