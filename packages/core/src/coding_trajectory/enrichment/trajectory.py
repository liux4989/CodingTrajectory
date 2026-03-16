"""Trajectory enrichment helpers."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from typing import Iterable
from uuid import UUID

from coding_trajectory.ingestion.models import Trajectory
from coding_trajectory.query import DocumentStore

from .base import EnrichmentPlugin
from .models import EnrichedTrajectory, EnrichmentNote, EnrichmentOverlay


def build_enriched_trajectory(
    canonical: Trajectory,
    *,
    structural: dict | None = None,
    derived: dict | None = None,
    agent_specific: dict | None = None,
    notes: Iterable[EnrichmentNote] | None = None,
    plugins: Iterable[EnrichmentPlugin] = (),
    store: DocumentStore | None = None,
) -> EnrichedTrajectory:
    overlay = EnrichmentOverlay(
        structural=dict(structural or {}),
        derived=dict(derived or {}),
        agent_specific=dict(agent_specific or {}),
        notes=list(notes or []),
    )

    for plugin in plugins:
        plugin_overlay = plugin.enrich_trajectory(canonical, store=store)
        _merge_plugin_overlay(overlay, plugin.namespace, plugin_overlay)

    return EnrichedTrajectory(trajectory_id=canonical.trajectory_id, enrichment=overlay)


def build_default_trajectory_enrichment(
    canonical: Trajectory,
    *,
    plugins: Iterable[EnrichmentPlugin] = (),
    store: DocumentStore | None = None,
) -> EnrichedTrajectory:
    session_tree = _build_session_tree(canonical)
    operations = _build_operations(canonical)
    derived = _build_derived(canonical, session_tree=session_tree, operations=operations)
    agent_specific = _build_agent_specific(canonical)
    notes = _build_notes(canonical)

    return build_enriched_trajectory(
        canonical,
        structural={
            "session_tree": session_tree,
            "operations": operations,
        },
        derived=derived,
        agent_specific=agent_specific,
        notes=notes,
        plugins=plugins,
        store=store,
    )


def _build_session_tree(canonical: Trajectory) -> dict[str, object]:
    parent_by_session: dict[UUID, UUID | None] = {session.session_id: session.parent_session_id for session in canonical.sessions}
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
    nodes = []
    leaves = []

    for session in canonical.sessions:
        session_id = session.session_id
        children = child_ids_by_session.get(session_id, [])
        is_root = session_id in roots
        is_leaf = not children
        if is_leaf:
            leaves.append(session_id)
        incoming_edge = incoming_edge_by_session.get(session_id)
        nodes.append(
            {
                "session_id": session_id,
                "parent_session_id": parent_by_session.get(session_id),
                "depth": depths.get(session_id, 0),
                "incoming_edge_type": incoming_edge.type if incoming_edge else None,
                "is_root": is_root,
                "is_leaf": is_leaf,
            }
        )

    nodes.sort(key=lambda item: (item["depth"], str(item["session_id"])))
    roots.sort(key=str)
    leaves.sort(key=str)

    return {
        "root_session_id": canonical.summary.root_session_id if canonical.summary else (roots[0] if roots else None),
        "roots": roots,
        "leaves": leaves,
        "nodes": nodes,
    }


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


def _build_operations(canonical: Trajectory) -> list[dict[str, object]]:
    operations: list[dict[str, object]] = []
    for index, edge in enumerate(canonical.edges):
        tool_name = None
        if edge.metadata:
            raw_tool_name = edge.metadata.get("tool_name")
            if isinstance(raw_tool_name, str) and raw_tool_name.strip():
                tool_name = raw_tool_name
        operations.append(
            {
                "index": index,
                "kind": "cross_session_edge",
                "edge_type": edge.type,
                "source_session_id": edge.source_session_id,
                "target_session_id": edge.target_session_id,
                "source_turn_id": edge.source_turn_id,
                "source_step_id": edge.source_step_id,
                "source_event_id": edge.source_event_id,
                "tool_name": tool_name,
                "provenance": edge.provenance,
                "confidence": edge.confidence,
                "evidence_event_ids": list(edge.evidence_event_ids),
            }
        )
    return operations


def _build_derived(
    canonical: Trajectory,
    *,
    session_tree: dict[str, object],
    operations: list[dict[str, object]],
) -> dict[str, object]:
    child_counts = Counter(
        edge.source_session_id
        for edge in canonical.edges
    )
    edge_type_counts = Counter(edge.type for edge in canonical.edges)
    vendors = canonical.summary.vendors if canonical.summary else []
    topology = "single_session"
    if len(canonical.sessions) > 1:
        topology = "branching" if any(count > 1 for count in child_counts.values()) else "linear"

    return {
        "multi_agent_mode": "single_session" if len(canonical.sessions) <= 1 else "cross_session",
        "topology": topology,
        "root_session_id": session_tree.get("root_session_id"),
        "leaf_session_ids": session_tree.get("leaves", []),
        "has_observed_spawn": any(
            edge.type == "spawned_subagent" and edge.provenance == "observed"
            for edge in canonical.edges
        ),
        "vendor_set": [vendor.value for vendor in vendors],
        "edge_type_counts": dict(sorted(edge_type_counts.items())),
        "operation_count": len(operations),
    }


def _build_agent_specific(canonical: Trajectory) -> dict[str, object]:
    payload: dict[str, object] = {}

    # --- Codex-specific session metadata ---
    collaboration_modes = sorted(
        {
            session.extensions.codex.collaboration_mode
            for session in canonical.sessions
            if session.extensions and session.extensions.codex and session.extensions.codex.collaboration_mode
        }
    )
    agent_roles = sorted(
        {
            session.extensions.codex.agent_role
            for session in canonical.sessions
            if session.extensions and session.extensions.codex and session.extensions.codex.agent_role
        }
    )
    if collaboration_modes or agent_roles:
        payload["codex"] = {}
        if collaboration_modes:
            payload["codex"]["collaboration_modes"] = collaboration_modes  # type: ignore[index]
        if agent_roles:
            payload["codex"]["agent_roles"] = agent_roles  # type: ignore[index]

    # --- Cross-vendor: spawned_subagent edges ---
    spawned_edges = [e for e in canonical.edges if e.type == "spawned_subagent"]
    if spawned_edges:
        payload["subagents"] = {
            "count": len(spawned_edges),
            "links": [
                {
                    "source_session_id": e.source_session_id,
                    "target_session_id": e.target_session_id,
                    "source_step_id": e.source_step_id,
                    "source_event_id": e.source_event_id,
                }
                for e in spawned_edges
            ],
        }

    # --- Claude Code: teammate_of edges ---
    teammate_edges = [e for e in canonical.edges if e.type == "teammate_of"]
    if teammate_edges:
        team_names = sorted(
            {
                session.extensions.claude_code.team_name
                for session in canonical.sessions
                if session.extensions
                and session.extensions.claude_code
                and session.extensions.claude_code.team_name
            }
        )
        payload["claude_code"] = {
            "teammates": {
                "count": len(teammate_edges),
                "team_names": team_names,
                "links": [
                    {
                        "source_session_id": e.source_session_id,
                        "target_session_id": e.target_session_id,
                    }
                    for e in teammate_edges
                ],
            }
        }

    return payload


def _build_notes(canonical: Trajectory) -> list[EnrichmentNote]:
    notes: list[EnrichmentNote] = []
    for edge in canonical.edges:
        if edge.provenance == "observed" and edge.confidence == "high":
            continue
        notes.append(
            EnrichmentNote(
                subject="trajectory_edge",
                message=(
                    f"{edge.type} inferred from session linkage"
                    if edge.provenance != "observed"
                    else f"{edge.type} retained with {edge.confidence} confidence"
                ),
                provenance=edge.provenance,
                confidence=edge.confidence,
                evidence_event_ids=list(edge.evidence_event_ids),
            )
        )
    return notes


def _merge_plugin_overlay(
    target: EnrichmentOverlay,
    namespace: str,
    overlay: EnrichmentOverlay | None,
) -> None:
    if overlay is None:
        return
    if overlay.structural:
        target.structural.update(overlay.structural)
    if overlay.derived:
        target.derived.update(overlay.derived)
    if overlay.agent_specific:
        target.agent_specific.update(overlay.agent_specific)
    if overlay.notes:
        target.notes.extend(overlay.notes)
    plugin_payload: dict[str, object] = {}
    if overlay.structural:
        plugin_payload["structural"] = overlay.structural
    if overlay.derived:
        plugin_payload["derived"] = overlay.derived
    if overlay.agent_specific:
        plugin_payload["agent_specific"] = overlay.agent_specific
    if overlay.notes:
        plugin_payload["notes"] = [note.model_dump(mode="json") for note in overlay.notes]
    if overlay.plugins:
        plugin_payload["plugins"] = overlay.plugins
    if plugin_payload:
        target.plugins[namespace] = plugin_payload
