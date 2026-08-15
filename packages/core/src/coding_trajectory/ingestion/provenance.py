"""Source provenance for compact canonical ingestion.

The immutable vendor JSONL files remain the evidence authority.  Provenance
records where each canonical event/item came from — source byte ranges and
digests — so detail views can re-read exactly the originating record bytes
later instead of keeping bodies resident in derived storage.

One canonical object may map to several source ranges: merged agent messages
and tool call/result pairs span multiple records.  Digests verify that the
bytes hydrated later are the same bytes that produced the canonical object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from coding_trajectory.ingestion.models import Vendor


@dataclass(frozen=True, slots=True)
class RecordSpan:
    """Byte range + digest of one raw JSONL record in its source file."""

    byte_offset: int
    byte_end: int
    digest: str


@dataclass(frozen=True, slots=True)
class SessionProvenance:
    """Canonical-id -> source-span mapping for one ingested session."""

    session_id: UUID
    vendor: Vendor
    source_path: str
    events: dict[UUID, RecordSpan] = field(default_factory=dict)
    # Canonical item id -> ordered ids of the events whose spans produced it.
    # Spans are resolved through ``events`` so item locators do not duplicate
    # span objects.
    items: dict[UUID, tuple[UUID, ...]] = field(default_factory=dict)


__all__ = ["RecordSpan", "SessionProvenance"]
