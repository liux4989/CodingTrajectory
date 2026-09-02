"""Long-lived datahub runtime over revisioned SQLite read models.

This module is the integration boundary between the generic incremental store
and HTTP-facing datahub routes.  It performs metadata reconciliation on a
background worker, bootstraps canonical graph projections in-process, and
serves indexed rows without rebuilding source transcripts on request threads.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from coding_trajectory.datahub import (
    DocumentStore,
    rebuild_affected_session_graphs_with_measurements,
)
from coding_trajectory.analysis.measurements import MeasurementMismatchError

from datahub_plugin.projections.analytical_read_models import (
    CANONICAL_FACT_SCOPE,
    FACT_PROJECT_SESSION,
    FACT_SESSION_STATS,
    FACT_TOOL_USAGE,
    MODEL_META,
    MODEL_SESSION,
    MODEL_TURN,
    CanonicalFactsApiClient,
    build_canonical_root_fact_rows,
    build_model_usage_rows,
    canonical_fact_entity_kinds,
)
from datahub_plugin.runtime.materialize import (
    _candidate_paths,
    _delete_lineage_facts,
    _replace_analytical_scope_rows,
)
from datahub_plugin.store.core import (
    MaterializationContext,
    SourceFenceError,
)


class RuntimeEvidenceMixin:
    """Mechanically extracted runtime behavior."""

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
