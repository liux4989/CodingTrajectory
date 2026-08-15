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

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from coding_trajectory.ingestion.models import Vendor


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RecordSpan(_Strict):
    """Byte range + digest of one raw JSONL record in its source file."""

    byte_offset: int = Field(ge=0)
    byte_end: int = Field(gt=0)
    digest: str


class SessionProvenance(_Strict):
    """Canonical-id -> source-span mapping for one ingested session."""

    session_id: UUID
    vendor: Vendor
    source_path: str
    events: dict[UUID, RecordSpan] = Field(default_factory=dict)
    items: dict[UUID, tuple[RecordSpan, ...]] = Field(default_factory=dict)


__all__ = ["RecordSpan", "SessionProvenance"]
