"""Materialized detail projection behavior for the incremental store."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from itertools import islice
from typing import Any

from coding_trajectory.datahub import canonical_json

from datahub_plugin.store.models import DetailEventRow, DetailItemRow, DetailSpan


def _executemany_chunked(
    connection: sqlite3.Connection,
    sql: str,
    rows: Iterable[tuple[Any, ...]],
    *,
    chunk_size: int = 4000,
) -> None:
    iterator = iter(rows)
    while chunk := list(islice(iterator, chunk_size)):
        connection.executemany(sql, chunk)


class DetailMixin:
    def _replace_root_detail(
        self,
        connection: sqlite3.Connection,
        root_id: str,
        events: Iterable[DetailEventRow],
        items: Iterable[DetailItemRow],
    ) -> None:
        connection.execute("DELETE FROM detail_events WHERE root_id = ?", (root_id,))
        connection.execute("DELETE FROM detail_items WHERE root_id = ?", (root_id,))
        _executemany_chunked(
            connection,
            """
            INSERT INTO detail_events(
                event_id, root_id, session_id, source_path,
                byte_offset, byte_end, digest
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    row.event_id,
                    root_id,
                    row.session_id,
                    row.source_path,
                    row.byte_offset,
                    row.byte_end,
                    row.digest,
                )
                for row in events
            ),
        )
        _executemany_chunked(
            connection,
            """
            INSERT INTO detail_items(
                item_id, root_id, session_id, turn_id, kind, source_path,
                spans_json, edge_targets_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    row.item_id,
                    root_id,
                    row.session_id,
                    row.turn_id,
                    row.kind,
                    row.source_path,
                    canonical_json(
                        [span.model_dump(mode="json") for span in row.spans]
                    ),
                    canonical_json(row.edge_targets),
                )
                for row in items
            ),
        )

    def _clear_detail(self, connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM detail_events")
        connection.execute("DELETE FROM detail_items")

    def detail_events(self, event_ids: Iterable[str]) -> dict[str, DetailEventRow]:
        """Return current event locators for the supplied canonical ids."""

        ids = tuple(dict.fromkeys(str(value) for value in event_ids))
        if not ids:
            return {}
        with self._connect() as connection:
            rows: dict[str, DetailEventRow] = {}
            for chunk_start in range(0, len(ids), 500):
                chunk = ids[chunk_start : chunk_start + 500]
                placeholders = ",".join("?" for _ in chunk)
                for row in connection.execute(
                    f"SELECT * FROM detail_events WHERE event_id IN ({placeholders})",
                    chunk,
                ):
                    rows[row["event_id"]] = DetailEventRow(
                        event_id=row["event_id"],
                        root_id=row["root_id"],
                        session_id=row["session_id"],
                        source_path=row["source_path"],
                        byte_offset=row["byte_offset"],
                        byte_end=row["byte_end"],
                        digest=row["digest"],
                    )
        return rows

    def detail_items(self, item_ids: Iterable[str]) -> dict[str, DetailItemRow]:
        """Return current item locators for the supplied canonical ids."""

        ids = tuple(dict.fromkeys(str(value) for value in item_ids))
        if not ids:
            return {}
        with self._connect() as connection:
            rows: dict[str, DetailItemRow] = {}
            for chunk_start in range(0, len(ids), 500):
                chunk = ids[chunk_start : chunk_start + 500]
                placeholders = ",".join("?" for _ in chunk)
                for row in connection.execute(
                    f"SELECT * FROM detail_items WHERE item_id IN ({placeholders})",
                    chunk,
                ):
                    rows[row["item_id"]] = DetailItemRow(
                        item_id=row["item_id"],
                        root_id=row["root_id"],
                        session_id=row["session_id"],
                        turn_id=row["turn_id"],
                        kind=row["kind"],
                        source_path=row["source_path"],
                        spans=tuple(
                            DetailSpan.model_validate(span)
                            for span in json.loads(row["spans_json"])
                        ),
                        edge_targets=json.loads(row["edge_targets_json"]),
                    )
        return rows
