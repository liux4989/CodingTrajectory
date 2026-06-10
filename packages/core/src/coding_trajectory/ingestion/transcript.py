"""Shared transcript-to-chronicle projection.

Adapters keep vendor-specific parsing local, then emit this small transcript IR.
The projector owns the common Session -> Turn -> Item construction rules so each
adapter does not hand-roll the same state machine.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from coding_trajectory.ingestion.common import compact_dict
from coding_trajectory.ingestion.models import (
    AgentMessageItem,
    CommandExecutionItem,
    Event,
    EventType,
    FileChangeItem,
    Item,
    PlanItem,
    ReasoningItem,
    ToolCallItem,
    Turn,
    TurnStatus,
    Vendor,
)


TranscriptKind = Literal[
    "user_message",
    "assistant_message",
    "tool_call",
    "tool_result",
    "usage",
    "task_complete",
    "runtime",
]

TranscriptRole = Literal["user", "assistant", "tool", "runtime"]
TranscriptFidelity = Literal["observed", "synthetic", "lossy"]
TranscriptItemKind = Literal[
    "agent_message",
    "tool_call",
    "command_execution",
    "file_change",
    "reasoning",
    "plan",
]


class TranscriptRecord(BaseModel):
    """Provider-neutral event used internally by ingestion adapters."""

    record_id: UUID = Field(default_factory=uuid4)
    sequence: int
    timestamp: datetime
    vendor: Vendor
    role: TranscriptRole
    kind: TranscriptKind
    data: dict[str, Any] = Field(default_factory=dict)
    fidelity: TranscriptFidelity = "observed"


class TranscriptProjector:
    """Project transcript records into the canonical chronicle hierarchy."""

    def __init__(
        self,
        *,
        session_id: UUID,
        vendor: Vendor,
        records: list[TranscriptRecord],
        active_status: TurnStatus | None = None,
        default_previous_turn_status: TurnStatus = TurnStatus.COMPLETED,
    ) -> None:
        self.session_id = session_id
        self.vendor = vendor
        self.records = records
        self.active_status = active_status
        self.default_previous_turn_status = default_previous_turn_status

        self.turns: list[Turn] = []
        self.current_turn: Turn | None = None
        self.turn_sequence = 0
        self.item_sequence = 0
        self.current_turn_has_final_answer = False
        self.current_user_request_text: str | None = None

    def project(self) -> list[Turn]:
        for record in self.records:
            if record.kind == "user_message":
                self._handle_user_message(record)
            elif record.kind == "assistant_message":
                self._handle_assistant_message(record)
            elif record.kind == "tool_call":
                self._handle_tool_call(record)
            elif record.kind == "tool_result":
                self._handle_tool_result(record)
            elif record.kind == "usage":
                self._handle_usage(record)
            elif record.kind == "task_complete":
                self._handle_task_complete(record)
            elif record.kind == "runtime":
                self._append_turn_event_id(record.record_id)

        if self.current_turn is not None:
            status = self.active_status or TurnStatus.COMPLETED
            self._flush_turn(
                self.records[-1].timestamp if self.records else self.current_turn.started_at,
                status=status,
            )

        return self.turns

    # -- handlers ---------------------------------------------------------

    def _handle_user_message(self, record: TranscriptRecord) -> None:
        starts_turn = bool(record.data.get("starts_turn", True))
        if not starts_turn:
            self._append_turn_event_id(record.record_id)
            return

        if self.current_turn is not None:
            status = record.data.get("previous_turn_status") or self.default_previous_turn_status
            self._flush_turn(record.timestamp, status=TurnStatus(status))

        self.current_turn = Turn(
            session_id=self.session_id,
            sequence=self.turn_sequence,
            started_at=record.timestamp,
            user_request_event_id=record.record_id,
            event_ids=[record.record_id],
        )
        self.turn_sequence += 1
        self.item_sequence = 0
        self.current_turn_has_final_answer = False
        text = record.data.get("text")
        self.current_user_request_text = text if isinstance(text, str) else None

    def _handle_assistant_message(self, record: TranscriptRecord) -> None:
        if self.current_turn is None:
            return

        self._append_turn_event_id(record.record_id)
        text = record.data.get("text")
        cleaned = text.strip() if isinstance(text, str) else None
        vendor_data = record.data.get("vendor_data")
        vendor_map = vendor_data if isinstance(vendor_data, dict) else {}

        if cleaned or vendor_map:
            self._append_or_merge_agent_message(
                record.timestamp,
                text=cleaned or None,
                event_ids=[record.record_id],
                vendor_data=vendor_map,
            )

        if record.data.get("phase") == "final_answer" and cleaned:
            self.current_turn_has_final_answer = True

    def _handle_tool_call(self, record: TranscriptRecord) -> None:
        if self.current_turn is None:
            return

        self._append_turn_event_id(record.record_id)
        vendor_data = record.data.get("vendor_data")
        item_kind: TranscriptItemKind = record.data.get("item_kind") or "tool_call"
        status = record.data.get("status")

        if item_kind == "reasoning":
            text = record.data.get("text")
            cleaned = text.strip() if isinstance(text, str) else None
            if cleaned or vendor_data:
                self.current_turn.items.append(
                    ReasoningItem(
                        session_id=self.session_id,
                        turn_id=self.current_turn.turn_id,
                        sequence=self._next_item_sequence(),
                        started_at=record.timestamp,
                        completed_at=record.timestamp,
                        text=cleaned,
                        event_ids=[record.record_id],
                        vendor_data=vendor_data if isinstance(vendor_data, dict) else {},
                    )
                )
            return

        common = dict(
            session_id=self.session_id,
            turn_id=self.current_turn.turn_id,
            sequence=self._next_item_sequence(),
            started_at=record.timestamp,
            tool_name=record.data.get("tool_name"),
            tool_call_id=record.data.get("tool_call_id"),
            input=record.data.get("input"),
            output=record.data.get("output"),
            status=status if isinstance(status, str) else "requested",
            event_ids=[record.record_id],
            vendor_data=vendor_data if isinstance(vendor_data, dict) else {},
        )

        item: Item
        if item_kind == "command_execution":
            item = CommandExecutionItem(
                **common,
                command=record.data.get("command") or record.data.get("input"),
                exit_code=record.data.get("exit_code"),
            )
        elif item_kind == "file_change":
            item = FileChangeItem(
                **common,
                path=record.data.get("path"),
                operation=record.data.get("operation"),
            )
        elif item_kind == "plan":
            item = PlanItem(**common)
        else:
            item = ToolCallItem(**common)

        self.current_turn.items.append(item)

    def _handle_tool_result(self, record: TranscriptRecord) -> None:
        if self.current_turn is None:
            return

        self._append_turn_event_id(record.record_id)
        status = record.data.get("status")
        vendor_data = record.data.get("vendor_data")
        self._update_tool_item(
            tool_call_id=record.data.get("tool_call_id"),
            tool_name=record.data.get("tool_name"),
            output=record.data.get("output"),
            command=record.data.get("command"),
            exit_code=record.data.get("exit_code"),
            path=record.data.get("path"),
            operation=record.data.get("operation"),
            status=status if isinstance(status, str) else "completed",
            completed_at=record.timestamp,
            event_ids=[record.record_id],
            vendor_data=vendor_data if isinstance(vendor_data, dict) else None,
        )

    def _handle_usage(self, record: TranscriptRecord) -> None:
        if self.current_turn is None:
            return
        self._append_turn_event_id(record.record_id)

    def _handle_task_complete(self, record: TranscriptRecord) -> None:
        if self.current_turn is None:
            return
        self._append_turn_event_id(record.record_id)
        text = record.data.get("text")
        if not self.current_turn_has_final_answer and isinstance(text, str) and text.strip():
            self._append_or_merge_agent_message(
                record.timestamp,
                text=text.strip(),
                event_ids=[record.record_id],
                vendor_data={},
            )
        status = record.data.get("status") or TurnStatus.COMPLETED.value
        self._flush_turn(record.timestamp, status=TurnStatus(status))

    # -- helpers ----------------------------------------------------------

    def _next_item_sequence(self) -> int:
        value = self.item_sequence
        self.item_sequence += 1
        return value

    def _append_or_merge_agent_message(
        self,
        started_at: datetime,
        *,
        text: str | None,
        event_ids: list[UUID],
        vendor_data: dict[str, Any],
    ) -> None:
        assert self.current_turn is not None
        items = self.current_turn.items
        if items:
            last = items[-1]
            if isinstance(last, AgentMessageItem) and last.text == text:
                for event_id in event_ids:
                    if event_id not in last.event_ids:
                        last.event_ids.append(event_id)
                if vendor_data:
                    last.vendor_data.update({k: v for k, v in vendor_data.items() if v is not None})
                if started_at > (last.completed_at or last.started_at):
                    last.completed_at = started_at
                return

        item = AgentMessageItem(
            session_id=self.session_id,
            turn_id=self.current_turn.turn_id,
            sequence=self._next_item_sequence(),
            started_at=started_at,
            completed_at=started_at,
            text=text,
            event_ids=list(event_ids),
            vendor_data=dict(vendor_data),
        )
        items.append(item)

    def _update_tool_item(
        self,
        *,
        tool_call_id: str | None,
        tool_name: str | None,
        output: Any,
        command: Any,
        exit_code: int | None,
        path: str | None,
        operation: str | None,
        status: str | None,
        completed_at: datetime,
        event_ids: list[UUID],
        vendor_data: dict[str, Any] | None,
    ) -> None:
        assert self.current_turn is not None
        items = self.current_turn.items

        if tool_call_id:
            for item in reversed(items):
                if item.kind not in {"tool_call", "command_execution", "file_change", "plan"}:
                    continue
                if getattr(item, "tool_call_id", None) != tool_call_id:
                    continue
                self._merge_tool_shaped(
                    item,
                    tool_name=tool_name,
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
                return

        fallback = ToolCallItem(
            session_id=self.session_id,
            turn_id=self.current_turn.turn_id,
            sequence=self._next_item_sequence(),
            started_at=completed_at,
            completed_at=completed_at,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            output=output,
            status=status or "completed",
            event_ids=list(event_ids),
            vendor_data=dict(vendor_data or {}),
        )
        items.append(fallback)

    @staticmethod
    def _merge_tool_shaped(
        item: Item,
        *,
        tool_name: str | None,
        output: Any,
        command: Any,
        exit_code: int | None,
        path: str | None,
        operation: str | None,
        status: str | None,
        completed_at: datetime,
        event_ids: list[UUID],
        vendor_data: dict[str, Any] | None,
    ) -> None:
        if tool_name and not getattr(item, "tool_name", None):
            item.tool_name = tool_name  # type: ignore[attr-defined]
        if output is not None:
            item.output = output  # type: ignore[attr-defined]
        if status is not None:
            item.status = status
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

    def _flush_turn(self, ended_at: datetime, *, status: TurnStatus) -> None:
        if self.current_turn is None:
            return
        self.current_turn.ended_at = ended_at
        self.current_turn.status = status
        self.turns.append(self.current_turn)
        self.current_turn = None
        self.item_sequence = 0
        self.current_turn_has_final_answer = False
        self.current_user_request_text = None

    def _append_turn_event_id(self, event_id: UUID) -> None:
        if self.current_turn is None:
            return
        if event_id not in self.current_turn.event_ids:
            self.current_turn.event_ids.append(event_id)


def project_transcript(
    *,
    session_id: UUID,
    vendor: Vendor,
    records: list[TranscriptRecord],
    active_status: TurnStatus | None = None,
    default_previous_turn_status: TurnStatus = TurnStatus.COMPLETED,
) -> list[Turn]:
    return TranscriptProjector(
        session_id=session_id,
        vendor=vendor,
        records=records,
        active_status=active_status,
        default_previous_turn_status=default_previous_turn_status,
    ).project()


def events_from_transcript(*, session_id: UUID, records: list[TranscriptRecord]) -> list[Event]:
    return [
        Event(
            event_id=record.record_id,
            session_id=session_id,
            timestamp=record.timestamp,
            type=_event_type(record),
            vendor_source=record.vendor,
            actor=_actor(record),
            payload=_event_payload(record),
        )
        for record in records
    ]


def _event_type(record: TranscriptRecord) -> EventType:
    if record.kind == "user_message":
        return EventType.USER_PROMPT_SUBMITTED
    if record.kind == "assistant_message":
        return EventType.LLM_RESPONSE
    if record.kind == "tool_call":
        return EventType.TOOL_CALL_REQUESTED
    if record.kind == "tool_result":
        return (
            EventType.TOOL_CALL_FAILED
            if record.data.get("status") == "failed"
            else EventType.TOOL_CALL_SUCCEEDED
        )
    return EventType.VENDOR_RAW


def _actor(record: TranscriptRecord) -> str | None:
    if record.role in {"user", "assistant", "tool"}:
        return record.role
    return None


def _event_payload(record: TranscriptRecord) -> dict[str, Any]:
    projection_keys = {
        "item_kind",
        "previous_turn_status",
        "starts_turn",
        "vendor_data",
    }
    payload = compact_dict(
        {
            "transcript_kind": record.kind,
            **{
                key: value
                for key, value in record.data.items()
                if key not in projection_keys
            },
        }
    )
    if record.kind in {"usage", "task_complete", "runtime"}:
        payload.setdefault("raw_type", record.data.get("raw_type") or record.kind)
    return payload
