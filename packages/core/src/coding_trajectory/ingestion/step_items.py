"""Helpers for constructing concise canonical step items."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from coding_trajectory.ingestion.models import StepItem, StepTextItem, StepToolItem, ToolStatus


def append_text_item(items: list[StepItem], text: str | None, *, event_ids: list[UUID] | None = None) -> None:
    if not text:
        return
    cleaned = text.strip()
    if not cleaned:
        return

    if items:
        last_item = items[-1]
        if isinstance(last_item, StepTextItem) and last_item.text == cleaned:
            if event_ids:
                last_item.event_ids.extend(event_id for event_id in event_ids if event_id not in last_item.event_ids)
            return

    items.append(StepTextItem(text=cleaned, event_ids=list(event_ids or [])))


def append_tool_item(
    items: list[StepItem],
    *,
    tool_name: str | None,
    tool_call_id: str | None = None,
    input: Any = None,
    output: Any = None,
    status: ToolStatus = ToolStatus.REQUESTED,
    event_ids: list[UUID] | None = None,
) -> StepToolItem:
    item = StepToolItem(
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        input=input,
        output=output,
        status=status,
        event_ids=list(event_ids or []),
    )
    items.append(item)
    return item


def update_tool_item(
    items: list[StepItem],
    *,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
    input: Any = None,
    output: Any = None,
    status: ToolStatus | None = None,
    event_ids: list[UUID] | None = None,
) -> None:
    event_ids = list(event_ids or [])

    for item in reversed(items):
        if not isinstance(item, StepToolItem):
            continue
        if tool_call_id and item.tool_call_id == tool_call_id:
            _merge_tool_item(
                item,
                tool_name=tool_name,
                input=input,
                output=output,
                status=status,
                event_ids=event_ids,
            )
            return

    append_tool_item(
        items,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        input=input,
        output=output,
        status=status or ToolStatus.REQUESTED,
        event_ids=event_ids,
    )

def _merge_tool_item(
    item: StepToolItem,
    *,
    tool_name: str | None,
    input: Any,
    output: Any,
    status: ToolStatus | None,
    event_ids: list[UUID],
) -> None:
    if tool_name and not item.tool_name:
        item.tool_name = tool_name
    if input is not None and item.input is None:
        item.input = input
    if output is not None:
        item.output = output
    if status is not None:
        item.status = status
    for event_id in event_ids:
        if event_id not in item.event_ids:
            item.event_ids.append(event_id)
