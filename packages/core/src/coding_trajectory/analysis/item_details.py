"""Item-details projection helpers."""

from __future__ import annotations

from typing import Any

from coding_trajectory.analysis.concepts import TOOL_CONCEPT_MAP, ItemKind
from coding_trajectory.analysis.projection_utils import truncate_with_ref
from coding_trajectory.ingestion.common import prune_nones
from coding_trajectory.ingestion.indexes import (
    SessionGraphIndex,
    build_session_graph_index,
    target_session_id_for_item,
)
from coding_trajectory.ingestion.models import (
    AgentMessageItem,
    CommandExecutionItem,
    FileChangeItem,
    Item,
    PlanItem,
    ReasoningItem,
    SessionGraph,
    ToolCallItem,
)


def build_item_details(item: Item, *, session_graph: SessionGraph) -> dict[str, Any]:
    concept = _classify_item(item)
    index = build_session_graph_index(session_graph)

    if isinstance(item, AgentMessageItem):
        operations = ["text_reply"]
        shape = _agent_message_shape(item)
    elif isinstance(item, ReasoningItem):
        operations = ["reason"]
        shape = _reasoning_shape(item)
    elif isinstance(item, FileChangeItem):
        operations = [item.operation or (item.tool_name or "edit")]
        shape = _file_change_shape(item)
    elif isinstance(item, CommandExecutionItem):
        operations = ["execute"]
        shape = _command_execution_shape(item)
    elif isinstance(item, PlanItem):
        if concept == ItemKind.PLAN_SUBAGENT:
            operations = ["spawn", "collect_result"]
            shape = _plan_subagent_shape(item, index=index)
        elif concept == ItemKind.SESSION_HANDOFF:
            operations = ["handoff"]
            shape = _session_handoff_shape(item, index=index)
        else:
            operations = ["update"]
            shape = _plan_shape(item)
    else:
        tool_name = item.tool_name if isinstance(item, ToolCallItem) else None
        operations = [tool_name] if tool_name else None
        shape = _tool_call_shape(item)

    return prune_nones(
        {
            "item_id": str(item.item_id),
            "kind": item.kind,
            "type": concept,
            "operations": operations or None,
            "shape": shape or None,
            "event_ids": [str(eid) for eid in item.event_ids] or None,
        }
    )


def _classify_item(item: Item) -> ItemKind:
    if isinstance(item, AgentMessageItem):
        return ItemKind.ASSISTANT_RESPONSE
    if isinstance(item, ReasoningItem):
        return ItemKind.REASONING
    if isinstance(item, FileChangeItem):
        return ItemKind.FILE_CHANGE
    if isinstance(item, CommandExecutionItem):
        return ItemKind.COMMAND_EXECUTION

    tool_name = getattr(item, "tool_name", None)
    if tool_name:
        concept = TOOL_CONCEPT_MAP.get(tool_name)
        if concept is not None:
            return concept
    return ItemKind.TOOL_CALL


def _agent_message_shape(item: AgentMessageItem) -> dict[str, Any]:
    stop_reason = item.vendor_data.get("stop_reason")
    metrics = item.vendor_data.get("metrics")
    usage_raw = metrics.get("usage") if isinstance(metrics, dict) else None
    usage = usage_raw if isinstance(usage_raw, dict) else None
    return prune_nones(
        {
            "texts": [item.text] if item.text else None,
            "stop_reason": stop_reason,
            "usage": usage,
        }
    )


def _reasoning_shape(item: ReasoningItem) -> dict[str, Any]:
    return prune_nones({"text": item.text})


def _tool_call_shape(item: Item) -> dict[str, Any]:
    return truncate_with_ref(
        prune_nones(
            {
                "tool_name": getattr(item, "tool_name", None),
                "tool_input": getattr(item, "input", None),
                "tool_output": getattr(item, "output", None),
            }
        ),
        item.event_ids,
    )


def _file_change_shape(item: FileChangeItem) -> dict[str, Any]:
    return truncate_with_ref(
        prune_nones(
            {
                "tool_name": item.tool_name,
                "path": item.path,
                "operation": item.operation,
                "tool_input": item.input,
                "tool_output": item.output,
            }
        ),
        item.event_ids,
    )


def _command_execution_shape(item: CommandExecutionItem) -> dict[str, Any]:
    return truncate_with_ref(
        prune_nones(
            {
                "tool_name": item.tool_name,
                "command": item.command,
                "exit_code": item.exit_code,
                "output": item.output,
            }
        ),
        item.event_ids,
    )


def _plan_shape(item: PlanItem) -> dict[str, Any]:
    return _tool_call_shape(item)


def _lookup_target_session(
    item: Item, *, index: SessionGraphIndex, edge_type: str
) -> str | None:
    target_session_id = target_session_id_for_item(index, item, edge_type=edge_type)
    return str(target_session_id) if target_session_id is not None else None


def _plan_subagent_shape(item: Item, *, index: SessionGraphIndex) -> dict[str, Any]:
    agent_session_id = _lookup_target_session(
        item, index=index, edge_type="spawned_subagent"
    )
    return prune_nones(
        {
            "agent_input": getattr(item, "input", None),
            "agent_output": getattr(item, "output", None),
            "agent_session_id": agent_session_id,
        }
    )


def _session_handoff_shape(item: Item, *, index: SessionGraphIndex) -> dict[str, Any]:
    handoff_session_id = _lookup_target_session(
        item, index=index, edge_type="handoff_to"
    )
    return truncate_with_ref(
        prune_nones(
            {
                "handoff_input": getattr(item, "input", None),
                "handoff_session_id": handoff_session_id,
            }
        ),
        item.event_ids,
    )
