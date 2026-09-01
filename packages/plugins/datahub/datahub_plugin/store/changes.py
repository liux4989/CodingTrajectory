"""Revision change-log behavior for the incremental store."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
from typing import Any, Literal

from coding_trajectory.datahub import canonical_json

from datahub_plugin.store.models import (
    ChangeKind,
    ChangesPage,
    RevisionChange,
    SourceChange,
    _Cursor,
)

_MAX_CHANGE_PAYLOAD_BYTES = 64 * 1024
_MAX_CHANGE_IDENTIFIER_BYTES = 16 * 1024


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class ChangesMixin:
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
                len(canonical_json(change.model_dump(mode="json")).encode("utf-8"))
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
        payload_json = canonical_json(payload)
        identifier_bytes = len(entity_kind.encode("utf-8")) + len(
            entity_key.encode("utf-8")
        )
        if identifier_bytes > _MAX_CHANGE_IDENTIFIER_BYTES:
            operation = "invalidate"
            entity_key = "oversized:" + _digest(entity_key.encode("utf-8"))
            payload_json = canonical_json(
                {
                    "reason": "identifier_too_large",
                    "identifier_bytes": identifier_bytes,
                }
            )
        if len(payload_json.encode("utf-8")) > _MAX_CHANGE_PAYLOAD_BYTES:
            operation = "invalidate"
            payload_json = canonical_json(
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
            connection.execute("DELETE FROM revisions WHERE revision <= ?", (cutoff,))
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
