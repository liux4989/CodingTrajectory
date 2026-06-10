"""Helpers for constructing canonical turn items."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from coding_trajectory.ingestion.models import (
    AgentMessageItem,
    CommandExecutionItem,
    FileChangeItem,
    Item,
    PlanItem,
    ReasoningItem,
    ToolCallItem,
)


def append_agent_message(
    items: list[Item],
    *,
    session_id: UUID,
    turn_id: UUID,
    sequence: int,
    started_at: datetime,
    completed_at: datetime | None = None,
    text: str | None,
    event_ids: list[UUID] | None = None,
    vendor_data: dict[str, Any] | None = None,
) -> AgentMessageItem | None:
    if text is None:
        cleaned: str | None = None
    else:
        stripped = text.strip()
        cleaned = stripped or None

    if items:
        last = items[-1]
        if isinstance(last, AgentMessageItem) and last.text == cleaned:
            for event_id in event_ids or []:
                if event_id not in last.event_ids:
                    last.event_ids.append(event_id)
            if vendor_data:
                last.vendor_data.update({k: v for k, v in vendor_data.items() if v is not None})
            if completed_at is not None and (last.completed_at is None or completed_at > last.completed_at):
                last.completed_at = completed_at
            return last

    if cleaned is None and not vendor_data:
        return None

    item = AgentMessageItem(
        session_id=session_id,
        turn_id=turn_id,
        sequence=sequence,
        started_at=started_at,
        completed_at=completed_at,
        text=cleaned,
        event_ids=list(event_ids or []),
        vendor_data=dict(vendor_data or {}),
    )
    items.append(item)
    return item


def append_tool_call(
    items: list[Item],
    *,
    session_id: UUID,
    turn_id: UUID,
    sequence: int,
    started_at: datetime,
    tool_name: str | None,
    tool_call_id: str | None = None,
    input: Any = None,
    output: Any = None,
    status: str | None = None,
    event_ids: list[UUID] | None = None,
    vendor_data: dict[str, Any] | None = None,
) -> ToolCallItem:
    item = ToolCallItem(
        session_id=session_id,
        turn_id=turn_id,
        sequence=sequence,
        started_at=started_at,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        input=input,
        output=output,
        status=status or "requested",
        event_ids=list(event_ids or []),
        vendor_data=dict(vendor_data or {}),
    )
    items.append(item)
    return item


def append_command_execution(
    items: list[Item],
    *,
    session_id: UUID,
    turn_id: UUID,
    sequence: int,
    started_at: datetime,
    tool_name: str | None,
    tool_call_id: str | None = None,
    command: Any = None,
    exit_code: int | None = None,
    output: Any = None,
    status: str | None = None,
    event_ids: list[UUID] | None = None,
    vendor_data: dict[str, Any] | None = None,
) -> CommandExecutionItem:
    item = CommandExecutionItem(
        session_id=session_id,
        turn_id=turn_id,
        sequence=sequence,
        started_at=started_at,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        command=command,
        exit_code=exit_code,
        output=output,
        status=status or "requested",
        event_ids=list(event_ids or []),
        vendor_data=dict(vendor_data or {}),
    )
    items.append(item)
    return item


def append_file_change(
    items: list[Item],
    *,
    session_id: UUID,
    turn_id: UUID,
    sequence: int,
    started_at: datetime,
    tool_name: str | None,
    tool_call_id: str | None = None,
    path: str | None = None,
    operation: str | None = None,
    input: Any = None,
    output: Any = None,
    status: str | None = None,
    event_ids: list[UUID] | None = None,
    vendor_data: dict[str, Any] | None = None,
) -> FileChangeItem:
    item = FileChangeItem(
        session_id=session_id,
        turn_id=turn_id,
        sequence=sequence,
        started_at=started_at,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        path=path,
        operation=operation,
        input=input,
        output=output,
        status=status or "requested",
        event_ids=list(event_ids or []),
        vendor_data=dict(vendor_data or {}),
    )
    items.append(item)
    return item


def append_reasoning(
    items: list[Item],
    *,
    session_id: UUID,
    turn_id: UUID,
    sequence: int,
    started_at: datetime,
    text: str | None,
    event_ids: list[UUID] | None = None,
    vendor_data: dict[str, Any] | None = None,
) -> ReasoningItem | None:
    cleaned = (text.strip() if isinstance(text, str) else None) or None
    if cleaned is None and not vendor_data:
        return None
    item = ReasoningItem(
        session_id=session_id,
        turn_id=turn_id,
        sequence=sequence,
        started_at=started_at,
        text=cleaned,
        event_ids=list(event_ids or []),
        vendor_data=dict(vendor_data or {}),
    )
    items.append(item)
    return item


def append_plan(
    items: list[Item],
    *,
    session_id: UUID,
    turn_id: UUID,
    sequence: int,
    started_at: datetime,
    tool_name: str | None,
    tool_call_id: str | None = None,
    input: Any = None,
    output: Any = None,
    status: str | None = None,
    event_ids: list[UUID] | None = None,
    vendor_data: dict[str, Any] | None = None,
) -> PlanItem:
    item = PlanItem(
        session_id=session_id,
        turn_id=turn_id,
        sequence=sequence,
        started_at=started_at,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        input=input,
        output=output,
        status=status or "requested",
        event_ids=list(event_ids or []),
        vendor_data=dict(vendor_data or {}),
    )
    items.append(item)
    return item


def update_tool_item(
    items: list[Item],
    *,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
    input: Any = None,
    output: Any = None,
    command: Any = None,
    exit_code: int | None = None,
    path: str | None = None,
    operation: str | None = None,
    status: str | None = None,
    completed_at: datetime | None = None,
    event_ids: list[UUID] | None = None,
    vendor_data: dict[str, Any] | None = None,
) -> Item | None:
    """Update the most recent tool-shaped item matching tool_call_id.

    Falls back to appending a ToolCallItem if no matching item is found.
    """
    event_ids = list(event_ids or [])

    if tool_call_id:
        for item in reversed(items):
            if not _is_tool_shaped(item):
                continue
            tool_id = getattr(item, "tool_call_id", None)
            if tool_id != tool_call_id:
                continue
            _merge_tool_shaped(
                item,
                tool_name=tool_name,
                input=input,
                output=output,
                command=command,
                exit_code=exit_code,
                path=path,
                operation=operation,
                status=status,
                completed_at=completed_at,
                event_ids=event_ids,
                vendor_data=vendor_data,
            )
            return item

    if not any((tool_name, input, output, command, path, event_ids, vendor_data)):
        return None

    fallback = ToolCallItem(
        session_id=_session_id_from_items(items),
        turn_id=_turn_id_from_items(items),
        sequence=_next_sequence(items),
        started_at=completed_at or _latest_timestamp(items),
        completed_at=completed_at,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        input=input,
        output=output,
        status=status or "requested",
        event_ids=event_ids,
        vendor_data=dict(vendor_data or {}),
    )
    items.append(fallback)
    return fallback


def _is_tool_shaped(item: Item) -> bool:
    return item.kind in {"tool_call", "command_execution", "file_change", "plan"}


def _merge_tool_shaped(
    item: Item,
    *,
    tool_name: str | None,
    input: Any,
    output: Any,
    command: Any,
    exit_code: int | None,
    path: str | None,
    operation: str | None,
    status: str | None,
    completed_at: datetime | None,
    event_ids: list[UUID],
    vendor_data: dict[str, Any] | None,
) -> None:
    if tool_name and not getattr(item, "tool_name", None):
        item.tool_name = tool_name  # type: ignore[attr-defined]
    if input is not None and getattr(item, "input", None) is None:
        item.input = input  # type: ignore[attr-defined]
    if output is not None:
        item.output = output  # type: ignore[attr-defined]
    if status is not None:
        item.status = status
    if completed_at is not None:
        item.completed_at = completed_at
    if isinstance(item, CommandExecutionItem):
        if command is not None and item.command is None:
            item.command = command
        if exit_code is not None and item.exit_code is None:
            item.exit_code = exit_code
    if isinstance(item, FileChangeItem):
        if path is not None and item.path is None:
            item.path = path
        if operation is not None and item.operation is None:
            item.operation = operation
    for event_id in event_ids:
        if event_id not in item.event_ids:
            item.event_ids.append(event_id)
    if vendor_data:
        item.vendor_data.update({k: v for k, v in vendor_data.items() if v is not None})


def _session_id_from_items(items: list[Item]):
    for item in items:
        return item.session_id
    from uuid import uuid4
    return uuid4()


def _turn_id_from_items(items: list[Item]):
    for item in items:
        return item.turn_id
    from uuid import uuid4
    return uuid4()


def _next_sequence(items: list[Item]) -> int:
    if not items:
        return 0
    return max(item.sequence for item in items) + 1


def _latest_timestamp(items: list[Item]) -> datetime:
    if not items:
        from datetime import datetime as _dt
        return _dt.now()
    return max(item.started_at for item in items)
