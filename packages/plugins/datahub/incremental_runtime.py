"""Long-lived datahub runtime over revisioned SQLite read models.

This module is the integration boundary between the generic incremental store
and HTTP-facing datahub routes.  It performs metadata reconciliation on a
background worker, bootstraps canonical graph projections in-process, and
serves indexed rows without rebuilding source transcripts on request threads.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Literal
from urllib.parse import quote

from coding_trajectory.datahub import (
    DocumentStore,
    ResourceNotFoundError,
    plan_session_graph_components_from_files,
    project_list_metadata,
    rebuild_affected_session_graphs_from_files,
    rebuild_affected_session_graphs_with_measurements,
)
from coding_trajectory.metrics.measurements import MeasurementMismatchError
from pydantic import BaseModel, ConfigDict, Field

try:
    from . import context_window
    from .analytical_read_models import (
        CANONICAL_FACT_SCOPE,
        FACT_GRAPH_OVERVIEW,
        FACT_GRAPH_STATS,
        FACT_GRAPH_USAGE,
        FACT_PROJECT,
        FACT_PROJECT_SESSION,
        FACT_SESSION_OVERVIEW,
        FACT_SESSION_STATS,
        FACT_SESSION_TREE,
        FACT_SESSION_USAGE,
        FACT_TOOL_USAGE,
        MODEL_META,
        MODEL_SESSION,
        MODEL_TURN,
        TOKEN_HOTSPOT,
        TOKEN_OUTLIER,
        TOKEN_PATTERN,
        TOKEN_PROJECT_META,
        CanonicalFactsApiClient,
        analytical_scope_key,
        build_canonical_fact_rows,
        build_canonical_root_fact_rows,
        build_model_usage_rows,
        build_token_efficiency_project_rows,
        canonical_fact_entity_kinds,
        page_metadata,
        reconstruct_model_usage,
        reconstruct_token_efficiency_project,
    )
    from .detail_hydration import DetailHydrator, DetailUnavailable
    from .incremental_store import (
        DetailEventRow,
        DetailItemRow,
        IncrementalStore,
        MaterializationContext,
        SourceFenceError,
    )
    from .read_models import (
        DEFAULT_RECENT_HORIZON_DAYS,
        aggregate_read_models,
        materialize_graph,
        reconstruct_recent_work,
    )
except ImportError:
    import context_window
    from analytical_read_models import (
        CANONICAL_FACT_SCOPE,
        FACT_GRAPH_OVERVIEW,
        FACT_GRAPH_STATS,
        FACT_GRAPH_USAGE,
        FACT_PROJECT,
        FACT_PROJECT_SESSION,
        FACT_SESSION_OVERVIEW,
        FACT_SESSION_STATS,
        FACT_SESSION_TREE,
        FACT_SESSION_USAGE,
        FACT_TOOL_USAGE,
        MODEL_META,
        MODEL_SESSION,
        MODEL_TURN,
        TOKEN_HOTSPOT,
        TOKEN_OUTLIER,
        TOKEN_PATTERN,
        TOKEN_PROJECT_META,
        CanonicalFactsApiClient,
        analytical_scope_key,
        build_canonical_fact_rows,
        build_canonical_root_fact_rows,
        build_model_usage_rows,
        build_token_efficiency_project_rows,
        canonical_fact_entity_kinds,
        page_metadata,
        reconstruct_model_usage,
        reconstruct_token_efficiency_project,
    )
    from detail_hydration import DetailHydrator, DetailUnavailable
    from incremental_store import (
        DetailEventRow,
        DetailItemRow,
        IncrementalStore,
        MaterializationContext,
        SourceFenceError,
    )
    from read_models import (
        DEFAULT_RECENT_HORIZON_DAYS,
        aggregate_read_models,
        materialize_graph,
        reconstruct_recent_work,
    )


PARSER_VERSION = "core-source-checkpoint-v3"
READ_MODEL_SCHEMA_VERSION = "dashboard-read-model-v5"
DEFAULT_REFRESH_SECONDS = 15.0
RETAINED_CHANGE_REVISIONS = 96
OBSOLETE_DATABASE_GRACE_SECONDS = 24 * 60 * 60

# Context Window resolves only these session-scoped telemetry facts; every row
# is partitioned by the graph root, so one root partition is authoritative.
_CONTEXT_WINDOW_FACT_KINDS = (
    FACT_SESSION_STATS,
    FACT_SESSION_OVERVIEW,
    FACT_SESSION_USAGE,
    FACT_TOOL_USAGE,
)

# Graph detail serves the root-partitioned graph.* facts, one row per kind.
_GRAPH_FACT_PAYLOAD_KEYS = (
    ("overview", FACT_GRAPH_OVERVIEW),
    ("stats", FACT_GRAPH_STATS),
    ("usage", FACT_GRAPH_USAGE),
)

_SESSION_TREE_FACT_KEY = ("tree", FACT_SESSION_TREE)


class RuntimeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    generated_at: str
    freshness: dict[str, Any]
    catching_up: bool
    source_status: dict[str, int]
    minimum_available_revision: int = Field(ge=0)
    bootstrap: dict[str, Any]


class DatahubIncrementalRuntime:
    """Own one persistent store and one coalesced reconciliation worker."""

    def __init__(
        self,
        *,
        current_dir: Path,
        database_path: Path | None = None,
        since_days: int = DEFAULT_RECENT_HORIZON_DAYS,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
        autostart: bool = True,
    ) -> None:
        if since_days < 1:
            raise ValueError("since_days must be at least 1")
        self.current_dir = current_dir.resolve()
        self.since_days = since_days
        self.refresh_seconds = max(1.0, refresh_seconds)
        self._uses_default_database = database_path is None
        resolved_database_path = (database_path or _default_database_path()).resolve()
        self.store = IncrementalStore(
            resolved_database_path,
            parser_version=PARSER_VERSION,
            schema_version=READ_MODEL_SCHEMA_VERSION,
            retained_change_revisions=RETAINED_CHANGE_REVISIONS,
            retain_source_messages=False,
        )
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="datahub-ingest"
        )
        self._future: Future[dict[str, Any]] | None = None
        self._lock = threading.Lock()
        self._evidence_lock = threading.Lock()
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._last_scan_started_at: str | None = None
        self._last_scan_finished_at: str | None = None
        self._last_error: str | None = None
        self._last_result: dict[str, Any] | None = None
        if autostart:
            self.request_refresh(force_bootstrap=self.store.current_revision() == 0)
            self._monitor = threading.Thread(
                target=self._monitor_loop,
                name="datahub-reconcile",
                daemon=True,
            )
            self._monitor.start()

    def shutdown(self) -> None:
        self._stop.set()
        monitor = self._monitor
        if monitor is not None:
            monitor.join(timeout=1.0)
        self._executor.shutdown(wait=True, cancel_futures=True)

    def request_refresh(self, *, force_bootstrap: bool = False) -> dict[str, Any]:
        """Schedule one refresh, coalescing concurrent browser requests."""

        with self._lock:
            future = self._future
            if future is not None and not future.done():
                return {
                    "status": "catching_up",
                    "revision": self.store.current_revision(),
                    "reused": True,
                }
            self._future = self._executor.submit(self._refresh, force_bootstrap)
        return {
            "status": "catching_up",
            "revision": self.store.current_revision(),
            "reused": False,
        }

    def is_ready(self) -> bool:
        """Return whether the minimum product core is revisioned and publishable."""

        return self._has_route_models()

    def snapshot(self) -> dict[str, Any]:
        revision = self.store.current_revision()
        sources = self.store.sources()
        source_status = {"ready": 0, "ingesting": 0, "failed": 0, "incomplete": 0}
        for source in sources:
            if source.deleted:
                continue
            if source.status.value == "ready":
                source_status["ready"] += 1
            elif source.status.value == "partial":
                source_status["incomplete"] += 1
            elif source.status.value == "error":
                source_status["failed"] += 1
            else:
                source_status["ingesting"] += 1
        with self._lock:
            running = self._future is not None and not self._future.done()
            started_at = self._last_scan_started_at
            finished_at = self._last_scan_finished_at
            error = self._last_error
            last_result = dict(self._last_result) if self._last_result else None
        changes = self.store.changes(revision)
        last_ingested_at = changes.last_ingested_at
        lag_seconds = _lag_seconds(last_ingested_at)
        coverage_row = self._singleton(
            "bootstrap_coverage", f"recent:{self.since_days}d"
        )
        return RuntimeSnapshot(
            revision=revision,
            generated_at=datetime.now(UTC).isoformat(),
            freshness={
                "last_refresh_at": last_ingested_at,
                "lag_seconds": lag_seconds,
            },
            catching_up=running or changes.catching_up,
            source_status=source_status,
            minimum_available_revision=max(
                0, (changes.retained_from_revision or 1) - 1
            ),
            bootstrap={
                "ready": self.is_ready(),
                "scan_started_at": started_at,
                "scan_finished_at": finished_at,
                "error": error,
                "last_result": last_result,
                "coverage": coverage_row.payload if coverage_row is not None else None,
            },
        ).model_dump(mode="json")

    def changes(self, after_revision: int) -> dict[str, Any]:
        page = self.store.changes(after_revision)
        upserts: list[dict[str, Any]] = []
        deletions: list[dict[str, Any]] = []
        invalidations: set[str] = set()
        for change in page.changes:
            family = _delivery_family(change.entity_kind)
            if change.operation == "upsert" and change.entity_kind != "source":
                upserts.append(
                    {
                        "entity_type": family,
                        "entity_id": change.entity_key,
                        "revision": change.revision,
                        "payload": change.payload,
                    }
                )
            elif change.operation == "delete" and change.entity_kind != "source":
                deletions.append(
                    {
                        "entity_type": family,
                        "entity_id": change.entity_key,
                        "revision": change.revision,
                    }
                )
            elif change.operation == "invalidate":
                # Keep the invalidation scope on the wire ("family@scope") so
                # clients can target per-graph queries; bare families stay
                # family-wide for backward compatibility.
                invalidations.add(f"{family}@{change.entity_key}")
        snapshot = self.snapshot()
        return {
            "from_revision": page.from_revision,
            "to_revision": page.to_revision,
            "reset_required": page.reset_required or page.has_more,
            "upserts": upserts,
            "deletions": deletions,
            "invalidations": sorted(invalidations),
            "freshness": snapshot["freshness"],
            "catching_up": snapshot["catching_up"],
            "source_status": snapshot["source_status"],
        }

    def overview(self, *, since_days: int) -> dict[str, Any] | None:
        row = self._singleton("overview", f"recent:{since_days}d")
        if row is None:
            return None
        payload = dict(row.payload)
        sessions = payload.get("sessions") or {}
        usage = sessions.get("usage") or {}
        payload.update(
            {
                "schema_version": 1,
                "revision": self.store.current_revision(),
                "generated_at": datetime.now(UTC).isoformat(),
                "cohort": {
                    "since_days": since_days,
                    "session_graph_count": int(sessions.get("count") or 0),
                },
                "coverage": {
                    "total": int(sessions.get("count") or 0),
                    "pricing": int(usage.get("known_cost_count") or 0),
                    "missing_pricing": int(usage.get("missing_cost_count") or 0),
                },
                "warnings": list(sessions.get("warnings") or []),
            }
        )
        return payload

    def today(self) -> dict[str, Any] | None:
        """Serve the trailing-day work projection from retained session rows."""

        if not self._has_route_models():
            return None
        rows: list[Any] = []
        cursor: str | None = None
        while True:
            page = self.store.query_entities(
                "session",
                limit=500,
                cursor=cursor,
                scope_key=f"recent:{self.since_days}d",
            )
            rows.extend(page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        payload = reconstruct_recent_work(rows, since_days=1)
        payload["revision"] = self.store.current_revision()
        return payload

    def projects(
        self, *, agent_vendor: str | None, limit: int, cursor: str | None
    ) -> dict[str, Any] | None:
        if not self._has_route_models():
            return None
        page = self.store.query_entities(
            "project",
            limit=limit,
            cursor=cursor,
            scope_key=f"recent:{self.since_days}d",
        )
        items = [row.payload for row in page.items]
        if agent_vendor:
            items = [
                item for item in items if agent_vendor in (item.get("vendors") or [])
            ]
        return _page_payload(items, page.revision, page.next_cursor)

    def sessions(
        self,
        *,
        since_days: int,
        project_name: str | None,
        agent_vendor: str | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any] | None:
        scope = f"recent:{since_days}d"
        if self._singleton("overview", scope) is None:
            return None
        page = self.store.query_entities(
            "session",
            limit=limit,
            cursor=cursor,
            direction="desc",
            scope_key=scope,
            partition_key=project_name,
        )
        items = []
        for row in page.items:
            payload = dict(row.payload)
            payload.pop("cost_usd", None)
            payload.pop("pricing_confidence", None)
            payload.pop("runtime", None)
            payload.pop("usage", None)
            payload.pop("warnings", None)
            if agent_vendor and agent_vendor not in (payload.get("vendors") or []):
                continue
            items.append(payload)
        return _page_payload(items, page.revision, page.next_cursor)

    def project_detail(
        self,
        *,
        project_name: str,
        since_days: int,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any] | None:
        scope = f"recent:{since_days}d"
        detail_id = f"{quote(project_name, safe='')}:{scope}"
        revision = self.store.current_revision()
        detail = self.store.get_entity("project_detail", detail_id, revision=revision)
        if detail is None:
            return None
        page = self.store.query_entities(
            "session",
            limit=limit,
            cursor=cursor,
            direction="desc",
            scope_key=scope,
            partition_key=project_name,
            revision=revision,
        )
        payload = dict(detail.payload)
        payload["sessions"] = [
            _session_inventory_payload(row.payload) for row in page.items
        ]
        payload["page"] = {
            "revision": page.revision,
            "next_cursor": page.next_cursor,
            "has_more": page.next_cursor is not None,
        }
        return payload

    def session_timeline(
        self, *, since_days: int, limit: int, cursor: str | None
    ) -> dict[str, Any] | None:
        scope = f"recent:{since_days}d"
        if self._singleton("overview", scope) is None:
            return None
        page = self.store.query_entities(
            "session_timeline_contribution",
            limit=limit,
            cursor=cursor,
            direction="desc",
            scope_key=scope,
        )
        by_date: dict[str, list[dict[str, Any]]] = {}
        for row in page.items:
            date = str(row.payload.get("date") or "unknown")
            item = row.payload.get("session")
            if isinstance(item, dict):
                by_date.setdefault(date, []).append(item)
        materialized = self._singleton("session_timeline", scope)
        total = (
            int(materialized.payload.get("total") or 0)
            if materialized is not None
            else len(page.items)
        )
        return {
            "timeline": [
                {"date": date, "count": len(rows), "sessions": rows}
                for date, rows in sorted(by_date.items(), reverse=True)
            ],
            "total": total,
            "page": {
                "revision": page.revision,
                "next_cursor": page.next_cursor,
                "has_more": page.next_cursor is not None,
            },
        }

    def model_usage(
        self,
        *,
        since_days: int,
        project_name: str | None = None,
        model_key: str | None = None,
        detail: Literal["sessions", "turns", "both"] = "both",
        limit: int = 50,
        cursor: str | None = None,
        revision: int | None = None,
    ) -> dict[str, Any] | None:
        """Serve one default-scope Model Usage detail page from SQLite."""

        if since_days != self.since_days:
            return None
        if not self._has_canonical_facts():
            return None
        if detail not in {"sessions", "turns", "both"}:
            raise ValueError("model usage detail must be sessions, turns, or both")
        scope = analytical_scope_key(
            "model_usage",
            since_days=self.since_days,
            project_name=project_name,
            model_key=model_key,
        )
        if self._analytical_meta(MODEL_META, scope) is None:
            self._materialize_filtered_analytical_scope(
                route="model_usage",
                scope=scope,
                project_name=project_name,
                model_key=model_key,
            )
        if detail == "both":
            sessions_page = self.store.query_entities(
                MODEL_SESSION,
                limit=limit,
                scope_key=scope,
                partition_key="sessions",
                revision=revision,
            )
            turns_page = self.store.query_entities(
                MODEL_TURN,
                limit=limit,
                scope_key=scope,
                partition_key="turns",
                revision=sessions_page.revision,
            )
            meta = self._analytical_meta(
                MODEL_META, scope, revision=sessions_page.revision
            )
            if meta is None:
                return None
            payload = dict(meta.payload)
            payload["sessions"] = [row.payload for row in sessions_page.items]
            payload["turns"] = [row.payload for row in turns_page.items]
            payload["pages"] = {
                "sessions": page_metadata(sessions_page, limit=limit),
                "turns": page_metadata(turns_page, limit=limit),
            }
            return _model_usage_contract(payload, revision=sessions_page.revision)
        entity_kind, partition = (
            (MODEL_SESSION, "sessions")
            if detail == "sessions"
            else (MODEL_TURN, "turns")
        )
        page = self.store.query_entities(
            entity_kind,
            limit=limit,
            cursor=cursor,
            scope_key=scope,
            partition_key=partition,
            revision=revision,
        )
        meta = self._analytical_meta(MODEL_META, scope, revision=page.revision)
        if meta is None:
            return None
        payload = reconstruct_model_usage(
            meta,
            detail=detail,
            rows=page.items,
            page=page,
            limit=limit,
        )
        return _model_usage_contract(payload, revision=page.revision)

    def token_efficiency_project(
        self,
        *,
        project_name: str,
        since_days: int,
        limit: int = 50,
        cursor: str | None = None,
        detail: Literal["patterns", "hotspots", "outliers"] | None = None,
        grain: Literal["daily", "weekly"] | None = None,
    ) -> dict[str, Any] | None:
        """Build a project drilldown lazily from persisted canonical facts."""

        if since_days != self.since_days:
            return None
        if not self._has_canonical_facts():
            return None
        try:
            if not self._ensure_project_evidence(project_name):
                return None
        except SourceFenceError:
            # Live transcripts may append while evidence is being rebuilt.  The
            # request has no stable revision yet, so let the HTTP layer return
            # its normal retryable bootstrap response rather than a raw 500.
            return None
        if (detail is None) != (grain is None):
            raise ValueError("token project detail and grain must be supplied together")
        if detail is not None and detail not in {"patterns", "hotspots", "outliers"}:
            raise ValueError(
                "token project detail must be patterns, hotspots, or outliers"
            )
        if grain is not None and grain not in {"daily", "weekly"}:
            raise ValueError("token project grain must be daily or weekly")
        scope = analytical_scope_key(
            "token_efficiency_project",
            since_days=since_days,
            project_name=project_name,
        )
        if self._analytical_meta(TOKEN_PROJECT_META, scope) is None:

            def publish(context: MaterializationContext) -> None:
                adapter = CanonicalFactsApiClient(
                    context.current_entities(
                        entity_kinds=canonical_fact_entity_kinds(),
                        scope_key=CANONICAL_FACT_SCOPE,
                    )
                )
                try:
                    rows = build_token_efficiency_project_rows(
                        client=adapter,
                        project_name=project_name,
                        since_days=since_days,
                    )
                except (ResourceNotFoundError, RuntimeError):
                    return
                _replace_analytical_scope_rows(
                    context,
                    rows,
                    entity_kinds=_token_project_entity_kinds(),
                    scope_key=scope,
                )
                context.assert_sources_current()
                context.record_invalidation("token-efficiency", scope)

            try:
                self._materialize_on_demand(publish)
            except SourceFenceError:
                return None

        revision = self.store.current_revision()
        meta = self._analytical_meta(TOKEN_PROJECT_META, scope, revision=revision)
        if meta is None:
            return None
        if detail is not None and grain is not None:
            kind = {
                "patterns": TOKEN_PATTERN,
                "hotspots": TOKEN_HOTSPOT,
                "outliers": TOKEN_OUTLIER,
            }[detail]
            page = self.store.query_entities(
                kind,
                limit=limit,
                cursor=cursor,
                revision=revision,
                scope_key=scope,
                partition_key=f"{project_name}:{grain}",
            )
            return reconstruct_token_efficiency_project(
                meta,
                detail=detail,
                grain=grain,
                rows=page.items,
                page=page,
                limit=limit,
            )
        return self._token_project_first_pages(
            meta=meta,
            scope=scope,
            project_name=project_name,
            revision=revision,
            limit=limit,
        )

    def context_window(
        self, *, session_id: str, turn_id: str | None = None
    ) -> dict[str, Any] | None:
        """Project Context Window from facts without discovery or subprocesses."""

        if not self._has_canonical_facts():
            return None
        root_id = self._root_for_entrypoint(session_id)
        if root_id is None:
            return None
        if not self._root_has_evidence(root_id) and not self._materialize_evidence(
            {root_id}
        ):
            return None
        revision = self.store.current_revision()
        facts = self._canonical_fact_rows(revision=revision, partition_key=root_id)
        if not facts:
            return None
        try:
            projection = context_window.build_projection(
                session_id,
                turn_id=turn_id,
                client=CanonicalFactsApiClient(facts),
            )
        except (ResourceNotFoundError, RuntimeError):
            return None
        return projection.model_dump(mode="json")

    def graph_detail(self, *, session_id: str) -> dict[str, Any] | None:
        """Serve retained graph overview/stats/usage facts for one entrypoint."""

        if not self._has_canonical_facts():
            return None
        root_id = self._root_for_entrypoint(session_id)
        if root_id is None:
            return None
        if not self._root_has_evidence(root_id) and not self._materialize_evidence(
            {root_id}
        ):
            return None
        revision = self.store.current_revision()
        payload: dict[str, Any] = {"root_session_id": root_id}
        for key, kind in _GRAPH_FACT_PAYLOAD_KEYS:
            page = self.store.query_entities(
                kind,
                limit=1,
                revision=revision,
                scope_key=CANONICAL_FACT_SCOPE,
                partition_key=root_id,
            )
            if not page.items:
                return None
            payload[key] = page.items[0].payload
        return payload

    def session_tree(self, *, session_id: str) -> dict[str, Any] | None:
        """Serve the retained ordinary conversation tree for one entrypoint."""

        if not self._has_canonical_facts():
            return None
        root_id = self._root_for_entrypoint(session_id)
        if root_id is None:
            return None
        if not self._root_has_evidence(root_id) and not self._materialize_evidence(
            {root_id}
        ):
            return None
        page = self.store.query_entities(
            _SESSION_TREE_FACT_KEY[1],
            limit=1,
            revision=self.store.current_revision(),
            scope_key=CANONICAL_FACT_SCOPE,
            partition_key=root_id,
        )
        return page.items[0].payload if page.items else None

    def session_event_details(
        self,
        *,
        event_ids: list[str],
        turn_id: str | None = None,
        event_type: str | None = None,
    ) -> dict[str, Any] | None:
        """Hydrate full event detail from authoritative JSONL byte ranges."""

        if not self._has_canonical_facts():
            return None
        hydrator = DetailHydrator(self.store, reconcile=lambda: self.request_refresh())
        try:
            return hydrator.events(event_ids, turn_id=turn_id, event_type=event_type)
        except DetailUnavailable:
            return None

    def session_item_details(
        self,
        *,
        item_ids: list[str],
        include_content: bool = False,
        turn_id: str | None = None,
    ) -> list[dict[str, Any]] | None:
        """Hydrate full item detail from authoritative JSONL byte ranges."""

        if not self._has_canonical_facts():
            return None
        hydrator = DetailHydrator(self.store, reconcile=lambda: self.request_refresh())
        try:
            return hydrator.items(
                item_ids, include_content=include_content, turn_id=turn_id
            )
        except DetailUnavailable:
            return None

    def _token_project_first_pages(
        self,
        *,
        meta: Any,
        scope: str,
        project_name: str,
        revision: int,
        limit: int,
    ) -> dict[str, Any]:
        payload = dict(meta.payload)
        payload["patterns"] = {}
        payload["hotspots"] = {}
        payload["outliers"] = {}
        payload["pages"] = {"patterns": {}, "hotspots": {}, "outliers": {}}
        for detail, kind in (
            ("patterns", TOKEN_PATTERN),
            ("hotspots", TOKEN_HOTSPOT),
            ("outliers", TOKEN_OUTLIER),
        ):
            for grain in ("daily", "weekly"):
                page = self.store.query_entities(
                    kind,
                    limit=limit,
                    revision=revision,
                    scope_key=scope,
                    partition_key=f"{project_name}:{grain}",
                )
                payload[detail][grain] = [row.payload for row in page.items]
                payload["pages"][detail][grain] = page_metadata(page, limit=limit)
        return payload

    def _canonical_fact_rows(self, *, revision: int, partition_key: str) -> list[Any]:
        rows: list[Any] = []
        for kind in _CONTEXT_WINDOW_FACT_KINDS:
            cursor: str | None = None
            while True:
                page = self.store.query_entities(
                    kind,
                    limit=500,
                    cursor=cursor,
                    revision=revision,
                    scope_key=CANONICAL_FACT_SCOPE,
                    partition_key=partition_key,
                )
                rows.extend(page.items)
                cursor = page.next_cursor
                if cursor is None:
                    break
        return rows

    def _ensure_project_evidence(self, project_name: str) -> bool:
        root_ids = {
            str(source.root_link)
            for source in self.store.sources(include_deleted=False)
            if source.root_link
            and str(source.metadata.get("project_identifier") or "") == project_name
        }
        if not root_ids:
            return False
        missing = {root for root in root_ids if not self._root_has_evidence(root)}
        return not missing or self._materialize_evidence(missing)

    def _materialize_evidence(self, root_ids: set[str]) -> bool:
        from coding_trajectory.analysis.session_graph_views import (
            session_graph_has_visible_overview_content,
        )

        with self._evidence_lock:
            missing = {root for root in root_ids if not self._root_has_evidence(root)}
            if not missing:
                return True
            sources = tuple(
                source
                for source in self.store.sources(include_deleted=False)
                if source.root_link in missing
            )
            if not sources:
                return False
            try:
                graph_build = rebuild_affected_session_graphs_with_measurements(
                    sources=sources
                )
            except MeasurementMismatchError as exc:
                raise SourceFenceError(str(exc)) from exc
            if not graph_build.graphs:
                return False
            project_items = self._project_catalog_items()
            rows: list[dict[str, Any]] = []
            rebuilt_roots: set[str] = set()
            skipped_roots: set[str] = set()
            for graph in graph_build.graphs:
                root = str(graph.root_session_id)
                if not session_graph_has_visible_overview_content(graph):
                    # A source can retain project metadata while its graph has
                    # no visible session content.  It contributes no telemetry
                    # facts, so it must not block a project's other roots.
                    skipped_roots.add(root)
                    continue
                project_name = str(graph.project_identifier or "unknown")
                root_rows = build_canonical_root_fact_rows(
                    store=DocumentStore.from_session_graphs([graph]),
                    root_session_id=root,
                    current_dir=self.current_dir,
                    project_list={
                        "items": {
                            project_name: project_items.get(
                                project_name,
                                {"path": None, "vendors": []},
                            )
                        }
                    },
                    include_projects=False,
                    economics_detail="evidence",
                )
                rebuilt_roots.add(root)
                rows.extend(root_rows)

            def publish(context: MaterializationContext) -> None:
                for root in rebuilt_roots:
                    _delete_lineage_facts(context, root)
                context.mutate_many(rows)
                context.assert_sources_current(sources)
                context.record_invalidation(
                    "context-window",
                    ",".join(sorted(rebuilt_roots)),
                    {"detail": "evidence"},
                )
                context.record_invalidation(
                    "token-efficiency",
                    ",".join(sorted(rebuilt_roots)),
                    {"detail": "evidence"},
                )

            if rows:
                self._materialize_on_demand(publish)
            return missing <= (rebuilt_roots | skipped_roots)

    def _root_for_entrypoint(self, entrypoint_id: str) -> str | None:
        cursor: str | None = None
        while True:
            page = self.store.query_entities(
                FACT_PROJECT_SESSION,
                limit=500,
                cursor=cursor,
                scope_key=CANONICAL_FACT_SCOPE,
            )
            for row in page.items:
                root = str(row.payload.get("root_session_id") or row.tiebreaker)
                session_ids = {
                    str(value) for value in row.payload.get("session_ids") or []
                }
                if entrypoint_id == root or entrypoint_id in session_ids:
                    return root
            cursor = page.next_cursor
            if cursor is None:
                return None

    def _root_has_evidence(self, root_id: str) -> bool:
        return bool(
            self.store.query_entities(
                FACT_TOOL_USAGE,
                limit=1,
                scope_key=CANONICAL_FACT_SCOPE,
                partition_key=root_id,
            ).items
            and self.store.query_entities(
                FACT_SESSION_STATS,
                limit=1,
                scope_key=CANONICAL_FACT_SCOPE,
                partition_key=root_id,
            ).items
        )

    def _project_catalog_items(self) -> dict[str, dict[str, Any]]:
        scope = f"recent:{self.since_days}d"
        return {
            str(row.payload.get("name") or row.entity_key): {
                "path": row.payload.get("path"),
                "vendors": row.payload.get("vendors") or [],
            }
            for row in self.store.query_entities(
                "project_catalog",
                limit=500,
                scope_key=scope,
            ).items
        }

    def _materialize_on_demand(
        self,
        publish: Callable[[MaterializationContext], None],
        *,
        attempts: int = 3,
    ) -> None:
        """Materialize from a request thread, tolerating live source writes.

        A session file can be appended between checkpoint registration and the
        source-fence check, so refresh the checkpoints and retry before
        surfacing the fence error.
        """
        for attempt in range(attempts):
            try:
                self.store.materialize_revision(publish)
                return
            except SourceFenceError:
                if attempt == attempts - 1:
                    raise
                self.store.refresh(_candidate_paths(self.since_days))

    def _materialize_filtered_analytical_scope(
        self,
        *,
        route: Literal["model_usage"],
        scope: str,
        project_name: str | None,
        model_key: str | None = None,
    ) -> None:
        def publish(context: MaterializationContext) -> None:
            meta_kind = MODEL_META
            if any(
                context.current_entities(entity_kinds=(meta_kind,), scope_key=scope)
            ):
                return
            adapter = CanonicalFactsApiClient(
                context.current_entities(
                    entity_kinds=canonical_fact_entity_kinds(),
                    scope_key=CANONICAL_FACT_SCOPE,
                )
            )
            if route == "model_usage":
                rows = build_model_usage_rows(
                    client=adapter,
                    since_days=self.since_days,
                    project_name=project_name,
                    model_key=model_key,
                )
                kinds = (MODEL_META, MODEL_SESSION, MODEL_TURN)
                family = "model-usage"
            _replace_analytical_scope_rows(
                context,
                rows,
                entity_kinds=kinds,
                scope_key=scope,
            )
            context.assert_sources_current()
            context.record_invalidation(family, scope)

        self._materialize_on_demand(publish)

    def _refresh(self, force_bootstrap: bool) -> dict[str, Any]:
        started = datetime.now(UTC).isoformat()
        with self._lock:
            self._last_scan_started_at = started
            self._last_error = None
        try:
            candidates = _candidate_paths(self.since_days)
            bootstrap = (
                force_bootstrap
                or not self._has_route_models()
                or not self._has_canonical_facts()
                or not self._has_default_analytical_models()
            )
            if bootstrap:
                result = self._bootstrap(candidates)
            else:
                result = self._incremental_refresh(candidates)
            with self._lock:
                self._last_result = dict(result)
        except Exception as exc:
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            with self._lock:
                self._last_scan_finished_at = datetime.now(UTC).isoformat()
        return result

    def _bootstrap(self, candidates: Sequence[Path]) -> dict[str, Any]:
        bootstrap_started = perf_counter()
        ingestion_started = perf_counter()
        ingestion = self.store.refresh(candidates)
        ingestion_seconds = perf_counter() - ingestion_started
        snapshots = tuple(self.store.sources(include_deleted=False))
        snapshots_by_path = {source.path: source for source in snapshots}
        planning_started = perf_counter()
        plan = plan_session_graph_components_from_files(sources=snapshots)
        planning_seconds = perf_counter() - planning_started
        project_catalog = project_list_metadata(
            {}, global_scope=True, current_dir=self.current_dir
        )
        project_items = project_catalog.get("items") or {}
        if not isinstance(project_items, dict):
            raise TypeError("project metadata catalog has invalid items")
        scope = f"recent:{self.since_days}d"
        catalog_rows = _bootstrap_catalog_mutations(project_items, scope=scope)
        catalog_facts = build_canonical_fact_rows(
            store=DocumentStore.from_session_graphs([]),
            current_dir=self.current_dir,
            project_list=project_catalog,
            economics_detail="core",
        )
        graph_seconds = 0.0
        core_seconds = 0.0
        fact_seconds = 0.0
        publish_seconds = 0.0
        processed_components = 0
        processed_sources = 0
        projected_entities = 0
        fact_entities = len(catalog_facts)
        issue_count = len(plan.issues)
        batch_count = 0
        first_useful_seconds: float | None = None
        first_batch = True
        pending_entities: list[Any] = []
        pending_facts: list[dict[str, Any]] = []
        pending_relationships: list[Any] = []
        pending_projection_issues: list[Any] = []
        pending_graph_issues: list[Any] = list(plan.issues)
        pending_sources: list[Any] = []
        pending_detail: dict[str, tuple[list[DetailEventRow], list[DetailItemRow]]] = {}
        batch_bytes = 0
        batch_started = perf_counter()

        def publish_batch() -> None:
            nonlocal first_batch, publish_seconds, batch_count, first_useful_seconds
            entities = tuple(pending_entities)
            facts = tuple(pending_facts)
            relationships = tuple(pending_relationships)
            projection_issues = tuple(pending_projection_issues)
            graph_issues = tuple(pending_graph_issues)
            fenced_sources = tuple(pending_sources)
            detail = dict(pending_detail)
            coverage = _bootstrap_coverage_payload(
                state="catching_up",
                processed_sources=processed_sources,
                total_sources=plan.total_sources,
                processed_components=processed_components,
                total_components=len(plan.components),
                issue_count=issue_count,
                complete=False,
            )

            def publish(context: MaterializationContext) -> None:
                if first_batch:
                    _clear_all_entities(context)
                    context.clear_detail()
                    context.mutate_many(catalog_rows)
                    context.mutate_many(catalog_facts)
                context.mutate_many(entities)
                context.mutate_many(facts)
                for root, (detail_events, detail_items) in detail.items():
                    context.publish_detail(
                        root, events=detail_events, items=detail_items
                    )
                _publish_relationships(context, relationships)
                _publish_build_issues(context, projection_issues, scope=scope)
                _publish_graph_issues(context, graph_issues, scope=scope)
                aggregates = aggregate_read_models(
                    context.current_entities(
                        entity_kinds=(
                            "project_catalog",
                            "project_contribution",
                            "session_timeline_contribution",
                            "session",
                        ),
                        scope_key=scope,
                    ),
                    since_days=self.since_days,
                )
                _replace_read_model_subset(
                    context,
                    aggregates,
                    entity_kinds=(
                        "project",
                        "project_detail",
                        "overview",
                        "session_timeline",
                    ),
                    scope_key=scope,
                )
                context.mutate(_bootstrap_coverage_mutation(coverage, scope=scope))
                for family in _delivery_families():
                    context.record_invalidation(family, scope, coverage)
                context.assert_sources_current(fenced_sources)

            started = perf_counter()
            self.store.materialize_revision(publish, status="catching_up")
            publish_seconds += perf_counter() - started
            batch_count += 1
            first_batch = False
            if first_useful_seconds is None and self.is_ready():
                first_useful_seconds = perf_counter() - bootstrap_started

        for component in plan.components:
            component_sources = tuple(
                snapshots_by_path[path]
                for path in component.source_paths
                if path in snapshots_by_path
            )
            graph_started = perf_counter()
            graph_build = rebuild_affected_session_graphs_from_files(
                sources=component_sources,
                retention="measurements",
            )
            graph_seconds += perf_counter() - graph_started
            component_relationships = graph_build.source_relationships
            pending_relationships.extend(component_relationships)
            pending_graph_issues.extend(graph_build.issues)
            issue_count += len(graph_build.issues)
            component_graphs = graph_build.graphs
            component_provenance = {
                str(prov.session_id): prov for prov in graph_build.provenance
            }
            del graph_build
            relationships_by_root: dict[Any, list[Any]] = {}
            for relationship in component_relationships:
                relationships_by_root.setdefault(
                    relationship.root_session_id, []
                ).append(relationship)
            for graph in component_graphs:
                root_id = graph.root_session_id
                graph_relationships = relationships_by_root.get(root_id, [])
                graph_sources = tuple(
                    snapshots_by_path[relationship.source_path]
                    for relationship in graph_relationships
                    if relationship.source_path in snapshots_by_path
                )
                # Flush inside large components so detail locators and
                # provenance for one component never pile up in one batch.
                pending_sources.extend(graph_sources)
                pending_relationships.extend(graph_relationships)
                batch_bytes += sum(snapshot.size for snapshot in graph_sources)
                pending_detail[str(root_id)] = _detail_rows_for_graph(
                    graph, component_provenance
                )
                project_name = str(graph.project_identifier or "unknown")
                source_paths = [
                    relationship.source_path for relationship in graph_relationships
                ]
                core_started = perf_counter()
                projected = materialize_graph(
                    graph,
                    current_dir=self.current_dir,
                    since_days=self.since_days,
                    project_metadata=project_items.get(project_name),
                    source_paths=source_paths,
                )
                core_seconds += perf_counter() - core_started
                pending_entities.extend(
                    entity.as_mutation() for entity in projected.entities
                )
                projected_entities += len(projected.entities)
                pending_projection_issues.extend(projected.issues)
                issue_count += len(projected.issues)
                if not projected.entities:
                    continue
                facts_started = perf_counter()
                graph_facts = build_canonical_root_fact_rows(
                    store=DocumentStore.from_session_graphs([graph]),
                    root_session_id=str(graph.root_session_id),
                    current_dir=self.current_dir,
                    project_list={
                        "items": {
                            project_name: project_items.get(
                                project_name,
                                {"path": None, "vendors": []},
                            )
                        }
                    },
                    include_projects=False,
                    economics_detail="core",
                )
                fact_seconds += perf_counter() - facts_started
                pending_facts.extend(graph_facts)
                fact_entities += len(graph_facts)
                del graph
                root_limit = 5 if first_batch else 10
                if (
                    len(pending_detail) >= root_limit
                    or batch_bytes >= 32 * 1024 * 1024
                    or perf_counter() - batch_started >= 2.0
                ):
                    publish_batch()
                    pending_entities.clear()
                    pending_facts.clear()
                    pending_relationships.clear()
                    pending_projection_issues.clear()
                    pending_graph_issues.clear()
                    pending_sources.clear()
                    pending_detail.clear()
                    batch_bytes = 0
                    batch_started = perf_counter()
            del component_graphs
            del component_relationships
            del relationships_by_root
            processed_components += 1
            processed_sources += len(component.source_paths)
        if pending_detail or pending_entities or pending_relationships or first_batch:
            publish_batch()

        analytical_rows: list[dict[str, Any]] = []
        analytical_started = perf_counter()

        def finalize(context: MaterializationContext) -> None:
            nonlocal analytical_rows
            if first_batch:
                _clear_all_entities(context)
                context.mutate_many(catalog_rows)
                context.mutate_many(catalog_facts)
                aggregates = aggregate_read_models(
                    context.current_entities(
                        entity_kinds=("project_catalog",), scope_key=scope
                    ),
                    since_days=self.since_days,
                )
                _replace_read_model_subset(
                    context,
                    aggregates,
                    entity_kinds=(
                        "project",
                        "project_detail",
                        "overview",
                        "session_timeline",
                    ),
                    scope_key=scope,
                )
            fact_adapter = CanonicalFactsApiClient(
                context.current_entities(
                    entity_kinds=canonical_fact_entity_kinds(),
                    scope_key=CANONICAL_FACT_SCOPE,
                )
            )
            analytical_rows = build_model_usage_rows(
                client=fact_adapter,
                since_days=self.since_days,
            )
            _replace_analytical_rows(
                context,
                analytical_rows,
                entity_kinds=_default_analytical_entity_kinds(),
            )
            final_state = "complete" if issue_count == 0 else "partial"
            coverage = _bootstrap_coverage_payload(
                state=final_state,
                processed_sources=processed_sources,
                total_sources=plan.total_sources,
                processed_components=processed_components,
                total_components=len(plan.components),
                issue_count=issue_count,
                complete=True,
            )
            context.mutate(_bootstrap_coverage_mutation(coverage, scope=scope))
            for family in _delivery_families():
                context.record_invalidation(family, scope, coverage)
            context.assert_sources_current()

        publish_started = perf_counter()
        published = self.store.materialize_revision(finalize, status="complete")
        publish_seconds += perf_counter() - publish_started
        analytical_seconds = perf_counter() - analytical_started
        gc_started = perf_counter()
        gc_result = self.store.garbage_collect(compact=True)
        gc_seconds = perf_counter() - gc_started
        obsolete_databases_removed = self._retire_obsolete_databases()
        status = "complete" if issue_count == 0 else "partial"
        return {
            "status": status,
            "revision": published.revision,
            "source_count": len(candidates),
            "component_count": len(plan.components),
            "batch_count": batch_count,
            "entity_count": projected_entities,
            "fact_entity_count": fact_entities,
            "analytical_entity_count": len(analytical_rows),
            "issue_count": issue_count,
            "parsed_bytes": ingestion.parsed_bytes,
            "parsed_lines": ingestion.parsed_lines,
            "garbage_collection": gc_result,
            "obsolete_databases_removed": obsolete_databases_removed,
            "timings": {
                "source_ingestion_seconds": round(ingestion_seconds, 6),
                "topology_planning_seconds": round(planning_seconds, 6),
                "canonical_graph_rebuild_seconds": round(graph_seconds, 6),
                "core_projection_seconds": round(core_seconds, 6),
                "canonical_facts_seconds": round(fact_seconds, 6),
                "analytical_projection_seconds": round(analytical_seconds, 6),
                "sqlite_publication_seconds": round(publish_seconds, 6),
                "first_useful_revision_seconds": (
                    round(first_useful_seconds, 6)
                    if first_useful_seconds is not None
                    else None
                ),
                "garbage_collection_seconds": round(gc_seconds, 6),
                "total_seconds": round(perf_counter() - bootstrap_started, 6),
            },
        }

    def _incremental_refresh(self, candidates: Sequence[Path]) -> dict[str, Any]:
        refresh_started = perf_counter()
        materialization_timings: dict[str, float] = {}

        def materialize(context: MaterializationContext) -> None:
            materialization_timings.update(
                _materialize_changed_graphs(
                    context,
                    current_dir=self.current_dir,
                    since_days=self.since_days,
                )
            )

        result = self.store.refresh(candidates, materialize=materialize)
        gc_started = perf_counter()
        gc_result = self.store.garbage_collect(compact=True)
        gc_seconds = perf_counter() - gc_started
        obsolete_databases_removed = self._retire_obsolete_databases()
        return {
            "status": "unchanged" if not result.changed_sources else "updated",
            "revision": result.revision,
            "changed_sources": len(result.changed_sources),
            "parsed_bytes": result.parsed_bytes,
            "parsed_lines": result.parsed_lines,
            "garbage_collection": gc_result,
            "obsolete_databases_removed": obsolete_databases_removed,
            "timings": {
                **materialization_timings,
                "garbage_collection_seconds": round(gc_seconds, 6),
                "total_seconds": round(perf_counter() - refresh_started, 6),
            },
        }

    def _retire_obsolete_databases(self) -> list[str]:
        if not self._uses_default_database:
            return []
        return _remove_obsolete_dashboard_databases(
            grace_seconds=OBSOLETE_DATABASE_GRACE_SECONDS
        )

    def _singleton(self, entity_kind: str, entity_key: str):
        return self.store.get_entity(entity_kind, entity_key)

    def _analytical_meta(
        self,
        entity_kind: str,
        scope_key: str,
        *,
        revision: int | None = None,
    ):
        page = self.store.query_entities(
            entity_kind,
            limit=1,
            scope_key=scope_key,
            revision=revision,
        )
        return page.items[0] if page.items else None

    def _has_route_models(self) -> bool:
        return bool(self.store.query_entities("overview", limit=1).items)

    def _has_default_analytical_models(self) -> bool:
        scope = analytical_scope_key("model_usage", since_days=self.since_days)
        return self._analytical_meta(MODEL_META, scope) is not None

    def _has_canonical_facts(self) -> bool:
        return bool(
            self.store.query_entities(
                FACT_PROJECT,
                limit=1,
                scope_key=CANONICAL_FACT_SCOPE,
            ).items
            or self.store.query_entities(
                FACT_PROJECT_SESSION,
                limit=1,
                scope_key=CANONICAL_FACT_SCOPE,
            ).items
        )

    def _monitor_loop(self) -> None:
        while not self._stop.wait(self.refresh_seconds):
            self.request_refresh()


try:
    from .incremental_publish import (
        _bootstrap_catalog_mutations,
        _bootstrap_coverage_mutation,
        _bootstrap_coverage_payload,
        _candidate_paths,
        _clear_all_entities,
        _default_analytical_entity_kinds,
        _default_database_path,
        _delete_lineage_facts,
        _delivery_families,
        _delivery_family,
        _detail_rows_for_graph,
        _lag_seconds,
        _materialize_changed_graphs,
        _page_payload,
        _publish_build_issues,
        _publish_graph_issues,
        _publish_relationships,
        _remove_obsolete_dashboard_databases,
        _replace_analytical_rows,
        _replace_analytical_scope_rows,
        _replace_read_model_subset,
        _session_inventory_payload,
        _token_project_entity_kinds,
    )
except ImportError:  # pragma: no cover - direct plugin-directory imports
    from incremental_publish import (
        _bootstrap_catalog_mutations,
        _bootstrap_coverage_mutation,
        _bootstrap_coverage_payload,
        _candidate_paths,
        _clear_all_entities,
        _default_analytical_entity_kinds,
        _default_database_path,
        _delete_lineage_facts,
        _delivery_families,
        _delivery_family,
        _detail_rows_for_graph,
        _lag_seconds,
        _materialize_changed_graphs,
        _page_payload,
        _publish_build_issues,
        _publish_graph_issues,
        _publish_relationships,
        _remove_obsolete_dashboard_databases,
        _replace_analytical_rows,
        _replace_analytical_scope_rows,
        _replace_read_model_subset,
        _session_inventory_payload,
        _token_project_entity_kinds,
    )


def _model_usage_contract(payload: dict[str, Any], *, revision: int) -> dict[str, Any]:
    summary = payload.get("summary") or {}
    filters = payload.get("filters") or {}
    payload.update(
        {
            "revision": revision,
            "generated_at": datetime.now(UTC).isoformat(),
            "cohort": {
                "since_days": int(filters.get("since_days") or 0),
                "session_graph_count": int(summary.get("sessions") or 0),
                "turn_count": int(summary.get("turns") or 0),
            },
            "coverage": {
                "total_models": int(summary.get("models") or 0),
                "missing_pricing": int(summary.get("missing_price_count") or 0),
            },
        }
    )
    return payload


__all__ = ["DatahubIncrementalRuntime", "RuntimeSnapshot"]
