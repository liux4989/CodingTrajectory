from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from coding_trajectory.ingestion.models import Session, SessionGraph, Vendor
from coding_trajectory.query import DocumentStore
from coding_trajectory.service import _resolve_session_graph, _session_graph_entrypoint_id


def test_resolve_session_graph_accepts_member_session_id() -> None:
    root_session_id = uuid4()
    parent_session_id = uuid4()
    child_session_id = uuid4()
    session_graph = SessionGraph(
        root_session_id=root_session_id,
        sessions=[
            Session(
                session_id=parent_session_id,
                vendor=Vendor.CODEX_CLI,
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            Session(
                session_id=child_session_id,
                vendor=Vendor.CODEX_CLI,
                started_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
                parent_session_id=parent_session_id,
            ),
        ],
    )
    store = DocumentStore.from_session_graphs([session_graph])

    assert _resolve_session_graph(store, str(child_session_id)) is session_graph


def test_session_graph_entrypoint_prefers_session_id() -> None:
    assert _session_graph_entrypoint_id(
        {
            "session_id": "session-entrypoint",
            "root_session_id": "root-entrypoint",
        }
    ) == "session-entrypoint"


def test_session_graph_entrypoint_accepts_root_session_id() -> None:
    assert _session_graph_entrypoint_id({"root_session_id": "root-entrypoint"}) == "root-entrypoint"
