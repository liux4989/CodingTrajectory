"""Analysis-oriented overview projections built from canonical resources."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from coding_trajectory.enrichment.codex import CodexWorkflowPlugin
from coding_trajectory.enrichment.sidecars import collect_sidecars
from coding_trajectory.enrichment.structure import build_trajectory_structure
from coding_trajectory.enrichment.structure_models import CrossSessionOperation, SessionTreeNode, TrajectoryStructure
from coding_trajectory.ingestion.models import Event, Session, Step, StepToolItem, Trajectory, Turn
from coding_trajectory.query import DocumentStore
from coding_trajectory.service import format_datetime, prune_nones

_OVERVIEW_PLUGINS = [CodexWorkflowPlugin()]


def _event_category(event_type: str) -> str:
    if event_type.startswith("user.prompt."):
        return "user_interaction"
    if event_type.startswith("tool.call."):
        return "tool_call"
    if event_type.startswith("llm."):
        return "llm_inference"
    if event_type.startswith("permission."):
        return "permission"
    if event_type.startswith("background_task."):
        return "background_task"
    if event_type.startswith("context.compaction."):
        return "context_compaction"
    return "session"


def _extract_user_request(event: Event | None) -> str | None:
    if event is None:
        return None
    for key in ("text", "message", "content"):
        value = event.payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _event_ref(event: Event) -> dict[str, Any]:
    return {
        "event_id": str(event.event_id),
        "type": event.type.value,
        "category": _event_category(event.type.value),
    }


def _cross_session_operation_status(operation: CrossSessionOperation) -> str:
    return "completed" if operation.provenance == "observed" else "inferred"


def _step_event_ids_from_source(store: DocumentStore, source_step_id: str | None) -> list[str]:
    if source_step_id is None:
        return []
    try:
        step = store.get_step(UUID(source_step_id))
    except Exception:
        return []
    return [str(event_id) for event_id in step.event_ids]


def _session_operation_from_cross_session(
    operation: CrossSessionOperation,
    *,
    session: Session,
) -> dict[str, Any]:
    related_session_ids = [
        str(session_id)
        for session_id in [operation.source_session_id, operation.target_session_id]
        if session_id != session.session_id
    ]
    event_ids = [str(event_id) for event_id in operation.evidence_event_ids]
    if operation.source_event_id is not None:
        source_event_id = str(operation.source_event_id)
        if source_event_id not in event_ids:
            event_ids.append(source_event_id)
    return prune_nones(
        {
            "operation_id": f"trajectory-op:{operation.index}",
            "level": "session",
            "kind": operation.edge_type,
            "vendor": session.vendor.value,
            "status": _cross_session_operation_status(operation),
            "title": operation.tool_name or operation.edge_type.replace("_", " "),
            "related_session_ids": related_session_ids or None,
            "turn_ids": [str(operation.source_turn_id)] if operation.source_turn_id else None,
            "step_ids": [str(operation.source_step_id)] if operation.source_step_id else None,
            "event_ids": event_ids or None,
            "metadata": prune_nones(
                {
                    "source_session_id": str(operation.source_session_id),
                    "target_session_id": str(operation.target_session_id),
                    "provenance": operation.provenance,
                    "confidence": operation.confidence,
                    "tool_name": operation.tool_name,
                }
            )
            or None,
        }
    )


def _session_operations(session: Session, *, store: DocumentStore, structure: TrajectoryStructure) -> list[dict[str, Any]]:
    operations = [
        _session_operation_from_cross_session(operation, session=session)
        for operation in structure.operations
        if operation.source_session_id == session.session_id or operation.target_session_id == session.session_id
    ]

    sidecars = collect_sidecars(_OVERVIEW_PLUGINS, session, store=store)
    codex_workflow = sidecars.namespaces.get("codex.workflow")
    if codex_workflow is None:
        return operations

    data = codex_workflow.data
    for index, plan in enumerate(data.get("plans", [])):
        source_step_id = plan.get("source_step_id")
        operations.append(
            prune_nones(
                {
                    "operation_id": f"codex-plan:{session.session_id}:{index}",
                    "level": "session",
                    "kind": "plan_update",
                    "vendor": session.vendor.value,
                    "status": "completed",
                    "title": "update_plan",
                    "step_ids": [source_step_id] if isinstance(source_step_id, str) else None,
                    "event_ids": _step_event_ids_from_source(store, source_step_id) or None,
                    "metadata": prune_nones(
                        {
                            "tool_name": plan.get("tool_name"),
                            "tool_call_id": plan.get("tool_call_id"),
                            "explanation": plan.get("explanation"),
                            "items": plan.get("items"),
                        }
                    )
                    or None,
                }
            )
        )

    for index, spawned in enumerate(data.get("spawned_agents", [])):
        source_step_id = spawned.get("source_step_id")
        operations.append(
            prune_nones(
                {
                    "operation_id": f"codex-spawn:{session.session_id}:{index}",
                    "level": "session",
                    "kind": "spawn_subsession",
                    "vendor": session.vendor.value,
                    "status": "completed",
                    "title": spawned.get("nickname") or spawned.get("tool_name") or "spawn_agent",
                    "step_ids": [source_step_id] if isinstance(source_step_id, str) else None,
                    "event_ids": _step_event_ids_from_source(store, source_step_id) or None,
                    "metadata": prune_nones(
                        {
                            "tool_name": spawned.get("tool_name"),
                            "tool_call_id": spawned.get("tool_call_id"),
                            "agent_type": spawned.get("agent_type"),
                            "role": spawned.get("role"),
                            "nickname": spawned.get("nickname"),
                            "agent_id": spawned.get("agent_id"),
                        }
                    )
                    or None,
                }
            )
        )

    return operations


def _step_operation_kind(tool_name: str | None, *, has_target_sessions: bool) -> str:
    if has_target_sessions:
        return "spawn_subsession"
    if tool_name == "update_plan":
        return "plan_update"
    if tool_name == "task":
        return "task"
    return "tool_call"


def _step_operations(step: Step, *, store: DocumentStore, structure: TrajectoryStructure) -> list[dict[str, Any]]:
    target_session_ids = [
        str(operation.target_session_id)
        for operation in structure.operations
        if operation.source_step_id == step.step_id
    ]
    operations: list[dict[str, Any]] = []
    for index, item in enumerate(step.items):
        if not isinstance(item, StepToolItem):
            continue
        operations.append(
            prune_nones(
                {
                    "operation_id": item.tool_call_id or f"step-tool:{step.step_id}:{index}",
                    "level": "step",
                    "kind": _step_operation_kind(item.tool_name, has_target_sessions=bool(target_session_ids)),
                    "vendor": step.vendor.value,
                    "status": item.status.value,
                    "title": item.tool_name or "tool_call",
                    "target_session_ids": target_session_ids or None,
                    "step_ids": [str(step.step_id)],
                    "event_ids": [str(event_id) for event_id in item.event_ids] or None,
                    "tool_name": item.tool_name,
                    "metadata": prune_nones(
                        {
                            "input": item.input if isinstance(item.input, dict) else None,
                            "output": item.output if isinstance(item.output, dict) else None,
                        }
                    )
                    or None,
                }
            )
        )
    return operations


def _build_step_overview(step: Step, *, store: DocumentStore, structure: TrajectoryStructure | None = None) -> dict[str, Any]:
    events = [store.get_event(event_id) for event_id in step.event_ids]
    if structure is None:
        session = store.get_session(step.session_id)
        structure = build_trajectory_structure(store.get_trajectory(session.trajectory_id))
    return prune_nones(
        {
            "step_id": str(step.step_id),
            "session_id": str(step.session_id),
            "turn_id": str(step.turn_id),
            "sequence": step.sequence,
            "timestamp": format_datetime(step.timestamp),
            "vendor": step.vendor.value,
            "items": [item.model_dump(mode="json") for item in step.items],
            "event_refs": [_event_ref(event) for event in events],
            "operations": _step_operations(step, store=store, structure=structure) or None,
        }
    )


def _tool_actions(step: Step) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for item in step.items:
        if not isinstance(item, StepToolItem):
            continue
        actions.append(
            prune_nones(
                {
                    "tool_name": item.tool_name,
                    "tool_call_id": item.tool_call_id,
                    "status": item.status.value,
                }
            )
        )
    return actions


def build_turn_overview(turn: Turn, *, store: DocumentStore) -> dict[str, Any]:
    request_event = store.get_event(turn.user_request_event_id) if turn.user_request_event_id is not None else None
    tool_actions = [action for step in turn.steps for action in _tool_actions(step)]
    session = store.get_session(turn.session_id)
    structure = build_trajectory_structure(store.get_trajectory(session.trajectory_id))
    return prune_nones(
        {
            "turn": prune_nones(
                {
                    "turn_id": str(turn.turn_id),
                    "sequence": turn.sequence,
                    "started_at": format_datetime(turn.started_at),
                    "ended_at": format_datetime(turn.ended_at),
                    "session_id": str(turn.session_id),
                    "user_request": _extract_user_request(request_event),
                    "tool_actions": tool_actions or None,
                    "steps": [_build_step_overview(step, store=store, structure=structure) for step in turn.steps],
                }
            )
        }
    )


def build_session_overview(
    session: Session,
    *,
    store: DocumentStore,
    structure_node: SessionTreeNode | None = None,
    structure: TrajectoryStructure | None = None,
) -> dict[str, Any]:
    structure = structure or build_trajectory_structure(store.get_trajectory(session.trajectory_id))
    context: dict[str, Any] | None = None
    if structure_node is not None:
        context = prune_nones(
            {
                "incoming_edge_type": structure_node.incoming_edge_type,
                "is_root": structure_node.is_root,
                "is_leaf": structure_node.is_leaf,
                "parent_session_id": str(structure_node.parent_session_id) if structure_node.parent_session_id else None,
            }
        ) or None
    return {
        "session": prune_nones(
            {
                "session_id": str(session.session_id),
                "trajectory_id": str(session.trajectory_id),
                "parent_session_id": str(session.parent_session_id) if session.parent_session_id else None,
                "vendor": session.vendor.value,
                "agent_name": session.agent_name,
                "started_at": format_datetime(session.started_at),
                "ended_at": format_datetime(session.ended_at),
                "context": context,
            }
        ),
        "operations": _session_operations(session, store=store, structure=structure) or None,
        "turns": [build_turn_overview(turn, store=store)["turn"] for turn in session.turns],
    }


def build_trajectory_overview(
    trajectory: Trajectory,
    *,
    store: DocumentStore,
    structure: TrajectoryStructure | None = None,
) -> dict[str, Any]:
    structure = structure or build_trajectory_structure(trajectory)
    tree = structure.session_tree
    sessions_by_id = {session.session_id: session for session in trajectory.sessions}

    def build_node(session_id: UUID) -> dict[str, Any]:
        session = sessions_by_id[session_id]
        node = tree.nodes_by_session_id.get(session_id)
        overview = build_session_overview(session, store=store, structure_node=node, structure=structure)
        child_ids = node.child_session_ids if node else []
        return {
            "session": overview["session"],
            "operations": overview.get("operations", []),
            "turns": overview["turns"],
            "children": [build_node(child_id) for child_id in child_ids],
        }

    return {
        "trajectory": prune_nones(
            {
                "trajectory_id": str(trajectory.trajectory_id),
                "project_identifier": trajectory.project_identifier,
                "session_count": trajectory.summary.session_count if trajectory.summary else None,
                "turn_count": trajectory.summary.turn_count if trajectory.summary else None,
                "vendors": [vendor.value for vendor in trajectory.summary.vendors] if trajectory.summary else None,
                "context": prune_nones(
                    {
                        "multi_agent_mode": structure.multi_agent_mode,
                        "topology": structure.topology,
                        "has_observed_spawn": structure.has_observed_spawn,
                        "edge_type_counts": structure.edge_type_counts or None,
                    }
                )
                or None,
            }
        ),
        "operations": [op.model_dump(mode="json") for op in structure.operations],
        "tree": [build_node(root_id) for root_id in tree.root_session_ids],
        "notes": [note.model_dump(mode="json") for note in structure.notes],
    }

def build_step_overview(step: Step, *, store: DocumentStore) -> dict[str, Any]:
    return {"step": _build_step_overview(step, store=store)}
