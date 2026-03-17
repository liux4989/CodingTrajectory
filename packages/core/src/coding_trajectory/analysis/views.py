"""View builders for the CLI navigation tree and step evidence."""

from __future__ import annotations

from typing import Any
from uuid import UUID

_MISSING = object()


def _resolve_path(obj: Any, path: str) -> Any:
    """Resolve a dot-separated path into a nested dict, returning _MISSING if absent."""
    cur: Any = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return _MISSING
        cur = cur.get(part, _MISSING)
        if cur is _MISSING:
            return _MISSING
    return cur


def _match_filter(shape: dict[str, Any], expr: str) -> bool:
    """Evaluate one filter expression against a step shape.

    Syntax:
        key=value   exact match (string comparison)
        key=*       key exists and is not None
        key=!       key absent or None
    """
    if "=" not in expr:
        raise ValueError(f"invalid filter expression {expr!r}: expected key=value, key=*, or key=!")
    key, _, value = expr.partition("=")
    resolved = _resolve_path(shape, key)
    if value == "*":
        return resolved is not _MISSING and resolved is not None
    if value == "!":
        return resolved is _MISSING or resolved is None
    return str(resolved) == value

from coding_trajectory.analysis.concepts import TOOL_CONCEPT_MAP
from coding_trajectory.analysis.structure import build_trajectory_structure
from coding_trajectory.analysis.structure_models import TrajectoryStructure
from coding_trajectory.ingestion.models import Session, Step, StepTextItem, StepToolItem, Trajectory, Turn
from coding_trajectory.query import DocumentStore
from coding_trajectory.service import prune_nones


def _extract_user_request(store: DocumentStore, turn: Turn) -> str | None:
    if turn.user_request_event_id is None:
        return None
    try:
        event = store.get_event(turn.user_request_event_id)
    except Exception:
        return None
    for key in ("text", "message", "content"):
        value = event.payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _classify_step(step: Step) -> str:
    tool_items = [item for item in step.items if isinstance(item, StepToolItem)]
    if not tool_items:
        return "assistant_response"
    for item in tool_items:
        if item.tool_name:
            concept = TOOL_CONCEPT_MAP.get(item.tool_name)
            if concept:
                return concept
    return "tool_call"


def _session_connection(session: Session, *, structure: TrajectoryStructure) -> dict[str, Any]:
    node = structure.session_tree.nodes_by_session_id.get(session.session_id)
    if node is None or (node.is_root and not node.incoming_edge_type):
        return {"role": "main"}
    return prune_nones({
        "relationship": node.incoming_edge_type,
        "parent_session_id": str(node.parent_session_id) if node.parent_session_id else None,
    })


def build_trajectory_overview(trajectory: Trajectory, *, store: DocumentStore) -> dict[str, Any]:
    structure = build_trajectory_structure(trajectory)
    sessions_by_id = {s.session_id: s for s in trajectory.sessions}

    ordered: list[dict[str, Any]] = []
    visited: set[UUID] = set()
    queue = list(structure.session_tree.root_session_ids)
    while queue:
        session_id = queue.pop(0)
        if session_id in visited:
            continue
        visited.add(session_id)
        session = sessions_by_id.get(session_id)
        if session:
            ordered.append(_session_nav_node(session, store=store, structure=structure))
        node = structure.session_tree.nodes_by_session_id.get(session_id)
        if node:
            queue.extend(node.child_session_ids)

    for session in trajectory.sessions:
        if session.session_id not in visited:
            ordered.append(_session_nav_node(session, store=store, structure=structure))

    return {
        "trajectory_id": str(trajectory.trajectory_id),
        "sessions": ordered,
    }


def _session_nav_node(session: Session, *, store: DocumentStore, structure: TrajectoryStructure) -> dict[str, Any]:
    return {
        "session_id": str(session.session_id),
        "connection": _session_connection(session, structure=structure),
        "turns": [_turn_nav_node(turn, store=store) for turn in session.turns],
    }


def _turn_nav_node(turn: Turn, *, store: DocumentStore) -> dict[str, Any]:
    return prune_nones({
        "turn_id": str(turn.turn_id),
        "user_request": _extract_user_request(store, turn),
        "steps": [{"step_id": str(step.step_id), "type": _classify_step(step)} for step in turn.steps],
    })


# ---------------------------------------------------------------------------
# Step details
# ---------------------------------------------------------------------------

def _assistant_response_shape(step: Step) -> dict[str, Any]:
    text_items = [item for item in step.items if isinstance(item, StepTextItem)]
    text = "\n".join(item.text for item in text_items if item.text).strip() or None
    stop_reason = step.vendor_data.get("stop_reason")
    usage_raw = step.vendor_data.get("usage")
    usage = usage_raw if isinstance(usage_raw, dict) else None
    return prune_nones({"text": text, "stop_reason": stop_reason, "usage": usage})


def _tool_call_shape(tool_items: list[StepToolItem]) -> dict[str, Any]:
    if len(tool_items) == 1:
        item = tool_items[0]
        return prune_nones({
            "tool_name": item.tool_name,
            "tool_input": item.input,
            "tool_output": item.output,
        })
    return {
        "tools": [
            prune_nones({
                "tool_name": item.tool_name,
                "tool_input": item.input,
                "tool_output": item.output,
            })
            for item in tool_items
        ]
    }


def _lookup_target_session(step: Step, *, store: DocumentStore, edge_type: str) -> str | None:
    try:
        session = store.get_session(step.session_id)
        trajectory = store.get_trajectory(session.trajectory_id)
        structure = build_trajectory_structure(trajectory)
        for op in structure.operations:
            if op.source_step_id == step.step_id and op.edge_type == edge_type:
                return str(op.target_session_id)
    except Exception:
        pass
    return None


def _plan_subagent_shape(step: Step, *, store: DocumentStore) -> dict[str, Any]:
    tool_items = [item for item in step.items if isinstance(item, StepToolItem)]
    spawn_item = next(
        (item for item in tool_items if TOOL_CONCEPT_MAP.get(item.tool_name or "") == "plan_subagent"),
        tool_items[0] if tool_items else None,
    )
    agent_session_id = _lookup_target_session(step, store=store, edge_type="spawned_subagent")
    if spawn_item is None:
        return prune_nones({"agent_session_id": agent_session_id})
    return prune_nones({
        "agent_input": spawn_item.input,
        "agent_output": spawn_item.output,
        "agent_session_id": agent_session_id,
    })


def _todo_list_shape(tool_items: list[StepToolItem]) -> dict[str, Any]:
    return _tool_call_shape(tool_items)


def _session_handoff_shape(step: Step, *, store: DocumentStore) -> dict[str, Any]:
    tool_items = [item for item in step.items if isinstance(item, StepToolItem)]
    handoff_item = next(
        (item for item in tool_items if TOOL_CONCEPT_MAP.get(item.tool_name or "") == "session_handoff"),
        tool_items[0] if tool_items else None,
    )
    handoff_session_id = _lookup_target_session(step, store=store, edge_type="handoff_to")
    if handoff_item is None:
        return prune_nones({"handoff_session_id": handoff_session_id})
    return prune_nones({
        "handoff_input": handoff_item.input,
        "handoff_session_id": handoff_session_id,
    })


def build_step_details(step: Step, *, store: DocumentStore) -> dict[str, Any]:
    step_type = _classify_step(step)
    tool_items = [item for item in step.items if isinstance(item, StepToolItem)]

    if step_type == "assistant_response":
        operations: list[str] = ["text_reply"]
        shape = _assistant_response_shape(step)
    elif step_type == "plan_subagent":
        operations = ["spawn", "collect_result"]
        shape = _plan_subagent_shape(step, store=store)
    elif step_type == "todo_list":
        operations = ["update"]
        shape = _todo_list_shape(tool_items)
    elif step_type == "session_handoff":
        operations = ["handoff"]
        shape = _session_handoff_shape(step, store=store)
    else:
        operations = [item.tool_name for item in tool_items if item.tool_name]
        shape = _tool_call_shape(tool_items)

    return prune_nones({
        "step_id": str(step.step_id),
        "type": step_type,
        "operations": operations or None,
        "shape": shape or None,
        "event_ids": [str(eid) for eid in step.event_ids] or None,
    })


# ---------------------------------------------------------------------------
# Trajectory scan
# ---------------------------------------------------------------------------

def build_trajectory_scan(
    trajectory: Trajectory,
    *,
    store: DocumentStore,
    step_type: str,
    filters: list[str] | None = None,
) -> dict[str, Any]:
    """Flatten the trajectory tree and return all steps matching *step_type*.

    Each ``--filter key=value`` expression is ANDed and applied against the
    step's ``shape`` dict.  Supports dot-path keys (e.g. ``tool_output.error``),
    ``key=*`` (exists), and ``key=!`` (absent).
    """
    matches: list[dict[str, Any]] = []

    for session in trajectory.sessions:
        for turn in session.turns:
            user_request = _extract_user_request(store, turn)
            for step in turn.steps:
                if _classify_step(step) != step_type:
                    continue
                detail = build_step_details(step, store=store)
                shape: dict[str, Any] = detail.get("shape") or {}
                if filters:
                    if not all(_match_filter(shape, f) for f in filters):
                        continue
                matches.append(prune_nones({
                    "step_id": str(step.step_id),
                    "session_id": str(session.session_id),
                    "turn_id": str(turn.turn_id),
                    "user_request": user_request,
                    "shape": shape or None,
                }))

    return {
        "trajectory_id": str(trajectory.trajectory_id),
        "type": step_type,
        "matches": matches,
    }
