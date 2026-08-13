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
from collections.abc import Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from coding_trajectory.discovery import DiscoveryResult, DiscoverySource
from coding_trajectory.query import DocumentStore
from coding_trajectory.service import project_list_metadata

try:
    from .incremental_store import (
        IncrementalStore,
        MaterializationContext,
    )
    from .read_models import (
        DEFAULT_RECENT_HORIZON_DAYS,
        aggregate_read_models,
        build_read_models_from_discovery,
        materialize_graph,
    )
    from .analytical_read_models import (
        CANONICAL_FACT_SCOPE,
        CACHE_ITEM,
        CACHE_META,
        CanonicalFactsCtJson,
        ERROR_ITEM,
        ERROR_META,
        FACT_PROJECT,
        FACT_PROJECT_SESSION,
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
        build_cache_break_rows_from_ct_json,
        build_error_collection_rows_from_ct_json,
        build_model_usage_rows_from_ct_json,
        build_standard_analytical_rows_from_ct_json,
        build_token_efficiency_index_rows_from_ct_json,
        build_token_efficiency_project_rows_from_ct_json,
        canonical_fact_entity_kinds,
        page_metadata,
        reconstruct_cache_breaks,
        reconstruct_error_collection,
        reconstruct_model_usage,
        reconstruct_token_efficiency_index,
        reconstruct_token_efficiency_project,
    )
    from . import context_window
    from .incremental_graphs import rebuild_affected_session_graphs
except ImportError:
    from incremental_store import (
        IncrementalStore,
        MaterializationContext,
    )
    from read_models import (
        DEFAULT_RECENT_HORIZON_DAYS,
        aggregate_read_models,
        build_read_models_from_discovery,
        materialize_graph,
    )
    from analytical_read_models import (
        CANONICAL_FACT_SCOPE,
        CACHE_ITEM,
        CACHE_META,
        CanonicalFactsCtJson,
        ERROR_ITEM,
        ERROR_META,
        FACT_PROJECT,
        FACT_PROJECT_SESSION,
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
        build_cache_break_rows_from_ct_json,
        build_error_collection_rows_from_ct_json,
        build_model_usage_rows_from_ct_json,
        build_standard_analytical_rows_from_ct_json,
        build_token_efficiency_index_rows_from_ct_json,
        build_token_efficiency_project_rows_from_ct_json,
        canonical_fact_entity_kinds,
        page_metadata,
        reconstruct_cache_breaks,
        reconstruct_error_collection,
        reconstruct_model_usage,
        reconstruct_token_efficiency_index,
        reconstruct_token_efficiency_project,
    )
    import context_window
    from incremental_graphs import rebuild_affected_session_graphs


PARSER_VERSION = "dashboard-jsonl-v1"
READ_MODEL_SCHEMA_VERSION = "dashboard-read-model-v2"
DEFAULT_REFRESH_SECONDS = 15.0


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
        self.store = IncrementalStore(
            database_path or _default_database_path(),
            parser_version=PARSER_VERSION,
            schema_version=READ_MODEL_SCHEMA_VERSION,
        )
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="dashboard-ingest"
        )
        self._future: Future[dict[str, Any]] | None = None
        self._lock = threading.Lock()
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
        """Return whether every default revisioned route family is publishable."""

        return (
            self._has_route_models()
            and self._has_canonical_facts()
            and self._has_default_analytical_models()
        )

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
                invalidations.add(family)
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
        if not self.is_ready():
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

    def error_collection(
        self,
        *,
        since_days: int,
        project_name: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any] | None:
        """Serve the default-scope Error Collection page from SQLite."""

        if since_days != self.since_days:
            return None
        if not self.is_ready():
            return None
        scope = analytical_scope_key(
            "error_collection",
            since_days=self.since_days,
            project_name=project_name,
        )
        if self._analytical_meta(ERROR_META, scope) is None:
            self._materialize_filtered_analytical_scope(
                route="error_collection",
                scope=scope,
                project_name=project_name,
            )
        page = self.store.query_entities(
            ERROR_ITEM,
            limit=limit,
            cursor=cursor,
            scope_key=scope,
            partition_key="errors",
        )
        meta = self._analytical_meta(ERROR_META, scope, revision=page.revision)
        if meta is None:
            return None
        return reconstruct_error_collection(
            meta,
            rows=page.items,
            page=page,
            limit=limit,
        )

    def cache_breaks(
        self,
        *,
        since_days: int,
        project_name: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any] | None:
        """Serve the default-scope Cache Breaks page from SQLite."""

        if since_days != self.since_days:
            return None
        if not self.is_ready():
            return None
        scope = analytical_scope_key(
            "cache_breaks",
            since_days=self.since_days,
            project_name=project_name,
        )
        if self._analytical_meta(CACHE_META, scope) is None:
            self._materialize_filtered_analytical_scope(
                route="cache_breaks",
                scope=scope,
                project_name=project_name,
            )
        page = self.store.query_entities(
            CACHE_ITEM,
            limit=limit,
            cursor=cursor,
            scope_key=scope,
            partition_key="breaks",
        )
        meta = self._analytical_meta(CACHE_META, scope, revision=page.revision)
        if meta is None:
            return None
        return reconstruct_cache_breaks(
            meta,
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
        if not self.is_ready():
            return None
        scope = analytical_scope_key(
            "token_efficiency_index", since_days=self.since_days
        )
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
        if not self.is_ready():
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
                rows = build_token_efficiency_project_rows_from_ct_json(
                    ct_json=adapter,
                    project_name=project_name,
                    since_days=since_days,
                )
                _replace_analytical_scope_rows(
                    context,
                    rows,
                    entity_kinds=_token_project_entity_kinds(),
                    scope_key=scope,
                )
                context.assert_sources_current()
                context.record_invalidation("token-efficiency", scope)

            self.store.materialize_revision(publish)

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
        revision = self.store.current_revision()
        facts = self._canonical_fact_rows(revision=revision)
        if not facts:
            return None
        projection = context_window.build_projection(
            session_id,
            turn_id=turn_id,
            ct_json=CanonicalFactsCtJson(facts),
        )
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

    def _canonical_fact_rows(self, *, revision: int) -> list[Any]:
        rows: list[Any] = []
        for kind in canonical_fact_entity_kinds():
            cursor: str | None = None
            while True:
                page = self.store.query_entities(
                    kind,
                    limit=500,
                    cursor=cursor,
                    revision=revision,
                    scope_key=CANONICAL_FACT_SCOPE,
                )
                rows.extend(page.items)
                cursor = page.next_cursor
                if cursor is None:
                    break
        return rows

    def _materialize_filtered_analytical_scope(
        self,
        *,
        route: Literal["model_usage", "error_collection", "cache_breaks"],
        scope: str,
        project_name: str | None,
        model_key: str | None = None,
    ) -> None:
        def publish(context: MaterializationContext) -> None:
            meta_kind = {
                "model_usage": MODEL_META,
                "error_collection": ERROR_META,
                "cache_breaks": CACHE_META,
            }[route]
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
            elif route == "error_collection":
                rows = build_error_collection_rows_from_ct_json(
                    ct_json=adapter,
                    since_days=self.since_days,
                    project_name=project_name,
                )
                kinds = (ERROR_META, ERROR_ITEM)
                family = "error-collection"
            else:
                rows = build_cache_break_rows_from_ct_json(
                    ct_json=adapter,
                    since_days=self.since_days,
                    project_name=project_name,
                )
                kinds = (CACHE_META, CACHE_ITEM)
                family = "cache-breaks"
            _replace_analytical_scope_rows(
                context,
                rows,
                entity_kinds=kinds,
                scope_key=scope,
            )
            context.assert_sources_current()
            context.record_invalidation(family, scope)

        self.store.materialize_revision(publish)

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
        # Bounded source registry ingestion: never retain the whole corpus's
        # parsed lines in pending SQLite plans.
        ingestion_started = perf_counter()
        parsed_bytes = 0
        parsed_lines = 0
        for path in candidates:
            result = self.store.refresh_paths([path])
            parsed_bytes += result.parsed_bytes
            parsed_lines += result.parsed_lines
        self.store.refresh(candidates)
        ingestion_seconds = perf_counter() - ingestion_started

        project_catalog = project_list_metadata(
            {}, global_scope=True, current_dir=self.current_dir
        )
        computed: dict[str, Any] = {}
        timings: dict[str, float] = {}

        def publish(context: MaterializationContext) -> None:
            graph_started = perf_counter()
            graph_build = rebuild_affected_session_graphs(
                sources=context.current_sources(include_deleted=True),
                messages_for_path=lambda path: context.active_messages(
                    source_path=path
                ),
            )
            timings["canonical_graph_rebuild_seconds"] = round(
                perf_counter() - graph_started, 6
            )
            canonical_store = DocumentStore.from_session_graphs(
                list(graph_build.graphs)
            )
            discovery = DiscoveryResult(
                store=canonical_store,
                sources=[
                    DiscoverySource(
                        vendor=relationship.vendor,
                        path=Path(relationship.source_path),
                        root_session_id=relationship.root_session_id,
                    )
                    for relationship in graph_build.source_relationships
                ],
            )

            core_started = perf_counter()
            build = build_read_models_from_discovery(
                discovery,
                current_dir=self.current_dir,
                since_days=self.since_days,
                project_catalog=project_catalog,
            )
            timings["core_projection_seconds"] = round(perf_counter() - core_started, 6)

            facts_started = perf_counter()
            project_list = _project_list_payload(build.entities)
            fact_rows = build_canonical_fact_rows(
                store=canonical_store,
                current_dir=self.current_dir,
                project_list=project_list,
            )
            timings["canonical_facts_seconds"] = round(
                perf_counter() - facts_started, 6
            )

            analytical_started = perf_counter()
            fact_adapter = CanonicalFactsCtJson(fact_rows)
            analytical_rows = [
                *build_standard_analytical_rows_from_ct_json(
                    ct_json=fact_adapter,
                    since_days=self.since_days,
                ),
                *build_token_efficiency_index_rows_from_ct_json(
                    ct_json=fact_adapter,
                    since_days=self.since_days,
                ),
            ]
            timings["analytical_projection_seconds"] = round(
                perf_counter() - analytical_started, 6
            )

            _replace_all_read_models(context, build.entities)
            _replace_analytical_rows(
                context,
                fact_rows,
                entity_kinds=canonical_fact_entity_kinds(),
            )
            _replace_analytical_rows(
                context,
                analytical_rows,
                entity_kinds=_default_analytical_entity_kinds(),
            )
            for relationship in graph_build.source_relationships:
                context.update_source_metadata(
                    relationship.source_path,
                    root_link=str(relationship.root_session_id),
                    parent_link=(
                        str(relationship.parent_session_id)
                        if relationship.parent_session_id is not None
                        else None
                    ),
                    metadata={
                        "vendor": relationship.vendor.value,
                        "session_id": str(relationship.session_id),
                        "parent_session_id": (
                            str(relationship.parent_session_id)
                            if relationship.parent_session_id is not None
                            else None
                        ),
                        "project_identifier": relationship.project_identifier,
                    },
                )
            for issue in build.issues:
                context.mutate(
                    {
                        "entity_kind": "ingestion_issue",
                        "entity_key": issue.issue_id,
                        "scope_key": build.scope_id,
                        "partition_key": issue.root_session_id or "",
                        "sort_key": issue.stage,
                        "tiebreaker": issue.issue_id,
                        "payload": issue.model_dump(mode="json"),
                    }
                )
            for issue in graph_build.issues:
                _record_ingestion_issue(
                    context,
                    scope=build.scope_id,
                    stage=issue.stage,
                    code=issue.code,
                    message=issue.message,
                    source_path=issue.source_path,
                    root_session_id=issue.session_id,
                    details=issue.details,
                    disposition=(
                        "failed" if issue.severity == "error" else "inconclusive"
                    ),
                )
            for family in _delivery_families():
                context.record_invalidation(family, build.scope_id)
            context.assert_sources_current()
            computed.update(
                {
                    "build": build,
                    "graph_build": graph_build,
                    "fact_rows": fact_rows,
                    "analytical_rows": analytical_rows,
                }
            )

        publish_started = perf_counter()
        published = self.store.materialize_revision(publish)
        publish_total = perf_counter() - publish_started
        build = computed["build"]
        graph_build = computed["graph_build"]
        fact_rows = computed["fact_rows"]
        analytical_rows = computed["analytical_rows"]
        status = (
            graph_build.status if graph_build.status != "complete" else build.status
        )
        measured_projection = sum(timings.values())
        timings["sqlite_publish_overhead_seconds"] = round(
            max(0.0, publish_total - measured_projection), 6
        )
        return {
            "status": status,
            "revision": published.revision,
            "source_count": len(candidates),
            "entity_count": len(build.entities),
            "fact_entity_count": len(fact_rows),
            "analytical_entity_count": len(analytical_rows),
            "issue_count": len(build.issues) + len(graph_build.issues),
            "parsed_bytes": parsed_bytes,
            "parsed_lines": parsed_lines,
            "timings": {
                "source_ingestion_seconds": round(ingestion_seconds, 6),
                **timings,
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
        return {
            "status": "unchanged" if not result.changed_sources else "updated",
            "revision": result.revision,
            "changed_sources": len(result.changed_sources),
            "parsed_bytes": result.parsed_bytes,
            "parsed_lines": result.parsed_lines,
            "timings": {
                **materialization_timings,
                "total_seconds": round(perf_counter() - refresh_started, 6),
            },
        }

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
        scopes = (
            (
                MODEL_META,
                analytical_scope_key("model_usage", since_days=self.since_days),
            ),
            (
                ERROR_META,
                analytical_scope_key("error_collection", since_days=self.since_days),
            ),
            (
                CACHE_META,
                analytical_scope_key("cache_breaks", since_days=self.since_days),
            ),
            (
                TOKEN_INDEX_META,
                analytical_scope_key(
                    "token_efficiency_index", since_days=self.since_days
                ),
            ),
        )
        return all(
            self._analytical_meta(kind, scope) is not None for kind, scope in scopes
        )

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


def _materialize_changed_graphs(
    context: MaterializationContext,
    *,
    current_dir: Path,
    since_days: int,
) -> dict[str, float]:
    """Repair changed graph partitions without reopening transcript files."""

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
    graph_build = rebuild_affected_session_graphs(
        sources=sources,
        messages_for_path=lambda path: context.active_messages(source_path=path),
        seed_paths=seed_paths,
        old_root_session_ids=old_roots,
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
    analytical_rows = [
        *build_standard_analytical_rows_from_ct_json(
            ct_json=fact_adapter,
            since_days=since_days,
        ),
        *build_token_efficiency_index_rows_from_ct_json(
            ct_json=fact_adapter,
            since_days=since_days,
        ),
    ]
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
        context.record_invalidation(family, scope)
    return {
        "graph_rebuild_seconds": round(graph_seconds, 6),
        "core_projection_seconds": round(core_seconds, 6),
        "analytical_projection_seconds": round(analytical_seconds, 6),
        "affected_projection_seconds": round(perf_counter() - started, 6),
    }


def _replace_all_read_models(
    context: MaterializationContext, entities: Iterable[Any]
) -> None:
    replacement = list(entities)
    desired = {(entity.entity_kind, entity.entity_id) for entity in replacement}
    kinds = _core_read_model_entity_kinds()
    for current in context.current_entities(entity_kinds=kinds):
        if (current.entity_kind, current.entity_key) not in desired:
            context.delete_entity(current.entity_kind, current.entity_key)
    context.mutate_many(entity.as_mutation() for entity in replacement)


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


def _core_read_model_entity_kinds() -> tuple[str, ...]:
    return (
        "project_catalog",
        "project_contribution",
        "session_timeline_contribution",
        "project",
        "session",
        "overview",
        "session_timeline",
        "project_detail",
    )


def _default_analytical_entity_kinds() -> tuple[str, ...]:
    return (
        MODEL_META,
        MODEL_SESSION,
        MODEL_TURN,
        ERROR_META,
        ERROR_ITEM,
        CACHE_META,
        CACHE_ITEM,
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
        "error-collection",
        "cache-breaks",
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


def _project_list_payload(entities: Sequence[Any]) -> dict[str, Any]:
    return {
        "items": {
            str(entity.payload["name"]): {
                "path": entity.payload.get("path"),
                "vendors": entity.payload.get("vendors") or [],
            }
            for entity in entities
            if entity.entity_kind == "project"
        }
    }


def _default_database_path() -> Path:
    return Path.home() / ".coding-trajectory" / "dashboard" / "read-models-v2.sqlite3"


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
