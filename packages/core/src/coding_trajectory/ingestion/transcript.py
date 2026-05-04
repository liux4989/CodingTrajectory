"""Shared transcript-to-chronicle projection.

Adapters should keep vendor-specific parsing local, then emit this small
transcript IR. The projector owns the common Session -> Turn -> Step -> Item
construction rules so each adapter does not hand-roll the same state machine.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from coding_trajectory.ingestion.common import compact_dict
from coding_trajectory.ingestion.models import (
    Event,
    EventType,
    Step,
    StepItem,
    ToolStatus,
    Turn,
    TurnStatus,
    Vendor,
)
from coding_trajectory.ingestion.step_items import append_text_item, append_tool_item, update_tool_item


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
        self.step_sequence = 0
        self.current_step_items: list[StepItem] = []
        self.current_step_event_ids: list[UUID] = []
        self.current_step_vendor_data: dict[str, Any] = {}
        self.current_step_timestamp: datetime | None = None
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
        self.step_sequence = 0
        self.current_step_items = []
        self.current_step_event_ids = []
        self.current_step_vendor_data = {}
        self.current_step_timestamp = None
        self.current_turn_has_final_answer = False
        text = record.data.get("text")
        self.current_user_request_text = text if isinstance(text, str) else None

    def _handle_assistant_message(self, record: TranscriptRecord) -> None:
        if self.current_turn is None:
            return

        if record.data.get("flush_before", False):
            self._flush_step()

        self._start_step(record.timestamp)
        self._append_turn_event_id(record.record_id)
        self._append_step_event_id(record.record_id)
        vendor_data = record.data.get("vendor_data")
        if isinstance(vendor_data, dict):
            self.current_step_vendor_data.update({k: v for k, v in vendor_data.items() if v is not None})

        text = record.data.get("text")
        append_text_item(
            self.current_step_items,
            text if isinstance(text, str) else None,
            event_ids=[record.record_id],
        )
        if record.data.get("phase") == "final_answer" and text:
            self.current_turn_has_final_answer = True

        if record.data.get("flush_after", False):
            self._flush_step()

    def _handle_tool_call(self, record: TranscriptRecord) -> None:
        if self.current_turn is None:
            return

        if record.data.get("flush_before", False):
            self._flush_step()

        self._start_step(record.timestamp)
        self._append_turn_event_id(record.record_id)
        self._append_step_event_id(record.record_id)
        vendor_data = record.data.get("vendor_data")
        if isinstance(vendor_data, dict):
            self.current_step_vendor_data.update({k: v for k, v in vendor_data.items() if v is not None})

        status = record.data.get("status")
        append_tool_item(
            self.current_step_items,
            tool_name=record.data.get("tool_name"),
            tool_call_id=record.data.get("tool_call_id"),
            input=record.data.get("input"),
            output=record.data.get("output"),
            status=ToolStatus(status) if isinstance(status, str) else ToolStatus.REQUESTED,
            event_ids=[record.record_id],
        )

        if record.data.get("flush_after", False):
            self._flush_step()

    def _handle_tool_result(self, record: TranscriptRecord) -> None:
        if self.current_turn is None:
            return

        self._append_turn_event_id(record.record_id)
        target_items = self.current_step_items
        target_event_ids = self.current_step_event_ids
        if record.data.get("attach_to_previous_step") and self.current_turn.steps:
            target_items = self.current_turn.steps[-1].items
            target_event_ids = self.current_turn.steps[-1].event_ids

        status = record.data.get("status")
        update_tool_item(
            target_items,
            tool_call_id=record.data.get("tool_call_id"),
            tool_name=record.data.get("tool_name"),
            output=record.data.get("output"),
            status=ToolStatus(status) if isinstance(status, str) else ToolStatus.COMPLETED,
            event_ids=[record.record_id],
        )
        if record.record_id not in target_event_ids:
            target_event_ids.append(record.record_id)

        if not record.data.get("attach_to_previous_step"):
            self._start_step(record.timestamp)

        if record.data.get("flush_after", False):
            self._flush_step()

    def _handle_usage(self, record: TranscriptRecord) -> None:
        if self.current_turn is None:
            return
        self._append_turn_event_id(record.record_id)
        self._append_step_event_id(record.record_id)
        vendor_data = record.data.get("vendor_data")
        if isinstance(vendor_data, dict):
            self.current_step_vendor_data.update({k: v for k, v in vendor_data.items() if v is not None})

    def _handle_task_complete(self, record: TranscriptRecord) -> None:
        if self.current_turn is None:
            return
        self._append_turn_event_id(record.record_id)
        self._append_step_event_id(record.record_id)
        text = record.data.get("text")
        if not self.current_turn_has_final_answer and isinstance(text, str) and text:
            self._start_step(record.timestamp)
            append_text_item(self.current_step_items, text, event_ids=[record.record_id])
        status = record.data.get("status") or TurnStatus.COMPLETED.value
        self._flush_turn(record.timestamp, status=TurnStatus(status))

    def _start_step(self, timestamp: datetime) -> None:
        if self.current_step_timestamp is None:
            self.current_step_timestamp = timestamp

    def _flush_step(self) -> None:
        if self.current_turn is None:
            return
        if not self.current_step_items and not self.current_step_vendor_data:
            self.current_step_event_ids = []
            self.current_step_timestamp = None
            return

        step = Step(
            session_id=self.session_id,
            turn_id=self.current_turn.turn_id,
            sequence=self.step_sequence,
            timestamp=self.current_step_timestamp or self.current_turn.started_at,
            vendor=self.vendor,
            items=list(self.current_step_items),
            vendor_data=dict(self.current_step_vendor_data),
            event_ids=list(self.current_step_event_ids),
        )
        self.current_turn.steps.append(step)
        self.step_sequence += 1
        self.current_step_items = []
        self.current_step_event_ids = []
        self.current_step_vendor_data = {}
        self.current_step_timestamp = None

    def _flush_turn(self, ended_at: datetime, *, status: TurnStatus) -> None:
        if self.current_turn is None:
            return
        self._flush_step()
        self.current_turn.ended_at = ended_at
        self.current_turn.status = status
        self.turns.append(self.current_turn)
        self.current_turn = None
        self.step_sequence = 0
        self.current_turn_has_final_answer = False
        self.current_user_request_text = None

    def _append_turn_event_id(self, event_id: UUID) -> None:
        if self.current_turn is None:
            return
        if event_id not in self.current_turn.event_ids:
            self.current_turn.event_ids.append(event_id)

    def _append_step_event_id(self, event_id: UUID) -> None:
        if event_id not in self.current_step_event_ids:
            self.current_step_event_ids.append(event_id)


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
            if record.data.get("status") == ToolStatus.FAILED.value
            else EventType.TOOL_CALL_SUCCEEDED
        )
    return EventType.VENDOR_RAW


def _actor(record: TranscriptRecord) -> str | None:
    if record.role in {"user", "assistant", "tool"}:
        return record.role
    return None


def _event_payload(record: TranscriptRecord) -> dict[str, Any]:
    projection_keys = {
        "attach_to_previous_step",
        "flush_after",
        "flush_before",
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
