"""Base adapter interface for agent ingestion."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from coding_trajectory.ingestion.models import Event, EventType, Session, TimelineItem, Turn, Vendor


class BaseAdapter(ABC):
    """Base class for vendor-specific agent adapters."""

    vendor: Vendor  # concrete subclasses declare which vendor they handle
    _file_glob: str = "*.jsonl"  # override in subclasses (e.g. Gemini uses *.json)

    @abstractmethod
    def _parse_raw_line(self, line: dict) -> Event | None:
        """Parse one raw JSON line into a canonical Event; return None to skip."""
        ...

    def _reset_ingest_state(self) -> None:
        """Clear any per-file adapter state before ingesting a new file."""

    def _load_records(self, path: Path) -> list[dict]:
        """Load raw records from *path*.

        Default implementation expects JSONL input and returns one parsed
        object per line. Adapters with different on-disk formats can override
        this method while keeping the rest of the ingest flow shared.
        """
        import json

        records: list[dict] = []
        with path.open(encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    records.append(obj)
        return records

    def _parse_record(self, source: Path, record: dict) -> list[Event]:
        """Parse one raw record into zero or more canonical events."""
        event = self._parse_raw_line(record)
        return [event] if event is not None else []

    def ingest_file(self, path: Path) -> Session:
        """Ingest a single source file and return a normalised Session."""
        self._reset_ingest_state()
        events: list[Event] = []
        for record in self._load_records(path):
            events.extend(self._parse_record(path, record))

        return self._build_session(path, events)

    def ingest_directory(self, directory: Path) -> list[Session]:
        """Ingest all matching files in *directory* and return one Session per file."""
        sessions: list[Session] = []
        for source_file in sorted(directory.glob(self._file_glob)):
            sessions.append(self.ingest_file(source_file))
        return sessions

    def ingest(self, source: Path) -> Session:
        """Ingest raw agent output and return a normalized Session.

        Delegates to ingest_file; kept for backwards compatibility.
        """
        return self.ingest_file(source)

    # ------------------------------------------------------------------
    # Override this to customise session construction per vendor.
    # ------------------------------------------------------------------

    def _group_into_turns(
        self,
        session_id: UUID,
        events: list[Event],
        *,
        end_at_next_user_prompt: bool = False,
    ) -> list[Turn]:
        turns: list[Turn] = []
        current_events: list[Event] = []
        current_turn_start: datetime | None = None
        current_user_request: str | None = None

        def _flush_turn(turn_end: datetime | None = None) -> None:
            nonlocal current_events, current_turn_start, current_user_request
            if not current_events:
                return

            turn = Turn(
                session_id=session_id,
                user_request=current_user_request,
                started_at=current_turn_start or current_events[0].timestamp,
                ended_at=turn_end or current_events[-1].timestamp,
                event_ids=[event.event_id for event in current_events],
            )
            for event in current_events:
                event.turn_id = turn.turn_id
            turns.append(turn)

            current_events = []
            current_turn_start = None
            current_user_request = None

        for event in events:
            if event.type == EventType.USER_PROMPT_SUBMITTED:
                if current_user_request is not None:
                    _flush_turn(event.timestamp if end_at_next_user_prompt else None)
                current_turn_start = event.timestamp
                current_user_request = event.payload.get("text")
                current_events = [event]
                continue

            if current_user_request is None:
                continue

            if current_turn_start is None:
                current_turn_start = event.timestamp
            current_events.append(event)

        _flush_turn()
        return turns

    def _build_timeline(self, events: list[Event], turns: list[Turn]) -> list[TimelineItem]:
        turn_by_id = {turn.turn_id: turn for turn in turns}
        turn_seen: set[UUID] = set()
        timeline: list[TimelineItem] = []

        for event in events:
            if event.turn_id is None:
                timeline.append(TimelineItem(kind="event", id=event.event_id))
                continue

            if event.turn_id in turn_by_id and event.turn_id not in turn_seen:
                timeline.append(TimelineItem(kind="turn", id=event.turn_id))
                turn_seen.add(event.turn_id)

        return timeline

    def _build_session(self, source: Path, events: list[Event]) -> Session:
        """Assemble a Session from the parsed events list.

        Default implementation: derive timestamps from events when available.
        Subclasses may override to populate turns, extensions, etc.
        """
        started_at = (
            min(e.timestamp for e in events)
            if events
            else datetime.now(timezone.utc)
        )
        ended_at = max(e.timestamp for e in events) if events else None
        session_id = events[0].session_id if events else uuid4()

        return Session(
            session_id=session_id,
            trajectory_id=uuid4(),
            vendor=self.vendor,
            started_at=started_at,
            ended_at=ended_at,
            events=events,
        )
