"""Builder for trajectory-wide structural analysis."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from uuid import UUID

from coding_trajectory.ingestion.models import Trajectory

from .structure_models import (
    CrossSessionOperation,
    SessionTree,
    SessionTreeNode,
    TrajectoryStructure,
)


def build_trajectory_structure(trajectory: Trajectory) -> TrajectoryStructure:
    session_tree = _build_session_tree(trajectory)
    operations = _build_operations(trajectory)

    child_counts = Counter(edge.source_session_id for edge in trajectory.edges)
    edge_type_counts = Counter(edge.type for edge in trajectory.edges)
    vendors = trajectory.summary.vendors if trajectory.summary else []

    topology: str = "single_session"
    if len(trajectory.sessions) > 1:
        topology = "branching" if any(count > 1 for count in child_counts.values()) else "linear"

    return TrajectoryStructure(
        trajectory_id=trajectory.trajectory_id,
        session_tree=session_tree,
        operations=operations,
        multi_agent_mode="single_session" if len(trajectory.sessions) <= 1 else "cross_session",
        topology=topology,
        has_observed_spawn=any(
            edge.type == "spawned_subagent" and edge.provenance == "observed"
            for edge in trajectory.edges
        ),
        vendor_set=[vendor.value for vendor in vendors],
        edge_type_counts=dict(sorted(edge_type_counts.items())),
    )


def _build_session_tree(canonical: Trajectory) -> SessionTree:
    parent_by_session: dict[UUID, UUID | None] = {
        session.session_id: session.parent_session_id for session in canonical.sessions
    }
    incoming_edge_by_session = {edge.target_session_id: edge for edge in canonical.edges}
    child_ids_by_session: dict[UUID, list[UUID]] = defaultdict(list)

    for session in canonical.sessions:
        parent_id = parent_by_session.get(session.session_id)
        if parent_id is None:
            edge = incoming_edge_by_session.get(session.session_id)
            if edge is not None:
                parent_id = edge.source_session_id
                parent_by_session[session.session_id] = parent_id
        if parent_id is not None:
            child_ids_by_session[parent_id].append(session.session_id)

    roots = [
        session.session_id
        for session in canonical.sessions
        if parent_by_session.get(session.session_id) is None
    ]
    if not roots and canonical.summary and canonical.summary.root_session_id is not None:
        roots = [canonical.summary.root_session_id]

    depths = _compute_depths(roots, child_ids_by_session)

    nodes_by_session_id: dict[UUID, SessionTreeNode] = {}
    leaf_ids: list[UUID] = []

    for session in canonical.sessions:
        session_id = session.session_id
        children = sorted(child_ids_by_session.get(session_id, []), key=str)
        is_root = session_id in roots
        is_leaf = not children
        if is_leaf:
            leaf_ids.append(session_id)
        incoming_edge = incoming_edge_by_session.get(session_id)
        nodes_by_session_id[session_id] = SessionTreeNode(
            session_id=session_id,
            parent_session_id=parent_by_session.get(session_id),
            child_session_ids=children,
            depth=depths.get(session_id, 0),
            incoming_edge_type=incoming_edge.type if incoming_edge else None,
            is_root=is_root,
            is_leaf=is_leaf,
        )

    roots.sort(key=str)
    leaf_ids.sort(key=str)

    return SessionTree(
        root_session_id=canonical.summary.root_session_id if canonical.summary else (roots[0] if roots else None),
        root_session_ids=roots,
        leaf_session_ids=leaf_ids,
        nodes_by_session_id=nodes_by_session_id,
    )


def _compute_depths(
    roots: list[UUID],
    child_ids_by_session: dict[UUID, list[UUID]],
) -> dict[UUID, int]:
    depths: dict[UUID, int] = {}
    queue = deque((root_id, 0) for root_id in roots)

    while queue:
        session_id, depth = queue.popleft()
        if session_id in depths and depths[session_id] <= depth:
            continue
        depths[session_id] = depth
        for child_id in child_ids_by_session.get(session_id, []):
            queue.append((child_id, depth + 1))

    return depths


def _build_operations(canonical: Trajectory) -> list[CrossSessionOperation]:
    operations: list[CrossSessionOperation] = []
    for index, edge in enumerate(canonical.edges):
        tool_name = None
        if edge.metadata:
            raw_tool_name = edge.metadata.get("tool_name")
            if isinstance(raw_tool_name, str) and raw_tool_name.strip():
                tool_name = raw_tool_name
        operations.append(
            CrossSessionOperation(
                index=index,
                edge_type=edge.type,
                source_session_id=edge.source_session_id,
                target_session_id=edge.target_session_id,
                source_turn_id=edge.source_turn_id,
                source_step_id=edge.source_step_id,
                source_event_id=edge.source_event_id,
                tool_name=tool_name,
                provenance=edge.provenance,
                confidence=edge.confidence,
                evidence_event_ids=list(edge.evidence_event_ids),
            )
        )
    return operations
