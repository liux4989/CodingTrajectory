"""Lazy host evidence attached only to matching published canonical sessions."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from coding_trajectory.control_plane.remote import (
    RemoteControlPlaneError,
    SupabaseHistoricalRepository,
)
from coding_trajectory.control_plane.shareable import build_shareable_graph_artifact
from coding_trajectory.ingestion.models import Session, SessionGraph
from coding_trajectory.query import DocumentStore


def requires_local_evidence(method: str, params: dict[str, Any]) -> bool:
    return (
        method in {"session.events", "session.search"}
        or method == "session.items"
        and bool(params.get("include_content"))
        or method == "graph.overview"
        and "narrative" in params.get("include", [])
    )


def _session_facts(session: Session) -> Any:
    graph = SessionGraph(root_session_id=session.session_id, sessions=[session])
    return build_shareable_graph_artifact(graph).sessions[0]


class LocalEvidenceRepository:
    """Supabase owns selection and facts; local files supply optional bodies.

    Never discovers files for ordinary reads. Evidence requests first resolve a
    published resource, then hydrate its session and verify all retained facts.
    Unpublished or changed sources fail closed instead of replacing the snapshot.
    """

    def __init__(self, canonical: SupabaseHistoricalRepository, *, current_dir: Path):
        self.canonical = canonical
        self.current_dir = current_dir
        self._evidence_scope = False

    def pin_snapshot(self) -> int:
        return self.canonical.pin_snapshot()

    def metadata(self) -> dict[str, Any] | None:
        metadata = self.canonical.metadata()
        if metadata is not None and self._evidence_scope:
            return {
                **metadata,
                "content_scope": "local_evidence",
                "evidence_source": "local",
            }
        return metadata

    def store_for(
        self, method: str, params: dict[str, Any]
    ) -> tuple[DocumentStore, str]:
        self._evidence_scope = False
        if not requires_local_evidence(method, params):
            return self.canonical.store_for(method, params)
        entry = {
            key: params[key]
            for key in ("session_id", "root_session_id", "turn_id")
            if params.get(key)
        }
        if not entry:
            raise RemoteControlPlaneError(
                "local evidence requires a published session, root-session, or turn scope"
            )
        published, note = self.canonical.store_for("session.overview", entry)
        if not published.sessions:
            raise RemoteControlPlaneError("local evidence resource is not published")
        selected = params.get("session_id") or params.get("root_session_id")
        if selected is None:
            turn = published.turns.get(UUID(params["turn_id"]))
            if turn is None:
                raise RemoteControlPlaneError("local evidence turn is not published")
            selected = str(turn.session_id)
        selected_id = UUID(selected)
        if selected_id not in published.sessions:
            raise RemoteControlPlaneError("local evidence session is not published")
        selected_ids = (
            set(published.sessions) if method == "graph.overview" else {selected_id}
        )
        if params.get("turn_id") and UUID(params["turn_id"]) not in published.turns:
            raise RemoteControlPlaneError("local evidence turn is not published")
        if any(
            UUID(item_id) not in published.items
            for item_id in params.get("item_ids") or []
        ):
            raise RemoteControlPlaneError("local evidence item is not published")

        # Import and initialize discovery only after database authorization/selection.
        from coding_trajectory.service import IndexCache, resolve_store

        cache = IndexCache.load()
        hydrated: dict[UUID, Session] = {}
        for session_id in sorted(selected_ids, key=str):
            local, _ = resolve_store(
                {"session_id": str(session_id)},
                global_scope=True,
                current_dir=self.current_dir,
                cache=cache,
                include_descendants=False,
            )
            session = local.sessions.get(session_id)
            if session is None:
                raise RemoteControlPlaneError(
                    "local evidence is unavailable on this host"
                )
            if _session_facts(session) != _session_facts(
                published.sessions[session_id]
            ):
                raise RemoteControlPlaneError(
                    "local evidence does not match the published snapshot; publish the current source before loading its content"
                )
            hydrated[session_id] = session
        graphs = [
            graph.model_copy(
                update={
                    "sessions": [hydrated.get(s.session_id, s) for s in graph.sessions]
                }
            )
            for graph in published.session_graphs.values()
        ]
        self._evidence_scope = True
        return DocumentStore.from_session_graphs(graphs), note
