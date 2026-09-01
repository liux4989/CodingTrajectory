"""Source ingestion behavior for the incremental store."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from coding_trajectory.datahub import canonical_json, last_complete_line_offset
from pydantic import TypeAdapter

from datahub_plugin.store.models import (
    ChangeKind,
    IngestionStatus,
    SourceChange,
    SourceFenceError,
    SourceMessage,
    SourceSnapshot,
    _DiskMetadata,
    _IngestionPlan,
)

_JSON_OBJECT = TypeAdapter(dict[str, Any])
_CHECKSUM_BYTES = 64 * 1024


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


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


class IngestionMixin:
    def _build_plans(
        self,
        paths: Sequence[str],
        previous: dict[str, SourceSnapshot],
    ) -> list[_IngestionPlan]:
        plans: list[_IngestionPlan] = []
        requested = set(paths)
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
                    committed = last_complete_line_offset(source, disk.size)
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
                    (_utc_now(), phase, canonical_json(source_paths), error),
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
        connection.execute(
            """
            INSERT INTO source_messages(
                source_message_id, source_id, byte_offset, byte_end, digest,
                explicit_event_id, event_type, event_timestamp, root_link,
                parent_link, active, first_revision, last_revision,
                deleted_revision
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, NULL)
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
                revision,
                revision,
            ),
        )

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
