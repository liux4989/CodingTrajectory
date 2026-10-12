"""Shared identity-based projections of direct items and script executions."""

from __future__ import annotations

from typing import Any

from coding_trajectory.analysis.projection_utils import truncate_text_preview
from coding_trajectory.ingestion.common import prune_nones
from coding_trajectory.ingestion.execution_models import (
    ExecutionUnit,
    ToolScriptExecution,
)
from coding_trajectory.ingestion.models import (
    AgentMessageItem,
    Item,
    is_tool_shaped_item,
)


def public_activity_outcome(value: Any) -> str | None:
    return str(value) if value in {"succeeded", "failed"} else None


def is_control_only_activity_cell(item: dict[str, Any]) -> bool:
    # Control ownership is represented by the execution layer, not tool names.
    return False


def is_low_value_activity_cell(item: dict[str, Any]) -> bool:
    return (
        item.get("type") == "tool_call"
        and not item.get("name")
        and not item.get("description")
    )


def project_tool_activity(item: Item) -> dict[str, Any] | None:
    if not is_tool_shaped_item(item):
        return None
    from coding_trajectory.analysis.tool_summary import summarize_tool_call

    summary = summarize_tool_call(item)
    if summary is None:
        return None
    return {"type": "tool_call", **summary, "item_id": str(item.item_id)}


def build_flows(
    items: list[Item],
    *,
    flatten_commands: bool = False,
    execution_units: list[ExecutionUnit] | None = None,
) -> list[dict[str, Any]]:
    scripts = {
        u.item_id: u
        for u in execution_units or []
        if isinstance(u, ToolScriptExecution)
    }
    owned = {
        identity
        for u in scripts.values()
        for identity in u.item_ids
        if identity != u.item_id
    }
    result = []
    roots = {unit.unit_id for unit in execution_units or []}
    for item in items:
        identity = str(item.item_id)
        if execution_units and identity not in roots:
            continue
        if identity in scripts:
            script = scripts[identity]
            result.append(
                {
                    "type": "tool_script",
                    "name": script.tool_name,
                    "item_id": identity,
                    "item_ids": script.item_ids,
                    "script": script.model_dump(mode="json"),
                }
            )
        elif identity in owned:
            continue
        elif is_tool_shaped_item(item):
            if activity := project_tool_activity(item):
                result.append(activity)
        elif isinstance(item, AgentMessageItem):
            text = item.measurements.text_preview if item.measurements else item.text
            if text:
                result.append(
                    {"type": "assistant_response", "text": text, "item_id": identity}
                )
    return result


def build_overview_flows(
    items: list[Item],
    *,
    flatten_commands: bool = False,
    execution_units: list[ExecutionUnit] | None = None,
) -> list[dict[str, Any]]:
    result = []
    for item in build_flows(
        items, flatten_commands=flatten_commands, execution_units=execution_units
    ):
        if item["type"] == "assistant_response":
            result.append(
                {
                    "text": truncate_text_preview(item["text"], max_len=220),
                    "item_ids": [item["item_id"]],
                }
            )
        elif item["type"] == "tool_script":
            result.append(
                {
                    "tool": item.get("name") or "Tool script",
                    "kind": "tool_script",
                    "item_ids": item["item_ids"],
                    "script_id": item["script"]["unit_id"],
                    "target": item["script"].get("source_preview"),
                    "status": item["script"]["result"]["lifecycle"],
                }
            )
        else:
            result.append(
                prune_nones(
                    {
                        "tool": item.get("name"),
                        "target": item.get("description"),
                        "status": item.get("status"),
                        "item_ids": [item["item_id"]],
                        "outcome": public_activity_outcome(
                            item.get("activity_outcome")
                        ),
                    }
                )
            )
    return result
