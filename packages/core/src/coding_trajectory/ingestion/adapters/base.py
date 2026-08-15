"""Base adapter interface for agent ingestion."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from coding_trajectory.ingestion.models import Session, Vendor
from coding_trajectory.ingestion.provenance import RecordSpan, SessionProvenance

if TYPE_CHECKING:
    from coding_trajectory.ingestion.retention import CanonicalRetention


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
        for record, _span in self._iter_record_spans(path):
            yield record

    def _iter_record_spans(self, path: Path) -> Iterator[tuple[dict, RecordSpan]]:
        """Yield parsed records with their raw byte spans and digests.

        The digest covers the exact stripped line bytes that ``json.loads``
        parsed, so hydration can verify it re-read the same record bytes.
        """
        import hashlib

        with path.open("rb") as fh:
            offset = 0
            for raw_line in fh:
                end = offset + len(raw_line)
                stripped = raw_line.strip()
                if stripped:
                    try:
                        obj = json.loads(stripped.decode("utf-8"))
                    except json.JSONDecodeError:
                        offset = end
                        continue
                    if isinstance(obj, dict):
                        yield (
                            obj,
                            RecordSpan(
                                byte_offset=offset,
                                byte_end=end,
                                digest=hashlib.sha256(stripped).hexdigest(),
                            ),
                        )
                offset = end

    def _load_records(self, path: Path) -> list[dict]:
        return list(self._iter_records(path))

    def ingest_file(
        self,
        path: Path,
        *,
        parent_started_turn_ids: set[str] | None = None,
        retention: CanonicalRetention = "trajectory",
    ) -> Session:
        """Ingest one source file.

        With ``retention="trajectory"`` the returned session keeps every
        canonical body and carries random pre-stabilization ids; the caller
        applies ``stabilize_session``.  With ``retention="measurements"`` the
        adapter streams records, assigns canonical stable ids inline, and
        discards message/tool bodies at translation time, so the returned
        session is already final and compact; ``self.last_provenance`` then
        carries canonical-id -> source-byte-span mappings.
        """
        self._reset_ingest_state()
        self.last_provenance: SessionProvenance | None = None
        records: Iterable[tuple[dict, RecordSpan | None]] = (
            self._iter_record_spans(path)
            if retention == "measurements"
            else ((record, None) for record in self._load_records(path))
        )
        return self._build_session(path, records, retention=retention)

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
    def _build_session(
        self,
        source: Path,
        records: Iterable[tuple[dict, RecordSpan | None]],
        *,
        retention: CanonicalRetention = "trajectory",
    ) -> Session: ...

    @abstractmethod
    def scan_header(self, source: Path) -> SessionHeader | None:
        """Extract lightweight session metadata without projecting the transcript."""
        ...
