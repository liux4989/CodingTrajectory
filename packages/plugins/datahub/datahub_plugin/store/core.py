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

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import Any, Final, Literal

from coding_trajectory.datahub import (
    canonical_json,
)
from pydantic import TypeAdapter

from datahub_plugin.store.changes import ChangesMixin
from datahub_plugin.store.detail import DetailMixin
from datahub_plugin.store.entities import EntitiesMixin
from datahub_plugin.store.ingestion import IngestionMixin
from datahub_plugin.store.models import (
    ChangeKind,
    ChangesPage,
    DetailEventRow,
    DetailItemRow,
    DetailSpan,
    EntityMutation,
    EntityRow,
    IncompatibleStoreError,
    IngestionStatus,
    KeysetPage,
    ProjectionInvalidation,
    RefreshFailure,
    RefreshResult,
    RevisionChange,
    SourceChange,
    SourceFenceError,
    SourceMessage,
    SourceSnapshot,
    _DiskMetadata,
    _IngestionPlan,
)

_CHECKSUM_BYTES: Final = 64 * 1024
_MAX_CHANGE_PAYLOAD_BYTES: Final = 64 * 1024
_MAX_CHANGE_IDENTIFIER_BYTES: Final = 16 * 1024
_STORE_FORMAT_VERSION: Final = "3"
_JSON_OBJECT = TypeAdapter(dict[str, Any])


EventIdentityExtractor = Callable[[dict[str, Any]], str | None]
Materializer = Callable[["MaterializationContext"], None]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _executemany_chunked(
    connection: sqlite3.Connection,
    sql: str,
    rows: Iterable[tuple[Any, ...]],
    *,
    chunk_size: int = 4000,
) -> None:
    """Insert a lazily produced row stream in bounded executemany chunks."""

    iterator = iter(rows)
    while chunk := list(islice(iterator, chunk_size)):
        connection.executemany(sql, chunk)


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
        """Yield active canonical payloads from the current store format."""

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
                canonical_json(validated_metadata),
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

    def publish_detail(
        self,
        root_id: str,
        *,
        events: Iterable[DetailEventRow],
        items: Iterable[DetailItemRow],
    ) -> None:
        """Replace one root partition's current-only detail locators.

        Rows are deleted and re-inserted inside the same transaction as the
        revision that publishes the corresponding canonical facts, so a
        locator can never point at a source range the fence would reject.
        Event and item iterables are consumed in bounded chunks so a large
        root never materializes its full insertion list.
        """

        self._ensure_active()
        self._store._replace_root_detail(self._connection, root_id, events, items)

    def clear_detail(self) -> None:
        """Delete every current-only detail locator (bootstrap reset)."""

        self._ensure_active()
        self._store._clear_detail(self._connection)

    def _close(self) -> None:
        self._active = False


class IncrementalStore(IngestionMixin, EntitiesMixin, ChangesMixin, DetailMixin):
    """Persistent incremental registry and materialized datahub read store."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        parser_version: str = "json-object-v1",
        schema_version: str = "dashboard-source-v1",
        retained_change_revisions: int = 512,
        event_identity: EventIdentityExtractor | None = None,
        retain_source_messages: bool = True,
        post_commit: Callable[[int], None] | None = None,
    ) -> None:
        if retained_change_revisions < 1:
            raise ValueError("retained_change_revisions must be at least 1")
        self.database_path = Path(database_path).expanduser().resolve()
        self.parser_version = parser_version
        self.schema_version = schema_version
        self.retained_change_revisions = retained_change_revisions
        self._event_identity = event_identity or _default_event_identity
        self.retain_source_messages = retain_source_messages
        self._post_commit = post_commit
        self._refresh_lock = threading.Lock()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._assert_compatible_store()
        self._initialize()

    def _assert_compatible_store(self) -> None:
        """Reject obsolete derived state instead of migrating or decoding it."""

        if not self.database_path.is_file() or self.database_path.stat().st_size == 0:
            return
        uri = f"{self.database_path.as_uri()}?mode=ro"
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=1.0)
            row = connection.execute(
                "SELECT value FROM store_metadata WHERE key = 'store_format_version'"
            ).fetchone()
        except sqlite3.Error as exc:
            raise IncompatibleStoreError(
                f"derived store has no supported format marker: {self.database_path}"
            ) from exc
        finally:
            if connection is not None:
                connection.close()
        if row is None or str(row[0]) != _STORE_FORMAT_VERSION:
            actual = str(row[0]) if row is not None else "missing"
            raise IncompatibleStoreError(
                "derived store format is incompatible: "
                f"expected {_STORE_FORMAT_VERSION}, found {actual}"
            )

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
                CREATE INDEX IF NOT EXISTS idx_entity_keyset ON entity_versions(
                    entity_kind, scope_key, deleted, sort_key, tiebreaker,
                    entity_key, valid_from_revision, valid_to_revision
                ) WHERE deleted = 0;
                CREATE INDEX IF NOT EXISTS idx_entity_partition_keyset
                    ON entity_versions(
                        entity_kind, scope_key, partition_key, deleted, sort_key,
                        tiebreaker, entity_key, valid_from_revision,
                        valid_to_revision
                    ) WHERE deleted = 0;
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

                -- Current-only canonical-id -> source-byte-range locators.
                -- These are not versioned: they exist to hydrate event/item
                -- detail on demand from the authoritative JSONL, never to
                -- serve historical snapshots.  Rows are replaced per root
                -- partition inside the publishing transaction.
                CREATE TABLE IF NOT EXISTS detail_events (
                    event_id TEXT PRIMARY KEY,
                    root_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    byte_offset INTEGER NOT NULL,
                    byte_end INTEGER NOT NULL,
                    digest TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_detail_events_root
                    ON detail_events(root_id);

                CREATE TABLE IF NOT EXISTS detail_items (
                    item_id TEXT PRIMARY KEY,
                    root_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    spans_json TEXT NOT NULL,
                    edge_targets_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_detail_items_root
                    ON detail_items(root_id);
                """
            )
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO store_metadata(key, value) "
                "VALUES('store_format_version', ?)",
                (_STORE_FORMAT_VERSION,),
            )
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
        can be removed immediately; checkpoint-only stores remove all retained
        source-message copies.
        """

        with self._refresh_lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                messages_before = _table_count(connection, "source_messages")
                versions_before = _table_count(connection, "entity_versions")
                changes_before = _table_count(connection, "revision_changes")
                tombstones_before = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM entity_versions WHERE deleted = 1"
                    ).fetchone()[0]
                )
                self._prune_changes(
                    connection,
                    self._current_revision(connection),
                )
                cutoff = int(
                    self._metadata(connection, "changes_pruned_through") or "0"
                )
                if self.retain_source_messages:
                    connection.execute("DELETE FROM source_messages WHERE active = 0")
                else:
                    connection.execute("DELETE FROM source_messages")
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
                messages_deleted = messages_before - _table_count(
                    connection, "source_messages"
                )
                versions_deleted = versions_before - _table_count(
                    connection, "entity_versions"
                )
                changes_deleted = changes_before - _table_count(
                    connection, "revision_changes"
                )
                tombstones_deleted = tombstones_before - int(
                    connection.execute(
                        "SELECT COUNT(*) FROM entity_versions WHERE deleted = 1"
                    ).fetchone()[0]
                )
                connection.execute("PRAGMA optimize")
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
                    compact_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                with self._connect() as measured:
                    pages_after = int(
                        measured.execute("PRAGMA page_count").fetchone()[0]
                    )
            else:
                pages_after = pages_before
        return {
            "messages_deleted": max(messages_deleted, 0),
            "entity_versions_deleted": max(versions_deleted, 0),
            "revision_changes_deleted": max(changes_deleted, 0),
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
        """

        return self._refresh(candidates, materialize=materialize)

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
                if self._post_commit is not None:
                    self._post_commit(revision)
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
                plans = self._build_plans(resolved, previous)
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
            if self._post_commit is not None:
                self._post_commit(revision)
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
    @staticmethod
    @staticmethod
    @staticmethod
    @staticmethod
    @staticmethod
    @staticmethod
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


def _table_count(
    connection: sqlite3.Connection,
    table: Literal["source_messages", "entity_versions", "revision_changes"],
) -> int:
    queries = {
        "source_messages": "SELECT COUNT(*) FROM source_messages",
        "entity_versions": "SELECT COUNT(*) FROM entity_versions",
        "revision_changes": "SELECT COUNT(*) FROM revision_changes",
    }
    return int(connection.execute(queries[table]).fetchone()[0])


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


def _default_event_identity(payload: dict[str, Any]) -> str | None:
    """Extract only explicit top-level IDs; nested payload IDs are ambiguous."""

    for key in ("event_id", "message_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


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


__all__ = [
    "ChangeKind",
    "ChangesPage",
    "DetailEventRow",
    "DetailItemRow",
    "DetailSpan",
    "EntityMutation",
    "EntityRow",
    "IncompatibleStoreError",
    "IncrementalStore",
    "IngestionStatus",
    "KeysetPage",
    "MaterializationContext",
    "ProjectionInvalidation",
    "RefreshFailure",
    "RefreshResult",
    "RevisionChange",
    "SourceChange",
    "SourceFenceError",
    "SourceMessage",
    "SourceSnapshot",
]
