"""Materialization and publish helpers for the dashboard incremental runtime."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from datetime import timedelta

from coding_trajectory.ingestion.incremental import (
    rebuild_affected_session_graphs_from_files,
)
from coding_trajectory.ingestion.provenance import SessionProvenance
from coding_trajectory.query import DocumentStore
from datetime import UTC
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any

try:
    from .analytical_read_models import (
        CANONICAL_FACT_SCOPE,
        CanonicalFactsApiClient,
        FACT_PROJECT_SESSION,
        MODEL_META,
        MODEL_SESSION,
        MODEL_TURN,
        TOKEN_HOTSPOT,
        TOKEN_OUTLIER,
        TOKEN_PATTERN,
        TOKEN_PROJECT_META,
        build_canonical_root_fact_rows,
        build_model_usage_rows,
        canonical_fact_entity_kinds,
    )
except ImportError:  # pragma: no cover - direct plugin-directory imports
    from analytical_read_models import (
        CANONICAL_FACT_SCOPE,
        CanonicalFactsApiClient,
        FACT_PROJECT_SESSION,
        MODEL_META,
        MODEL_SESSION,
        MODEL_TURN,
        TOKEN_HOTSPOT,
        TOKEN_OUTLIER,
        TOKEN_PATTERN,
        TOKEN_PROJECT_META,
        build_canonical_root_fact_rows,
        build_model_usage_rows,
        canonical_fact_entity_kinds,
    )
try:
    from .incremental_store import (
        DetailEventRow,
        DetailItemRow,
        DetailSpan,
        MaterializationContext,
    )
except ImportError:  # pragma: no cover - direct plugin-directory imports
    from incremental_store import (
        DetailEventRow,
        DetailItemRow,
        DetailSpan,
        MaterializationContext,
    )
try:
    from .read_models import aggregate_read_models, materialize_graph
except ImportError:  # pragma: no cover - direct plugin-directory imports
    from read_models import aggregate_read_models, materialize_graph


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
        _delete_lineage_facts(context, root)
        context.publish_detail(root, events=(), items=())

    provenance_by_session = {
        str(prov.session_id): prov for prov in graph_build.provenance
    }

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
        detail_events, detail_items = _detail_rows_for_graph(
            graph, provenance_by_session
        )
        context.publish_detail(root, events=detail_events, items=detail_items)
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
    fact_adapter = CanonicalFactsApiClient(
        context.current_entities(
            entity_kinds=canonical_fact_entity_kinds(),
            scope_key=CANONICAL_FACT_SCOPE,
        )
    )
    analytical_rows = build_model_usage_rows(
        client=fact_adapter,
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


def _detail_rows_for_graph(
    graph: Any,
    provenance_by_session: dict[str, SessionProvenance],
) -> tuple[Any, Any]:
    """Stream current-only detail locators for one compact session graph.

    Returns lazy event/item iterables over a lightweight per-session plan so a
    large root never materializes its full pydantic locator object graph
    before insertion.  Session provenance is popped from the shared map as it
    is captured into the plan.
    """

    root = str(graph.root_session_id)
    edge_targets: dict[Any, dict[str, str]] = {}
    for edge in graph.edges:
        if edge.source_item_id is not None:
            edge_targets.setdefault(edge.source_item_id, {})[edge.type] = str(
                edge.target_session_id
            )
    plan: list[tuple[str, SessionProvenance, list[tuple[Any, ...]]]] = []
    for session in graph.sessions:
        prov = provenance_by_session.pop(str(session.session_id), None)
        if prov is None:
            continue
        item_metas = [
            (
                item.item_id,
                item.turn_id,
                item.kind,
                edge_targets.get(item.item_id) or {},
            )
            for turn in session.turns
            for item in turn.items
            if item.item_id in prov.items
        ]
        plan.append((str(session.session_id), prov, item_metas))

    def events() -> Any:
        for session_id, prov, _ in plan:
            for event_id, span in prov.events.items():
                yield DetailEventRow(
                    event_id=str(event_id),
                    root_id=root,
                    session_id=session_id,
                    source_path=prov.source_path,
                    byte_offset=span.byte_offset,
                    byte_end=span.byte_end,
                    digest=span.digest,
                )

    def items() -> Any:
        for session_id, prov, item_metas in plan:
            for item_id, turn_id, kind, targets in item_metas:
                yield DetailItemRow(
                    item_id=str(item_id),
                    root_id=root,
                    session_id=session_id,
                    turn_id=str(turn_id),
                    kind=kind,
                    source_path=prov.source_path,
                    spans=tuple(
                        DetailSpan(
                            byte_offset=span.byte_offset,
                            byte_end=span.byte_end,
                            digest=span.digest,
                        )
                        for span in (
                            prov.events[event_id] for event_id in prov.items[item_id]
                        )
                    ),
                    edge_targets=dict(targets),
                )

    return events(), items()


def _delete_root_facts(context: MaterializationContext, root: str) -> None:
    for row in tuple(
        context.current_entities(
            entity_kinds=canonical_fact_entity_kinds(include_projects=False),
            scope_key=CANONICAL_FACT_SCOPE,
            partition_key=root,
        )
    ):
        context.delete_entity(row.entity_kind, row.entity_key)


def _delete_lineage_facts(context: MaterializationContext, lineage_root: str) -> None:
    """Delete every branch-run fact partition owned by one lineage."""

    run_roots = {lineage_root}
    for row in tuple(
        context.current_entities(
            entity_kinds=(FACT_PROJECT_SESSION,),
            scope_key=CANONICAL_FACT_SCOPE,
        )
    ):
        payload = row.payload
        row_root = str(payload.get("root_session_id") or row.partition_key)
        row_lineage = str(payload.get("lineage_root_session_id") or row_root)
        if row_lineage == lineage_root:
            run_roots.add(row_root)
    for root in run_roots:
        _delete_root_facts(context, root)


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
        "session-tree",
        "session-graph",
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
    return Path.home() / ".coding-trajectory" / "dashboard" / "read-models-v4.sqlite3"


def _remove_obsolete_dashboard_databases(*, grace_seconds: int) -> list[str]:
    """Remove retired derived stores only after a long inactivity grace period."""

    cutoff = datetime.now(UTC).timestamp() - grace_seconds
    obsolete = (
        Path.home() / ".coding-trajectory" / "dashboard" / "read-models-v2.sqlite3",
        Path.home() / ".coding-trajectory" / "dashboard" / "read-models-v3.sqlite3",
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
