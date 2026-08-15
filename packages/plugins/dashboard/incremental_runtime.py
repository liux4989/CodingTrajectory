"""Long-lived dashboard runtime over revisioned SQLite read models.

This module is the integration boundary between the generic incremental store
and HTTP-facing dashboard routes.  It performs metadata reconciliation on a
background worker, bootstraps canonical graph projections in-process, and
serves indexed rows without rebuilding source transcripts on request threads.
"""

from __future__ import annotations

import hashlib
import threading
from time import perf_counter
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from coding_trajectory.ingestion.incremental import (
    plan_session_graph_components_from_files,
    rebuild_affected_session_graphs_from_files,
)
from coding_trajectory.query import DocumentStore, ResourceNotFoundError
from coding_trajectory.service import project_list_metadata

try:
    from .incremental_store import (
        IncrementalStore,
        MaterializationContext,
        SourceFenceError,
    )
    from .read_models import (
        DEFAULT_RECENT_HORIZON_DAYS,
        aggregate_read_models,
        materialize_graph,
    )
    from .analytical_read_models import (
        CANONICAL_FACT_SCOPE,
        CanonicalFactsCtJson,
        FACT_PROJECT,
        FACT_PROJECT_SESSION,
        FACT_SESSION_OVERVIEW,
        FACT_SESSION_STATS,
        FACT_SESSION_USAGE,
        FACT_TOOL_USAGE,
        MODEL_META,
        MODEL_SESSION,
        MODEL_TURN,
        TOKEN_INDEX_META,
        TOKEN_HOTSPOT,
        TOKEN_OUTLIER,
        TOKEN_PATTERN,
        TOKEN_PROJECT,
        TOKEN_PROJECT_META,
        analytical_scope_key,
        build_canonical_fact_rows,
        build_canonical_root_fact_rows,
        build_model_usage_rows_from_ct_json,
        build_token_efficiency_project_rows_from_ct_json,
        canonical_fact_entity_kinds,
        page_metadata,
        reconstruct_model_usage,
        reconstruct_token_efficiency_index,
        reconstruct_token_efficiency_project,
    )
    from . import context_window
except ImportError:
    from incremental_store import (
        IncrementalStore,
        MaterializationContext,
        SourceFenceError,
    )
    from read_models import (
        DEFAULT_RECENT_HORIZON_DAYS,
        aggregate_read_models,
        materialize_graph,
    )
    from analytical_read_models import (
        CANONICAL_FACT_SCOPE,
        CanonicalFactsCtJson,
        FACT_PROJECT,
        FACT_PROJECT_SESSION,
        FACT_SESSION_OVERVIEW,
        FACT_SESSION_STATS,
        FACT_SESSION_USAGE,
        FACT_TOOL_USAGE,
        MODEL_META,
        MODEL_SESSION,
        MODEL_TURN,
        TOKEN_INDEX_META,
        TOKEN_HOTSPOT,
        TOKEN_OUTLIER,
        TOKEN_PATTERN,
        TOKEN_PROJECT,
        TOKEN_PROJECT_META,
        analytical_scope_key,
        build_canonical_fact_rows,
        build_canonical_root_fact_rows,
        build_model_usage_rows_from_ct_json,
        build_token_efficiency_project_rows_from_ct_json,
        canonical_fact_entity_kinds,
        page_metadata,
        reconstruct_model_usage,
        reconstruct_token_efficiency_index,
        reconstruct_token_efficiency_project,
    )
    import context_window


PARSER_VERSION = "core-source-checkpoint-v3"
READ_MODEL_SCHEMA_VERSION = "dashboard-read-model-v3"
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


class RuntimeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    generated_at: str
    freshness: dict[str, Any]
    catching_up: bool
    source_status: dict[str, int]
    minimum_available_revision: int = Field(ge=0)
    bootstrap: dict[str, Any]


class DashboardIncrementalRuntime:
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
            max_workers=1, thread_name_prefix="dashboard-ingest"
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
                name="dashboard-reconcile",
                daemon=True,
            )
            self._monitor.start()

    def shutdown(self) -> None:
        self._stop.set()
        monitor = self._monitor
        if monitor is not None:
            monitor.join(timeout=1.0)
        self._executor.shutdown(wait=False, cancel_futures=True)

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

    def wait_until_idle(self, *, timeout: float | None = None) -> dict[str, Any] | None:
        """Wait for the current refresh; intended for CLI/bootstrap benchmarks."""

        with self._lock:
            future = self._future
        return future.result(timeout=timeout) if future is not None else None

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
        return row.payload if row is not None else None

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
            return payload
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
        return reconstruct_model_usage(
            meta,
            detail=detail,
            rows=page.items,
            page=page,
            limit=limit,
        )

    def token_efficiency_index(
        self,
        *,
        since_days: int,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any] | None:
        """Serve the default-scope Token Efficiency project index from SQLite."""

        if since_days != self.since_days:
            return None
        if not self._has_canonical_facts():
            return None
        scope = analytical_scope_key(
            "token_efficiency_index", since_days=self.since_days
        )
        if self._analytical_meta(TOKEN_INDEX_META, scope) is None:
            return None
        page = self.store.query_entities(
            TOKEN_PROJECT,
            limit=limit,
            cursor=cursor,
            scope_key=scope,
            partition_key="projects",
        )
        meta = self._analytical_meta(TOKEN_INDEX_META, scope, revision=page.revision)
        if meta is None:
            return None
        return reconstruct_token_efficiency_index(
            meta,
            rows=page.items,
            page=page,
            limit=limit,
        )

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
        if not self._ensure_project_evidence(project_name):
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
                adapter = CanonicalFactsCtJson(
                    context.current_entities(
                        entity_kinds=canonical_fact_entity_kinds(),
                        scope_key=CANONICAL_FACT_SCOPE,
                    )
                )
                try:
                    rows = build_token_efficiency_project_rows_from_ct_json(
                        ct_json=adapter,
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

            self._materialize_on_demand(publish)

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
                ct_json=CanonicalFactsCtJson(facts),
            )
        except (ResourceNotFoundError, RuntimeError):
            return None
        return projection.model_dump(mode="json")

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
            graph_build = rebuild_affected_session_graphs_from_files(sources=sources)
            if not graph_build.graphs:
                return False
            project_items = self._project_catalog_items()
            rows: list[dict[str, Any]] = []
            rebuilt_roots: set[str] = set()
            for graph in graph_build.graphs:
                root = str(graph.root_session_id)
                rebuilt_roots.add(root)
                project_name = str(graph.project_identifier or "unknown")
                rows.extend(
                    build_canonical_root_fact_rows(
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
                )

            def publish(context: MaterializationContext) -> None:
                for root in rebuilt_roots:
                    _delete_root_facts(context, root)
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

            self._materialize_on_demand(publish)
            return missing <= rebuilt_roots

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
            adapter = CanonicalFactsCtJson(
                context.current_entities(
                    entity_kinds=canonical_fact_entity_kinds(),
                    scope_key=CANONICAL_FACT_SCOPE,
                )
            )
            if route == "model_usage":
                rows = build_model_usage_rows_from_ct_json(
                    ct_json=adapter,
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
            raise RuntimeError("project metadata catalog has invalid items")
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
                    context.mutate_many(catalog_rows)
                    context.mutate_many(catalog_facts)
                context.mutate_many(entities)
                context.mutate_many(facts)
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
            del graph_build
            for graph in component_graphs:
                project_name = str(graph.project_identifier or "unknown")
                source_paths = [
                    relationship.source_path
                    for relationship in component_relationships
                    if relationship.root_session_id == graph.root_session_id
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
            if component_graphs:
                del graph
            # Do not keep the previous component resident while Python
            # evaluates the next canonical rebuild call.
            del component_graphs
            del component_relationships
            processed_components += 1
            processed_sources += len(component.source_paths)
            pending_sources.extend(component_sources)
            batch_bytes += component.total_bytes
            root_limit = 5 if first_batch else 10
            if (
                processed_components == len(plan.components)
                or len(pending_sources) >= root_limit
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
                batch_bytes = 0
                batch_started = perf_counter()

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
            fact_adapter = CanonicalFactsCtJson(
                context.current_entities(
                    entity_kinds=canonical_fact_entity_kinds(),
                    scope_key=CANONICAL_FACT_SCOPE,
                )
            )
            analytical_rows = build_model_usage_rows_from_ct_json(
                ct_json=fact_adapter,
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

    def _has_evidence_facts(self) -> bool:
        return bool(
            self.store.query_entities(
                FACT_TOOL_USAGE,
                limit=1,
                scope_key=CANONICAL_FACT_SCOPE,
            ).items
            and self.store.query_entities(
                FACT_SESSION_STATS,
                limit=1,
                scope_key=CANONICAL_FACT_SCOPE,
            ).items
        )

    def _monitor_loop(self) -> None:
        while not self._stop.wait(self.refresh_seconds):
            self.request_refresh()


def _materialize_changed_graphs(
    context: MaterializationContext,
    *,
    current_dir: Path,
    since_days: int,
) -> dict[str, float]:
    """Repair changed partitions by reopening only affected source components."""

    started = perf_counter()
    scope = f"recent:{since_days}d"
    errored = [change for change in context.changed_sources if change.error]
    if errored:
        for change in errored:
            _record_ingestion_issue(
                context,
                scope=scope,
                stage="message_ingestion",
                code="incremental.message_ingestion_failed",
                message=change.error or "message ingestion failed",
                source_path=change.path,
                root_session_id=(
                    change.previous.root_link if change.previous is not None else None
                ),
            )
        for family in _delivery_families():
            context.record_invalidation(
                family, scope, {"reason": "message_ingestion_failed"}
            )
        return {"affected_projection_seconds": round(perf_counter() - started, 6)}

    sources = tuple(context.current_sources(include_deleted=True))
    source_by_path = {source.path: source for source in sources}
    seed_paths = tuple(change.path for change in context.changed_sources)
    old_roots = tuple(
        sorted(
            {
                change.previous.root_link
                for change in context.changed_sources
                if change.previous is not None and change.previous.root_link
            }
        )
    )

    graph_started = perf_counter()
    graph_build = rebuild_affected_session_graphs_from_files(
        sources=sources,
        seed_paths=seed_paths,
        old_root_session_ids=old_roots,
        retention="measurements",
    )
    graph_seconds = perf_counter() - graph_started

    affected_roots = {str(value) for value in graph_build.affected_root_session_ids}
    affected_roots.update(old_roots)
    for root in sorted(affected_roots):
        _delete_graph_contributions(context, root)
        _delete_root_facts(context, root)

    core_started = perf_counter()
    new_fact_rows: list[dict[str, Any]] = []
    catalog_by_name = {
        str(row.payload.get("name") or row.entity_key): dict(row.payload)
        for row in context.current_entities(
            entity_kinds=("project_catalog",), scope_key=scope
        )
    }
    for graph in graph_build.graphs:
        root = str(graph.root_session_id)
        source_paths = [
            relationship.source_path
            for relationship in graph_build.source_relationships
            if str(relationship.root_session_id) == root
        ]
        projected = materialize_graph(
            graph,
            current_dir=current_dir,
            since_days=since_days,
            source_paths=source_paths,
        )
        context.mutate_many(entity.as_mutation() for entity in projected.entities)
        for issue in projected.issues:
            context.mutate(
                {
                    "entity_kind": "ingestion_issue",
                    "entity_key": issue.issue_id,
                    "scope_key": scope,
                    "partition_key": root,
                    "sort_key": issue.stage,
                    "tiebreaker": issue.issue_id,
                    "payload": issue.model_dump(mode="json"),
                }
            )
        if not projected.entities:
            continue
        contribution = next(
            (
                entity
                for entity in projected.entities
                if entity.entity_kind == "project_contribution"
            ),
            None,
        )
        project_name = str(graph.project_identifier or "unknown")
        project_metadata = dict(catalog_by_name.get(project_name) or {})
        if contribution is not None:
            project_metadata["name"] = project_name
            if not project_metadata.get("path") and contribution.payload.get("path"):
                project_metadata["path"] = contribution.payload["path"]
            project_metadata["vendors"] = sorted(
                {
                    *[str(value) for value in project_metadata.get("vendors") or []],
                    *[
                        str(value)
                        for value in contribution.payload.get("vendors") or []
                    ],
                }
            )
        catalog_by_name[project_name] = project_metadata
        context.mutate(
            {
                "entity_kind": "project_catalog",
                "entity_key": project_name,
                "scope_key": scope,
                "partition_key": project_name,
                "sort_key": project_name.casefold(),
                "tiebreaker": project_name,
                "payload": project_metadata,
            }
        )
        root_store = DocumentStore.from_session_graphs([graph])
        new_fact_rows.extend(
            build_canonical_root_fact_rows(
                store=root_store,
                root_session_id=root,
                current_dir=current_dir,
                project_list={"items": {project_name: project_metadata}},
                include_projects=True,
                economics_detail="core",
            )
        )
    context.mutate_many(new_fact_rows)

    core_inputs = tuple(
        context.current_entities(
            entity_kinds=(
                "project_catalog",
                "project_contribution",
                "session_timeline_contribution",
                "session",
            ),
            scope_key=scope,
        )
    )
    aggregates = aggregate_read_models(core_inputs, since_days=since_days)
    _replace_read_model_subset(
        context,
        aggregates,
        entity_kinds=("project", "project_detail", "overview", "session_timeline"),
        scope_key=scope,
    )
    core_seconds = perf_counter() - core_started

    analytical_started = perf_counter()
    fact_adapter = CanonicalFactsCtJson(
        context.current_entities(
            entity_kinds=canonical_fact_entity_kinds(),
            scope_key=CANONICAL_FACT_SCOPE,
        )
    )
    analytical_rows = build_model_usage_rows_from_ct_json(
        ct_json=fact_adapter,
        since_days=since_days,
    )
    _replace_analytical_rows(
        context,
        analytical_rows,
        entity_kinds=_default_analytical_entity_kinds(),
    )
    _replace_analytical_rows(
        context,
        (),
        entity_kinds=_token_project_entity_kinds(),
    )
    analytical_seconds = perf_counter() - analytical_started

    for relationship in graph_build.source_relationships:
        path = relationship.source_path
        metadata = {
            "vendor": relationship.vendor.value,
            "session_id": str(relationship.session_id),
            "parent_session_id": (
                str(relationship.parent_session_id)
                if relationship.parent_session_id is not None
                else None
            ),
            "project_identifier": relationship.project_identifier,
        }
        previous = source_by_path.get(path)
        root_link = str(relationship.root_session_id)
        parent_link = metadata["parent_session_id"]
        if previous is not None and (
            previous.root_link == root_link
            and previous.parent_link == parent_link
            and previous.metadata == metadata
        ):
            continue
        context.update_source_metadata(
            path,
            root_link=root_link,
            parent_link=parent_link,
            metadata=metadata,
        )

    for issue in graph_build.issues:
        _record_ingestion_issue(
            context,
            scope=scope,
            stage=issue.stage,
            code=issue.code,
            message=issue.message,
            source_path=issue.source_path,
            root_session_id=issue.session_id,
            details=issue.details,
            disposition="failed" if issue.severity == "error" else "inconclusive",
        )
    context.assert_sources_current()
    for family in _delivery_families():
        if family == "context-window":
            # Context windows are per-graph queries; broadcast per-root scopes
            # so clients refetch only the windows whose facts actually changed.
            if affected_roots:
                context.record_invalidation(family, ",".join(sorted(affected_roots)))
            continue
        context.record_invalidation(family, scope)
    return {
        "graph_rebuild_seconds": round(graph_seconds, 6),
        "core_projection_seconds": round(core_seconds, 6),
        "analytical_projection_seconds": round(analytical_seconds, 6),
        "affected_projection_seconds": round(perf_counter() - started, 6),
    }


def _clear_all_entities(context: MaterializationContext) -> None:
    """Reset disposable projections at the first progressive bootstrap batch."""

    for current in tuple(context.current_entities()):
        context.delete_entity(current.entity_kind, current.entity_key)


def _bootstrap_catalog_mutations(
    project_items: dict[str, Any], *, scope: str
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name, raw_item in sorted(project_items.items()):
        item = raw_item if isinstance(raw_item, dict) else {}
        rows.append(
            {
                "entity_kind": "project_catalog",
                "entity_key": str(name),
                "scope_key": scope,
                "partition_key": str(name),
                "sort_key": str(name).casefold(),
                "tiebreaker": str(name),
                "payload": {
                    "name": str(name),
                    "path": item.get("path"),
                    "vendors": list(item.get("vendors") or []),
                },
            }
        )
    return rows


def _bootstrap_coverage_payload(
    *,
    state: str,
    processed_sources: int,
    total_sources: int,
    processed_components: int,
    total_components: int,
    issue_count: int,
    complete: bool,
) -> dict[str, Any]:
    return {
        "state": state,
        "processed_sources": processed_sources,
        "total_sources": total_sources,
        "processed_components": processed_components,
        "total_components": total_components,
        "inconclusive_records": issue_count,
        "complete": complete,
    }


def _bootstrap_coverage_mutation(
    payload: dict[str, Any], *, scope: str
) -> dict[str, Any]:
    return {
        "entity_kind": "bootstrap_coverage",
        "entity_key": scope,
        "scope_key": scope,
        "partition_key": scope,
        "sort_key": scope,
        "tiebreaker": scope,
        "payload": payload,
    }


def _publish_relationships(
    context: MaterializationContext, relationships: Iterable[Any]
) -> None:
    for relationship in relationships:
        parent = (
            str(relationship.parent_session_id)
            if relationship.parent_session_id is not None
            else None
        )
        context.update_source_metadata(
            relationship.source_path,
            root_link=str(relationship.root_session_id),
            parent_link=parent,
            metadata={
                "vendor": relationship.vendor.value,
                "session_id": str(relationship.session_id),
                "parent_session_id": parent,
                "project_identifier": relationship.project_identifier,
            },
        )


def _publish_build_issues(
    context: MaterializationContext, issues: Iterable[Any], *, scope: str
) -> None:
    for issue in issues:
        context.mutate(
            {
                "entity_kind": "ingestion_issue",
                "entity_key": issue.issue_id,
                "scope_key": scope,
                "partition_key": issue.root_session_id or "",
                "sort_key": issue.stage,
                "tiebreaker": issue.issue_id,
                "payload": issue.model_dump(mode="json"),
            }
        )


def _publish_graph_issues(
    context: MaterializationContext, issues: Iterable[Any], *, scope: str
) -> None:
    for issue in issues:
        _record_ingestion_issue(
            context,
            scope=scope,
            stage=issue.stage,
            code=issue.code,
            message=issue.message,
            source_path=issue.source_path,
            root_session_id=issue.session_id,
            details=issue.details,
            disposition="failed" if issue.severity == "error" else "inconclusive",
        )


def _replace_analytical_rows(
    context: MaterializationContext,
    replacements: Sequence[dict[str, Any]],
    *,
    entity_kinds: Sequence[str],
) -> None:
    desired = {
        (str(row["entity_kind"]), str(row["entity_key"])) for row in replacements
    }
    for current in context.current_entities(entity_kinds=entity_kinds):
        if (current.entity_kind, current.entity_key) not in desired:
            context.delete_entity(current.entity_kind, current.entity_key)
    context.mutate_many(replacements)


def _replace_read_model_subset(
    context: MaterializationContext,
    replacements: Sequence[Any],
    *,
    entity_kinds: Sequence[str],
    scope_key: str,
) -> None:
    desired = {(entity.entity_kind, entity.entity_id) for entity in replacements}
    for current in context.current_entities(
        entity_kinds=entity_kinds, scope_key=scope_key
    ):
        if (current.entity_kind, current.entity_key) not in desired:
            context.delete_entity(current.entity_kind, current.entity_key)
    context.mutate_many(entity.as_mutation() for entity in replacements)


def _replace_analytical_scope_rows(
    context: MaterializationContext,
    replacements: Sequence[dict[str, Any]],
    *,
    entity_kinds: Sequence[str],
    scope_key: str,
) -> None:
    desired = {
        (str(row["entity_kind"]), str(row["entity_key"])) for row in replacements
    }
    for current in context.current_entities(
        entity_kinds=entity_kinds, scope_key=scope_key
    ):
        if (current.entity_kind, current.entity_key) not in desired:
            context.delete_entity(current.entity_kind, current.entity_key)
    context.mutate_many(replacements)


def _delete_graph_contributions(context: MaterializationContext, root: str) -> None:
    for kind in ("session", "project_contribution", "session_timeline_contribution"):
        context.delete_entity(kind, root)


def _delete_root_facts(context: MaterializationContext, root: str) -> None:
    for row in tuple(
        context.current_entities(
            entity_kinds=canonical_fact_entity_kinds(include_projects=False),
            scope_key=CANONICAL_FACT_SCOPE,
            partition_key=root,
        )
    ):
        context.delete_entity(row.entity_kind, row.entity_key)


def _record_ingestion_issue(
    context: MaterializationContext,
    *,
    scope: str,
    stage: str,
    code: str,
    message: str,
    source_path: str | None,
    root_session_id: str | None,
    details: dict[str, Any] | None = None,
    disposition: str = "failed",
) -> None:
    identity = "\0".join(
        (stage, code, source_path or "", root_session_id or "", message)
    )
    issue_id = hashlib.sha256(identity.encode()).hexdigest()
    context.mutate(
        {
            "entity_kind": "ingestion_issue",
            "entity_key": issue_id,
            "scope_key": scope,
            "partition_key": root_session_id or "",
            "sort_key": stage,
            "tiebreaker": issue_id,
            "payload": {
                "issue_id": issue_id,
                "disposition": disposition,
                "stage": stage,
                "code": code,
                "message": message,
                "source_path": source_path,
                "root_session_id": root_session_id,
                "context": details or {},
            },
        }
    )


def _default_analytical_entity_kinds() -> tuple[str, ...]:
    return (
        MODEL_META,
        MODEL_SESSION,
        MODEL_TURN,
        TOKEN_INDEX_META,
        TOKEN_PROJECT,
    )


def _token_project_entity_kinds() -> tuple[str, ...]:
    return (TOKEN_PROJECT_META, TOKEN_PATTERN, TOKEN_HOTSPOT, TOKEN_OUTLIER)


def _delivery_families() -> tuple[str, ...]:
    return (
        "overview",
        "projects",
        "sessions",
        "model-usage",
        "token-efficiency",
        "context-window",
    )


def _candidate_paths(since_days: int) -> tuple[Path, ...]:
    cutoff = (datetime.now(UTC) - timedelta(days=since_days)).timestamp()
    roots = (
        (Path.home() / ".codex" / "sessions", "*.jsonl"),
        (Path.home() / ".claude" / "projects", "*.jsonl"),
        (Path.home() / ".pi" / "agent" / "sessions", "*.jsonl"),
    )
    paths: list[Path] = []
    for root, pattern in roots:
        if not root.is_dir():
            continue
        for path in root.rglob(pattern):
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime >= cutoff:
                paths.append(path.resolve())
    return tuple(sorted(set(paths)))


def _default_database_path() -> Path:
    return Path.home() / ".coding-trajectory" / "dashboard" / "read-models-v3.sqlite3"


def _remove_obsolete_dashboard_databases(*, grace_seconds: int) -> list[str]:
    """Remove retired derived stores only after a long inactivity grace period."""

    cutoff = datetime.now(UTC).timestamp() - grace_seconds
    obsolete = (
        Path.home() / ".coding-trajectory" / "dashboard" / "read-models-v2.sqlite3",
    )
    removed: list[str] = []
    for database in obsolete:
        members = tuple(
            path
            for path in (
                database,
                Path(f"{database}-wal"),
                Path(f"{database}-shm"),
            )
            if path.exists()
        )
        if not members:
            continue
        try:
            latest_mtime = max(path.stat().st_mtime for path in members)
        except OSError:
            continue
        if latest_mtime > cutoff:
            continue
        for path in members:
            try:
                path.unlink()
            except FileNotFoundError:
                continue
        removed.append(str(database))
    return removed


def _delivery_family(entity_kind: str) -> str:
    if entity_kind in {"project", "project_detail", "project_contribution"}:
        return "projects"
    if entity_kind in {"session", "session_timeline", "session_timeline_contribution"}:
        return "sessions"
    return entity_kind.replace("_", "-")


def _page_payload(
    items: list[dict[str, Any]], revision: int, next_cursor: str | None
) -> dict[str, Any]:
    return {
        "items": items,
        "page": {
            "revision": revision,
            "next_cursor": next_cursor,
            "has_more": next_cursor is not None,
        },
    }


def _session_inventory_payload(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    for key in ("cost_usd", "pricing_confidence", "runtime", "usage", "warnings"):
        result.pop(key, None)
    return result


def _lag_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds())


__all__ = ["DashboardIncrementalRuntime", "RuntimeSnapshot"]
