"""Long-lived datahub runtime over revisioned SQLite read models.

This module is the integration boundary between the generic incremental store
and HTTP-facing datahub routes.  It performs metadata reconciliation on a
background worker, bootstraps canonical graph projections in-process, and
serves indexed rows without rebuilding source transcripts on request threads.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote

from coding_trajectory.datahub import (
    ResourceNotFoundError,
)
from pydantic import BaseModel, ConfigDict, Field

from datahub_plugin.projections import context_window
from datahub_plugin.projections.analytical_read_models import (
    CANONICAL_FACT_SCOPE,
    FACT_GRAPH_OVERVIEW,
    FACT_GRAPH_STATS,
    FACT_GRAPH_USAGE,
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
    build_token_efficiency_project_rows,
    canonical_fact_entity_kinds,
    page_metadata,
    reconstruct_model_usage,
    reconstruct_token_efficiency_project,
)
from datahub_plugin.projections.detail_hydration import (
    DetailHydrator,
    DetailUnavailable,
)
from datahub_plugin.projections.read_models import (
    reconstruct_recent_work,
)
from datahub_plugin.projections.session_timeline import build_session_evidence_timeline
from datahub_plugin.runtime.materialize import (
    _delivery_family,
    _lag_seconds,
    _page_payload,
    _replace_analytical_scope_rows,
    _session_inventory_payload,
    _token_project_entity_kinds,
)
from datahub_plugin.store.core import (
    MaterializationContext,
    SourceFenceError,
)

PARSER_VERSION = "core-source-checkpoint-v3"
READ_MODEL_SCHEMA_VERSION = "dashboard-read-model-v6"
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


class RuntimeReadApiMixin:
    """Mechanically extracted runtime behavior."""

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

    def session_evidence_timeline(self, *, session_id: str) -> dict[str, Any] | None:
        """Serve ordered, source-linked session evidence from retained facts."""

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
        overview_payloads: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = self.store.query_entities(
                FACT_SESSION_OVERVIEW,
                limit=500,
                cursor=cursor,
                revision=revision,
                scope_key=CANONICAL_FACT_SCOPE,
                partition_key=root_id,
            )
            overview_payloads.extend(row.payload for row in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        graph_page = self.store.query_entities(
            FACT_GRAPH_OVERVIEW,
            limit=1,
            revision=revision,
            scope_key=CANONICAL_FACT_SCOPE,
            partition_key=root_id,
        )
        graph_overview = graph_page.items[0].payload if graph_page.items else None
        usage_payloads: list[dict[str, Any]] = []
        cursor = None
        while True:
            usage_page = self.store.query_entities(
                FACT_SESSION_USAGE,
                limit=500,
                cursor=cursor,
                revision=revision,
                scope_key=CANONICAL_FACT_SCOPE,
                partition_key=root_id,
            )
            usage_payloads.extend(row.payload for row in usage_page.items)
            cursor = usage_page.next_cursor
            if cursor is None:
                break
        return build_session_evidence_timeline(
            overview_payloads,
            revision=revision,
            root_session_id=root_id,
            entrypoint_session_id=session_id,
            graph_overview=graph_overview,
            usage_payloads=usage_payloads,
        ).model_dump(mode="json")

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
