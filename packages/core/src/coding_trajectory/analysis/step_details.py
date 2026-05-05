"""Step-details projection helpers."""

from __future__ import annotations

from typing import Any

from coding_trajectory.analysis.concepts import TOOL_CONCEPT_MAP, StepType
from coding_trajectory.analysis.projection_utils import truncate_with_ref
from coding_trajectory.ingestion.common import prune_nones
from coding_trajectory.ingestion.indexes import TrajectoryIndex, build_trajectory_index, target_session_id_for_step
from coding_trajectory.ingestion.models import Step, StepTextItem, StepToolItem, Trajectory


def build_step_details(step: Step, *, trajectory: Trajectory) -> dict[str, Any]:
    step_type = _classify_step(step)
    tool_items = [item for item in step.items if isinstance(item, StepToolItem)]
    index = build_trajectory_index(trajectory)

    if step_type == StepType.ASSISTANT_RESPONSE:
        operations: list[str] = ["text_reply"]
        shape = _assistant_response_shape(step)
    elif step_type == StepType.PLAN_SUBAGENT:
        operations = ["spawn", "collect_result"]
        shape = _plan_subagent_shape(step, index=index)
    elif step_type == StepType.TODO_LIST:
        operations = ["update"]
        shape = _todo_list_shape(tool_items)
    elif step_type == StepType.SESSION_HANDOFF:
        operations = ["handoff"]
        shape = _session_handoff_shape(step, index=index)
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


def _classify_step(step: Step) -> StepType:
    tool_items = [item for item in step.items if isinstance(item, StepToolItem)]
    if not tool_items:
        return StepType.ASSISTANT_RESPONSE
    for item in tool_items:
        if item.tool_name:
            concept = TOOL_CONCEPT_MAP.get(item.tool_name)
            if concept is not None:
                return concept
    return StepType.TOOL_CALL


def _assistant_response_shape(step: Step) -> dict[str, Any]:
    text_items = [item for item in step.items if isinstance(item, StepTextItem)]
    texts = [item.text for item in text_items if item.text]
    stop_reason = step.vendor_data.get("stop_reason")
    metrics = step.vendor_data.get("metrics")
    usage_raw = metrics.get("usage") if isinstance(metrics, dict) else None
    usage = usage_raw if isinstance(usage_raw, dict) else None
    return prune_nones({"texts": texts or None, "stop_reason": stop_reason, "usage": usage})


def _tool_call_shape(tool_items: list[StepToolItem]) -> dict[str, Any]:
    if len(tool_items) == 1:
        item = tool_items[0]
        return truncate_with_ref(
            prune_nones(
                {
                    "tool_name": item.tool_name,
                    "tool_input": item.input,
                    "tool_output": item.output,
                }
            ),
            item.event_ids,
        )
    return {
        "tools": [
            truncate_with_ref(
                prune_nones(
                    {
                        "tool_name": item.tool_name,
                        "tool_input": item.input,
                        "tool_output": item.output,
                    }
                ),
                item.event_ids,
            )
            for item in tool_items
        ]
    }


def _lookup_target_session(step: Step, *, index: TrajectoryIndex, edge_type: str) -> str | None:
    target_session_id = target_session_id_for_step(index, step, edge_type=edge_type)
    return str(target_session_id) if target_session_id is not None else None


def _plan_subagent_shape(step: Step, *, index: TrajectoryIndex) -> dict[str, Any]:
    tool_items = [item for item in step.items if isinstance(item, StepToolItem)]
    spawn_item = next(
        (item for item in tool_items if TOOL_CONCEPT_MAP.get(item.tool_name or "") == StepType.PLAN_SUBAGENT),
        tool_items[0] if tool_items else None,
    )
    agent_session_id = _lookup_target_session(step, index=index, edge_type="spawned_subagent")
    if spawn_item is None:
        return prune_nones({"agent_session_id": agent_session_id})
    return prune_nones({
        "agent_input": spawn_item.input,
        "agent_output": spawn_item.output,
        "agent_session_id": agent_session_id,
    })


def _todo_list_shape(tool_items: list[StepToolItem]) -> dict[str, Any]:
    return _tool_call_shape(tool_items)


def _session_handoff_shape(step: Step, *, index: TrajectoryIndex) -> dict[str, Any]:
    tool_items = [item for item in step.items if isinstance(item, StepToolItem)]
    handoff_item = next(
        (item for item in tool_items if TOOL_CONCEPT_MAP.get(item.tool_name or "") == StepType.SESSION_HANDOFF),
        tool_items[0] if tool_items else None,
    )
    handoff_session_id = _lookup_target_session(step, index=index, edge_type="handoff_to")
    if handoff_item is None:
        return prune_nones({"handoff_session_id": handoff_session_id})
    return truncate_with_ref(
        prune_nones(
            {
                "handoff_input": handoff_item.input,
                "handoff_session_id": handoff_session_id,
            }
        ),
        handoff_item.event_ids,
    )
