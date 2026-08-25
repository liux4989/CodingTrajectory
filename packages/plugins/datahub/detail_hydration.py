"""Bounded JSONL hydration for datahub event/item detail views.

Derived SQLite state retains only canonical identities and source byte-range
locators.  When a detail view needs content, this module re-reads the
authoritative JSONL: the source checkpoint fence and per-record digests are
verified first, the file is re-ingested at full fidelity (with the same
fork-cut inputs the compact path used), and canonical responses are produced
by the same core serializers.  A fence or digest failure schedules
reconciliation and refuses the detail instead of returning unverified data.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace as _dataclass_replace
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

from coding_trajectory.analysis.projections import build_item_details
from coding_trajectory.discovery import stabilize_session
from coding_trajectory.ingestion.adapters.claude_code import ClaudeCodeAdapter
from coding_trajectory.ingestion.adapters.codex import CodexAdapter
from coding_trajectory.ingestion.adapters.pi import PiAdapter
from coding_trajectory.ingestion.indexes import (
    SessionGraphIndex,
    build_session_graph_index,
)
from coding_trajectory.ingestion.models import (
    PlanItem,
    Session,
    SessionEdge,
    SessionGraph,
    Vendor,
)
from coding_trajectory.service import serialize_event_detail

try:
    from .incremental_store import (
        DetailItemRow,
        DetailSpan,
        IncrementalStore,
        SourceFenceError,
    )
except ImportError:
    from incremental_store import (
        DetailItemRow,
        DetailSpan,
        IncrementalStore,
        SourceFenceError,
    )


class DetailUnavailable(RuntimeError):
    """The requested detail could not be verified against its source."""


_ADAPTER_BY_VENDOR = {
    Vendor.CODEX_CLI: CodexAdapter,
    Vendor.CLAUDE_CODE: ClaudeCodeAdapter,
    Vendor.PI: PiAdapter,
}


class _HydratedFile:
    """One fence-verified, full-fidelity re-ingestion of a single source."""

    def __init__(
        self,
        *,
        session: Session,
        graph: SessionGraph,
        index: SessionGraphIndex,
    ) -> None:
        self.session = session
        self.graph = graph
        self.index = index


class DetailHydrator:
    """Hydrate canonical event/item detail from authoritative JSONL bytes."""

    def __init__(
        self,
        store: IncrementalStore,
        *,
        reconcile: Callable[[], None],
    ) -> None:
        self._store = store
        self._reconcile = reconcile
        self._files: dict[str, _HydratedFile | None] = {}

    def events(
        self,
        event_ids: list[str],
        *,
        turn_id: str | None = None,
        event_type: str | None = None,
    ) -> dict[str, Any]:
        rows = self._store.detail_events(event_ids)
        matches: list[dict[str, Any]] = []
        root_id: str | None = None
        for raw_id in event_ids:
            row = rows.get(str(raw_id))
            if row is None:
                continue
            hydrated = self._file_for(
                row.source_path,
                spans=[
                    DetailSpan(
                        byte_offset=row.byte_offset,
                        byte_end=row.byte_end,
                        digest=row.digest,
                    )
                ],
            )
            event = hydrated.index.events_by_id.get(_parse_uuid(row.event_id))
            if event is None:
                raise DetailUnavailable(
                    f"event {row.event_id} no longer reproducible from source"
                )
            if turn_id is not None:
                allowed = _turn_event_ids(hydrated.session, turn_id)
                if allowed is not None and event.event_id not in allowed:
                    continue
            related = hydrated.index.items_by_event_id.get(event.event_id)
            matches.append(serialize_event_detail(event, related_item=related))
            if root_id is None:
                root_id = row.root_id
        return {"root_session_id": root_id, "type": event_type, "matches": matches}

    def items(
        self,
        item_ids: list[str],
        *,
        include_content: bool = False,
        turn_id: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._store.detail_items(item_ids)
        result: list[dict[str, Any]] = []
        for raw_id in item_ids:
            row = rows.get(str(raw_id))
            if row is None:
                continue
            hydrated = self._file_for(row.source_path, spans=list(row.spans))
            item = hydrated.index.items_by_id.get(_parse_uuid(row.item_id))
            if item is None:
                raise DetailUnavailable(
                    f"item {row.item_id} no longer reproducible from source"
                )
            if turn_id is not None and str(item.turn_id) != str(turn_id):
                continue
            index = (
                _index_with_edge_targets(hydrated.index, row)
                if isinstance(item, PlanItem)
                else None
            )
            result.append(
                build_item_details(
                    item,
                    session_graph=hydrated.graph,
                    include_content=include_content,
                    index=index,
                )
            )
        return result

    # -- hydration core ---------------------------------------------------

    def _file_for(
        self,
        source_path: str,
        *,
        spans: list[DetailSpan],
    ) -> _HydratedFile:
        cached = self._files.get(source_path)
        if cached is not None:
            return cached
        hydrated = self._load_file(source_path, spans=spans)
        self._files[source_path] = hydrated
        return hydrated

    def _load_file(
        self,
        source_path: str,
        *,
        spans: list[DetailSpan],
    ) -> _HydratedFile:
        snapshot = self._store.source(source_path)
        if snapshot is None or snapshot.deleted:
            raise DetailUnavailable(f"source no longer registered: {source_path}")
        try:
            self._store._assert_source_snapshots_current((snapshot,))
            self._verify_spans(source_path, spans)
        except (SourceFenceError, DetailUnavailable):
            self._reconcile()
            raise DetailUnavailable(f"source changed since ingestion: {source_path}")

        vendor_value = snapshot.metadata.get("vendor")
        try:
            vendor = Vendor(str(vendor_value))
            adapter_cls = _ADAPTER_BY_VENDOR[vendor]
        except (ValueError, KeyError) as exc:
            raise DetailUnavailable(
                f"source {source_path} has unsupported vendor: {vendor_value}"
            ) from exc
        adapter = adapter_cls()
        parent_started: set[str] | None = None
        if snapshot.parent_link:
            parent_source = next(
                (
                    source
                    for source in self._store.sources(include_deleted=False)
                    if source.metadata.get("session_id") == snapshot.parent_link
                ),
                None,
            )
            if parent_source is not None:
                parent_started = adapter.scan_started_turn_ids(parent_source.path)
        session = adapter.ingest_file(
            Path(source_path), parent_started_turn_ids=parent_started
        )
        session = stabilize_session(session, vendor=vendor, source=Path(source_path))
        graph = SessionGraph(
            root_session_id=session.session_id,
            sessions=[session],
        )
        return _HydratedFile(
            session=session,
            graph=graph,
            index=build_session_graph_index(graph),
        )

    @staticmethod
    def _verify_spans(source_path: str, spans: list[DetailSpan]) -> None:
        try:
            with open(source_path, "rb") as handle:
                for span in spans:
                    handle.seek(span.byte_offset)
                    raw = handle.read(span.byte_end - span.byte_offset)
                    if hashlib.sha256(raw.strip()).hexdigest() != span.digest:
                        raise DetailUnavailable(
                            f"digest mismatch at {source_path}:{span.byte_offset}"
                        )
        except OSError as exc:
            raise DetailUnavailable(f"source unreadable: {source_path}") from exc


def _parse_uuid(raw: str) -> UUID:
    try:
        return UUID(raw)
    except (ValueError, AttributeError, TypeError):
        return UUID(int=0)


def _turn_event_ids(session: Session, turn_id: str) -> set[UUID] | None:
    parsed = _parse_uuid(turn_id)
    for turn in session.turns:
        if turn.turn_id != parsed:
            continue
        return {
            *turn.event_ids,
            *([turn.user_request_event_id] if turn.user_request_event_id else []),
            *(event_id for item in turn.items for event_id in item.event_ids),
        }
    return set()


def _index_with_edge_targets(
    index: SessionGraphIndex, row: DetailItemRow
) -> SessionGraphIndex:
    """Overlay persisted spawn/handoff edge targets onto the hydrated index."""
    edges = dict(index.outgoing_edges_by_source_item)
    item_uuid = _parse_uuid(row.item_id)
    edges[item_uuid] = [
        SessionEdge(
            type=edge_type,  # type: ignore[arg-type]
            source_session_id=_parse_uuid(row.session_id),
            target_session_id=_parse_uuid(target),
            source_item_id=item_uuid,
        )
        for edge_type, target in row.edge_targets.items()
    ]
    return _dataclass_replace(index, outgoing_edges_by_source_item=edges)


__all__ = ["DetailHydrator", "DetailUnavailable"]
