"""Shared, disposable preparation of local graphs for reads and publication.

Source graphs remain authoritative. Cache keys include canonical graph content
and the preparation version; publication limits are applied only at upload.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from coding_trajectory.control_plane.artifact_protocol import (
    ARTIFACT_PREPARATION_VERSION,
    PreparedGraphSummary,
)
from coding_trajectory.control_plane.fact_projection import build_fact_rows
from coding_trajectory.control_plane.published_facts import (
    FactIndex,
    FactRow,
    PublishedFactSet,
    compute_fact_set_digest,
)
from coding_trajectory.ingestion.common import (
    canonical_json,
    format_datetime,
    normalize_project_key,
)
from coding_trajectory.ingestion.models import SessionGraph


class PreparedGraph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[FactRow]
    summary: PreparedGraphSummary

    def publication(self) -> PublishedFactSet:
        """Enforce transport bounds without truncating the local read view."""
        return PublishedFactSet.from_rows(self.summary.graph_id, self.rows)


def graph_input_digest(graph: SessionGraph) -> str:
    return hashlib.sha256(
        canonical_json(graph.model_dump(mode="json", exclude_none=True)).encode()
    ).hexdigest()


def prepared_graph_summary(index: FactIndex) -> PreparedGraphSummary:
    from coding_trajectory.service.handlers import dispatch
    from coding_trajectory.service.store import IndexCache

    (graph_id,) = index.graph_ids
    rows = list(index.rows_for_graph(graph_id))
    response = dispatch(
        "project.sessions",
        {},
        store=index,
        global_scope=True,
        current_dir=Path.cwd(),
        discovery_note="prepared artifact",
        cache=IndexCache(),
    )
    # Project ownership belongs to the local inventory / remote manifest, not
    # content-addressed graph bytes. Keep existing immutable summary bytes stable.
    for item in response["items"]:
        item.pop("project_id", None)
    return PreparedGraphSummary(
        graph_id=graph_id,
        fact_set_digest=compute_fact_set_digest(graph_id, rows),
        aliases=sorted(
            {
                row.fact_id
                for row in rows
                if row.kind in {"graph", "session", "turn", "item"}
            },
            key=str,
        ),
        project_sessions=response["items"],
    )


def prepare_graph(
    graph: SessionGraph, *, cache_path: Path | None = None
) -> PreparedGraph:
    """Refresh only changed graphs; share prepared bytes across local processes."""
    path = cache_path or Path.home() / ".coding-trajectory" / "prepared-graphs.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Create privately before SQLite opens it; cached facts may contain previews.
    path.touch(mode=0o600, exist_ok=True)
    key = f"{ARTIFACT_PREPARATION_VERSION}:{graph_input_digest(graph)}"
    with closing(sqlite3.connect(path, timeout=30)) as connection, connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS prepared (key TEXT PRIMARY KEY, body BLOB NOT NULL)"
        )
        row = connection.execute(
            "SELECT body FROM prepared WHERE key=?", (key,)
        ).fetchone()
        if row is not None:
            return PreparedGraph.model_validate_json(row[0])
        rows = build_fact_rows(graph)
        prepared = PreparedGraph(
            rows=rows, summary=prepared_graph_summary(FactIndex.from_rows(rows))
        )
        body = prepared.model_dump_json().encode()
        # Bound disk use as well as individual entries; oversized local graphs
        # still work, but are not persisted in this disposable cache.
        budget = 128 * 1024 * 1024
        if len(body) <= budget:
            connection.execute(
                "INSERT OR IGNORE INTO prepared VALUES (?, ?)", (key, body)
            )
            while (
                connection.execute(
                    "SELECT COALESCE(SUM(length(body)),0) FROM prepared"
                ).fetchone()[0]
                > budget
            ):
                connection.execute(
                    "DELETE FROM prepared WHERE rowid=(SELECT MIN(rowid) FROM prepared)"
                )
        return prepared


def filter_session_cards(
    items: list[dict[str, Any]], params: dict[str, Any]
) -> dict[str, Any]:
    """Apply identical lightweight filters to local and remote prepared cards."""
    from coding_trajectory.contracts import service_contract

    name = params.get("project_name")
    vendor = params.get("agent_vendor")
    modified = params.get("modified_since")
    selected = [
        item
        for item in items
        if (
            not name
            or normalize_project_key(item.get("project") or "")
            == normalize_project_key(name)
        )
        and (not vendor or vendor in item.get("vendors", []))
        and (
            not modified
            or item.get("modified") is not None
            and item["modified"] >= format_datetime(modified)
        )
    ]
    selected.sort(
        key=lambda item: (
            item.get("project") or "",
            item.get("lineage_root_session_id") or "",
        )
    )
    return service_contract("project.sessions").validate_response({"items": selected})
