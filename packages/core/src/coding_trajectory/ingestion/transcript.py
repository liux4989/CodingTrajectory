"""Shared transcript-to-chronicle projection.

Adapters keep vendor-specific parsing local, then emit this small transcript IR.
The projector owns the common Session -> Turn -> Item construction rules so each
adapter does not hand-roll the same state machine.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from coding_trajectory.ingestion.common import compact_dict, stable_uuid
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
from coding_trajectory.ingestion.provenance import RecordSpan, SessionProvenance
from coding_trajectory.ingestion.retention import retain_event_for_measurements

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
    # Raw source byte span + digest, set only on the streaming compact path.
    origin: RecordSpan | None = None


class TranscriptStabilizer:
    """Inline canonical ID assignment for the compact (measurements) path.

    Reproduces ``discovery.stabilize_session``'s ID recipe record-by-record so
    event payloads are hashed and discarded at translation time instead of
    staying resident until post-assembly stabilization.  The event index,
    timestamp, type, actor, and full event payload enter each hash exactly as
    the post-assembly path computes them, so compact and full ingestion
    produce identical identifiers, topology, and metric outputs.
    """

    def __init__(self, *, vendor: Vendor, source: Any) -> None:
        self._vendor = vendor
        self._source = source
        self.event_ids: dict[UUID, UUID] = {}
        self.spans: dict[UUID, RecordSpan] = {}
        self._event_index = 0
        # First cwd observed in an event payload, mirroring the full-event
        # scan ``stabilize_session`` performs before retention filtering.
        self.cwd: str | None = None

    def stabilize_event(self, record: TranscriptRecord) -> UUID:
        """Assign the canonical event id for one record and capture cwd."""
        payload = _event_payload(record)
        stable = stable_uuid(
            self._vendor,
            self._source,
            index=self._event_index,
            timestamp=record.timestamp.isoformat(),
            type=_event_type(record).value,
            actor=_actor(record),
            payload=payload,
        )
        self._event_index += 1
        self.event_ids[record.record_id] = stable
        if record.origin is not None:
            self.spans[stable] = record.origin
        if self.cwd is None:
            self.cwd = _payload_cwd(payload)
        return stable

    def retained_event(
        self, record: TranscriptRecord, *, session_id: UUID
    ) -> Event | None:
        """Return the retained compact event for ``record``, if any."""
        stable = self.stabilize_event(record)
        event = Event(
            event_id=stable,
            session_id=session_id,
            timestamp=record.timestamp,
            type=_event_type(record),
            vendor_source=record.vendor,
            actor=_actor(record),
            payload=_event_payload(record),
        )
        return retain_event_for_measurements(event)

    def map_event_id(self, raw_id: UUID) -> UUID:
        """Resolve one record id to its stable event id (post event pass)."""
        return self.event_ids.get(raw_id, raw_id)

    def stabilize_turn(
        self, *, turn_index: int, session_id: UUID, sequence: int, started_at: datetime
    ) -> UUID:
        """Assign the canonical turn id exactly as post-assembly stabilization."""
        return stable_uuid(
            self._vendor,
            self._source,
            turn_index=turn_index,
            session_id=str(session_id),
            sequence=sequence,
            started_at=started_at.isoformat(),
        )

    def stabilize_item(
        self,
        *,
        turn_index: int,
        item_index: int,
        kind: str,
        sequence: int,
        started_at: datetime,
        tool_call_id: str | None,
    ) -> UUID:
        """Assign the canonical item id exactly as post-assembly stabilization."""
        return stable_uuid(
            self._vendor,
            self._source,
            turn_index=turn_index,
            item_index=item_index,
            kind=kind,
            sequence=sequence,
            started_at=started_at.isoformat(),
            tool_call_id=tool_call_id,
        )


def build_session_provenance(
    *,
    session_id: UUID,
    vendor: Vendor,
    source: Any,
    stabilizer: TranscriptStabilizer,
    turns: list[Turn],
) -> SessionProvenance:
    """Assemble canonical-id -> source-span provenance for one compact session.

    Items inherit the ordered spans of their constituent events, so merged
    agent messages and tool call/result pairs map to every source range that
    produced them.
    """

    item_spans: dict[UUID, tuple[RecordSpan, ...]] = {}
    for turn in turns:
        for item in turn.items:
            spans = tuple(
                stabilizer.spans[event_id]
                for event_id in item.event_ids
                if event_id in stabilizer.spans
            )
            if spans:
                item_spans[item.item_id] = spans
    return SessionProvenance(
        session_id=session_id,
        vendor=vendor,
        source_path=str(source),
        events=dict(stabilizer.spans),
        items=item_spans,
    )


def _payload_cwd(payload: dict[str, Any]) -> str | None:
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return cwd
    raw = payload.get("raw")
    if isinstance(raw, dict):
        raw_cwd = raw.get("cwd")
        if isinstance(raw_cwd, str) and raw_cwd:
            return raw_cwd
    return None


def compact_session_cwd(
    *, vendor: Vendor, source: Any, extensions: Any, payload_cwd: str | None
) -> str | None:
    """Resolve session cwd for the compact path identically to
    ``discovery._extract_session_cwd``: vendor extensions first, then the cwd
    captured from the first full event payload, then the Claude Code path
    encoding fallback.
    """

    if extensions:
        codex = getattr(extensions, "codex", None)
        if codex is not None and getattr(codex, "cwd", None):
            return codex.cwd
        pi = getattr(extensions, "pi", None)
        if pi is not None and getattr(pi, "cwd", None):
            return pi.cwd
    if payload_cwd:
        return payload_cwd
    if vendor == Vendor.CLAUDE_CODE:
        from coding_trajectory.discovery_paths import _decode_claude_encoded_path

        base = Path.home() / ".claude" / "projects"
        try:
            encoded = Path(source).relative_to(base).parts[0]
            return _decode_claude_encoded_path(encoded)
        except ValueError:
            pass
    return None


class _TurnProjectionState:
    """Own turn lifecycle state and its ordered event-id invariants."""

    def __init__(
        self, *, session_id: UUID, compact: TranscriptStabilizer | None = None
    ) -> None:
        self._session_id = session_id
        self._compact = compact
        self.turns: list[Turn] = []
        self.current_turn: Turn | None = None
        self.current_turn_index = 0
        self._current_event_ids: set[UUID] = set()
        self._last_event_ids: set[UUID] = set()
        self._turn_sequence = 0
        self._item_sequence = 0
        self.has_final_answer = False
        self.vendor_turn_id: str | None = None
        # Shadow of the trailing agent message's real text in compact mode:
        # merge equality is decided on the discarded text exactly as the
        # trajectory path decides it on resident text.
        self._last_agent_text: str | None = None

    def map_event_id(self, event_id: UUID) -> UUID:
        compact = self._compact
        return compact.map_event_id(event_id) if compact is not None else event_id

    def next_item_id(
        self,
        *,
        kind: str,
        sequence: int,
        started_at: datetime,
        tool_call_id: str | None,
        item_index: int,
    ) -> UUID | None:
        """Assign the stable item id inline when running compact."""
        compact = self._compact
        if compact is None:
            return None
        return compact.stabilize_item(
            turn_index=self.current_turn_index,
            item_index=item_index,
            kind=kind,
            sequence=sequence,
            started_at=started_at,
            tool_call_id=tool_call_id,
        )

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
            user_request_event_id=(
                self.map_event_id(user_request_event_id)
                if user_request_event_id
                else None
            ),
            event_ids=[],
        )
        if self._compact is not None:
            turn.turn_id = self._compact.stabilize_turn(
                turn_index=self._turn_sequence,
                session_id=self._session_id,
                sequence=self._turn_sequence,
                started_at=started_at,
            )
        self.current_turn = turn
        self.current_turn_index = self._turn_sequence
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
            turn.user_request_event_id = self.map_event_id(event_id)

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
        self._last_agent_text = None

    def _append_unique_event_id(
        self, turn: Turn, known_event_ids: set[UUID], event_id: UUID
    ) -> None:
        event_id = self.map_event_id(event_id)
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
        compact: TranscriptStabilizer | None = None,
    ) -> None:
        self.session_id = session_id
        self.vendor = vendor
        self.records = records
        self.active_status = active_status
        self.default_previous_turn_status = default_previous_turn_status
        self._prefer_lifecycle = prefer_lifecycle
        self._compact = compact

        self._turn_state = _TurnProjectionState(session_id=session_id, compact=compact)
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
                items = current_turn.items
                sequence = self._next_item_sequence()
                compact_id = self._turn_state.next_item_id(
                    kind="reasoning",
                    sequence=sequence,
                    started_at=record.timestamp,
                    tool_call_id=None,
                    item_index=len(items),
                )
                item = ReasoningItem(
                    session_id=self.session_id,
                    turn_id=current_turn.turn_id,
                    sequence=sequence,
                    started_at=record.timestamp,
                    completed_at=record.timestamp,
                    text=None if self._compact is not None else cleaned,
                    event_ids=[self._turn_state.map_event_id(record.record_id)],
                    vendor_data=(
                        {}
                        if self._compact is not None
                        else vendor_data
                        if isinstance(vendor_data, dict)
                        else {}
                    ),
                )
                if compact_id is not None:
                    item.item_id = compact_id
                items.append(item)
            return

        common = {
            "session_id": self.session_id,
            "turn_id": current_turn.turn_id,
            "sequence": self._next_item_sequence(),
            "started_at": record.timestamp,
            "tool_name": record.data.get("tool_name"),
            "tool_call_id": record.data.get("tool_call_id"),
            "input": None if self._compact is not None else record.data.get("input"),
            "output": None if self._compact is not None else record.data.get("output"),
            "status": status if isinstance(status, str) else "requested",
            "event_ids": [self._turn_state.map_event_id(record.record_id)],
            "vendor_data": (
                {}
                if self._compact is not None
                else vendor_data
                if isinstance(vendor_data, dict)
                else {}
            ),
        }
        compact_id = self._turn_state.next_item_id(
            kind=item_kind,
            sequence=common["sequence"],
            started_at=record.timestamp,
            tool_call_id=record.data.get("tool_call_id"),
            item_index=len(current_turn.items),
        )

        item: Item
        if item_kind == "command_execution":
            item = CommandExecutionItem(
                **common,
                command=(
                    None
                    if self._compact is not None
                    else record.data.get("command") or record.data.get("input")
                ),
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
        if compact_id is not None:
            item.item_id = compact_id

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
        compact = self._compact is not None
        items = current_turn.items
        if items:
            last = items[-1]
            if isinstance(last, AgentMessageItem):
                last_text = self._turn_state._last_agent_text if compact else last.text
                if last_text == text:
                    for event_id in event_ids:
                        mapped = self._turn_state.map_event_id(event_id)
                        if mapped not in last.event_ids:
                            last.event_ids.append(mapped)
                    if vendor_data and not compact:
                        last.vendor_data.update(
                            {k: v for k, v in vendor_data.items() if v is not None}
                        )
                    if started_at > (last.completed_at or last.started_at):
                        last.completed_at = started_at
                    return

        sequence = self._next_item_sequence()
        item = AgentMessageItem(
            session_id=self.session_id,
            turn_id=current_turn.turn_id,
            sequence=sequence,
            started_at=started_at,
            completed_at=started_at,
            text=None if compact else text,
            event_ids=[self._turn_state.map_event_id(eid) for eid in event_ids],
            vendor_data={} if compact else dict(vendor_data),
        )
        compact_id = self._turn_state.next_item_id(
            kind="agent_message",
            sequence=sequence,
            started_at=started_at,
            tool_call_id=None,
            item_index=len(items),
        )
        if compact_id is not None:
            item.item_id = compact_id
        items.append(item)
        self._turn_state._last_agent_text = text if compact else None

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
            output=None if self._compact is not None else output,
            status=status or "completed",
            event_ids=[self._turn_state.map_event_id(eid) for eid in event_ids],
            vendor_data={} if self._compact is not None else dict(vendor_data or {}),
        )
        compact_id = self._turn_state.next_item_id(
            kind="tool_call",
            sequence=fallback.sequence,
            started_at=completed_at,
            tool_call_id=tool_call_id,
            item_index=len(items),
        )
        if compact_id is not None:
            fallback.item_id = compact_id
        items.append(fallback)

    def _merge_tool_shaped(
        self,
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
        compact = self._compact is not None
        if tool_name and not getattr(item, "tool_name", None):
            item.tool_name = tool_name  # type: ignore[attr-defined]
        if output is not None and not compact:
            item.output = output  # type: ignore[attr-defined]
        if status is not None:
            item.status = status
        item.completed_at = completed_at
        if isinstance(item, CommandExecutionItem):
            if command is not None and item.command is None and not compact:
                item.command = command
            if exit_code is not None and item.exit_code is None:
                item.exit_code = exit_code
        if isinstance(item, FileChangeItem):
            if path is not None and item.path is None:
                item.path = path
            if operation is not None and item.operation is None:
                item.operation = operation
        for event_id in event_ids:
            mapped = self._turn_state.map_event_id(event_id)
            if mapped not in item.event_ids:
                item.event_ids.append(mapped)
        if vendor_data and not compact:
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
    compact: TranscriptStabilizer | None = None,
) -> list[Turn]:
    return TranscriptProjector(
        session_id=session_id,
        vendor=vendor,
        records=records,
        active_status=active_status,
        default_previous_turn_status=default_previous_turn_status,
        prefer_lifecycle=prefer_lifecycle,
        compact=compact,
    ).project()


def events_from_transcript(
    *,
    session_id: UUID,
    records: list[TranscriptRecord],
    stabilizer: TranscriptStabilizer | None = None,
) -> list[Event]:
    if stabilizer is not None:
        retained: list[Event] = []
        for record in records:
            event = stabilizer.retained_event(record, session_id=session_id)
            if event is not None:
                retained.append(event)
        return retained
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
