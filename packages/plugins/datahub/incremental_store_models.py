"""Revisioned-store contracts: rows, mutations, snapshots, and paging models."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Pydantic boundary model which rejects silently misspelled fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ChangeKind(StrEnum):
    """Filesystem/source transition discovered during reconciliation."""

    NEW = "new"
    APPEND = "append"
    TRUNCATE = "truncate"
    REPLACE = "replace"
    DELETE = "delete"
    REINDEX = "reindex"
    METADATA = "metadata"
    ERROR = "error"


class IngestionStatus(StrEnum):
    """Last observable ingestion state for one source."""

    READY = "ready"
    PARTIAL = "partial"
    ERROR = "error"
    DELETED = "deleted"


class SourceSnapshot(StrictModel):
    """API representation of a persisted source checkpoint."""

    path: str
    file_identity: str | None
    size: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)
    committed_offset: int = Field(ge=0)
    committed_ctime_ns: int = Field(default=0, ge=0)
    prefix_checksum: str | None
    tail_checksum: str | None
    parser_version: str
    schema_version: str
    status: IngestionStatus
    error: str | None
    last_success_revision: int | None
    revision: int
    deleted: bool
    root_link: str | None
    parent_link: str | None
    metadata: dict[str, Any]


class SourceMessage(StrictModel):
    """Validated normalized message passed to a materializer boundary."""

    source_message_id: str
    source_path: str
    byte_offset: int = Field(ge=0)
    byte_end: int = Field(gt=0)
    digest: str
    explicit_event_id: str | None
    event_type: str | None
    event_timestamp: str | None
    root_link: str | None
    parent_link: str | None
    payload: dict[str, Any]
    payload_complete: bool = True


class SourceChange(StrictModel):
    """One changed source and its before/after checkpoint metadata."""

    path: str
    kind: ChangeKind
    previous: SourceSnapshot | None
    current: SourceSnapshot | None
    messages: tuple[SourceMessage, ...] = ()
    invalidated_message_ids: tuple[str, ...] = ()
    trailing_bytes: int = Field(default=0, ge=0)
    error: str | None = None


class DetailSpan(StrictModel):
    """One verified source byte range for a canonical detail object."""

    byte_offset: int = Field(ge=0)
    byte_end: int = Field(gt=0)
    digest: str


class DetailEventRow(StrictModel):
    """Current-only event locator row."""

    event_id: str
    root_id: str
    session_id: str
    source_path: str
    byte_offset: int = Field(ge=0)
    byte_end: int = Field(gt=0)
    digest: str


class DetailItemRow(StrictModel):
    """Current-only item locator row, including spawn/handoff edge targets."""

    item_id: str
    root_id: str
    session_id: str
    turn_id: str
    kind: str
    source_path: str
    spans: tuple[DetailSpan, ...]
    edge_targets: dict[str, str] = Field(default_factory=dict)


class RefreshResult(StrictModel):
    """Result of a complete reconciliation attempt."""

    revision: int = Field(ge=0)
    changed_sources: tuple[SourceChange, ...]
    parsed_bytes: int = Field(ge=0)
    parsed_lines: int = Field(ge=0)
    catching_up: bool
    last_ingested_at: str | None


class EntityMutation(StrictModel):
    """Validated generic read-model mutation accepted by a transaction context."""

    entity_kind: str = Field(min_length=1, max_length=256)
    entity_key: str = Field(min_length=1, max_length=4096)
    scope_key: str = Field(default="", max_length=4096)
    partition_key: str = Field(default="", max_length=4096)
    sort_key: str = Field(max_length=4096)
    tiebreaker: str = Field(min_length=1, max_length=4096)
    payload: dict[str, Any] = Field(default_factory=dict)
    deleted: bool = False


class ProjectionInvalidation(StrictModel):
    """Explicit invalidation published by a projection-only revision."""

    entity_kind: str = Field(min_length=1, max_length=256)
    entity_key: str = Field(min_length=1, max_length=4096)
    details: dict[str, Any] = Field(default_factory=dict)


class EntityRow(StrictModel):
    """Versioned materialized entity returned from an indexed keyset query."""

    entity_kind: str
    entity_key: str
    scope_key: str
    partition_key: str
    sort_key: str
    tiebreaker: str
    payload: dict[str, Any]
    revision: int


class KeysetPage(StrictModel):
    """A page whose opaque cursor is bound to one snapshot revision and order."""

    revision: int
    items: tuple[EntityRow, ...]
    next_cursor: str | None


class RevisionChange(StrictModel):
    """A bounded browser-delivery mutation record."""

    revision: int
    entity_kind: str
    entity_key: str
    operation: Literal["upsert", "delete", "invalidate", "status"]
    payload: dict[str, Any]


class ChangesPage(StrictModel):
    """Revision delta response, including explicit retained-history gap state."""

    from_revision: int
    to_revision: int
    current_revision: int
    retained_from_revision: int | None
    reset_required: bool
    changes: tuple[RevisionChange, ...]
    has_more: bool
    last_ingested_at: str | None
    catching_up: bool


class RefreshFailure(StrictModel):
    """A callback/transaction failure which never published a revision."""

    failure_id: int
    occurred_at: str
    phase: str
    source_paths: tuple[str, ...]
    error: str


class IncompatibleStoreError(RuntimeError):
    """A disposable SQLite store was created by another storage format."""


class SourceFenceError(RuntimeError):
    """A registered source no longer matches its transaction snapshot."""


class _Cursor(StrictModel):
    revision: int = Field(ge=0)
    entity_kind: str
    scope_key: str | None
    partition_key: str | None
    direction: Literal["asc", "desc"]
    sort_key: str
    tiebreaker: str
    entity_key: str


class _DiskMetadata(StrictModel):
    path: str
    file_identity: str
    size: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)
    ctime_ns: int = Field(ge=0)


class _IngestionPlan(StrictModel):
    path: str
    kind: ChangeKind
    disk: _DiskMetadata | None
    previous: SourceSnapshot | None
    messages: tuple[SourceMessage, ...] = ()
    invalidated_message_ids: tuple[str, ...] = ()
    committed_offset: int = Field(ge=0)
    trailing_bytes: int = Field(default=0, ge=0)
    parsed_bytes: int = Field(default=0, ge=0)
    parsed_lines: int = Field(default=0, ge=0)
    error: str | None = None
    omitted_from_inventory: bool = False



