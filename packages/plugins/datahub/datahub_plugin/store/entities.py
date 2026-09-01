"""Versioned entity behavior for the incremental store."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Literal

from coding_trajectory.datahub import canonical_json

from datahub_plugin.store.models import EntityMutation, EntityRow, KeysetPage, _Cursor


class EntitiesMixin:
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
        payload_json = canonical_json(mutation.payload)
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
                    AND (sort_key, tiebreaker, entity_key) {comparator} (?, ?, ?)
                """
                params.extend(
                    [
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
