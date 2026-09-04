"""Long-lived datahub runtime over revisioned SQLite read models.

This module is the integration boundary between the generic incremental store
and HTTP-facing datahub routes.  It performs metadata reconciliation on a
background worker, bootstraps canonical graph projections in-process, and
serves indexed rows without rebuilding source transcripts on request threads.
"""

from __future__ import annotations

import queue
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from coding_trajectory.datahub import (
    DocumentStore,
    plan_session_graph_components_from_files,
    project_list_metadata,
    rebuild_affected_session_graphs_from_files,
)

from datahub_plugin.projections.analytical_read_models_facts import (
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
    build_canonical_root_fact_rows,
    build_model_usage_rows,
    canonical_fact_entity_kinds,
)
from datahub_plugin.projections.analytical_read_models_clients import (
    CanonicalFactsApiClient,
    build_canonical_fact_rows,
)
from datahub_plugin.projections.analytical_read_models_reconstruction import (
    analytical_scope_key,
)
from datahub_plugin.projections.read_models_reconstruction import (
    aggregate_read_models,
)
from datahub_plugin.projections.read_models_materialization import (
    materialize_graph,
)
from datahub_plugin.runtime.materialize import (
    _bootstrap_catalog_mutations,
    _bootstrap_coverage_mutation,
    _bootstrap_coverage_payload,
    _candidate_paths,
    _clear_all_entities,
    _default_analytical_entity_kinds,
    _delivery_families,
    _detail_rows_for_graph,
    _materialize_changed_graphs,
    _publish_build_issues,
    _publish_graph_issues,
    _publish_relationships,
    _remove_obsolete_dashboard_databases,
    _replace_analytical_rows,
    _replace_read_model_subset,
)
from datahub_plugin.store.core import (
    DetailEventRow,
    DetailItemRow,
    MaterializationContext,
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


class RuntimeMonitorMixin:
    """Mechanically extracted runtime behavior."""

    def shutdown(self) -> None:
        self._stop.set()
        with self._subscriber_lock:
            subscribers = tuple(self._subscribers)
            self._subscribers.clear()
            for subscriber in subscribers:
                self._coalesce_revision(subscriber, -1)
        monitor = self._monitor
        if monitor is not None:
            monitor.join(timeout=1.0)
        self._executor.shutdown(wait=True, cancel_futures=True)

    def subscribe_revisions(self) -> tuple[queue.Queue[int], int]:
        """Atomically register a bounded revision wakeup subscription."""
        subscriber: queue.Queue[int] = queue.Queue(maxsize=1)
        with self._subscriber_lock:
            self._subscribers.add(subscriber)
            revision = self.store.current_revision()
            self._coalesce_revision(subscriber, revision)
        return subscriber, revision

    def unsubscribe_revisions(self, subscriber: queue.Queue[int]) -> None:
        with self._subscriber_lock:
            self._subscribers.discard(subscriber)

    def _notify_revision(self, revision: int) -> None:
        """Wake live clients without ever allowing them to block publication."""
        with self._subscriber_lock:
            for subscriber in self._subscribers:
                self._coalesce_revision(subscriber, revision)

    @staticmethod
    def _coalesce_revision(subscriber: queue.Queue[int], revision: int) -> None:
        try:
            subscriber.put_nowait(revision)
            return
        except queue.Full:
            pass
        try:
            subscriber.get_nowait()
        except queue.Empty:
            pass
        try:
            subscriber.put_nowait(revision)
        except queue.Full:
            pass

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
