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
    "turn_started",
    "assistant_message",
    "tool_call",
    "tool_result",
    "usage",
    "task_complete",
    "runtime",
]


def _non_empty_str(value: Any) -> str | None:
    """Return ``value`` when it is a non-empty string, else ``None``."""
    if isinstance(value, str) and value:
        return value
    return None


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


class _TurnProjectionState:
    """Own turn lifecycle state and its ordered event-id invariants."""

    def __init__(self, *, session_id: UUID) -> None:
        self._session_id = session_id
        self.turns: list[Turn] = []
        self.current_turn: Turn | None = None
        self._current_event_ids: set[UUID] = set()
        self._last_event_ids: set[UUID] = set()
        self._turn_sequence = 0
        self._item_sequence = 0
        self.has_final_answer = False
        self.vendor_turn_id: str | None = None

    def open_turn(
        self,
        *,
        started_at: datetime,
        opening_event_id: UUID,
        user_request_event_id: UUID | None,
        vendor_turn_id: str | None,
    ) -> None:
        if self.current_turn is not None:
            msg = "cannot open a turn while another turn is active"
            raise RuntimeError(msg)

        turn = Turn(
            session_id=self._session_id,
            sequence=self._turn_sequence,
            started_at=started_at,
            user_request_event_id=user_request_event_id,
            event_ids=[],
        )
        self.current_turn = turn
        self._current_event_ids = set()
        self.append_event_id(opening_event_id)
        self._turn_sequence += 1
        self._item_sequence = 0
        self.has_final_answer = False
        self.vendor_turn_id = vendor_turn_id

    def close_turn(self, ended_at: datetime, *, status: TurnStatus) -> None:
        turn = self.current_turn
        if turn is None:
            return

        turn.ended_at = ended_at
        turn.status = status
        self.turns.append(turn)
        self._last_event_ids = self._current_event_ids
        self._reset_current_turn()

    def append_event_id(self, event_id: UUID) -> None:
        turn = self.current_turn
        if turn is None:
            return
        self._append_unique_event_id(turn, self._current_event_ids, event_id)

    def append_late_event_id(self, event_id: UUID) -> None:
        """Attach a post-flush event to the most recently completed turn."""
        if not self.turns:
            return
        self._append_unique_event_id(self.turns[-1], self._last_event_ids, event_id)

    def attach_user_request(self, event_id: UUID) -> None:
        turn = self.current_turn
        if turn is None:
            return

        self.append_event_id(event_id)
        if turn.user_request_event_id is None:
            turn.user_request_event_id = event_id

    def next_item_sequence(self) -> int:
        value = self._item_sequence
        self._item_sequence += 1
        return value

    def _reset_current_turn(self) -> None:
        self.current_turn = None
        self._current_event_ids = set()
        self._item_sequence = 0
        self.has_final_answer = False
        self.vendor_turn_id = None

    @staticmethod
    def _append_unique_event_id(
        turn: Turn, known_event_ids: set[UUID], event_id: UUID
    ) -> None:
        if event_id in known_event_ids:
            return
        turn.event_ids.append(event_id)
        known_event_ids.add(event_id)


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
        prefer_lifecycle: bool = False,
    ) -> None:
        self.session_id = session_id
        self.vendor = vendor
        self.records = records
        self.active_status = active_status
        self.default_previous_turn_status = default_previous_turn_status
        self._prefer_lifecycle = prefer_lifecycle

        self._turn_state = _TurnProjectionState(session_id=session_id)
        # Turn_ids that terminate (``task_complete``/``turn_aborted``) in THIS
        # file. Used to skip inherited/orphan ``turn_started`` markers carried
        # into a forked continuation window from its source - their matching
        # completion lives in another file and would otherwise spawn empty
        # spurious turns. ``None`` when no terminals are observed.
        self._completable_turn_ids: set[str] | None = None
        # Bracket turns with task_started/task_complete lifecycle boundaries
        # instead of user messages. Vendors whose authoritative turn delimiter is
        # the lifecycle boundary (Codex: ``user_message`` is an in-turn item, not
        # a boundary) pass ``prefer_lifecycle`` and use lifecycle mode whenever
        # task_started records exist; other vendors fall back to lifecycle mode
        # only when there is no user_message to group by.
        self._use_lifecycle_turns: bool = False

    def project(self) -> list[Turn]:
        # Pre-compute the set of vendor turn_ids that terminate in THIS file so
        # inherited/orphan ``turn_started`` markers (carried into a forked
        # continuation window from its source, whose completion lives in another
        # file) can be skipped instead of spawning empty spurious turns.
        completable: set[str] = set()
        has_user_message = False
        has_turn_started = False
        for record in self.records:
            if record.kind == "task_complete":
                terminal_id = _non_empty_str(record.data.get("turn_id_raw"))
                if terminal_id is not None:
                    completable.add(terminal_id)
            elif record.kind == "user_message":
                has_user_message = True
            elif record.kind == "turn_started":
                has_turn_started = True
        self._completable_turn_ids = completable or None
        # Lifecycle mode when the vendor's authoritative delimiter is the
        # lifecycle boundary and boundaries exist (Codex), or when there is no
        # user_message to group by (inter-agent-triggered continuation windows).
        self._use_lifecycle_turns = (
            self._prefer_lifecycle and has_turn_started
        ) or not has_user_message

        for record in self.records:
            if record.kind == "user_message":
                if self._use_lifecycle_turns:
                    self._handle_user_message_in_turn(record)
                else:
                    self._handle_user_message(record)
            elif record.kind == "turn_started":
                if self._use_lifecycle_turns:
                    self._handle_turn_started(record)
                else:
                    self._append_turn_event_id(record.record_id)
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

        if self._turn_state.current_turn is not None:
            status = self.active_status or TurnStatus.COMPLETED
            self._flush_turn(
                self.records[-1].timestamp
                if self.records
                else self._turn_state.current_turn.started_at,
                status=status,
            )

        return self._turn_state.turns

    # -- handlers ---------------------------------------------------------

    def _handle_user_message(self, record: TranscriptRecord) -> None:
        starts_turn = bool(record.data.get("starts_turn", True))
        if not starts_turn:
            self._append_turn_event_id(record.record_id)
            return

        if self._turn_state.current_turn is not None:
            status = (
                record.data.get("previous_turn_status")
                or self.default_previous_turn_status
            )
            self._flush_turn(record.timestamp, status=TurnStatus(status))

        self._turn_state.open_turn(
            started_at=record.timestamp,
            opening_event_id=record.record_id,
            user_request_event_id=record.record_id,
            vendor_turn_id=None,
        )

    def _handle_turn_started(self, record: TranscriptRecord) -> None:
        vendor_turn_id = _non_empty_str(record.data.get("turn_id_raw"))
        # Skip inherited/orphan turn-start markers whose completion lives in
        # another file (forked continuation windows carry these from the fork
        # source). Only open a turn when it completes in this file. A live
        # in-flight turn whose task_complete has not been written yet is also
        # skipped, mirroring the prior user_message-based behavior where the
        # in-flight turn was not reconstructed until it terminated.
        if (
            self._completable_turn_ids is not None
            and vendor_turn_id is not None
            and vendor_turn_id not in self._completable_turn_ids
        ):
            if self._turn_state.current_turn is not None:
                self._append_turn_event_id(record.record_id)
            return
        if self._turn_state.current_turn is not None:
            # A new turn began before the prior terminated: close the prior as
            # interrupted (its terminal event was not observed in this file).
            self._flush_turn(record.timestamp, status=self.default_previous_turn_status)
        self._turn_state.open_turn(
            started_at=record.timestamp,
            opening_event_id=record.record_id,
            user_request_event_id=None,
            vendor_turn_id=vendor_turn_id,
        )

    def _handle_user_message_in_turn(self, record: TranscriptRecord) -> None:
        """Attach a ``user_message`` to the currently open turn as an in-turn
        item rather than using it as a turn boundary.

        Codex's authoritative turn delimiter is the ``task_started``/
        ``task_complete`` lifecycle boundary; ``user_message`` is an in-turn
        item (the user's prompt for that turn). In lifecycle mode the open turn
        is set by ``task_started``, so the user_message attaches to it -
        capturing its event id/text as the turn's user request - instead of
        opening a new turn.
        """
        if self._turn_state.current_turn is None:
            return
        self._turn_state.attach_user_request(record.record_id)

    def _handle_assistant_message(self, record: TranscriptRecord) -> None:
        if self._turn_state.current_turn is None:
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
            self._turn_state.has_final_answer = True

    def _handle_tool_call(self, record: TranscriptRecord) -> None:
        current_turn = self._turn_state.current_turn
        if current_turn is None:
            return

        self._append_turn_event_id(record.record_id)
        vendor_data = record.data.get("vendor_data")
        item_kind: TranscriptItemKind = record.data.get("item_kind") or "tool_call"
        status = record.data.get("status")

        if item_kind == "reasoning":
            text = record.data.get("text")
            cleaned = text.strip() if isinstance(text, str) else None
            if cleaned or vendor_data:
                current_turn.items.append(
                    ReasoningItem(
                        session_id=self.session_id,
                        turn_id=current_turn.turn_id,
                        sequence=self._next_item_sequence(),
                        started_at=record.timestamp,
                        completed_at=record.timestamp,
                        text=cleaned,
                        event_ids=[record.record_id],
                        vendor_data=vendor_data
                        if isinstance(vendor_data, dict)
                        else {},
                    )
                )
            return

        common = {
            "session_id": self.session_id,
            "turn_id": current_turn.turn_id,
            "sequence": self._next_item_sequence(),
            "started_at": record.timestamp,
            "tool_name": record.data.get("tool_name"),
            "tool_call_id": record.data.get("tool_call_id"),
            "input": record.data.get("input"),
            "output": record.data.get("output"),
            "status": status if isinstance(status, str) else "requested",
            "event_ids": [record.record_id],
            "vendor_data": vendor_data if isinstance(vendor_data, dict) else {},
        }

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

        current_turn.items.append(item)

    def _handle_tool_result(self, record: TranscriptRecord) -> None:
        if self._turn_state.current_turn is None:
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
        if self._turn_state.current_turn is not None:
            self._append_turn_event_id(record.record_id)
            return
        self._turn_state.append_late_event_id(record.record_id)

    def _handle_task_complete(self, record: TranscriptRecord) -> None:
        if self._turn_state.current_turn is None:
            return
        terminal_turn_id = _non_empty_str(record.data.get("turn_id_raw"))
        # When the open turn was opened by a lifecycle boundary (vendor turn_id
        # known), a terminal event for a different turn_id is an inherited/orphan
        # marker (forked continuation window) and must not close an unrelated
        # turn. Applies in lifecycle mode and to lifecycle turns reconstructed
        # within user-message files. User-message turns keep no vendor turn_id,
        # so they are unaffected (preserving prior user_message-mode behavior).
        if (
            self._turn_state.vendor_turn_id is not None
            and terminal_turn_id is not None
            and terminal_turn_id != self._turn_state.vendor_turn_id
        ):
            return
        self._append_turn_event_id(record.record_id)
        text = record.data.get("text")
        if (
            not self._turn_state.has_final_answer
            and isinstance(text, str)
            and text.strip()
        ):
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
        return self._turn_state.next_item_sequence()

    def _append_or_merge_agent_message(
        self,
        started_at: datetime,
        *,
        text: str | None,
        event_ids: list[UUID],
        vendor_data: dict[str, Any],
    ) -> None:
        current_turn = self._turn_state.current_turn
        assert current_turn is not None
        items = current_turn.items
        if items:
            last = items[-1]
            if isinstance(last, AgentMessageItem) and last.text == text:
                for event_id in event_ids:
                    if event_id not in last.event_ids:
                        last.event_ids.append(event_id)
                if vendor_data:
                    last.vendor_data.update(
                        {k: v for k, v in vendor_data.items() if v is not None}
                    )
                if started_at > (last.completed_at or last.started_at):
                    last.completed_at = started_at
                return

        item = AgentMessageItem(
            session_id=self.session_id,
            turn_id=current_turn.turn_id,
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
        current_turn = self._turn_state.current_turn
        assert current_turn is not None
        items = current_turn.items

        if tool_call_id:
            for item in reversed(items):
                if item.kind not in {
                    "tool_call",
                    "command_execution",
                    "file_change",
                    "plan",
                }:
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
            turn_id=current_turn.turn_id,
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
            item.vendor_data.update(
                {k: v for k, v in vendor_data.items() if v is not None}
            )

    def _flush_turn(self, ended_at: datetime, *, status: TurnStatus) -> None:
        self._turn_state.close_turn(ended_at, status=status)

    def _append_turn_event_id(self, event_id: UUID) -> None:
        self._turn_state.append_event_id(event_id)


def project_transcript(
    *,
    session_id: UUID,
    vendor: Vendor,
    records: list[TranscriptRecord],
    active_status: TurnStatus | None = None,
    default_previous_turn_status: TurnStatus = TurnStatus.COMPLETED,
    prefer_lifecycle: bool = False,
) -> list[Turn]:
    return TranscriptProjector(
        session_id=session_id,
        vendor=vendor,
        records=records,
        active_status=active_status,
        default_previous_turn_status=default_previous_turn_status,
        prefer_lifecycle=prefer_lifecycle,
    ).project()


def events_from_transcript(
    *, session_id: UUID, records: list[TranscriptRecord]
) -> list[Event]:
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
