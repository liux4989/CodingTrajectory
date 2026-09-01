"""Durable SQLite read model for ``ct.living_events.v1`` resources.

Versioned resource store with signed snapshot/delta cursors. The projection
logic that fills it lives in ``coding_trajectory.living_events``.
"""


import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from coding_trajectory.ingestion.common import (
    canonical_json,
)
from coding_trajectory.living_sources import (
    LivingSourceSnapshot,
)

SCHEMA_VERSION = "ct.living_events.v1"
_STORE_FORMAT_VERSION = "living-events-store-v1"
_RETAINED_REVISIONS = 512


@dataclass(frozen=True, slots=True)
class ProjectedResource:
    kind: str
    key: str
    root_session_id: str
    path: dict[str, str]
    sort_key: str
    view: dict[str, Any]
    details: dict[str, Any]



class LivingEventsStore:
    """Versioned SQLite resource store with signed snapshot/delta cursors."""

    def __init__(
        self,
        path: Path,
        *,
        schema_version: str = SCHEMA_VERSION,
        store_format_version: str = _STORE_FORMAT_VERSION,
    ) -> None:
        self.path = path.expanduser().resolve()
        self.schema_version = schema_version
        self.store_format_version = store_format_version
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS living_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS living_revisions (
                    revision INTEGER PRIMARY KEY,
                    committed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS living_resource_versions (
                    resource_kind TEXT NOT NULL,
                    resource_key TEXT NOT NULL,
                    valid_from_revision INTEGER NOT NULL,
                    valid_to_revision INTEGER,
                    root_session_id TEXT NOT NULL,
                    session_id TEXT,
                    turn_id TEXT,
                    item_id TEXT,
                    source_session_id TEXT,
                    target_session_id TEXT,
                    path_json TEXT NOT NULL,
                    sort_key TEXT NOT NULL,
                    view_payload_json TEXT NOT NULL,
                    detail_payload_z BLOB NOT NULL,
                    payload_digest TEXT NOT NULL,
                    PRIMARY KEY(resource_kind, resource_key, valid_from_revision)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_living_resource_current
                    ON living_resource_versions(resource_kind, resource_key)
                    WHERE valid_to_revision IS NULL;
                CREATE INDEX IF NOT EXISTS idx_living_resource_snapshot
                    ON living_resource_versions(
                        root_session_id, sort_key, valid_from_revision,
                        valid_to_revision
                    );
                CREATE TABLE IF NOT EXISTS living_changes (
                    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    revision INTEGER NOT NULL,
                    operation TEXT NOT NULL,
                    resource_kind TEXT NOT NULL,
                    resource_key TEXT NOT NULL,
                    root_session_id TEXT NOT NULL,
                    session_id TEXT,
                    turn_id TEXT,
                    item_id TEXT,
                    source_session_id TEXT,
                    target_session_id TEXT,
                    path_json TEXT NOT NULL,
                    reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_living_changes_revision
                    ON living_changes(revision, change_id);
                CREATE TABLE IF NOT EXISTS living_sources (
                    path TEXT PRIMARY KEY,
                    vendor TEXT NOT NULL,
                    file_identity TEXT,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    ctime_ns INTEGER NOT NULL,
                    committed_offset INTEGER NOT NULL,
                    prefix_checksum TEXT,
                    tail_checksum TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    session_id TEXT,
                    parent_session_id TEXT,
                    root_session_id TEXT,
                    materialized_revision INTEGER,
                    cwd TEXT,
                    title TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_living_sources_root
                    ON living_sources(root_session_id, session_id);
                """
            )
            for table in ("living_resource_versions", "living_changes"):
                for column in (
                    "session_id",
                    "turn_id",
                    "item_id",
                    "source_session_id",
                    "target_session_id",
                ):
                    self._ensure_column(connection, table, column, "TEXT")
                self._backfill_path_columns(connection, table)
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_living_resource_scope
                    ON living_resource_versions(
                        root_session_id, session_id, turn_id, item_id,
                        valid_from_revision, valid_to_revision, sort_key
                    );
                CREATE INDEX IF NOT EXISTS idx_living_resource_order
                    ON living_resource_versions(
                        sort_key, resource_kind, resource_key,
                        valid_from_revision, valid_to_revision
                    );
                CREATE INDEX IF NOT EXISTS idx_living_change_scope
                    ON living_changes(
                        root_session_id, session_id, turn_id, item_id,
                        revision, change_id
                    );
                """
            )
            self._set_default(connection, "store_format_version", self.store_format_version)
            self._set_default(connection, "revision", "0")
            self._set_default(connection, "pruned_through", "0")
            self._set_default(connection, "cursor_secret", secrets.token_hex(32))
            actual = self._metadata(connection, "store_format_version")
            if actual != self.store_format_version:
                raise ValueError(
                    "living-events store format is incompatible: "
                    f"expected {self.store_format_version}, found {actual}"
                )

    def publish(
        self,
        resources: list[ProjectedResource],
        *,
        affected_roots: list[str],
    ) -> int:
        roots = sorted(set(affected_roots))
        if not roots:
            return self.current_revision()
        incoming = {(value.kind, value.key): value for value in resources}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_revision = self._current_revision(connection)
            placeholders = ",".join("?" for _ in roots)
            current_rows = connection.execute(
                f"""
                SELECT * FROM living_resource_versions
                 WHERE valid_to_revision IS NULL
                   AND root_session_id IN ({placeholders})
                """,
                roots,
            ).fetchall()
            current = {
                (row["resource_kind"], row["resource_key"]): row for row in current_rows
            }
            changed: list[ProjectedResource] = []
            for identity, resource in incoming.items():
                row = current.get(identity)
                if row is None or row["payload_digest"] != _resource_digest(resource):
                    changed.append(resource)
            removed = [
                row for identity, row in current.items() if identity not in incoming
            ]
            if not changed and not removed:
                connection.rollback()
                return current_revision

            revision = current_revision + 1
            connection.execute(
                "INSERT INTO living_revisions(revision, committed_at) VALUES(?, ?)",
                (revision, datetime.now().astimezone().isoformat()),
            )
            for resource in changed:
                connection.execute(
                    """
                    UPDATE living_resource_versions
                       SET valid_to_revision = ?
                     WHERE resource_kind = ? AND resource_key = ?
                       AND valid_to_revision IS NULL
                    """,
                    (revision, resource.kind, resource.key),
                )
                connection.execute(
                    """
                    INSERT INTO living_resource_versions(
                        resource_kind, resource_key, valid_from_revision,
                        valid_to_revision, root_session_id, session_id, turn_id,
                        item_id, source_session_id, target_session_id,
                        path_json, sort_key,
                        view_payload_json, detail_payload_z, payload_digest
                    ) VALUES(?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resource.kind,
                        resource.key,
                        revision,
                        resource.root_session_id,
                        resource.path.get("session_id"),
                        resource.path.get("turn_id"),
                        resource.path.get("item_id"),
                        resource.path.get("source_session_id"),
                        resource.path.get("target_session_id"),
                        canonical_json(resource.path),
                        resource.sort_key,
                        canonical_json(resource.view),
                        sqlite3.Binary(
                            zlib.compress(
                                canonical_json(resource.details).encode("utf-8")
                            )
                        ),
                        _resource_digest(resource),
                    ),
                )
                self._insert_change(
                    connection,
                    revision=revision,
                    operation="upsert",
                    resource_kind=resource.kind,
                    resource_key=resource.key,
                    root_session_id=resource.root_session_id,
                    path=resource.path,
                )
            for row in removed:
                connection.execute(
                    """
                    UPDATE living_resource_versions
                       SET valid_to_revision = ?
                     WHERE resource_kind = ? AND resource_key = ?
                       AND valid_to_revision IS NULL
                    """,
                    (revision, row["resource_kind"], row["resource_key"]),
                )
                self._insert_change(
                    connection,
                    revision=revision,
                    operation="remove",
                    resource_kind=row["resource_kind"],
                    resource_key=row["resource_key"],
                    root_session_id=row["root_session_id"],
                    path=json.loads(row["path_json"]),
                )
            self._set_metadata(connection, "revision", str(revision))
            self._prune(connection, revision)
            connection.commit()
        return revision

    def current_revision(self) -> int:
        with self._connect() as connection:
            return self._current_revision(connection)

    def routing_scope(self, scope: dict[str, Any]) -> dict[str, Any]:
        """Resolve persisted ancestors without changing the cursor-bound scope."""

        with self._connect() as connection:
            return self._expand_scope(
                connection,
                scope,
                self._current_revision(connection),
            )

    def source_snapshots(self) -> dict[str, LivingSourceSnapshot]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM living_sources ORDER BY path"
            ).fetchall()
        return {
            str(row["path"]): LivingSourceSnapshot.model_validate(dict(row))
            for row in rows
        }

    def save_source_snapshots(self, snapshots: list[LivingSourceSnapshot]) -> None:
        if not snapshots:
            return
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO living_sources(
                    path, vendor, file_identity, size, mtime_ns, ctime_ns,
                    committed_offset, prefix_checksum, tail_checksum, status,
                    error, session_id, parent_session_id, root_session_id,
                    materialized_revision, cwd, title
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    vendor = excluded.vendor,
                    file_identity = excluded.file_identity,
                    size = excluded.size,
                    mtime_ns = excluded.mtime_ns,
                    ctime_ns = excluded.ctime_ns,
                    committed_offset = excluded.committed_offset,
                    prefix_checksum = excluded.prefix_checksum,
                    tail_checksum = excluded.tail_checksum,
                    status = excluded.status,
                    error = excluded.error,
                    session_id = excluded.session_id,
                    parent_session_id = excluded.parent_session_id,
                    root_session_id = excluded.root_session_id,
                    materialized_revision = excluded.materialized_revision,
                    cwd = excluded.cwd,
                    title = excluded.title
                """,
                [
                    (
                        value.path,
                        value.vendor,
                        value.file_identity,
                        value.size,
                        value.mtime_ns,
                        value.ctime_ns,
                        value.committed_offset,
                        value.prefix_checksum,
                        value.tail_checksum,
                        value.status,
                        value.error,
                        value.session_id,
                        value.parent_session_id,
                        value.root_session_id,
                        value.materialized_revision,
                        value.cwd,
                        value.title,
                    )
                    for value in snapshots
                ],
            )

    def page(
        self,
        *,
        mode: str,
        scope: dict[str, Any],
        after: str | None,
        through: str | None,
        limit: int,
    ) -> dict[str, Any]:
        if mode not in {"view", "details"}:
            raise ValueError("mode must be 'view' or 'details'")
        scope_hash = hashlib.sha256(canonical_json(scope).encode("utf-8")).hexdigest()
        with self._connect() as connection:
            current = self._current_revision(connection)
            pruned_through = int(self._metadata(connection, "pruned_through") or "0")
            through_cursor = (
                self._decode_cursor(connection, through) if through else None
            )
            if through_cursor is not None:
                self._validate_cursor_scope(through_cursor, scope_hash)
                if through_cursor.get("kind") != "watermark":
                    raise ValueError("through must be a watermark cursor")
                snapshot_revision = int(through_cursor["revision"])
            else:
                snapshot_revision = current
            if snapshot_revision > current:
                raise ValueError("through cursor is ahead of the current revision")
            if snapshot_revision <= pruned_through and snapshot_revision != current:
                raise ValueError("through cursor has expired")

            after_cursor = self._decode_cursor(connection, after) if after else None
            if after_cursor is not None:
                self._validate_cursor_scope(after_cursor, scope_hash)
            expanded_scope = self._expand_scope(connection, scope, snapshot_revision)
            watermark = self._encode_cursor(
                connection,
                {
                    "kind": "watermark",
                    "revision": snapshot_revision,
                    "scope": scope_hash,
                },
            )

            if after_cursor is None or after_cursor.get("kind") == "snapshot":
                if (
                    after_cursor is not None
                    and int(after_cursor["through"]) != snapshot_revision
                ):
                    raise ValueError("snapshot cursor does not match through")
                return self._snapshot_page(
                    connection,
                    mode=mode,
                    scope=expanded_scope,
                    scope_hash=scope_hash,
                    revision=snapshot_revision,
                    after_cursor=after_cursor,
                    watermark=watermark,
                    limit=limit,
                )

            if after_cursor.get("kind") not in {"watermark", "delta"}:
                raise ValueError("after cursor has an unsupported kind")
            base_revision = int(
                after_cursor.get("base_revision", after_cursor.get("revision", 0))
            )
            if base_revision > current or base_revision < pruned_through:
                return self._reset_page(
                    connection,
                    mode=mode,
                    scope=expanded_scope,
                    scope_hash=scope_hash,
                    revision=snapshot_revision,
                    watermark=watermark,
                    reason="cursor_expired",
                )
            if (
                after_cursor.get("kind") == "delta"
                and int(after_cursor["through"]) != snapshot_revision
            ):
                raise ValueError("delta cursor does not match through")
            return self._delta_page(
                connection,
                mode=mode,
                scope=expanded_scope,
                scope_hash=scope_hash,
                base_revision=base_revision,
                through_revision=snapshot_revision,
                after_change_id=int(after_cursor.get("change_id", 0)),
                watermark=watermark,
                limit=limit,
            )

    def _snapshot_page(
        self,
        connection: sqlite3.Connection,
        *,
        mode: str,
        scope: dict[str, Any],
        scope_hash: str,
        revision: int,
        after_cursor: dict[str, Any] | None,
        watermark: str,
        limit: int,
    ) -> dict[str, Any]:
        position = str(after_cursor.get("position") or "") if after_cursor else ""
        conditions = [
            "valid_from_revision <= ?",
            "(valid_to_revision IS NULL OR valid_to_revision > ?)",
        ]
        bindings: list[Any] = [revision, revision]
        scope_sql, scope_bindings = _scope_sql(scope)
        conditions.extend(scope_sql)
        bindings.extend(scope_bindings)
        if position:
            position_parts = position.split("\x00", 2)
            if len(position_parts) != 3:
                raise ValueError("snapshot cursor position is invalid")
            conditions.append("(sort_key, resource_kind, resource_key) > (?, ?, ?)")
            bindings.extend(position_parts)
        bindings.append(limit + 1)
        rows = connection.execute(
            f"""
            SELECT * FROM living_resource_versions
             WHERE {" AND ".join(conditions)}
             ORDER BY sort_key, resource_kind, resource_key
             LIMIT ?
            """,
            bindings,
        ).fetchall()
        page_rows = rows[:limit]
        has_more = len(rows) > limit
        changes = []
        for row in page_rows:
            cursor = self._encode_cursor(
                connection,
                {
                    "kind": "snapshot",
                    "through": revision,
                    "position": _row_position(row),
                    "scope": scope_hash,
                },
            )
            changes.append(
                self._change_from_resource_row(row, mode=mode, cursor=cursor)
            )
        next_cursor = changes[-1]["cursor"] if has_more and changes else None
        return {
            "schema_version": self.schema_version,
            "mode": mode,
            "page_kind": "snapshot",
            "through": watermark,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "changes": changes,
            "issues": [],
        }

    def _delta_page(
        self,
        connection: sqlite3.Connection,
        *,
        mode: str,
        scope: dict[str, Any],
        scope_hash: str,
        base_revision: int,
        through_revision: int,
        after_change_id: int,
        watermark: str,
        limit: int,
    ) -> dict[str, Any]:
        conditions = ["revision > ?", "revision <= ?", "change_id > ?"]
        bindings: list[Any] = [base_revision, through_revision, after_change_id]
        scope_sql, scope_bindings = _scope_sql(scope)
        conditions.extend(scope_sql)
        bindings.extend(scope_bindings)
        bindings.append(limit + 1)
        rows = connection.execute(
            f"""
            SELECT * FROM living_changes
             WHERE {" AND ".join(conditions)}
             ORDER BY change_id
             LIMIT ?
            """,
            bindings,
        ).fetchall()
        page_rows = rows[:limit]
        has_more = len(rows) > limit
        changes: list[dict[str, Any]] = []
        for row in page_rows:
            cursor = self._encode_cursor(
                connection,
                {
                    "kind": "delta",
                    "base_revision": base_revision,
                    "through": through_revision,
                    "change_id": row["change_id"],
                    "scope": scope_hash,
                },
            )
            resource = self._resource_for_change(connection, row, mode=mode)
            changes.append(
                {
                    "cursor": cursor,
                    "revision": row["revision"],
                    "operation": row["operation"],
                    "resource_kind": row["resource_kind"],
                    "path": json.loads(row["path_json"]),
                    "resource": resource,
                    "reason": row["reason"],
                }
            )
        next_cursor = changes[-1]["cursor"] if has_more and changes else None
        return {
            "schema_version": self.schema_version,
            "mode": mode,
            "page_kind": "delta",
            "through": watermark,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "changes": changes,
            "issues": [],
        }

    def _reset_page(
        self,
        connection: sqlite3.Connection,
        *,
        mode: str,
        scope: dict[str, Any],
        scope_hash: str,
        revision: int,
        watermark: str,
        reason: str,
    ) -> dict[str, Any]:
        root_id = str(scope.get("root_session_id") or "*")
        cursor = self._encode_cursor(
            connection,
            {
                "kind": "watermark",
                "revision": revision,
                "scope": scope_hash,
            },
        )
        return {
            "schema_version": self.schema_version,
            "mode": mode,
            "page_kind": "delta",
            "through": watermark,
            "next_cursor": None,
            "has_more": False,
            "changes": [
                {
                    "cursor": cursor,
                    "revision": revision,
                    "operation": "reset",
                    "resource_kind": "session",
                    "path": {"root_session_id": root_id},
                    "resource": None,
                    "reason": reason,
                }
            ],
            "issues": [],
        }

    def _change_from_resource_row(
        self, row: sqlite3.Row, *, mode: str, cursor: str
    ) -> dict[str, Any]:
        return {
            "cursor": cursor,
            "revision": row["valid_from_revision"],
            "operation": "upsert",
            "resource_kind": row["resource_kind"],
            "path": json.loads(row["path_json"]),
            "resource": self._payload(row, mode=mode),
            "reason": None,
        }

    def _resource_for_change(
        self, connection: sqlite3.Connection, change: sqlite3.Row, *, mode: str
    ) -> dict[str, Any] | None:
        if change["operation"] != "upsert":
            return None
        row = connection.execute(
            """
            SELECT * FROM living_resource_versions
             WHERE resource_kind = ? AND resource_key = ?
               AND valid_from_revision = ?
             LIMIT 1
            """,
            (
                change["resource_kind"],
                change["resource_key"],
                change["revision"],
            ),
        ).fetchone()
        return self._payload(row, mode=mode) if row is not None else None

    @staticmethod
    def _payload(row: sqlite3.Row, *, mode: str) -> dict[str, Any]:
        if mode == "details":
            return json.loads(zlib.decompress(row["detail_payload_z"]).decode("utf-8"))
        return json.loads(row["view_payload_json"])

    def _expand_scope(
        self,
        connection: sqlite3.Connection,
        scope: dict[str, Any],
        revision: int,
    ) -> dict[str, Any]:
        expanded = {key: value for key, value in scope.items() if value}
        if expanded.get("item_id"):
            lookup_kind = "item"
        elif expanded.get("turn_id"):
            lookup_kind = "turn"
        elif expanded.get("session_id"):
            lookup_kind = "session"
        else:
            lookup_kind = None
        lookup_key = (
            expanded.get("item_id")
            or expanded.get("turn_id")
            or expanded.get("session_id")
        )
        if lookup_kind and lookup_key:
            row = connection.execute(
                """
                SELECT path_json FROM living_resource_versions
                 WHERE resource_kind = ? AND resource_key = ?
                   AND valid_from_revision <= ?
                   AND (valid_to_revision IS NULL OR valid_to_revision > ?)
                 ORDER BY valid_from_revision DESC LIMIT 1
                """,
                (lookup_kind, str(lookup_key), revision, revision),
            ).fetchone()
            if row is not None:
                path = json.loads(row["path_json"])
                for key, value in path.items():
                    existing = expanded.get(key)
                    if existing and existing != value:
                        raise ValueError(f"scope {key} does not match {lookup_kind}")
                    expanded.setdefault(key, value)
        return expanded

    def _insert_change(
        self,
        connection: sqlite3.Connection,
        *,
        revision: int,
        operation: str,
        resource_kind: str,
        resource_key: str,
        root_session_id: str,
        path: dict[str, Any],
        reason: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO living_changes(
                revision, operation, resource_kind, resource_key,
                root_session_id, session_id, turn_id, item_id,
                source_session_id, target_session_id, path_json, reason
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision,
                operation,
                resource_kind,
                resource_key,
                root_session_id,
                path.get("session_id"),
                path.get("turn_id"),
                path.get("item_id"),
                path.get("source_session_id"),
                path.get("target_session_id"),
                canonical_json(path),
                reason,
            ),
        )

    def _prune(self, connection: sqlite3.Connection, revision: int) -> None:
        cutoff = revision - _RETAINED_REVISIONS
        if cutoff <= 0:
            return
        connection.execute("DELETE FROM living_changes WHERE revision <= ?", (cutoff,))
        connection.execute(
            "DELETE FROM living_revisions WHERE revision <= ?", (cutoff,)
        )
        connection.execute(
            """
            DELETE FROM living_resource_versions
             WHERE valid_to_revision IS NOT NULL AND valid_to_revision <= ?
            """,
            (cutoff,),
        )
        previous = int(self._metadata(connection, "pruned_through") or "0")
        if cutoff > previous:
            self._set_metadata(connection, "pruned_through", str(cutoff))

    def _encode_cursor(
        self, connection: sqlite3.Connection, value: dict[str, Any]
    ) -> str:
        raw = canonical_json(value).encode("utf-8")
        signature = hmac.new(
            self._metadata(connection, "cursor_secret").encode("utf-8"),
            raw,
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(signature + raw).decode("ascii").rstrip("=")

    def _decode_cursor(
        self, connection: sqlite3.Connection, token: str | None
    ) -> dict[str, Any] | None:
        if token is None:
            return None
        try:
            padded = token + "=" * (-len(token) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
            signature, raw = decoded[:32], decoded[32:]
            expected = hmac.new(
                self._metadata(connection, "cursor_secret").encode("utf-8"),
                raw,
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("signature mismatch")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("cursor payload is not an object")
            return value
        except Exception as exc:
            raise ValueError("invalid or expired living-events cursor") from exc

    @staticmethod
    def _validate_cursor_scope(cursor: dict[str, Any], scope_hash: str) -> None:
        if cursor.get("scope") != scope_hash:
            raise ValueError("cursor scope does not match request scope")

    @staticmethod
    def _set_default(connection: sqlite3.Connection, key: str, value: str) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO living_metadata(key, value) VALUES(?, ?)",
            (key, value),
        )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        existing = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _backfill_path_columns(connection: sqlite3.Connection, table: str) -> None:
        rows = connection.execute(
            f"""
            SELECT rowid AS source_rowid, path_json FROM {table}
             WHERE session_id IS NULL
               AND turn_id IS NULL
               AND item_id IS NULL
               AND source_session_id IS NULL
               AND target_session_id IS NULL
            """
        ).fetchall()
        updates = []
        for row in rows:
            path = json.loads(row["path_json"])
            updates.append(
                (
                    path.get("session_id"),
                    path.get("turn_id"),
                    path.get("item_id"),
                    path.get("source_session_id"),
                    path.get("target_session_id"),
                    row["source_rowid"],
                )
            )
        if updates:
            connection.executemany(
                f"""
                UPDATE {table}
                   SET session_id = ?, turn_id = ?, item_id = ?,
                       source_session_id = ?, target_session_id = ?
                 WHERE rowid = ?
                """,
                updates,
            )

    @staticmethod
    def _set_metadata(connection: sqlite3.Connection, key: str, value: str) -> None:
        connection.execute(
            """
            INSERT INTO living_metadata(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    @staticmethod
    def _metadata(connection: sqlite3.Connection, key: str) -> str:
        row = connection.execute(
            "SELECT value FROM living_metadata WHERE key = ?", (key,)
        ).fetchone()
        return str(row[0]) if row is not None else ""

    @classmethod
    def _current_revision(cls, connection: sqlite3.Connection) -> int:
        return int(cls._metadata(connection, "revision") or "0")


def _resource_digest(resource: ProjectedResource) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "path": resource.path,
                "sort_key": resource.sort_key,
                "view": resource.view,
                "details": resource.details,
            }
        ).encode("utf-8")
    ).hexdigest()


def _scope_sql(scope: dict[str, Any]) -> tuple[list[str], list[Any]]:
    conditions: list[str] = []
    bindings: list[Any] = []
    root_id = scope.get("root_session_id")
    if root_id:
        conditions.append("root_session_id = ?")
        bindings.append(root_id)
    session_id = scope.get("session_id")
    if session_id:
        conditions.append(
            """
            (
                (resource_kind = 'session_edge' AND
                    (source_session_id = ? OR target_session_id = ?))
                OR
                (resource_kind != 'session_edge' AND session_id = ?)
            )
            """
        )
        bindings.extend((session_id, session_id, session_id))
    turn_id = scope.get("turn_id")
    if turn_id:
        conditions.append(
            """
            (
                resource_kind IN ('session', 'context_checkpoint', 'session_edge')
                OR turn_id = ?
            )
            """
        )
        bindings.append(turn_id)
    item_id = scope.get("item_id")
    if item_id:
        conditions.append(
            """
            (
                resource_kind IN (
                    'session', 'turn', 'context_checkpoint', 'session_edge'
                )
                OR item_id = ?
            )
            """
        )
        bindings.append(item_id)
    return conditions, bindings


def _row_position(row: sqlite3.Row) -> str:
    return "\x00".join((row["sort_key"], row["resource_kind"], row["resource_key"]))
