"""Canonical graph repair from checkpointed source-message deltas.

This is the storage-neutral core boundary used by incremental consumers.  It
deliberately never owns SQLite policy: callers supply checkpoint and message
records, while core owns vendor parsing, inherited-turn cutting, topology
selection, stabilization, and canonical graph assembly.

Only lightweight headers are read for the complete source registry.  Full
record streams are requested solely for the connected components selected by
``seed_paths`` and ``old_root_session_ids``.  This is important for steady-state
append refreshes: repairing one graph must not deserialize the whole corpus.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, TypeAlias
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from coding_trajectory.discovery import (
    discover_store_from_files,
    infer_project_identifier,
    stabilize_session,
)
from coding_trajectory.ingestion.adapters.base import BaseAdapter
from coding_trajectory.ingestion.adapters.claude_code import (
    ClaudeCodeAdapter,
    _subagent_input,
)
from coding_trajectory.ingestion.adapters.codex import (
    CodexAdapter,
    _cut_inherited_records,
)
from coding_trajectory.ingestion.adapters.pi import PiAdapter
from coding_trajectory.ingestion.graph import assemble_project_session_graphs
from coding_trajectory.ingestion.models import Session, SessionGraph, Vendor
from coding_trajectory.ingestion.retention import CanonicalRetention
from coding_trajectory.ingestion.vendor_mechanisms.claude_subagent import (
    canonical_session_ids,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceStatus(StrEnum):
    """Storage-neutral state of a source checkpoint."""

    READY = "ready"
    PARTIAL = "partial"
    ERROR = "error"
    DELETED = "deleted"


class SourceSnapshot(_StrictModel):
    """Core input contract for a persisted source checkpoint."""

    path: str
    file_identity: str | None
    size: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)
    committed_offset: int = Field(ge=0)
    committed_ctime_ns: int = Field(default=0, ge=0)
    prefix_checksum: str | None
    tail_checksum: str | None
    parser_version: str
    schema_version: str
    status: SourceStatus
    error: str | None
    last_success_revision: int | None
    revision: int
    deleted: bool
    root_link: str | None
    parent_link: str | None
    metadata: dict[str, Any]


class SourceMessage(_StrictModel):
    """Core input contract for one complete canonical JSONL record."""

    source_message_id: str
    source_path: str
    byte_offset: int = Field(ge=0)
    byte_end: int = Field(gt=0)
    digest: str
    explicit_event_id: str | None
    event_type: str | None
    event_timestamp: str | None
    root_link: str | None
    parent_link: str | None
    payload: dict[str, Any]
    payload_complete: bool = True


class GraphBuildIssue(_StrictModel):
    """Observable failure or uncertainty encountered during graph repair."""

    severity: Literal["warning", "error"]
    stage: Literal["selection", "header", "messages", "adapter", "assembly"]
    code: str = Field(min_length=1, max_length=256)
    message: str = Field(min_length=1, max_length=4096)
    source_path: str | None = None
    session_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class SourceGraphRelationship(_StrictModel):
    """Canonical source-to-session/root relationship for checkpoint metadata."""

    source_path: str
    vendor: Vendor
    session_id: UUID
    parent_session_id: UUID | None = None
    root_session_id: UUID
    project_identifier: str


class IncrementalGraphBuild(_StrictModel):
    """Affected canonical graphs plus relationship and failure evidence."""

    status: Literal["complete", "inconclusive", "failed"]
    graphs: tuple[SessionGraph, ...] = ()
    source_relationships: tuple[SourceGraphRelationship, ...] = ()
    issues: tuple[GraphBuildIssue, ...] = ()
    selected_source_paths: tuple[str, ...] = ()
    old_root_session_ids: tuple[UUID, ...] = ()
    affected_root_session_ids: tuple[UUID, ...] = ()
    removed_root_session_ids: tuple[UUID, ...] = ()


class SourceGraphComponent(_StrictModel):
    """One independently rebuildable parent/child source component."""

    component_id: UUID
    source_paths: tuple[str, ...]
    session_ids: tuple[UUID, ...]
    total_bytes: int = Field(ge=0)
    latest_mtime_ns: int = Field(ge=0)


class SourceGraphComponentPlan(_StrictModel):
    """Bounded topology plan used for component-streamed cold ingestion."""

    status: Literal["complete", "inconclusive", "failed"]
    total_sources: int = Field(ge=0)
    components: tuple[SourceGraphComponent, ...] = ()
    issues: tuple[GraphBuildIssue, ...] = ()


SourceInput: TypeAlias = SourceSnapshot | Mapping[str, Any]
MessageInput: TypeAlias = SourceMessage | Mapping[str, Any]
MessagesForPath: TypeAlias = Callable[[str], Iterable[MessageInput]]


class _SourceHeader(_StrictModel):
    path: str
    vendor: Vendor
    session_id: UUID
    parent_session_id: UUID | None = None


class _BuiltSource(_StrictModel):
    path: str
    vendor: Vendor
    project_identifier: str
    session: Session


def plan_session_graph_components_from_files(
    *, sources: Iterable[SourceInput]
) -> SourceGraphComponentPlan:
    """Scan only source headers and partition the corpus into connected graphs.

    Components from the same modification-day bucket are ordered smallest-first,
    then newest-first. This makes a recent useful dashboard revision available
    without allowing one large active graph to monopolize cold readiness.
    """

    snapshots, validation_issues = _validated_snapshots(sources)
    issues = list(validation_issues)
    active = {snapshot.path: snapshot for snapshot in snapshots if not snapshot.deleted}
    headers: dict[str, _SourceHeader] = {}
    paths_by_session: dict[UUID, list[str]] = defaultdict(list)
    for path, snapshot in sorted(active.items()):
        try:
            header = _file_source_header(snapshot)
        except Exception as exc:
            issues.append(
                _issue(
                    "error",
                    "header",
                    "incremental_graph.header_failed",
                    f"{type(exc).__name__}: {exc}",
                    source_path=path,
                )
            )
            continue
        if header is None:
            issues.append(
                _issue(
                    "error",
                    "header",
                    "incremental_graph.header_missing",
                    "no canonical session header could be scanned",
                    source_path=path,
                )
            )
            continue
        headers[path] = header
        paths_by_session[header.session_id].append(path)

    canonical_path_by_session: dict[UUID, str] = {}
    for session_id, paths in paths_by_session.items():
        canonical_path_by_session[session_id] = min(paths)
        if len(paths) > 1:
            issues.append(
                _issue(
                    "error",
                    "header",
                    "incremental_graph.duplicate_session_id",
                    "multiple active sources identify the same canonical session",
                    session_id=str(session_id),
                    details={"source_paths": sorted(paths)},
                )
            )

    neighbours: dict[UUID, set[UUID]] = defaultdict(set)
    for header in headers.values():
        parent_id = header.parent_session_id
        if parent_id is None or parent_id not in canonical_path_by_session:
            continue
        neighbours[header.session_id].add(parent_id)
        neighbours[parent_id].add(header.session_id)

    components: list[SourceGraphComponent] = []
    visited: set[UUID] = set()
    for session_id in sorted(canonical_path_by_session, key=str):
        if session_id in visited:
            continue
        queue = deque([session_id])
        component_sessions: set[UUID] = set()
        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            component_sessions.add(current)
            queue.extend(sorted(neighbours.get(current, ()), key=str))
        component_paths = tuple(
            sorted(canonical_path_by_session[value] for value in component_sessions)
        )
        roots = sorted(
            (
                value
                for value in component_sessions
                if headers[canonical_path_by_session[value]].parent_session_id
                not in component_sessions
            ),
            key=str,
        )
        component_id = roots[0] if roots else min(component_sessions, key=str)
        components.append(
            SourceGraphComponent(
                component_id=component_id,
                source_paths=component_paths,
                session_ids=tuple(sorted(component_sessions, key=str)),
                total_bytes=sum(active[path].size for path in component_paths),
                latest_mtime_ns=max(
                    (active[path].mtime_ns for path in component_paths), default=0
                ),
            )
        )

    day_ns = 86_400 * 1_000_000_000
    components.sort(
        key=lambda item: (
            -(item.latest_mtime_ns // day_ns),
            item.total_bytes,
            -item.latest_mtime_ns,
            str(item.component_id),
        )
    )
    if any(issue.severity == "error" for issue in issues) and not components:
        status: Literal["complete", "inconclusive", "failed"] = "failed"
    elif issues:
        status = "inconclusive"
    else:
        status = "complete"
    return SourceGraphComponentPlan(
        status=status,
        total_sources=len(active),
        components=tuple(components),
        issues=tuple(issues),
    )


def rebuild_affected_session_graphs(
    *,
    sources: Iterable[SourceInput],
    messages_for_path: MessagesForPath,
    seed_paths: Iterable[str | Path] = (),
    old_root_session_ids: Iterable[str | UUID] = (),
) -> IncrementalGraphBuild:
    """Rebuild only graph components affected by the supplied seeds.

    ``sources`` must be the source registry as visible inside the publishing
    transaction, and ``messages_for_path`` must return active messages from that
    same transaction ordered by (or orderable by) ``byte_offset``.  The callback
    must be repeatable because header selection and full projection are separate
    bounded passes.

    Passing neither seeds nor old roots selects every active source, which is
    useful for an initial canonical bootstrap.  Deleted/truncated/replaced files
    are handled by the caller including both their path and previous root in the
    seeds; active messages then naturally describe only the replacement state.
    """

    snapshots, validation_issues = _validated_snapshots(sources)
    issues = list(validation_issues)
    active = {snapshot.path: snapshot for snapshot in snapshots if not snapshot.deleted}
    by_path = {snapshot.path: snapshot for snapshot in snapshots}

    resolved_seeds = tuple(
        sorted({str(Path(raw_path).expanduser().resolve()) for raw_path in seed_paths})
    )
    old_roots, root_issues = _validated_roots(old_root_session_ids)
    issues.extend(root_issues)
    # A deleted source retains its last relationship in the registry.  Treat it
    # as an implicit old-root seed so deletion repair remains correct even when
    # the caller has only the changed path at hand.
    for path in resolved_seeds:
        snapshot = by_path.get(path)
        if snapshot is None or not snapshot.deleted or snapshot.root_link is None:
            continue
        old_root = _uuid_or_none(snapshot.root_link)
        if old_root is not None:
            old_roots.add(old_root)

    headers: dict[str, _SourceHeader] = {}
    for path, snapshot in sorted(active.items()):
        try:
            header = _source_header(snapshot, messages_for_path)
        except Exception as exc:  # retain bad source evidence; do not abort peers
            issues.append(
                _issue(
                    "error",
                    "header",
                    "incremental_graph.header_failed",
                    f"{type(exc).__name__}: {exc}",
                    source_path=path,
                )
            )
            continue
        if header is None:
            issues.append(
                _issue(
                    "error",
                    "header",
                    "incremental_graph.header_missing",
                    "no canonical session header could be reconstructed",
                    source_path=path,
                )
            )
            continue
        headers[path] = header

    paths_by_session: dict[UUID, list[str]] = defaultdict(list)
    for path, header in headers.items():
        paths_by_session[header.session_id].append(path)
    for session_id, paths in sorted(
        paths_by_session.items(), key=lambda item: str(item[0])
    ):
        if len(paths) <= 1:
            continue
        issues.append(
            _issue(
                "error",
                "header",
                "incremental_graph.duplicate_session_id",
                "multiple active sources identify the same canonical session",
                session_id=str(session_id),
                details={"source_paths": sorted(paths)},
            )
        )

    selected = _select_component_paths(
        active=active,
        by_path=by_path,
        headers=headers,
        seed_paths=resolved_seeds,
        old_roots=old_roots,
        issues=issues,
    )

    built_sources: list[_BuiltSource] = []
    selected_records: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(selected):
        snapshot = active.get(path)
        header = headers.get(path)
        if snapshot is None or header is None:
            continue
        if snapshot.status.value != "ready":
            issues.append(
                _issue(
                    "warning",
                    "messages",
                    "incremental_graph.source_not_ready",
                    f"source checkpoint is {snapshot.status.value}; using its last complete messages",
                    source_path=path,
                    session_id=str(header.session_id),
                    details={"source_status": snapshot.status.value},
                )
            )
        try:
            records = _complete_records(path, messages_for_path(path))
        except Exception as exc:
            issues.append(
                _issue(
                    "error",
                    "messages",
                    "incremental_graph.messages_unavailable",
                    f"{type(exc).__name__}: {exc}",
                    source_path=path,
                    session_id=str(header.session_id),
                )
            )
            continue
        selected_records[path] = records

    started_turn_ids = _codex_parent_turn_ids(headers, selected_records)
    for path in sorted(selected_records):
        snapshot = active[path]
        header = headers[path]
        records = selected_records[path]
        try:
            parent_turns = (
                started_turn_ids.get(header.parent_session_id)
                if header.vendor == Vendor.CODEX_CLI
                and header.parent_session_id is not None
                else None
            )
            session = _build_session_from_records(
                snapshot=snapshot,
                vendor=header.vendor,
                records=records,
                parent_started_turn_ids=parent_turns,
            )
            project = infer_project_identifier(
                session, Path(path), fallback=Path(path).stem
            )
            if not project:
                raise ValueError("canonical project identifier is empty")
        except Exception as exc:
            issues.append(
                _issue(
                    "error",
                    "adapter",
                    "incremental_graph.adapter_failed",
                    f"{type(exc).__name__}: {exc}",
                    source_path=path,
                    session_id=str(header.session_id),
                )
            )
            continue
        built_sources.append(
            _BuiltSource(
                path=path,
                vendor=header.vendor,
                project_identifier=project,
                session=session,
            )
        )

    graphs: list[SessionGraph] = []
    sessions_by_project: dict[str, list[Session]] = defaultdict(list)
    for built in built_sources:
        sessions_by_project[built.project_identifier].append(built.session)
    for project_identifier, sessions in sorted(sessions_by_project.items()):
        try:
            graphs.extend(assemble_project_session_graphs(project_identifier, sessions))
        except Exception as exc:
            issues.append(
                _issue(
                    "error",
                    "assembly",
                    "incremental_graph.assembly_failed",
                    f"{type(exc).__name__}: {exc}",
                    details={"project_identifier": project_identifier},
                )
            )

    session_to_graph: dict[UUID, SessionGraph] = {
        session.session_id: graph for graph in graphs for session in graph.sessions
    }
    relationships: list[SourceGraphRelationship] = []
    for built in built_sources:
        graph = session_to_graph.get(built.session.session_id)
        if graph is None:
            issues.append(
                _issue(
                    "error",
                    "assembly",
                    "incremental_graph.session_unassigned",
                    "canonical session was not assigned to an assembled graph",
                    source_path=built.path,
                    session_id=str(built.session.session_id),
                )
            )
            continue
        relationships.append(
            SourceGraphRelationship(
                source_path=built.path,
                vendor=built.vendor,
                session_id=built.session.session_id,
                parent_session_id=built.session.parent_session_id,
                root_session_id=graph.root_session_id,
                project_identifier=graph.project_identifier or built.project_identifier,
            )
        )

    relationships.sort(key=lambda item: (str(item.root_session_id), item.source_path))
    graphs.sort(
        key=lambda graph: (
            graph.project_identifier or "",
            graph.summary.started_at if graph.summary else None,
            str(graph.root_session_id),
        )
    )
    new_roots = {graph.root_session_id for graph in graphs}
    affected_roots = tuple(sorted({*old_roots, *new_roots}, key=str))
    removed_roots = tuple(sorted(set(old_roots) - new_roots, key=str))
    error_count = sum(issue.severity == "error" for issue in issues)
    if error_count and not graphs:
        status: Literal["complete", "inconclusive", "failed"] = "failed"
    elif issues:
        status = "inconclusive"
    else:
        status = "complete"
    return IncrementalGraphBuild(
        status=status,
        graphs=tuple(graphs),
        source_relationships=tuple(relationships),
        issues=tuple(issues),
        selected_source_paths=tuple(sorted(selected)),
        old_root_session_ids=tuple(sorted(old_roots, key=str)),
        affected_root_session_ids=affected_roots,
        removed_root_session_ids=removed_roots,
    )


def rebuild_affected_session_graphs_from_files(
    *,
    sources: Iterable[SourceInput],
    seed_paths: Iterable[str | Path] = (),
    old_root_session_ids: Iterable[str | UUID] = (),
    retention: CanonicalRetention = "trajectory",
) -> IncrementalGraphBuild:
    """Rebuild affected components directly from immutable JSONL sources.

    Only lightweight headers are scanned across the registry.  Full canonical
    ingestion is delegated to :func:`discover_store_from_files` for the selected
    connected components, so consumers need not persist a second transcript.
    """

    snapshots, validation_issues = _validated_snapshots(sources)
    issues = list(validation_issues)
    active = {snapshot.path: snapshot for snapshot in snapshots if not snapshot.deleted}
    by_path = {snapshot.path: snapshot for snapshot in snapshots}
    resolved_seeds = tuple(
        sorted({str(Path(raw_path).expanduser().resolve()) for raw_path in seed_paths})
    )
    old_roots, root_issues = _validated_roots(old_root_session_ids)
    issues.extend(root_issues)
    for path in resolved_seeds:
        snapshot = by_path.get(path)
        if snapshot is None or not snapshot.deleted or snapshot.root_link is None:
            continue
        old_root = _uuid_or_none(snapshot.root_link)
        if old_root is not None:
            old_roots.add(old_root)

    headers: dict[str, _SourceHeader] = {}
    for path, snapshot in sorted(active.items()):
        try:
            header = _file_source_header(snapshot)
        except Exception as exc:
            issues.append(
                _issue(
                    "error",
                    "header",
                    "incremental_graph.header_failed",
                    f"{type(exc).__name__}: {exc}",
                    source_path=path,
                )
            )
            continue
        if header is None:
            issues.append(
                _issue(
                    "error",
                    "header",
                    "incremental_graph.header_missing",
                    "no canonical session header could be scanned",
                    source_path=path,
                )
            )
            continue
        headers[path] = header

    selected = _select_component_paths(
        active=active,
        by_path=by_path,
        headers=headers,
        seed_paths=resolved_seeds,
        old_roots=old_roots,
        issues=issues,
    )
    if not selected:
        new_roots: set[UUID] = set()
        return _graph_build_result(
            graphs=(),
            relationships=(),
            issues=issues,
            selected=selected,
            old_roots=old_roots,
            new_roots=new_roots,
        )

    try:
        discovery = discover_store_from_files(
            [Path(path) for path in sorted(selected)], retention=retention
        )
    except Exception as exc:
        issues.append(
            _issue(
                "error",
                "adapter",
                "incremental_graph.discovery_failed",
                f"{type(exc).__name__}: {exc}",
                details={"source_count": len(selected)},
            )
        )
        return _graph_build_result(
            graphs=(),
            relationships=(),
            issues=issues,
            selected=selected,
            old_roots=old_roots,
            new_roots=set(),
        )

    graphs = tuple(discovery.store.session_graphs.values())
    graph_by_root = {graph.root_session_id: graph for graph in graphs}
    relationships: list[SourceGraphRelationship] = []
    discovered_paths: set[str] = set()
    for source in discovery.sources:
        path = str(source.path.resolve())
        discovered_paths.add(path)
        header = headers.get(path)
        root_id = source.root_session_id
        graph = graph_by_root.get(root_id) if root_id is not None else None
        if header is None or root_id is None or graph is None:
            issues.append(
                _issue(
                    "error",
                    "assembly",
                    "incremental_graph.source_unassigned",
                    "canonical source was not assigned to an assembled graph",
                    source_path=path,
                )
            )
            continue
        relationships.append(
            SourceGraphRelationship(
                source_path=path,
                vendor=source.vendor,
                session_id=header.session_id,
                parent_session_id=header.parent_session_id,
                root_session_id=root_id,
                project_identifier=graph.project_identifier or Path(path).stem,
            )
        )
    for missing in sorted(selected - discovered_paths):
        issues.append(
            _issue(
                "error",
                "adapter",
                "incremental_graph.source_not_ingested",
                "canonical discovery omitted the selected source",
                source_path=missing,
            )
        )
    new_roots = {graph.root_session_id for graph in graphs}
    return _graph_build_result(
        graphs=graphs,
        relationships=relationships,
        issues=issues,
        selected=selected,
        old_roots=old_roots,
        new_roots=new_roots,
    )


def _file_source_header(snapshot: SourceSnapshot) -> _SourceHeader | None:
    vendor = _snapshot_vendor(snapshot) or _detect_vendor(snapshot.path, ())
    if vendor is None:
        raise ValueError("unable to identify source vendor")
    adapter_cls: type[BaseAdapter]
    if vendor == Vendor.CODEX_CLI:
        adapter_cls = CodexAdapter
    elif vendor == Vendor.CLAUDE_CODE:
        adapter_cls = ClaudeCodeAdapter
    else:
        adapter_cls = PiAdapter
    header = adapter_cls().scan_header(Path(snapshot.path))
    if header is None:
        return None
    return _SourceHeader(
        path=snapshot.path,
        vendor=vendor,
        session_id=header.session_id,
        parent_session_id=header.parent_session_id,
    )


def _graph_build_result(
    *,
    graphs: Iterable[SessionGraph],
    relationships: Iterable[SourceGraphRelationship],
    issues: list[GraphBuildIssue],
    selected: set[str],
    old_roots: set[UUID],
    new_roots: set[UUID],
) -> IncrementalGraphBuild:
    graph_rows = tuple(
        sorted(
            graphs,
            key=lambda graph: (
                graph.project_identifier or "",
                graph.summary.started_at if graph.summary else None,
                str(graph.root_session_id),
            ),
        )
    )
    relationship_rows = tuple(
        sorted(
            relationships,
            key=lambda item: (str(item.root_session_id), item.source_path),
        )
    )
    if any(issue.severity == "error" for issue in issues) and not graph_rows:
        status: Literal["complete", "inconclusive", "failed"] = "failed"
    elif issues:
        status = "inconclusive"
    else:
        status = "complete"
    return IncrementalGraphBuild(
        status=status,
        graphs=graph_rows,
        source_relationships=relationship_rows,
        issues=tuple(issues),
        selected_source_paths=tuple(sorted(selected)),
        old_root_session_ids=tuple(sorted(old_roots, key=str)),
        affected_root_session_ids=tuple(sorted({*old_roots, *new_roots}, key=str)),
        removed_root_session_ids=tuple(sorted(old_roots - new_roots, key=str)),
    )


def _validated_snapshots(
    sources: Iterable[SourceInput],
) -> tuple[list[SourceSnapshot], list[GraphBuildIssue]]:
    snapshots: list[SourceSnapshot] = []
    issues: list[GraphBuildIssue] = []
    seen: set[str] = set()
    for position, raw in enumerate(sources):
        try:
            snapshot = (
                raw
                if isinstance(raw, SourceSnapshot)
                else SourceSnapshot.model_validate(
                    raw.model_dump(mode="python") if isinstance(raw, BaseModel) else raw
                )
            )
            resolved = str(Path(snapshot.path).expanduser().resolve())
            snapshot = snapshot.model_copy(update={"path": resolved})
            if resolved in seen:
                raise ValueError("duplicate source path")
        except Exception as exc:
            issues.append(
                _issue(
                    "error",
                    "selection",
                    "incremental_graph.invalid_source_snapshot",
                    f"source snapshot {position}: {type(exc).__name__}: {exc}",
                )
            )
            continue
        seen.add(resolved)
        snapshots.append(snapshot)
    return snapshots, issues


def _validated_roots(
    roots: Iterable[str | UUID],
) -> tuple[set[UUID], list[GraphBuildIssue]]:
    validated: set[UUID] = set()
    issues: list[GraphBuildIssue] = []
    for raw in roots:
        try:
            validated.add(raw if isinstance(raw, UUID) else UUID(str(raw)))
        except (TypeError, ValueError) as exc:
            issues.append(
                _issue(
                    "error",
                    "selection",
                    "incremental_graph.invalid_old_root",
                    f"invalid old root {raw!r}: {exc}",
                )
            )
    return validated, issues


def _select_component_paths(
    *,
    active: Mapping[str, SourceSnapshot],
    by_path: Mapping[str, SourceSnapshot],
    headers: Mapping[str, _SourceHeader],
    seed_paths: Sequence[str],
    old_roots: set[UUID],
    issues: list[GraphBuildIssue],
) -> set[str]:
    if not seed_paths and not old_roots:
        return set(active)

    selected: set[str] = set()
    for path in seed_paths:
        snapshot = by_path.get(path)
        if snapshot is None:
            issues.append(
                _issue(
                    "warning",
                    "selection",
                    "incremental_graph.seed_unknown",
                    "seed path is not present in the current source registry",
                    source_path=path,
                )
            )
            continue
        if snapshot.deleted:
            if snapshot.root_link is None:
                issues.append(
                    _issue(
                        "warning",
                        "selection",
                        "incremental_graph.deleted_seed_unlinked",
                        "deleted seed has no previous root relationship",
                        source_path=path,
                    )
                )
            continue
        selected.add(path)

    old_root_text = {str(root) for root in old_roots}
    for path, snapshot in active.items():
        snapshot_root = _uuid_or_none(snapshot.root_link)
        if snapshot_root is not None and str(snapshot_root) in old_root_text:
            selected.add(path)
    for path, header in headers.items():
        if header.session_id in old_roots:
            selected.add(path)
    for path in tuple(selected):
        root = active[path].root_link
        if root:
            selected.update(
                candidate.path
                for candidate in active.values()
                if candidate.root_link == root
            )

    path_by_session = {header.session_id: path for path, header in headers.items()}
    children: dict[UUID, set[UUID]] = defaultdict(set)
    parent: dict[UUID, UUID] = {}
    for header in headers.values():
        if header.parent_session_id is None:
            continue
        parent[header.session_id] = header.parent_session_id
        children[header.parent_session_id].add(header.session_id)

    queue: deque[UUID] = deque(
        headers[path].session_id for path in selected if path in headers
    )
    seen_sessions = set(queue)
    while queue:
        session_id = queue.popleft()
        neighbours = set(children.get(session_id, ()))
        parent_id = parent.get(session_id)
        if parent_id is not None:
            neighbours.add(parent_id)
        for neighbour in neighbours:
            if neighbour in seen_sessions:
                continue
            neighbour_path = path_by_session.get(neighbour)
            if neighbour_path is None:
                continue
            seen_sessions.add(neighbour)
            selected.add(neighbour_path)
            queue.append(neighbour)
    return selected


def _source_header(
    snapshot: SourceSnapshot, messages_for_path: MessagesForPath
) -> _SourceHeader | None:
    vendor = _snapshot_vendor(snapshot)
    if vendor is None:
        vendor = _detect_vendor(snapshot.path, messages_for_path(snapshot.path))
    if vendor is None:
        raise ValueError("unable to identify source vendor")

    metadata_session = _uuid_or_none(snapshot.metadata.get("session_id"))
    metadata_parent = _uuid_or_none(
        snapshot.metadata.get("parent_session_id") or snapshot.parent_link
    )
    if metadata_session is not None:
        return _SourceHeader(
            path=snapshot.path,
            vendor=vendor,
            session_id=metadata_session,
            parent_session_id=metadata_parent,
        )

    records = _header_records(snapshot.path, messages_for_path(snapshot.path), vendor)
    if vendor == Vendor.CODEX_CLI:
        for record in records:
            if record.get("type") != "session_meta":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                return None
            session_id = _uuid_or_none(payload.get("id"))
            if session_id is None:
                return None
            parent_session_id = _codex_parent_from_meta(payload)
            return _SourceHeader(
                path=snapshot.path,
                vendor=vendor,
                session_id=session_id,
                parent_session_id=parent_session_id,
            )
        return None

    if vendor == Vendor.CLAUDE_CODE:
        first = next((record for record in records if record.get("sessionId")), None)
        if first is None:
            return None
        raw_session_id = _uuid_or_none(first.get("sessionId"))
        if raw_session_id is None:
            return None
        mechanism = _subagent_input(Path(snapshot.path), records, raw_session_id)
        session_id, parent_session_id = canonical_session_ids(mechanism)
        return _SourceHeader(
            path=snapshot.path,
            vendor=vendor,
            session_id=session_id,
            parent_session_id=parent_session_id,
        )

    for record in records:
        if record.get("type") != "session":
            continue
        raw_id = record.get("id")
        session_id = _uuid_or_none(raw_id)
        if session_id is None and isinstance(raw_id, str):
            session_id = uuid5(NAMESPACE_URL, f"pi:{Path(snapshot.path)}:{raw_id}")
        if session_id is not None:
            return _SourceHeader(
                path=snapshot.path, vendor=vendor, session_id=session_id
            )
    return _SourceHeader(
        path=snapshot.path,
        vendor=vendor,
        session_id=uuid5(NAMESPACE_URL, f"pi:{Path(snapshot.path)}"),
    )


def _header_records(
    path: str, messages: Iterable[MessageInput], vendor: Vendor
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in messages:
        message = _validated_message(path, raw)
        records.append(message.payload)
        if vendor == Vendor.CODEX_CLI and message.payload.get("type") == "session_meta":
            break
        if vendor == Vendor.CLAUDE_CODE and message.payload.get("sessionId"):
            break
        if vendor == Vendor.PI and message.payload.get("type") == "session":
            break
    return records


def _complete_records(
    path: str, messages: Iterable[MessageInput]
) -> list[dict[str, Any]]:
    validated = [_validated_message(path, raw) for raw in messages]
    validated.sort(key=lambda message: (message.byte_offset, message.byte_end))
    prior_end = -1
    seen_ids: set[str] = set()
    records: list[dict[str, Any]] = []
    for message in validated:
        if not message.payload_complete:
            raise ValueError(
                f"persisted message {message.source_message_id} lacks its canonical payload"
            )
        if message.source_message_id in seen_ids:
            raise ValueError(f"duplicate message id {message.source_message_id}")
        if message.byte_offset < prior_end:
            raise ValueError("persisted source message byte ranges overlap")
        seen_ids.add(message.source_message_id)
        prior_end = message.byte_end
        records.append(message.payload)
    if not records:
        raise ValueError("source has no active complete messages")
    return records


def _validated_message(path: str, raw: MessageInput) -> SourceMessage:
    message = (
        raw
        if isinstance(raw, SourceMessage)
        else SourceMessage.model_validate(
            raw.model_dump(mode="python") if isinstance(raw, BaseModel) else raw
        )
    )
    if str(Path(message.source_path).expanduser().resolve()) != path:
        raise ValueError("message source_path does not match requested source")
    return message


def _snapshot_vendor(snapshot: SourceSnapshot) -> Vendor | None:
    raw = snapshot.metadata.get("vendor")
    try:
        return Vendor(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


def _detect_vendor(path: str, messages: Iterable[MessageInput]) -> Vendor | None:
    normalized = Path(path).parts
    if ".codex" in normalized and "sessions" in normalized:
        return Vendor.CODEX_CLI
    if ".claude" in normalized and "projects" in normalized:
        return Vendor.CLAUDE_CODE
    if ".pi" in normalized and "sessions" in normalized:
        return Vendor.PI
    for raw in messages:
        message = _validated_message(path, raw)
        payload = message.payload
        if "sessionId" in payload:
            return Vendor.CLAUDE_CODE
        record_type = payload.get("type")
        if record_type in {
            "session_meta",
            "turn_context",
            "response_item",
            "event_msg",
        }:
            return Vendor.CODEX_CLI
        if record_type in {"session", "session_info", "model_change"}:
            return Vendor.PI
    return None


def _codex_parent_from_meta(meta: Mapping[str, Any]) -> UUID | None:
    direct = _uuid_or_none(meta.get("forked_from_id"))
    if direct is not None:
        return direct
    source = meta.get("source")
    if not isinstance(source, Mapping):
        return None
    subagent = source.get("subagent")
    if not isinstance(subagent, Mapping):
        return None
    spawn = subagent.get("thread_spawn")
    if not isinstance(spawn, Mapping):
        return None
    return _uuid_or_none(spawn.get("parent_thread_id"))


def _uuid_or_none(value: Any) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str) or not value:
        return None
    for candidate in (value, value.removeprefix("T-")):
        try:
            return UUID(candidate)
        except ValueError:
            continue
    return None


def _codex_parent_turn_ids(
    headers: Mapping[str, _SourceHeader],
    records_by_path: Mapping[str, list[dict[str, Any]]],
) -> dict[UUID, set[str]]:
    referenced = {
        header.parent_session_id
        for path, header in headers.items()
        if path in records_by_path
        and header.vendor == Vendor.CODEX_CLI
        and header.parent_session_id is not None
    }
    started: dict[UUID, set[str]] = {}
    for path, records in records_by_path.items():
        header = headers[path]
        if header.vendor != Vendor.CODEX_CLI or header.session_id not in referenced:
            continue
        started[header.session_id] = {
            turn_id
            for record in records
            if isinstance((payload := record.get("payload")), dict)
            and payload.get("type") == "task_started"
            and isinstance((turn_id := payload.get("turn_id")), str)
        }
    return started


def _build_session_from_records(
    *,
    snapshot: SourceSnapshot,
    vendor: Vendor,
    records: list[dict[str, Any]],
    parent_started_turn_ids: set[str] | None,
) -> Session:
    source = Path(snapshot.path)
    adapter: BaseAdapter
    if vendor == Vendor.CODEX_CLI:
        adapter = CodexAdapter()
        codex_records = _cut_inherited_records(records, parent_started_turn_ids)
        state = CodexAdapter._ParseState()
        transcript = adapter._build_transcript(codex_records, state)
        session = adapter._build_session(source, transcript, state)
    elif vendor == Vendor.CLAUDE_CODE:
        adapter = ClaudeCodeAdapter()
        adapter._reset_ingest_state()
        session = adapter._build_session(source, records)
    elif vendor == Vendor.PI:
        adapter = PiAdapter()
        adapter._reset_ingest_state()
        session = adapter._build_session(source, records)
    else:  # pragma: no cover - Vendor is closed, defensive for future members
        raise ValueError(f"unsupported vendor: {vendor}")
    return stabilize_session(session, vendor=vendor, source=source)


def _issue(
    severity: Literal["warning", "error"],
    stage: Literal["selection", "header", "messages", "adapter", "assembly"],
    code: str,
    message: str,
    *,
    source_path: str | None = None,
    session_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> GraphBuildIssue:
    return GraphBuildIssue(
        severity=severity,
        stage=stage,
        code=code,
        message=message,
        source_path=source_path,
        session_id=session_id,
        details=details or {},
    )


__all__ = [
    "GraphBuildIssue",
    "IncrementalGraphBuild",
    "MessagesForPath",
    "SourceMessage",
    "SourceGraphComponent",
    "SourceGraphComponentPlan",
    "SourceSnapshot",
    "SourceStatus",
    "SourceGraphRelationship",
    "plan_session_graph_components_from_files",
    "rebuild_affected_session_graphs",
    "rebuild_affected_session_graphs_from_files",
]
