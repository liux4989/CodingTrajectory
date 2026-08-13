"""Incremental, revisioned SQLite index over immutable JSONL sources.

The JSONL files remain authoritative.  This module owns only disposable derived
state: source checkpoints, normalized source messages, versioned generic read
models, and a bounded revision change log.  A refresh is one SQLite transaction,
including an optional materializer callback, so a published checkpoint can
never get ahead of its projections.

The implementation deliberately uses filesystem metadata and small prefix/tail
checksums to discover work.  Unchanged sources are never opened for JSON parsing.
Filesystem notifications may call :meth:`IncrementalStore.refresh` sooner, but
the metadata reconciliation performed by refresh is the correctness mechanism.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import zlib
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, BinaryIO, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


_CHECKSUM_BYTES: Final = 64 * 1024
_MAX_CHANGE_PAYLOAD_BYTES: Final = 64 * 1024
_MAX_CHANGE_IDENTIFIER_BYTES: Final = 16 * 1024
_ENTITY_PARTITION_MIGRATION_VERSION: Final = "1"
_SOURCE_PAYLOAD_MIGRATION_VERSION: Final = "1"
_MAX_CANONICAL_PAYLOAD_BYTES: Final = 256 * 1024 * 1024
_JSON_OBJECT = TypeAdapter(dict[str, Any])


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


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


class StoredPayloadError(RuntimeError):
    """A persisted canonical payload cannot be safely decoded or validated."""


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


EventIdentityExtractor = Callable[[dict[str, Any]], str | None]
Materializer = Callable[["MaterializationContext"], None]


class MaterializationContext:
    """Transaction-scoped projection API supplied to ``refresh(materialize=...)``.

    Instances are valid only during the callback.  All methods operate on the
    same ``BEGIN IMMEDIATE`` transaction as message writes and source offsets.
    Raising from the callback rolls the whole transaction back.
    """

    def __init__(
        self,
        store: IncrementalStore,
        connection: sqlite3.Connection,
        revision: int,
        changes: Sequence[SourceChange],
    ) -> None:
        self._store = store
        self._connection = connection
        self.revision = revision
        self.changed_sources = tuple(changes)
        self._active = True

    def _ensure_active(self) -> None:
        if not self._active:
            raise RuntimeError("materialization context is no longer active")

    def active_messages(
        self, *, source_path: str | None = None
    ) -> Iterator[SourceMessage]:
        """Yield active canonical payloads, with legacy-envelope fallback."""

        self._ensure_active()
        yield from self._store._active_messages(self._connection, source_path)

    def current_sources(
        self, *, include_deleted: bool = False
    ) -> Iterator[SourceSnapshot]:
        """Yield source checkpoints from this transaction's consistent view."""

        self._ensure_active()
        sql = "SELECT * FROM sources"
        if not include_deleted:
            sql += " WHERE deleted = 0"
        sql += " ORDER BY path"
        for row in self._connection.execute(sql):
            yield _source_from_row(row)

    def assert_sources_current(
        self, sources: Iterable[SourceSnapshot] | None = None
    ) -> None:
        """Fence projection commit on unchanged active source metadata.

        With no argument the fence checks every non-deleted registry source from
        this transaction.  Explicit snapshots are useful when a materializer has
        already selected affected siblings.  Tombstones are always ignored,
        including inventory-omitted paths that may still exist on disk.
        """

        self._ensure_active()
        snapshots = self.current_sources() if sources is None else sources
        self._store._assert_source_snapshots_current(tuple(snapshots))

    def mutate(self, mutation: EntityMutation | dict[str, Any]) -> None:
        """Upsert or tombstone one versioned generic projection entity."""

        self._ensure_active()
        validated = (
            mutation
            if isinstance(mutation, EntityMutation)
            else EntityMutation.model_validate(mutation)
        )
        self._store._mutate_entity(self._connection, self.revision, validated)

    def mutate_many(self, mutations: Iterable[EntityMutation | dict[str, Any]]) -> None:
        """Apply multiple validated mutations in the current revision."""

        for mutation in mutations:
            self.mutate(mutation)

    def current_entities(
        self,
        *,
        entity_kinds: Iterable[str] | None = None,
        scope_key: str | None = None,
        partition_key: str | None = None,
    ) -> Iterator[EntityRow]:
        """Yield current entities from this transaction's consistent view.

        Materializers use this after replacing affected graph contributions to
        fold project/overview rollups without opening another connection that
        would miss their uncommitted mutations.
        """

        self._ensure_active()
        kinds = tuple(dict.fromkeys(entity_kinds or ()))
        predicates = ["valid_to_revision IS NULL", "deleted = 0"]
        params: list[Any] = []
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            predicates.append(f"entity_kind IN ({placeholders})")
            params.extend(kinds)
        if scope_key is not None:
            predicates.append("scope_key = ?")
            params.append(scope_key)
        if partition_key is not None:
            predicates.append("partition_key = ?")
            params.append(partition_key)
        sql = f"""
            SELECT entity_kind, entity_key, scope_key, partition_key,
                   sort_key, tiebreaker, payload_json, valid_from_revision
              FROM entity_versions
             WHERE {" AND ".join(predicates)}
             ORDER BY entity_kind, sort_key, tiebreaker, entity_key
        """
        for row in self._connection.execute(sql, params):
            yield EntityRow(
                entity_kind=row["entity_kind"],
                entity_key=row["entity_key"],
                scope_key=row["scope_key"],
                partition_key=row["partition_key"],
                sort_key=row["sort_key"],
                tiebreaker=row["tiebreaker"],
                payload=json.loads(row["payload_json"]),
                revision=row["valid_from_revision"],
            )

    def delete_entity(self, entity_kind: str, entity_key: str) -> None:
        """Tombstone the current version of an entity, if it exists."""

        self._ensure_active()
        self._store._delete_entity(
            self._connection, self.revision, entity_kind, entity_key
        )

    def update_source_metadata(
        self,
        source_path: str,
        *,
        root_link: str | None = None,
        parent_link: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SourceSnapshot:
        """Attach materializer-owned relationship metadata to a source."""

        self._ensure_active()
        validated_metadata = _JSON_OBJECT.validate_python(metadata or {})
        cursor = self._connection.execute(
            """
            UPDATE sources
               SET root_link = ?, parent_link = ?, metadata_json = ?, revision = ?
             WHERE path = ?
            """,
            (
                root_link,
                parent_link,
                _canonical_json(validated_metadata),
                self.revision,
                str(Path(source_path).resolve()),
            ),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"unknown source: {source_path}")
        row = self._connection.execute(
            "SELECT * FROM sources WHERE path = ?",
            (str(Path(source_path).resolve()),),
        ).fetchone()
        assert row is not None
        snapshot = _source_from_row(row)
        self._store._record_change(
            self._connection,
            self.revision,
            "source",
            snapshot.path,
            "upsert",
            {
                "kind": "metadata",
                "root_link": snapshot.root_link,
                "parent_link": snapshot.parent_link,
                "metadata": snapshot.metadata,
            },
        )
        return snapshot

    def record_invalidation(
        self, entity_kind: str, entity_key: str, details: dict[str, Any] | None = None
    ) -> None:
        """Publish a projection invalidation without inventing replacement data."""

        self._ensure_active()
        payload = _JSON_OBJECT.validate_python(details or {})
        self._store._record_change(
            self._connection,
            self.revision,
            entity_kind,
            entity_key,
            "invalidate",
            payload,
        )

    def _close(self) -> None:
        self._active = False


class IncrementalStore:
    """Persistent incremental registry and materialized dashboard read store."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        parser_version: str = "json-object-v1",
        schema_version: str = "dashboard-source-v1",
        retained_change_revisions: int = 512,
        event_identity: EventIdentityExtractor | None = None,
        retain_source_messages: bool = True,
    ) -> None:
        if retained_change_revisions < 1:
            raise ValueError("retained_change_revisions must be at least 1")
        self.database_path = Path(database_path).expanduser().resolve()
        self.parser_version = parser_version
        self.schema_version = schema_version
        self.retained_change_revisions = retained_change_revisions
        self._event_identity = event_identity or _default_event_identity
        self.retain_source_messages = retain_source_messages
        self._refresh_lock = threading.Lock()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS store_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS revisions (
                    revision INTEGER PRIMARY KEY,
                    committed_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_change_count INTEGER NOT NULL,
                    message_count INTEGER NOT NULL,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS sources (
                    source_id INTEGER PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    file_identity TEXT,
                    size INTEGER NOT NULL DEFAULT 0,
                    mtime_ns INTEGER NOT NULL DEFAULT 0,
                    committed_offset INTEGER NOT NULL DEFAULT 0,
                    committed_ctime_ns INTEGER NOT NULL DEFAULT 0,
                    prefix_checksum TEXT,
                    tail_checksum TEXT,
                    parser_version TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    last_success_revision INTEGER,
                    revision INTEGER NOT NULL DEFAULT 0,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    root_link TEXT,
                    parent_link TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS source_messages (
                    source_message_id TEXT PRIMARY KEY,
                    source_id INTEGER NOT NULL REFERENCES sources(source_id),
                    byte_offset INTEGER NOT NULL,
                    byte_end INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    explicit_event_id TEXT,
                    event_type TEXT,
                    event_timestamp TEXT,
                    root_link TEXT,
                    parent_link TEXT,
                    normalized_json TEXT NOT NULL,
                    canonical_json_zlib BLOB,
                    canonical_json_size INTEGER,
                    active INTEGER NOT NULL DEFAULT 1,
                    first_revision INTEGER NOT NULL,
                    last_revision INTEGER NOT NULL,
                    deleted_revision INTEGER,
                    UNIQUE(source_id, byte_offset, digest)
                );
                CREATE INDEX IF NOT EXISTS idx_source_messages_source_active_offset
                    ON source_messages(source_id, active, byte_offset);
                CREATE INDEX IF NOT EXISTS idx_source_messages_root_active
                    ON source_messages(root_link, active);
                CREATE INDEX IF NOT EXISTS idx_source_messages_parent_active
                    ON source_messages(parent_link, active);
                CREATE INDEX IF NOT EXISTS idx_source_messages_type_time
                    ON source_messages(event_type, event_timestamp);

                CREATE TABLE IF NOT EXISTS entity_versions (
                    entity_kind TEXT NOT NULL,
                    entity_key TEXT NOT NULL,
                    valid_from_revision INTEGER NOT NULL,
                    valid_to_revision INTEGER,
                    scope_key TEXT NOT NULL DEFAULT '',
                    partition_key TEXT NOT NULL DEFAULT '',
                    sort_key TEXT NOT NULL,
                    tiebreaker TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(entity_kind, entity_key, valid_from_revision)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_current
                    ON entity_versions(entity_kind, entity_key)
                    WHERE valid_to_revision IS NULL;
                CREATE TABLE IF NOT EXISTS revision_changes (
                    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    revision INTEGER NOT NULL REFERENCES revisions(revision),
                    entity_kind TEXT NOT NULL,
                    entity_key TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_revision_changes_revision
                    ON revision_changes(revision, change_id);

                CREATE TABLE IF NOT EXISTS refresh_failures (
                    failure_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    source_paths_json TEXT NOT NULL,
                    error TEXT NOT NULL
                );
                """
            )
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            self._migrate_source_ctime(connection)
            self._migrate_source_payloads(connection)
            self._migrate_entity_partitions(connection)
            connection.execute(
                "INSERT OR IGNORE INTO store_metadata(key, value) VALUES('revision', '0')"
            )
            connection.execute(
                "INSERT OR IGNORE INTO store_metadata(key, value) VALUES('last_ingested_at', '')"
            )
            connection.execute(
                "INSERT OR IGNORE INTO store_metadata(key, value) VALUES('catching_up', '0')"
            )
            connection.execute(
                "INSERT OR IGNORE INTO store_metadata(key, value) "
                "VALUES('changes_pruned_through', '0')"
            )
            connection.execute(
                "INSERT OR IGNORE INTO store_metadata(key, value) VALUES('cursor_secret', ?)",
                (secrets.token_hex(32),),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _migrate_source_ctime(connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(sources)")
        }
        if "committed_ctime_ns" not in columns:
            connection.execute(
                "ALTER TABLE sources ADD COLUMN committed_ctime_ns "
                "INTEGER NOT NULL DEFAULT 0"
            )

    @staticmethod
    def _migrate_source_payloads(connection: sqlite3.Connection) -> None:
        """Add compressed canonical payload columns without rewriting legacy rows."""

        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(source_messages)")
        }
        if "canonical_json_zlib" not in columns:
            connection.execute(
                "ALTER TABLE source_messages ADD COLUMN canonical_json_zlib BLOB"
            )
        if "canonical_json_size" not in columns:
            connection.execute(
                "ALTER TABLE source_messages ADD COLUMN canonical_json_size INTEGER"
            )
        connection.execute(
            """
            INSERT INTO store_metadata(key, value)
            VALUES('source_payload_migration', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (_SOURCE_PAYLOAD_MIGRATION_VERSION,),
        )

    @staticmethod
    def _migrate_entity_partitions(connection: sqlite3.Connection) -> None:
        """Add disposable-store partition columns created by early v1 builds."""

        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(entity_versions)")
        }
        marker = connection.execute(
            "SELECT value FROM store_metadata WHERE key = ?",
            ("entity_partition_migration",),
        ).fetchone()
        requires_rebuild = bool(
            (marker is None or marker[0] != _ENTITY_PARTITION_MIGRATION_VERSION)
            and connection.execute("SELECT 1 FROM entity_versions LIMIT 1").fetchone()
        )
        if "scope_key" not in columns:
            connection.execute(
                "ALTER TABLE entity_versions ADD COLUMN scope_key TEXT NOT NULL DEFAULT ''"
            )
        if "partition_key" not in columns:
            connection.execute(
                "ALTER TABLE entity_versions ADD COLUMN partition_key TEXT NOT NULL DEFAULT ''"
            )
        connection.execute("DROP INDEX IF EXISTS idx_entity_keyset")
        connection.execute("DROP INDEX IF EXISTS idx_entity_partition_keyset")
        connection.execute(
            """
            CREATE INDEX idx_entity_keyset ON entity_versions(
                entity_kind, scope_key, deleted, sort_key, tiebreaker,
                entity_key, valid_from_revision, valid_to_revision
            )
            """
        )
        if requires_rebuild:
            # SQLite is disposable derived state.  Early v1 rows cannot be
            # assigned a trustworthy scope/partition after the fact, so make
            # the missing projection observable to the runtime and force a
            # canonical rebuild instead of silently serving empty filters.
            connection.execute("DELETE FROM entity_versions")
            connection.execute("DELETE FROM revision_changes")
            revision_row = connection.execute(
                "SELECT value FROM store_metadata WHERE key = 'revision'"
            ).fetchone()
            revision = int(revision_row[0]) if revision_row is not None else 0
            connection.execute(
                """
                INSERT INTO store_metadata(key, value)
                VALUES('changes_pruned_through', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(revision),),
            )
        connection.execute(
            """
            INSERT INTO store_metadata(key, value)
            VALUES('entity_partition_migration', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (_ENTITY_PARTITION_MIGRATION_VERSION,),
        )
        connection.execute(
            """
            CREATE INDEX idx_entity_partition_keyset ON entity_versions(
                entity_kind, scope_key, partition_key, deleted, sort_key,
                tiebreaker, entity_key, valid_from_revision, valid_to_revision
            )
            """
        )

    def current_revision(self) -> int:
        """Return the latest atomically published derived snapshot revision."""

        with self._connect() as connection:
            return self._current_revision(connection)

    def source(self, path: str | Path) -> SourceSnapshot | None:
        """Return one persisted source checkpoint."""

        resolved = str(Path(path).expanduser().resolve())
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sources WHERE path = ?", (resolved,)
            ).fetchone()
        return _source_from_row(row) if row is not None else None

    def sources(self, *, include_deleted: bool = True) -> tuple[SourceSnapshot, ...]:
        """Return source registry rows without touching JSONL files."""

        sql = "SELECT * FROM sources"
        if not include_deleted:
            sql += " WHERE deleted = 0"
        sql += " ORDER BY path"
        with self._connect() as connection:
            rows = connection.execute(sql).fetchall()
        return tuple(_source_from_row(row) for row in rows)

    def failures(self, *, limit: int = 100) -> tuple[RefreshFailure, ...]:
        """Return recent non-published transaction/materialization failures."""

        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM refresh_failures
                 ORDER BY failure_id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(
            RefreshFailure(
                failure_id=row["failure_id"],
                occurred_at=row["occurred_at"],
                phase=row["phase"],
                source_paths=tuple(json.loads(row["source_paths_json"])),
                error=row["error"],
            )
            for row in rows
        )

    def garbage_collect(self, *, compact: bool = False) -> dict[str, int | bool]:
        """Prune expired derived history and optionally reclaim SQLite pages.

        Revision changes define the supported snapshot TTL. Entity versions
        older than that boundary cannot be reached by a valid browser cursor and
        are disposable. Source-message tombstones are never a read authority and
        can be removed immediately; checkpoint-only stores remove all legacy
        transcript copies.
        """

        with self._refresh_lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cutoff = int(
                    self._metadata(connection, "changes_pruned_through") or "0"
                )
                if self.retain_source_messages:
                    messages_deleted = connection.execute(
                        "DELETE FROM source_messages WHERE active = 0"
                    ).rowcount
                else:
                    messages_deleted = connection.execute(
                        "DELETE FROM source_messages"
                    ).rowcount
                versions_deleted = connection.execute(
                    """
                    DELETE FROM entity_versions
                     WHERE valid_to_revision IS NOT NULL
                       AND valid_to_revision <= ?
                    """,
                    (cutoff,),
                ).rowcount
                tombstones_deleted = connection.execute(
                    """
                    DELETE FROM entity_versions
                     WHERE valid_to_revision IS NULL AND deleted = 1
                       AND valid_from_revision <= ?
                    """,
                    (cutoff,),
                ).rowcount
                connection.commit()
                page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
                pages_before = int(
                    connection.execute("PRAGMA page_count").fetchone()[0]
                )
                free_before = int(
                    connection.execute("PRAGMA freelist_count").fetchone()[0]
                )
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

            reclaimable_bytes = free_before * page_size
            compacted = bool(
                compact
                and reclaimable_bytes >= 64 * 1024 * 1024
                and free_before * 4 >= max(pages_before, 1)
            )
            if compacted:
                with self._connect() as compact_connection:
                    compact_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    compact_connection.execute("VACUUM")
                with self._connect() as measured:
                    pages_after = int(
                        measured.execute("PRAGMA page_count").fetchone()[0]
                    )
            else:
                pages_after = pages_before
        return {
            "messages_deleted": max(messages_deleted, 0),
            "entity_versions_deleted": max(versions_deleted, 0),
            "tombstones_deleted": max(tombstones_deleted, 0),
            "reclaimable_bytes": reclaimable_bytes,
            "compacted": compacted,
            "database_bytes_before": pages_before * page_size,
            "database_bytes_after": pages_after * page_size,
        }

    def refresh(
        self,
        candidates: Iterable[str | Path],
        *,
        materialize: Materializer | None = None,
    ) -> RefreshResult:
        """Reconcile candidate JSONL paths and publish one consistent revision.

        The candidate set is an authoritative inventory for deletion detection.
        Callers should therefore pass all in-scope paths on periodic/startup
        reconciliation.  Watcher-driven partial refreshes can use
        :meth:`refresh_paths`, which does not infer deletion of omitted paths.
        """

        return self._refresh(candidates, materialize=materialize, inventory=True)

    def refresh_paths(
        self,
        paths: Iterable[str | Path],
        *,
        materialize: Materializer | None = None,
    ) -> RefreshResult:
        """Refresh explicit paths without treating other registered paths as deleted."""

        return self._refresh(paths, materialize=materialize, inventory=False)

    def materialize_revision(
        self,
        materialize: Materializer,
        *,
        status: Literal["complete", "catching_up"] = "complete",
        invalidations: Iterable[ProjectionInvalidation | dict[str, Any]] = (),
    ) -> RefreshResult:
        """Atomically publish projections without advancing source checkpoints.

        This is the bounded-bootstrap boundary: callers may ingest a large corpus
        one source at a time, then compute and publish a canonical aggregate
        snapshot in a final independent revision.  Regular append projection
        should continue to use ``refresh(..., materialize=...)`` so offsets and
        affected read models share one commit.
        """

        validated_invalidations = tuple(
            item
            if isinstance(item, ProjectionInvalidation)
            else ProjectionInvalidation.model_validate(item)
            for item in invalidations
        )
        with self._refresh_lock:
            connection = self._connect()
            context: MaterializationContext | None = None
            try:
                connection.execute("BEGIN IMMEDIATE")
                revision = self._current_revision(connection) + 1
                committed_at = _utc_now()
                connection.execute(
                    """
                    INSERT INTO revisions(
                        revision, committed_at, status, source_change_count,
                        message_count, error
                    ) VALUES(?, ?, 'pending', 0, 0, NULL)
                    """,
                    (revision, committed_at),
                )
                context = MaterializationContext(self, connection, revision, ())
                for invalidation in validated_invalidations:
                    context.record_invalidation(
                        invalidation.entity_kind,
                        invalidation.entity_key,
                        invalidation.details,
                    )
                materialize(context)
                context._close()
                connection.execute(
                    "UPDATE revisions SET status = ? WHERE revision = ?",
                    (status, revision),
                )
                self._set_metadata(connection, "revision", str(revision))
                self._set_metadata(
                    connection,
                    "catching_up",
                    "1" if status == "catching_up" else "0",
                )
                self._prune_changes(connection, revision)
                last_ingested_at = (
                    self._metadata(connection, "last_ingested_at") or None
                )
                connection.commit()
            except Exception as exc:
                if context is not None:
                    context._close()
                connection.rollback()
                self._record_failure(
                    phase="materialize_revision",
                    source_paths=(),
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise
            finally:
                connection.close()
        return RefreshResult(
            revision=revision,
            changed_sources=(),
            parsed_bytes=0,
            parsed_lines=0,
            catching_up=status == "catching_up",
            last_ingested_at=last_ingested_at,
        )

    def _refresh(
        self,
        candidates: Iterable[str | Path],
        *,
        materialize: Materializer | None,
        inventory: bool,
    ) -> RefreshResult:
        resolved = tuple(
            sorted({str(Path(path).expanduser().resolve()) for path in candidates})
        )
        with self._refresh_lock:
            with self._connect() as read_connection:
                previous = {
                    row["path"]: _source_from_row(row)
                    for row in read_connection.execute("SELECT * FROM sources")
                }
                current_revision = self._current_revision(read_connection)
                last_ingested_at = (
                    self._metadata(read_connection, "last_ingested_at") or None
                )

            try:
                plans = self._build_plans(resolved, previous, inventory=inventory)
            except Exception as exc:
                self._record_failure(
                    phase="plan",
                    source_paths=resolved,
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise
            if not plans:
                # Another process may have committed between the first read
                # and plan construction.  Return current persisted state rather
                # than regressing the caller's revision/freshness view.
                with self._connect() as current_connection:
                    current_revision = self._current_revision(current_connection)
                    last_ingested_at = (
                        self._metadata(current_connection, "last_ingested_at") or None
                    )
                    catching_up = (
                        self._metadata(current_connection, "catching_up") == "1"
                    )
                return RefreshResult(
                    revision=current_revision,
                    changed_sources=(),
                    parsed_bytes=0,
                    parsed_lines=0,
                    catching_up=catching_up,
                    last_ingested_at=last_ingested_at,
                )
            return self._commit_plans(plans, materialize)

    def _build_plans(
        self,
        paths: Sequence[str],
        previous: dict[str, SourceSnapshot],
        *,
        inventory: bool,
    ) -> list[_IngestionPlan]:
        plans: list[_IngestionPlan] = []
        requested = set(paths)
        if inventory:
            for path, snapshot in previous.items():
                if path not in requested and not snapshot.deleted:
                    plans.append(
                        _IngestionPlan(
                            path=path,
                            kind=ChangeKind.DELETE,
                            disk=None,
                            previous=snapshot,
                            invalidated_message_ids=(
                                self._message_ids(path)
                                if self.retain_source_messages
                                else ()
                            ),
                            committed_offset=snapshot.committed_offset,
                            omitted_from_inventory=True,
                        )
                    )
        for path in paths:
            snapshot = previous.get(path)
            try:
                disk = _disk_metadata(path)
            except FileNotFoundError:
                if snapshot is not None and not snapshot.deleted:
                    plans.append(
                        _IngestionPlan(
                            path=path,
                            kind=ChangeKind.DELETE,
                            disk=None,
                            previous=snapshot,
                            invalidated_message_ids=(
                                self._message_ids(path)
                                if self.retain_source_messages
                                else ()
                            ),
                            committed_offset=snapshot.committed_offset,
                        )
                    )
                continue
            kind = self._classify(snapshot, disk)
            if kind is None:
                continue
            plans.append(self._read_plan(path, snapshot, disk, kind))
        return plans

    def _classify(
        self, snapshot: SourceSnapshot | None, disk: _DiskMetadata
    ) -> ChangeKind | None:
        if snapshot is None or snapshot.deleted:
            return ChangeKind.NEW
        if (
            snapshot.parser_version != self.parser_version
            or snapshot.schema_version != self.schema_version
        ):
            return ChangeKind.REINDEX
        if snapshot.file_identity != disk.file_identity:
            return ChangeKind.REPLACE
        if disk.size < snapshot.committed_offset or disk.size < snapshot.size:
            return ChangeKind.TRUNCATE
        if disk.size == snapshot.size and disk.mtime_ns == snapshot.mtime_ns:
            if (
                snapshot.status == IngestionStatus.READY
                and snapshot.committed_offset == disk.size
            ):
                return None
            if snapshot.status in {IngestionStatus.PARTIAL, IngestionStatus.ERROR}:
                return None
        if disk.size == snapshot.size:
            # A changed mtime with identical identity/size is a suspicious
            # rewrite.  Prefix/tail sampling cannot prove that the interior is
            # unchanged, so reparse it as a replacement.  Exact-metadata
            # steady-state checks above still avoid opening unchanged sources.
            return ChangeKind.REPLACE
        # Appends update ctime too, so ctime is retained as useful checkpoint
        # evidence but cannot distinguish append from rewrite+grow.  The saved
        # prefix/tail checksums authorize the incremental path below; identical-
        # size rewrites were already classified as replacements above.
        if not _checkpoint_matches(snapshot, disk.path):
            return ChangeKind.REPLACE
        if disk.size > snapshot.committed_offset:
            return ChangeKind.APPEND
        if snapshot.status == IngestionStatus.ERROR:
            return ChangeKind.APPEND
        return ChangeKind.METADATA

    def _read_plan(
        self,
        path: str,
        previous: SourceSnapshot | None,
        disk: _DiskMetadata,
        kind: ChangeKind,
    ) -> _IngestionPlan:
        reset = kind in {
            ChangeKind.NEW,
            ChangeKind.TRUNCATE,
            ChangeKind.REPLACE,
            ChangeKind.REINDEX,
        }
        start = 0 if reset else (previous.committed_offset if previous else 0)
        invalidated = (
            self._message_ids(path)
            if self.retain_source_messages and reset and previous
            else ()
        )
        if not self.retain_source_messages:
            try:
                with open(path, "rb") as source:
                    opened = os.fstat(source.fileno())
                    opened_identity = f"{opened.st_dev}:{opened.st_ino}"
                    if opened_identity != disk.file_identity:
                        raise RuntimeError("source identity changed during scan")
                    committed = _last_complete_line_offset(source, disk.size)
                after = _disk_metadata(path)
                if after != disk:
                    raise RuntimeError("source metadata changed during scan")
                return _IngestionPlan(
                    path=path,
                    kind=kind,
                    disk=after,
                    previous=previous,
                    committed_offset=committed,
                    trailing_bytes=disk.size - committed,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                return _IngestionPlan(
                    path=path,
                    kind=kind,
                    disk=disk,
                    previous=previous,
                    committed_offset=previous.committed_offset if previous else 0,
                    error=f"{type(exc).__name__}: {exc}",
                )
        try:
            with open(path, "rb") as source:
                opened = os.fstat(source.fileno())
                opened_identity = f"{opened.st_dev}:{opened.st_ino}"
                if opened_identity != disk.file_identity:
                    raise RuntimeError("source identity changed during scan")
                source.seek(start)
                chunk = source.read(max(0, disk.size - start))
            after = _disk_metadata(path)
            if after != disk:
                raise RuntimeError("source metadata changed during scan")
            messages, committed, trailing, parsed_lines = self._parse_complete_lines(
                path, disk.file_identity, chunk, start
            )
            return _IngestionPlan(
                path=path,
                kind=kind,
                disk=after,
                previous=previous,
                messages=tuple(messages),
                invalidated_message_ids=invalidated,
                committed_offset=committed,
                trailing_bytes=trailing,
                parsed_bytes=committed - start,
                parsed_lines=parsed_lines,
            )
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            RuntimeError,
        ) as exc:
            return _IngestionPlan(
                path=path,
                kind=kind,
                disk=disk,
                previous=previous,
                invalidated_message_ids=(),
                committed_offset=start,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _parse_complete_lines(
        self,
        path: str,
        file_identity: str,
        chunk: bytes,
        start: int,
    ) -> tuple[list[SourceMessage], int, int, int]:
        messages: list[SourceMessage] = []
        cursor = 0
        parsed_lines = 0
        while True:
            newline = chunk.find(b"\n", cursor)
            if newline < 0:
                break
            raw_with_ending = chunk[cursor : newline + 1]
            raw = raw_with_ending[:-1]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            byte_offset = start + cursor
            byte_end = start + newline + 1
            cursor = newline + 1
            if not raw.strip():
                continue
            payload = _JSON_OBJECT.validate_python(json.loads(raw.decode("utf-8")))
            line_digest = _digest(raw)
            explicit = self._event_identity(payload)
            source_message_id = _message_identity(
                path, file_identity, byte_offset, line_digest, explicit
            )
            event_type, timestamp, root_link, parent_link = _message_metadata(payload)
            messages.append(
                SourceMessage(
                    source_message_id=source_message_id,
                    source_path=path,
                    byte_offset=byte_offset,
                    byte_end=byte_end,
                    digest=line_digest,
                    explicit_event_id=explicit,
                    event_type=event_type,
                    event_timestamp=timestamp,
                    root_link=root_link,
                    parent_link=parent_link,
                    payload=payload,
                )
            )
            parsed_lines += 1
        return messages, start + cursor, len(chunk) - cursor, parsed_lines

    def _commit_plans(
        self, plans: Sequence[_IngestionPlan], materialize: Materializer | None
    ) -> RefreshResult:
        connection = self._connect()
        context: MaterializationContext | None = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            revision = self._current_revision(connection) + 1
            committed_at = _utc_now()
            connection.execute(
                """
                INSERT INTO revisions(
                    revision, committed_at, status, source_change_count,
                    message_count, error
                ) VALUES(?, ?, 'pending', ?, ?, NULL)
                """,
                (
                    revision,
                    committed_at,
                    len(plans),
                    sum(len(plan.messages) for plan in plans),
                ),
            )
            self._assert_plans_current(connection, plans)
            self._assert_files_current(plans)
            source_changes = [
                self._apply_plan(connection, revision, plan) for plan in plans
            ]
            context = MaterializationContext(self, connection, revision, source_changes)
            if materialize is not None:
                materialize(context)
            # An affected-graph rebuild may outlive the byte scan.  Do not
            # publish offsets and projections if any participating file moved
            # while the materializer was reading it.
            self._assert_files_current(plans)
            context._close()
            source_changes = [
                change.model_copy(
                    update={
                        "current": _source_from_row(
                            connection.execute(
                                "SELECT * FROM sources WHERE path = ?",
                                (change.path,),
                            ).fetchone()
                        )
                    }
                )
                for change in source_changes
            ]
            connection.execute(
                "UPDATE revisions SET status = ? WHERE revision = ?",
                (
                    "error" if any(plan.error for plan in plans) else "complete",
                    revision,
                ),
            )
            self._set_metadata(connection, "revision", str(revision))
            self._set_metadata(connection, "last_ingested_at", committed_at)
            self._set_metadata(
                connection,
                "catching_up",
                "1" if any(change.trailing_bytes for change in source_changes) else "0",
            )
            self._prune_changes(connection, revision)
            connection.commit()
        except Exception as exc:
            if context is not None:
                context._close()
            connection.rollback()
            self._record_failure(
                phase="materialize" if context is not None else "commit",
                source_paths=[plan.path for plan in plans],
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        finally:
            connection.close()
        return RefreshResult(
            revision=revision,
            changed_sources=tuple(source_changes),
            parsed_bytes=sum(plan.parsed_bytes for plan in plans),
            parsed_lines=sum(plan.parsed_lines for plan in plans),
            catching_up=any(change.trailing_bytes for change in source_changes),
            last_ingested_at=committed_at,
        )

    @staticmethod
    def _assert_plans_current(
        connection: sqlite3.Connection, plans: Sequence[_IngestionPlan]
    ) -> None:
        for plan in plans:
            row = connection.execute(
                "SELECT revision FROM sources WHERE path = ?", (plan.path,)
            ).fetchone()
            expected = plan.previous.revision if plan.previous is not None else None
            actual = int(row[0]) if row is not None else None
            if actual != expected:
                raise RuntimeError(
                    f"source checkpoint changed during refresh: {plan.path}"
                )

    @staticmethod
    def _assert_files_current(plans: Sequence[_IngestionPlan]) -> None:
        for plan in plans:
            if plan.kind == ChangeKind.DELETE:
                if not plan.omitted_from_inventory and Path(plan.path).exists():
                    raise RuntimeError(
                        f"deleted source reappeared during refresh: {plan.path}"
                    )
                continue
            assert plan.disk is not None
            try:
                current = _disk_metadata(plan.path)
            except (OSError, ValueError) as exc:
                raise RuntimeError(
                    f"source changed during refresh: {plan.path}"
                ) from exc
            if current != plan.disk:
                raise RuntimeError(
                    f"source metadata changed during refresh: {plan.path}"
                )

    @staticmethod
    def _assert_source_snapshots_current(
        snapshots: Sequence[SourceSnapshot],
    ) -> None:
        for snapshot in snapshots:
            if snapshot.deleted:
                continue
            try:
                current = _disk_metadata(snapshot.path)
            except (OSError, ValueError) as exc:
                raise SourceFenceError(
                    f"registered source is unavailable: {snapshot.path}"
                ) from exc
            metadata_changed = (
                current.file_identity != snapshot.file_identity
                or current.size != snapshot.size
                or current.mtime_ns != snapshot.mtime_ns
                or (
                    snapshot.committed_ctime_ns > 0
                    and current.ctime_ns != snapshot.committed_ctime_ns
                )
            )
            if metadata_changed:
                raise SourceFenceError(
                    f"registered source metadata changed: {snapshot.path}"
                )

    def _record_failure(
        self, *, phase: str, source_paths: Sequence[str], error: str
    ) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO refresh_failures(
                        occurred_at, phase, source_paths_json, error
                    ) VALUES(?, ?, ?, ?)
                    """,
                    (_utc_now(), phase, _canonical_json(source_paths), error),
                )
                connection.execute(
                    """
                    DELETE FROM refresh_failures
                     WHERE failure_id NOT IN (
                         SELECT failure_id FROM refresh_failures
                          ORDER BY failure_id DESC LIMIT 1000
                     )
                    """
                )
        except sqlite3.Error:
            # Preserve the original failure if even best-effort diagnostics fail.
            pass

    def _apply_plan(
        self, connection: sqlite3.Connection, revision: int, plan: _IngestionPlan
    ) -> SourceChange:
        previous = plan.previous
        if plan.kind == ChangeKind.DELETE:
            assert previous is not None
            source_id = self._source_id(connection, plan.path)
            if self.retain_source_messages:
                self._invalidate_messages(connection, source_id, revision)
            else:
                connection.execute(
                    "DELETE FROM source_messages WHERE source_id = ?", (source_id,)
                )
            connection.execute(
                """
                UPDATE sources
                   SET status = ?, error = NULL, deleted = 1, revision = ?,
                       last_success_revision = ?
                 WHERE source_id = ?
                """,
                (IngestionStatus.DELETED, revision, revision, source_id),
            )
            current = _source_from_row(
                connection.execute(
                    "SELECT * FROM sources WHERE source_id = ?", (source_id,)
                ).fetchone()
            )
            change = SourceChange(
                path=plan.path,
                kind=plan.kind,
                previous=previous,
                current=current,
                invalidated_message_ids=plan.invalidated_message_ids,
            )
            self._record_source_change(connection, revision, change)
            return change

        assert plan.disk is not None
        source_id = self._ensure_source(connection, plan, revision)
        if not self.retain_source_messages:
            connection.execute(
                "DELETE FROM source_messages WHERE source_id = ?", (source_id,)
            )
        if plan.error:
            connection.execute(
                """
                UPDATE sources
                   SET file_identity = ?, size = ?, mtime_ns = ?, status = ?,
                       error = ?, revision = ?, deleted = 0
                 WHERE source_id = ?
                """,
                (
                    plan.disk.file_identity,
                    plan.disk.size,
                    plan.disk.mtime_ns,
                    IngestionStatus.ERROR,
                    plan.error,
                    revision,
                    source_id,
                ),
            )
        else:
            if plan.kind in {
                ChangeKind.TRUNCATE,
                ChangeKind.REPLACE,
                ChangeKind.REINDEX,
            }:
                self._invalidate_messages(connection, source_id, revision)
            for message in plan.messages:
                self._upsert_message(connection, source_id, revision, message)
            prefix, tail = _checkpoint_checksums(plan.path, plan.committed_offset)
            status = (
                IngestionStatus.PARTIAL
                if plan.trailing_bytes
                else IngestionStatus.READY
            )
            connection.execute(
                """
                UPDATE sources
                   SET file_identity = ?, size = ?, mtime_ns = ?,
                       committed_offset = ?, committed_ctime_ns = ?,
                       prefix_checksum = ?, tail_checksum = ?,
                       parser_version = ?, schema_version = ?, status = ?, error = NULL,
                       last_success_revision = ?, revision = ?, deleted = 0
                 WHERE source_id = ?
                """,
                (
                    plan.disk.file_identity,
                    plan.disk.size,
                    plan.disk.mtime_ns,
                    plan.committed_offset,
                    plan.disk.ctime_ns,
                    prefix,
                    tail,
                    self.parser_version,
                    self.schema_version,
                    status,
                    revision,
                    revision,
                    source_id,
                ),
            )
        current = _source_from_row(
            connection.execute(
                "SELECT * FROM sources WHERE source_id = ?", (source_id,)
            ).fetchone()
        )
        change = SourceChange(
            path=plan.path,
            kind=ChangeKind.ERROR if plan.error else plan.kind,
            previous=previous,
            current=current,
            messages=plan.messages if not plan.error else (),
            invalidated_message_ids=(
                plan.invalidated_message_ids if not plan.error else ()
            ),
            trailing_bytes=plan.trailing_bytes,
            error=plan.error,
        )
        self._record_source_change(connection, revision, change)
        return change

    def _ensure_source(
        self, connection: sqlite3.Connection, plan: _IngestionPlan, revision: int
    ) -> int:
        connection.execute(
            """
            INSERT INTO sources(
                path, file_identity, size, mtime_ns, committed_offset,
                parser_version, schema_version, status, revision, deleted
            ) VALUES(?, ?, ?, ?, 0, ?, ?, ?, ?, 0)
            ON CONFLICT(path) DO NOTHING
            """,
            (
                plan.path,
                plan.disk.file_identity if plan.disk else None,
                plan.disk.size if plan.disk else 0,
                plan.disk.mtime_ns if plan.disk else 0,
                self.parser_version,
                self.schema_version,
                IngestionStatus.ERROR if plan.error else IngestionStatus.READY,
                revision,
            ),
        )
        return self._source_id(connection, plan.path)

    @staticmethod
    def _source_id(connection: sqlite3.Connection, path: str) -> int:
        row = connection.execute(
            "SELECT source_id FROM sources WHERE path = ?", (path,)
        ).fetchone()
        if row is None:
            raise KeyError(path)
        return int(row[0])

    @staticmethod
    def _invalidate_messages(
        connection: sqlite3.Connection, source_id: int, revision: int
    ) -> None:
        connection.execute(
            """
            UPDATE source_messages
               SET active = 0, last_revision = ?, deleted_revision = ?
             WHERE source_id = ? AND active = 1
            """,
            (revision, revision, source_id),
        )

    @staticmethod
    def _upsert_message(
        connection: sqlite3.Connection,
        source_id: int,
        revision: int,
        message: SourceMessage,
    ) -> None:
        canonical_payload, canonical_size = _compress_canonical_payload(message.payload)
        connection.execute(
            """
            INSERT INTO source_messages(
                source_message_id, source_id, byte_offset, byte_end, digest,
                explicit_event_id, event_type, event_timestamp, root_link,
                parent_link, normalized_json, canonical_json_zlib,
                canonical_json_size, active, first_revision, last_revision,
                deleted_revision
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, NULL)
            ON CONFLICT(source_message_id) DO UPDATE SET
                source_id = excluded.source_id,
                byte_offset = excluded.byte_offset,
                byte_end = excluded.byte_end,
                digest = excluded.digest,
                explicit_event_id = excluded.explicit_event_id,
                event_type = excluded.event_type,
                event_timestamp = excluded.event_timestamp,
                root_link = excluded.root_link,
                parent_link = excluded.parent_link,
                normalized_json = excluded.normalized_json,
                canonical_json_zlib = excluded.canonical_json_zlib,
                canonical_json_size = excluded.canonical_json_size,
                active = 1,
                last_revision = excluded.last_revision,
                deleted_revision = NULL
            """,
            (
                message.source_message_id,
                source_id,
                message.byte_offset,
                message.byte_end,
                message.digest,
                message.explicit_event_id,
                message.event_type,
                message.event_timestamp,
                message.root_link,
                message.parent_link,
                _canonical_json(_normalized_envelope(message.payload)),
                canonical_payload,
                canonical_size,
                revision,
                revision,
            ),
        )

    def _active_messages(
        self, connection: sqlite3.Connection, source_path: str | None
    ) -> Iterator[SourceMessage]:
        params: tuple[Any, ...] = ()
        sql = """
            SELECT m.*, s.path
              FROM source_messages AS m
              JOIN sources AS s ON s.source_id = m.source_id
             WHERE m.active = 1
        """
        if source_path is not None:
            sql += " AND s.path = ?"
            params = (str(Path(source_path).expanduser().resolve()),)
        sql += " ORDER BY s.path, m.byte_offset"
        for row in connection.execute(sql, params):
            yield _message_from_row(row)

    def _message_ids(self, path: str) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT m.source_message_id
                  FROM source_messages AS m
                  JOIN sources AS s ON s.source_id = m.source_id
                 WHERE s.path = ? AND m.active = 1
                 ORDER BY m.byte_offset
                """,
                (path,),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def _mutate_entity(
        self,
        connection: sqlite3.Connection,
        revision: int,
        mutation: EntityMutation,
    ) -> None:
        current = connection.execute(
            """
            SELECT * FROM entity_versions
             WHERE entity_kind = ? AND entity_key = ?
               AND valid_to_revision IS NULL
            """,
            (mutation.entity_kind, mutation.entity_key),
        ).fetchone()
        payload_json = _canonical_json(mutation.payload)
        if current is not None and (
            current["scope_key"],
            current["partition_key"],
            current["sort_key"],
            current["tiebreaker"],
            current["payload_json"],
            bool(current["deleted"]),
        ) == (
            mutation.scope_key,
            mutation.partition_key,
            mutation.sort_key,
            mutation.tiebreaker,
            payload_json,
            mutation.deleted,
        ):
            return
        if current is not None:
            if current["valid_from_revision"] == revision:
                connection.execute(
                    """
                    DELETE FROM entity_versions
                     WHERE entity_kind = ? AND entity_key = ?
                       AND valid_from_revision = ?
                    """,
                    (mutation.entity_kind, mutation.entity_key, revision),
                )
            else:
                connection.execute(
                    """
                    UPDATE entity_versions SET valid_to_revision = ?
                     WHERE entity_kind = ? AND entity_key = ?
                       AND valid_to_revision IS NULL
                    """,
                    (revision - 1, mutation.entity_kind, mutation.entity_key),
                )
        connection.execute(
            """
            INSERT INTO entity_versions(
                entity_kind, entity_key, valid_from_revision, valid_to_revision,
                scope_key, partition_key, sort_key, tiebreaker, payload_json,
                deleted
            ) VALUES(?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                mutation.entity_kind,
                mutation.entity_key,
                revision,
                mutation.scope_key,
                mutation.partition_key,
                mutation.sort_key,
                mutation.tiebreaker,
                payload_json,
                int(mutation.deleted),
            ),
        )
        operation: Literal["upsert", "delete"] = (
            "delete" if mutation.deleted else "upsert"
        )
        self._record_change(
            connection,
            revision,
            mutation.entity_kind,
            mutation.entity_key,
            operation,
            mutation.payload,
        )

    def _delete_entity(
        self,
        connection: sqlite3.Connection,
        revision: int,
        entity_kind: str,
        entity_key: str,
    ) -> None:
        current = connection.execute(
            """
            SELECT scope_key, partition_key, sort_key, tiebreaker, deleted
              FROM entity_versions
             WHERE entity_kind = ? AND entity_key = ?
               AND valid_to_revision IS NULL
            """,
            (entity_kind, entity_key),
        ).fetchone()
        if current is None or current["deleted"]:
            return
        self._mutate_entity(
            connection,
            revision,
            EntityMutation(
                entity_kind=entity_kind,
                entity_key=entity_key,
                scope_key=current["scope_key"],
                partition_key=current["partition_key"],
                sort_key=current["sort_key"],
                tiebreaker=current["tiebreaker"],
                deleted=True,
            ),
        )

    def get_entity(
        self,
        entity_kind: str,
        entity_key: str,
        *,
        revision: int | None = None,
    ) -> EntityRow | None:
        """Return one entity version from a validated snapshot revision.

        Current reads use the partial unique current-entity index; historical
        reads use the ``(entity_kind, entity_key, valid_from_revision)`` primary
        key.  A tombstone at the requested snapshot is returned as ``None``.
        """

        if not entity_kind or len(entity_kind) > 256:
            raise ValueError("entity_kind must contain between 1 and 256 characters")
        if not entity_key or len(entity_key) > 4096:
            raise ValueError("entity_key must contain between 1 and 4096 characters")
        with self._connect() as connection:
            current = self._current_revision(connection)
            snapshot = current if revision is None else revision
            if snapshot < 0 or snapshot > current:
                raise ValueError("revision is outside the available snapshot range")
            pruned_through = int(
                self._metadata(connection, "changes_pruned_through") or "0"
            )
            if snapshot <= pruned_through and snapshot != current:
                raise ValueError(
                    "revision has expired from the snapshot retention window"
                )
            if snapshot == current:
                row = connection.execute(
                    """
                    SELECT entity_kind, entity_key, scope_key, partition_key,
                           sort_key, tiebreaker, payload_json,
                           valid_from_revision, deleted
                      FROM entity_versions
                     WHERE entity_kind = ? AND entity_key = ?
                       AND valid_to_revision IS NULL
                     LIMIT 1
                    """,
                    (entity_kind, entity_key),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT entity_kind, entity_key, scope_key, partition_key,
                           sort_key, tiebreaker, payload_json,
                           valid_from_revision, deleted
                      FROM entity_versions
                     WHERE entity_kind = ? AND entity_key = ?
                       AND valid_from_revision <= ?
                       AND (
                           valid_to_revision IS NULL
                           OR valid_to_revision >= ?
                       )
                     ORDER BY valid_from_revision DESC
                     LIMIT 1
                    """,
                    (entity_kind, entity_key, snapshot, snapshot),
                ).fetchone()
        if row is None or row["deleted"]:
            return None
        return EntityRow(
            entity_kind=row["entity_kind"],
            entity_key=row["entity_key"],
            scope_key=row["scope_key"],
            partition_key=row["partition_key"],
            sort_key=row["sort_key"],
            tiebreaker=row["tiebreaker"],
            payload=json.loads(row["payload_json"]),
            revision=row["valid_from_revision"],
        )

    def query_entities(
        self,
        entity_kind: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
        revision: int | None = None,
        direction: Literal["asc", "desc"] = "asc",
        scope_key: str | None = None,
        partition_key: str | None = None,
    ) -> KeysetPage:
        """Query versioned entities using indexed keyset pagination.

        A supplied cursor overrides ``revision`` and ``direction`` only when they
        agree; mismatches are rejected.  Historical entity versions are retained,
        so a multi-page read stays stable while newer refreshes commit.
        """

        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connect() as connection:
            current = self._current_revision(connection)
            cursor_data = self._decode_cursor(connection, cursor) if cursor else None
            if cursor_data is not None:
                if cursor_data.entity_kind != entity_kind:
                    raise ValueError("cursor entity kind does not match query")
                if cursor_data.scope_key != scope_key:
                    raise ValueError("cursor scope does not match query")
                if cursor_data.partition_key != partition_key:
                    raise ValueError("cursor partition does not match query")
                if revision is not None and revision != cursor_data.revision:
                    raise ValueError("cursor revision does not match query")
                if direction != cursor_data.direction:
                    raise ValueError("cursor direction does not match query")
                snapshot = cursor_data.revision
            else:
                snapshot = current if revision is None else revision
            if snapshot < 0 or snapshot > current:
                raise ValueError("revision is outside the available snapshot range")
            pruned_through = int(
                self._metadata(connection, "changes_pruned_through") or "0"
            )
            if snapshot <= pruned_through and snapshot != current:
                raise ValueError("cursor revision has expired")
            comparator = ">" if direction == "asc" else "<"
            order = "ASC" if direction == "asc" else "DESC"
            params: list[Any] = [entity_kind, snapshot, snapshot]
            filters = ""
            if scope_key is not None:
                filters += " AND scope_key = ?"
                params.append(scope_key)
            if partition_key is not None:
                filters += " AND partition_key = ?"
                params.append(partition_key)
            keyset = ""
            if cursor_data is not None:
                keyset = f"""
                    AND (
                        sort_key {comparator} ?
                        OR (sort_key = ? AND tiebreaker {comparator} ?)
                        OR (sort_key = ? AND tiebreaker = ?
                            AND entity_key {comparator} ?)
                    )
                """
                params.extend(
                    [
                        cursor_data.sort_key,
                        cursor_data.sort_key,
                        cursor_data.tiebreaker,
                        cursor_data.sort_key,
                        cursor_data.tiebreaker,
                        cursor_data.entity_key,
                    ]
                )
            params.append(limit + 1)
            rows = connection.execute(
                f"""
                SELECT entity_kind, entity_key, scope_key, partition_key,
                       sort_key, tiebreaker, payload_json, valid_from_revision
                  FROM entity_versions
                 WHERE entity_kind = ? AND deleted = 0
                   AND valid_from_revision <= ?
                   AND (valid_to_revision IS NULL OR valid_to_revision >= ?)
                   {filters}
                   {keyset}
                 ORDER BY sort_key {order}, tiebreaker {order}, entity_key {order}
                 LIMIT ?
                """,
                params,
            ).fetchall()
            page_rows = rows[:limit]
            items = tuple(
                EntityRow(
                    entity_kind=row["entity_kind"],
                    entity_key=row["entity_key"],
                    scope_key=row["scope_key"],
                    partition_key=row["partition_key"],
                    sort_key=row["sort_key"],
                    tiebreaker=row["tiebreaker"],
                    payload=json.loads(row["payload_json"]),
                    revision=row["valid_from_revision"],
                )
                for row in page_rows
            )
            next_cursor = None
            if len(rows) > limit and page_rows:
                last = page_rows[-1]
                next_cursor = self._encode_cursor(
                    connection,
                    _Cursor(
                        revision=snapshot,
                        entity_kind=entity_kind,
                        scope_key=scope_key,
                        partition_key=partition_key,
                        direction=direction,
                        sort_key=last["sort_key"],
                        tiebreaker=last["tiebreaker"],
                        entity_key=last["entity_key"],
                    ),
                )
        return KeysetPage(revision=snapshot, items=items, next_cursor=next_cursor)

    def changes(
        self,
        after_revision: int,
        *,
        max_revisions: int = 100,
        max_changes: int = 500,
        max_payload_bytes: int = 1024 * 1024,
    ) -> ChangesPage:
        """Return complete revision deltas or explicitly require snapshot reset."""

        if after_revision < 0:
            raise ValueError("after_revision must not be negative")
        if not 1 <= max_revisions <= 500:
            raise ValueError("max_revisions must be between 1 and 500")
        if not 1 <= max_changes <= 5000:
            raise ValueError("max_changes must be between 1 and 5000")
        if not 1024 <= max_payload_bytes <= 8 * 1024 * 1024:
            raise ValueError("max_payload_bytes must be between 1024 and 8388608")
        with self._connect() as connection:
            current = self._current_revision(connection)
            retained_cutoff = int(
                self._metadata(connection, "changes_pruned_through") or "0"
            )
            retained = retained_cutoff + 1 if current else None
            last_ingested_at = self._metadata(connection, "last_ingested_at") or None
            catching_up = self._metadata(connection, "catching_up") == "1"
            reset = after_revision > current or after_revision < retained_cutoff
            if reset:
                return ChangesPage(
                    from_revision=after_revision,
                    to_revision=current,
                    current_revision=current,
                    retained_from_revision=retained,
                    reset_required=True,
                    changes=(),
                    has_more=False,
                    last_ingested_at=last_ingested_at,
                    catching_up=catching_up,
                )
            to_revision = min(current, after_revision + max_revisions)
            rows = connection.execute(
                """
                SELECT revision, entity_kind, entity_key, operation, payload_json
                  FROM revision_changes
                 WHERE revision > ? AND revision <= ?
                 ORDER BY revision, change_id
                 LIMIT ?
                """,
                (after_revision, to_revision, max_changes + 1),
            ).fetchall()
            changes = tuple(
                RevisionChange(
                    revision=row["revision"],
                    entity_kind=row["entity_kind"],
                    entity_key=row["entity_key"],
                    operation=row["operation"],
                    payload=json.loads(row["payload_json"]),
                )
                for row in rows[:max_changes]
            )
            payload_bytes = sum(
                len(_canonical_json(change.model_dump(mode="json")).encode("utf-8"))
                for change in changes
            )
            if len(rows) > max_changes or payload_bytes > max_payload_bytes:
                return ChangesPage(
                    from_revision=after_revision,
                    to_revision=current,
                    current_revision=current,
                    retained_from_revision=retained,
                    reset_required=True,
                    changes=(),
                    has_more=False,
                    last_ingested_at=last_ingested_at,
                    catching_up=catching_up,
                )
        return ChangesPage(
            from_revision=after_revision,
            to_revision=to_revision,
            current_revision=current,
            retained_from_revision=retained,
            reset_required=False,
            changes=changes,
            has_more=to_revision < current,
            last_ingested_at=last_ingested_at,
            catching_up=catching_up,
        )

    def _record_source_change(
        self, connection: sqlite3.Connection, revision: int, change: SourceChange
    ) -> None:
        operation: Literal["upsert", "delete", "status"]
        if change.kind == ChangeKind.DELETE:
            operation = "delete"
        elif change.kind == ChangeKind.ERROR:
            operation = "status"
        else:
            operation = "upsert"
        self._record_change(
            connection,
            revision,
            "source",
            change.path,
            operation,
            {
                "kind": change.kind,
                "status": change.current.status if change.current else None,
                "committed_offset": (
                    change.current.committed_offset if change.current else None
                ),
                "message_count": len(change.messages),
                "invalidated_message_count": len(change.invalidated_message_ids),
                "trailing_bytes": change.trailing_bytes,
                "error": change.error,
            },
        )

    @staticmethod
    def _record_change(
        connection: sqlite3.Connection,
        revision: int,
        entity_kind: str,
        entity_key: str,
        operation: str,
        payload: dict[str, Any],
    ) -> None:
        payload_json = _canonical_json(payload)
        identifier_bytes = len(entity_kind.encode("utf-8")) + len(
            entity_key.encode("utf-8")
        )
        if identifier_bytes > _MAX_CHANGE_IDENTIFIER_BYTES:
            operation = "invalidate"
            entity_key = "oversized:" + _digest(entity_key.encode("utf-8"))
            payload_json = _canonical_json(
                {
                    "reason": "identifier_too_large",
                    "identifier_bytes": identifier_bytes,
                }
            )
        if len(payload_json.encode("utf-8")) > _MAX_CHANGE_PAYLOAD_BYTES:
            operation = "invalidate"
            payload_json = _canonical_json(
                {
                    "reason": "payload_too_large",
                    "payload_bytes": len(payload_json.encode("utf-8")),
                }
            )
        connection.execute(
            """
            INSERT INTO revision_changes(
                revision, entity_kind, entity_key, operation, payload_json
            ) VALUES(?, ?, ?, ?, ?)
            """,
            (revision, entity_kind, entity_key, operation, payload_json),
        )

    def _prune_changes(self, connection: sqlite3.Connection, revision: int) -> None:
        cutoff = revision - self.retained_change_revisions
        if cutoff > 0:
            connection.execute(
                "DELETE FROM revision_changes WHERE revision <= ?", (cutoff,)
            )
            previous = int(self._metadata(connection, "changes_pruned_through") or "0")
            if cutoff > previous:
                self._set_metadata(connection, "changes_pruned_through", str(cutoff))
            connection.execute(
                """
                DELETE FROM entity_versions
                 WHERE valid_to_revision IS NOT NULL
                   AND valid_to_revision <= ?
                """,
                (cutoff,),
            )
            connection.execute(
                """
                DELETE FROM entity_versions
                 WHERE valid_to_revision IS NULL AND deleted = 1
                   AND valid_from_revision <= ?
                """,
                (cutoff,),
            )
        connection.execute("DELETE FROM source_messages WHERE active = 0")

    def _encode_cursor(self, connection: sqlite3.Connection, cursor: _Cursor) -> str:
        raw = cursor.model_dump_json().encode()
        signature = hmac.new(
            self._metadata(connection, "cursor_secret").encode(),
            raw,
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(signature + raw).decode().rstrip("=")

    def _decode_cursor(self, connection: sqlite3.Connection, token: str) -> _Cursor:
        try:
            padded = token + "=" * (-len(token) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode())
            signature, raw = decoded[:32], decoded[32:]
            expected = hmac.new(
                self._metadata(connection, "cursor_secret").encode(),
                raw,
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("invalid signature")
            return _Cursor.model_validate_json(raw)
        except Exception as exc:
            raise ValueError("invalid or expired cursor") from exc

    @staticmethod
    def _current_revision(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT value FROM store_metadata WHERE key = 'revision'"
        ).fetchone()
        return int(row[0]) if row is not None else 0

    @staticmethod
    def _metadata(connection: sqlite3.Connection, key: str) -> str:
        row = connection.execute(
            "SELECT value FROM store_metadata WHERE key = ?", (key,)
        ).fetchone()
        return str(row[0]) if row is not None else ""

    @staticmethod
    def _set_metadata(connection: sqlite3.Connection, key: str, value: str) -> None:
        connection.execute(
            """
            INSERT INTO store_metadata(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    @contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        """Expose a read-only-by-contract connection for custom hot SQL queries."""

        connection = self._connect()
        try:
            connection.execute("PRAGMA query_only = ON")
            yield connection
        finally:
            connection.close()


def _disk_metadata(path: str) -> _DiskMetadata:
    stat = os.stat(path)
    if not os.path.isfile(path):
        raise ValueError(f"source is not a regular file: {path}")
    return _DiskMetadata(
        path=path,
        file_identity=f"{stat.st_dev}:{stat.st_ino}",
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        ctime_ns=stat.st_ctime_ns,
    )


def _checkpoint_checksums(path: str, committed_offset: int) -> tuple[str, str]:
    if committed_offset == 0:
        empty = _digest(b"")
        return empty, empty
    prefix_size = min(_CHECKSUM_BYTES, committed_offset)
    tail_size = min(_CHECKSUM_BYTES, committed_offset)
    with open(path, "rb") as source:
        prefix = source.read(prefix_size)
        source.seek(committed_offset - tail_size)
        tail = source.read(tail_size)
    return _digest(prefix), _digest(tail)


def _checkpoint_matches(snapshot: SourceSnapshot, path: str) -> bool:
    try:
        prefix, tail = _checkpoint_checksums(path, snapshot.committed_offset)
    except OSError:
        return False
    return hmac.compare_digest(
        prefix, snapshot.prefix_checksum or ""
    ) and hmac.compare_digest(tail, snapshot.tail_checksum or "")


def _source_from_row(row: sqlite3.Row) -> SourceSnapshot:
    return SourceSnapshot(
        path=row["path"],
        file_identity=row["file_identity"],
        size=row["size"],
        mtime_ns=row["mtime_ns"],
        committed_offset=row["committed_offset"],
        committed_ctime_ns=row["committed_ctime_ns"],
        prefix_checksum=row["prefix_checksum"],
        tail_checksum=row["tail_checksum"],
        parser_version=row["parser_version"],
        schema_version=row["schema_version"],
        status=row["status"],
        error=row["error"],
        last_success_revision=row["last_success_revision"],
        revision=row["revision"],
        deleted=bool(row["deleted"]),
        root_link=row["root_link"],
        parent_link=row["parent_link"],
        metadata=json.loads(row["metadata_json"]),
    )


def _compress_canonical_payload(payload: dict[str, Any]) -> tuple[bytes, int]:
    canonical = _canonical_json(payload).encode("utf-8")
    if len(canonical) > _MAX_CANONICAL_PAYLOAD_BYTES:
        raise ValueError(
            "canonical source message exceeds the 268435456-byte safety bound"
        )
    return zlib.compress(canonical), len(canonical)


def _stored_payload(row: sqlite3.Row) -> tuple[dict[str, Any], bool]:
    compressed = row["canonical_json_zlib"]
    if compressed is None:
        try:
            envelope = _JSON_OBJECT.validate_python(json.loads(row["normalized_json"]))
        except Exception as exc:
            raise StoredPayloadError(
                f"invalid legacy payload envelope for {row['source_message_id']}"
            ) from exc
        return envelope, False

    try:
        expected_size = row["canonical_json_size"]
        if (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or not 0 <= expected_size <= _MAX_CANONICAL_PAYLOAD_BYTES
        ):
            raise ValueError("invalid canonical payload size")
        decompressor = zlib.decompressobj()
        canonical = decompressor.decompress(bytes(compressed), expected_size + 1)
        if decompressor.unconsumed_tail or len(canonical) > expected_size:
            raise ValueError("canonical payload exceeds its declared size")
        canonical += decompressor.flush()
        if (
            len(canonical) != expected_size
            or not decompressor.eof
            or decompressor.unused_data
        ):
            raise ValueError("canonical payload is truncated or has trailing data")
        payload = _JSON_OBJECT.validate_json(canonical)
        if _canonical_json(payload).encode("utf-8") != canonical:
            raise ValueError("stored payload is not canonical JSON")
        return payload, True
    except Exception as exc:
        raise StoredPayloadError(
            f"invalid compressed payload for {row['source_message_id']}"
        ) from exc


def _message_from_row(row: sqlite3.Row) -> SourceMessage:
    payload, payload_complete = _stored_payload(row)
    return SourceMessage(
        source_message_id=row["source_message_id"],
        source_path=row["path"],
        byte_offset=row["byte_offset"],
        byte_end=row["byte_end"],
        digest=row["digest"],
        explicit_event_id=row["explicit_event_id"],
        event_type=row["event_type"],
        event_timestamp=row["event_timestamp"],
        root_link=row["root_link"],
        parent_link=row["parent_link"],
        payload=payload,
        payload_complete=payload_complete,
    )


def _default_event_identity(payload: dict[str, Any]) -> str | None:
    """Extract only explicit top-level IDs; nested payload IDs are ambiguous."""

    for key in ("event_id", "message_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _last_complete_line_offset(source: BinaryIO, size: int) -> int:
    """Return the byte after the last newline without reading the transcript.

    Checkpoint-only stores still expose an incomplete trailing JSONL record as
    partial ingestion. Normal files cost one byte of I/O; only a malformed file
    with no newline requires walking back to its beginning.
    """

    if size <= 0:
        return 0
    source.seek(size - 1)
    if source.read(1) == b"\n":
        return size
    block_size = 64 * 1024
    end = size
    while end > 0:
        start = max(0, end - block_size)
        source.seek(start)
        chunk = source.read(end - start)
        newline = chunk.rfind(b"\n")
        if newline >= 0:
            return start + newline + 1
        end = start
    return 0


def _message_identity(
    path: str,
    file_identity: str,
    byte_offset: int,
    line_digest: str,
    explicit_event_id: str | None,
) -> str:
    if explicit_event_id:
        return "event:" + _digest(explicit_event_id.encode())
    fallback = f"{path}\0{file_identity}\0{byte_offset}\0{line_digest}".encode()
    return "source:" + _digest(fallback)


def _message_metadata(
    payload: dict[str, Any],
) -> tuple[str | None, str | None, str | None, str | None]:
    event_type = _first_string(payload, "type", "event_type", "kind")
    timestamp = _first_string(payload, "timestamp", "created_at", "time")
    root_link = _first_string(
        payload, "root_session_id", "root_thread_id", "session_id", "thread_id"
    )
    parent_link = _first_string(
        payload, "parent_session_id", "parent_thread_id", "parent_id"
    )
    return event_type, timestamp, root_link, parent_link


def _first_string(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _normalized_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep bounded routing/metric metadata, never raw message content."""

    allowed = {
        "event_id",
        "message_id",
        "id",
        "type",
        "event_type",
        "kind",
        "timestamp",
        "created_at",
        "time",
        "root_session_id",
        "root_thread_id",
        "session_id",
        "thread_id",
        "parent_session_id",
        "parent_thread_id",
        "parent_id",
        "project_name",
        "project_path",
        "model",
        "provider",
        "role",
        "tool_name",
        "status",
        "usage",
        "token_usage",
    }
    envelope = {key: payload[key] for key in allowed if key in payload}
    nested = payload.get("payload")
    if isinstance(nested, dict):
        envelope["payload"] = {key: nested[key] for key in allowed if key in nested}
    # Defensive bound for unusual usage metadata; source offsets remain authoritative.
    while len(_canonical_json(envelope).encode()) > 16 * 1024 and envelope:
        envelope.pop(sorted(envelope)[-1])
    return envelope


__all__ = [
    "ChangeKind",
    "ChangesPage",
    "EntityMutation",
    "EntityRow",
    "IncrementalStore",
    "IngestionStatus",
    "KeysetPage",
    "MaterializationContext",
    "ProjectionInvalidation",
    "RefreshResult",
    "RefreshFailure",
    "RevisionChange",
    "SourceChange",
    "SourceMessage",
    "SourceSnapshot",
    "SourceFenceError",
    "StoredPayloadError",
]
