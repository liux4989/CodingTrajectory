"""Base adapter interface for agent ingestion."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from coding_trajectory.ingestion.models import Session, Vendor


@dataclass(frozen=True, slots=True)
class SessionHeader:
    """Lightweight session metadata extracted without full transcript projection."""

    session_id: UUID
    vendor: Vendor
    parent_session_id: UUID | None = None
    title: str | None = None
    cwd: str | None = None


class BaseAdapter(ABC):
    vendor: Vendor
    _file_glob: str = "*.jsonl"

    def _reset_ingest_state(self) -> None:
        pass

    def _iter_records(self, path: Path) -> Iterator[dict]:
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
                    yield obj

    def _load_records(self, path: Path) -> list[dict]:
        return list(self._iter_records(path))

    def ingest_file(
        self, path: Path, *, parent_started_turn_ids: set[str] | None = None
    ) -> Session:
        self._reset_ingest_state()
        records = self._load_records(path)
        return self._build_session(path, records)

    def ingest_directory(self, directory: Path) -> list[Session]:
        sessions: list[Session] = []
        for source_file in sorted(directory.glob(self._file_glob)):
            sessions.append(self.ingest_file(source_file))
        return sessions

    def ingest(self, source: Path) -> Session:
        return self.ingest_file(source)

    def scan_started_turn_ids(self, source: Path) -> set[str] | None:
        """Return the set of vendor turn_ids that begin a turn in this file, or
        ``None`` when the vendor has no such concept. Used by multi-file
        discovery to give a forked file's adapter the parent's turn-id set so it
        can drop the inherited-history segment it re-materializes.
        """
        return None

    @abstractmethod
    def _build_session(self, source: Path, records: list[dict]) -> Session:
        ...

    @abstractmethod
    def scan_header(self, source: Path) -> SessionHeader | None:
        """Extract lightweight session metadata without projecting the transcript."""
        ...
