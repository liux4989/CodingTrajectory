"""In-process builders for durable datahub read models.

The builder in this module deliberately stops at storage-neutral entity records.
It performs canonical core discovery once, invokes versioned core service handlers
against that in-memory store, and returns Pydantic-validated records that a durable
store can upsert.  Route reconstruction helpers operate on the already-validated
payload dictionaries so a hot read does not rebuild canonical models row by row.
"""

# ruff: noqa: F401
from __future__ import annotations
import hashlib
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote
from coding_trajectory import debug
from coding_trajectory.datahub import (
    DiscoveryResult,
    DiscoverySource,
    DocumentError,
    DocumentStore,
    IndexCache,
    SessionGraph,
    discover_store,
    dispatch,
)
from pydantic import BaseModel, ConfigDict, Field
from datahub_plugin.projections.read_models_contracts import (
    BuildIssue,
    OverviewPayload,
    ProjectDetailPayload,
    ProjectPayload,
    ReadModelEntity,
    SessionTimelinePayload,
    SourceGraphRelationship,
)


DEFAULT_RECENT_HORIZON_DAYS = 7

EntityKind = Literal[
    "project_catalog",
    "project_contribution",
    "session_timeline_contribution",
    "project",
    "session",
    "overview",
    "session_timeline",
    "project_detail",
]
IssueDisposition = Literal["failed", "inconclusive"]
BuildStatus = Literal["success", "partial", "failed", "inconclusive"]


def reconstruct_projects(
    rows: Iterable[Any],
    *,
    agent_vendor: str | None = None,
) -> dict[str, Any]:
    """Return the current ``/api/projects`` shape from persisted project rows."""

    items = []
    for row in rows:
        if _row_kind(row) != "project":
            continue
        payload = _row_payload(row)
        vendors = payload.get("vendors") or []
        if agent_vendor and agent_vendor not in vendors:
            continue
        items.append(dict(payload))
    items.sort(key=lambda item: str(item.get("name") or ""))
    return {"items": items}


def reconstruct_sessions(
    rows: Iterable[Any],
    *,
    project_name: str | None = None,
    agent_vendor: str | None = None,
    include: Iterable[str] = (),
) -> dict[str, Any]:
    """Return the current ``/api/sessions`` shape without hot-row validation."""

    requested = set(include)
    items: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        if _row_kind(row) != "session":
            continue
        payload = dict(_row_payload(row))
        if project_name and payload.get("project") != project_name:
            continue
        if agent_vendor and agent_vendor not in (payload.get("vendors") or []):
            continue
        payload.pop("cost_usd", None)
        payload.pop("pricing_confidence", None)
        if "runtime" not in requested:
            payload.pop("runtime", None)
        if "usage" not in requested:
            payload.pop("usage", None)
            payload.pop("warnings", None)
        items.append((_row_sort_key(row), payload))
    items.sort(key=lambda item: item[0])
    return {"items": [payload for _sort_key, payload in items]}


def reconstruct_session_timeline(
    rows: Iterable[Any],
) -> dict[str, Any]:
    """Build the timeline route directly from contribution or session rows."""

    contributions = [
        _row_payload(row)
        for row in rows
        if _row_kind(row) == "session_timeline_contribution"
    ]
    by_date: dict[str, list[dict[str, Any]]] = {}
    for payload in contributions:
        date = str(payload.get("date") or "unknown")
        session = payload.get("session")
        if isinstance(session, Mapping):
            by_date.setdefault(date, []).append(dict(session))
    timeline = []
    for date, sessions in sorted(by_date.items(), reverse=True):
        sessions.sort(key=lambda item: str(item.get("id") or ""))
        timeline.append({"date": date, "count": len(sessions), "sessions": sessions})
    return {"timeline": timeline, "total": sum(len(rows) for rows in by_date.values())}


def reconstruct_project_detail(
    project_row: Any,
    session_rows: Iterable[Any],
    *,
    since_days: int | None,
) -> dict[str, Any]:
    """Return the current project-detail shape from indexed project/session rows."""

    project = _row_payload(project_row)
    project_name = str(project.get("name") or "")
    sessions = reconstruct_sessions(
        session_rows,
        project_name=project_name,
    )["items"]
    return {
        "name": project_name,
        "path": project.get("path"),
        "vendors": project.get("vendors") or [],
        "since_days": since_days,
        "sessions": sessions,
        "session_count": len(sessions),
    }


def reconstruct_overview(
    project_rows: Iterable[Any],
    session_rows: Iterable[Any],
    *,
    since_days: int,
    errors: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Return the overview route shape from persisted rows using plain dict math."""

    projects = reconstruct_projects(project_rows)["items"]
    session_payloads = [
        _datahub_session_item(_row_payload(row))
        for row in session_rows
        if _row_kind(row) == "session"
    ]
    vendor_counts: dict[str, int] = {}
    for item in projects:
        for vendor in item.get("vendors") or []:
            key = str(vendor)
            vendor_counts[key] = vendor_counts.get(key, 0) + 1
    activity = _overview_activity(session_payloads)
    return {
        "projects": {"count": len(projects), "vendors": vendor_counts},
        "sessions": {
            "count": len(session_payloads),
            "window_days": since_days,
            "runtime": activity["runtime"],
            "usage": activity["usage"],
            "top_projects": activity["top_projects"],
            "top_sessions": activity["top_sessions"],
            "warnings": activity["warnings"],
            "errors": [dict(error) for error in errors],
        },
    }


def reconstruct_recent_work(
    session_rows: Iterable[Any],
    *,
    since_days: int = 1,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the compact recent-work projection from retained session rows."""

    if since_days < 1:
        raise ValueError("since_days must be a positive integer")
    generated_at = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff = generated_at - timedelta(days=since_days)
    items = []
    for row in session_rows:
        if _row_kind(row) != "session":
            continue
        item = _datahub_session_item(_row_payload(row))
        started_at = _parse_utc_datetime((item.get("runtime") or {}).get("started_at"))
        if started_at is not None and started_at >= cutoff:
            items.append(item)
    activity = _overview_activity(items)
    vendors: dict[str, int] = {}
    for item in items:
        for vendor in item.get("vendors") or []:
            key = str(vendor)
            vendors[key] = vendors.get(key, 0) + 1
    project_count = len({str(item.get("project") or "unknown") for item in items})
    return {
        "schema_version": 1,
        "generated_at": generated_at.isoformat(),
        "cohort": {
            "since_days": since_days,
            "session_graph_count": len(items),
        },
        "coverage": _overview_coverage(items),
        "projects": {"count": project_count, "vendors": vendors},
        "sessions": {
            "count": len(items),
            "window_days": since_days,
            "runtime": activity["runtime"],
            "usage": activity["usage"],
            "top_projects": activity["top_projects"],
            "top_sessions": activity["top_sessions"],
            "warnings": activity["warnings"],
            "errors": [],
        },
        "hourly": _activity_buckets(items, grain="hour"),
        "daily": _activity_buckets(items, grain="day"),
        "warnings": activity["warnings"],
    }


def aggregate_read_models(
    rows: Iterable[Any],
    *,
    since_days: int = DEFAULT_RECENT_HORIZON_DAYS,
) -> list[ReadModelEntity]:
    """Fold persisted graph contributions into route-level aggregate entities."""

    if since_days < 1:
        raise ValueError("since_days must be a positive integer")
    materialized_rows = list(rows)
    scope_id = _recent_scope(since_days)
    project_inputs = [
        row
        for row in materialized_rows
        if _row_kind(row) in {"project_catalog", "project_contribution"}
    ]
    session_entities = [row for row in materialized_rows if _row_kind(row) == "session"]
    project_entities = _project_entities(project_inputs, scope_id=scope_id)
    timeline_payload = reconstruct_session_timeline(materialized_rows)
    overview_payload = reconstruct_overview(
        project_entities,
        session_entities,
        since_days=since_days,
    )
    aggregates = [
        _entity(
            entity_kind="overview",
            entity_id=scope_id,
            scope_id=scope_id,
            sort_key=scope_id,
            payload_model=OverviewPayload.model_validate(overview_payload),
        ),
        _entity(
            entity_kind="session_timeline",
            entity_id=scope_id,
            scope_id=scope_id,
            sort_key=scope_id,
            payload_model=SessionTimelinePayload.model_validate(timeline_payload),
        ),
        *project_entities,
    ]
    for project_entity in project_entities:
        payload = reconstruct_project_detail(
            project_entity,
            session_entities,
            since_days=since_days,
        )
        # Session rows remain normalized and are keyset-paginated at read time.
        # Keep only the materialized total in project metadata rather than
        # duplicating every session payload into a second large JSON document.
        payload["sessions"] = []
        name = str(project_entity.payload.get("name") or "")
        aggregates.append(
            _entity(
                entity_kind="project_detail",
                entity_id=_project_scope_id(name, scope_id),
                scope_id=scope_id,
                project_name=name,
                sort_key=name.casefold(),
                payload_model=ProjectDetailPayload.model_validate(payload),
            )
        )
    return aggregates


def _project_entities(
    contributions: Sequence[Any], *, scope_id: str
) -> list[ReadModelEntity]:
    grouped: dict[str, dict[str, Any]] = {}
    for contribution in contributions:
        payload = _row_payload(contribution)
        name = str(payload.get("name") or "unknown")
        target = grouped.setdefault(name, {"paths": set(), "vendors": set()})
        if payload.get("path"):
            target["paths"].add(str(payload["path"]))
        target["vendors"].update(str(value) for value in payload.get("vendors") or [])
    entities = []
    for name, values in sorted(grouped.items()):
        paths = sorted(values["paths"])
        entities.append(
            _entity(
                entity_kind="project",
                entity_id=name,
                scope_id=scope_id,
                project_name=name,
                sort_key=name.casefold(),
                payload_model=ProjectPayload(
                    name=name,
                    path=paths[0] if paths else None,
                    vendors=sorted(values["vendors"]),
                ),
            )
        )
    return entities


def _project_catalog_entities(
    catalog: Mapping[str, Any], *, scope_id: str
) -> list[ReadModelEntity]:
    raw_items = catalog.get("items") if isinstance(catalog, Mapping) else None
    if not isinstance(raw_items, Mapping):
        return []
    entities: list[ReadModelEntity] = []
    for name, raw in sorted(
        raw_items.items(), key=lambda item: str(item[0]).casefold()
    ):
        if not isinstance(raw, Mapping):
            continue
        project_name = str(name)
        entities.append(
            _entity(
                entity_kind="project_catalog",
                entity_id=project_name,
                scope_id=scope_id,
                project_name=project_name,
                sort_key=project_name.casefold(),
                payload_model=ProjectPayload(
                    name=project_name,
                    path=str(raw["path"]) if raw.get("path") else None,
                    vendors=sorted(str(value) for value in raw.get("vendors") or []),
                ),
            )
        )
    return entities


def _graph_cost(
    *,
    store: DocumentStore,
    root_session_id: str,
    current_dir: Path,
    cache: IndexCache,
    discovery_note: str,
) -> tuple[float | None, str | None]:
    usage = dispatch(
        "graph.usage",
        {"session_id": root_session_id},
        store=store,
        global_scope=True,
        current_dir=current_dir.resolve(),
        discovery_note=discovery_note,
        cache=cache,
    )
    models = [row for row in usage.get("models") or [] if isinstance(row, dict)]
    if not models:
        return None, None
    total = 0.0
    has_estimated_price = False
    for row in models:
        estimate = row.get("estimated_cost")
        if not isinstance(estimate, dict):
            continue
        value = estimate.get("value_usd")
        if isinstance(value, int | float) and not isinstance(value, bool):
            total += float(value)
        if estimate.get("confidence") == "estimated":
            has_estimated_price = True
    return round(total, 8), "estimated" if has_estimated_price else "missing_price"


def _source_relationships(
    sources: Sequence[DiscoverySource],
) -> tuple[list[SourceGraphRelationship], list[BuildIssue]]:
    relationships: list[SourceGraphRelationship] = []
    issues: list[BuildIssue] = []
    for source in sources:
        if source.root_session_id is None:
            issues.append(
                _issue(
                    "inconclusive",
                    stage="source_index",
                    message="discovered source has no canonical root graph",
                    code="read_model.source_root_missing",
                    source_path=str(source.path),
                )
            )
            continue
        try:
            stat = source.path.stat()
        except OSError as exc:
            issues.append(
                _issue(
                    "inconclusive",
                    stage="source_index",
                    message=str(exc),
                    code="read_model.source_stat_failed",
                    source_path=str(source.path),
                    root_session_id=str(source.root_session_id),
                )
            )
            size = None
            mtime_ns = None
        else:
            size = stat.st_size
            mtime_ns = stat.st_mtime_ns
        relationships.append(
            SourceGraphRelationship(
                source_path=str(source.path),
                root_session_id=str(source.root_session_id),
                vendor=source.vendor.value,
                size=size,
                mtime_ns=mtime_ns,
            )
        )
    relationships.sort(key=lambda item: (item.root_session_id, item.source_path))
    return relationships, issues


def _debug_issues(
    records: Sequence[Mapping[str, Any]],
    *,
    stage: str,
    root_session_id: str | None = None,
) -> list[BuildIssue]:
    issues = []
    for record in records:
        context = record.get("context")
        context_dict = dict(context) if isinstance(context, Mapping) else {}
        source_path = context_dict.get("source")
        severity = record.get("severity")
        issues.append(
            _issue(
                "failed" if severity == "error" else "inconclusive",
                stage=stage,
                message=str(record.get("message") or "core emitted an empty warning"),
                code=str(record["code"]) if record.get("code") else None,
                source_path=str(source_path) if source_path else None,
                root_session_id=root_session_id,
                context=context_dict,
            )
        )
    return issues


def _issue(
    disposition: IssueDisposition,
    *,
    stage: str,
    message: str,
    code: str | None = None,
    source_path: str | None = None,
    root_session_id: str | None = None,
    context: Mapping[str, Any] | None = None,
) -> BuildIssue:
    identity = "\x00".join(
        (stage, code or "", source_path or "", root_session_id or "", message)
    )
    return BuildIssue(
        issue_id=hashlib.sha256(identity.encode()).hexdigest(),
        disposition=disposition,
        stage=stage,
        message=message,
        code=code,
        source_path=source_path,
        root_session_id=root_session_id,
        context=dict(context or {}),
    )


def _entity(
    *,
    entity_kind: EntityKind,
    entity_id: str,
    scope_id: str,
    sort_key: str,
    payload_model: BaseModel,
    root_session_id: str | None = None,
    project_name: str | None = None,
) -> ReadModelEntity:
    return ReadModelEntity(
        entity_kind=entity_kind,
        entity_id=entity_id,
        scope_id=scope_id,
        sort_key=sort_key,
        root_session_id=root_session_id,
        project_name=project_name,
        payload=payload_model.model_dump(mode="json", exclude_none=True),
    )


def _project_path(
    session_graph: SessionGraph, project_metadata: Mapping[str, Any] | None
) -> str | None:
    if project_metadata and project_metadata.get("path"):
        return str(project_metadata["path"])
    paths = sorted(
        {
            str(Path(session.cwd).resolve())
            for session in session_graph.sessions
            if session.cwd
        }
    )
    return paths[0] if paths else None


def _graph_started_at(session_graph: SessionGraph) -> str:
    starts = [session.started_at for session in session_graph.sessions]
    if not starts:
        return ""
    return min(starts).astimezone(UTC).isoformat().replace("+00:00", "Z")


def _datahub_session_item(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("root_session_id"),
        "project": item.get("project"),
        "title": item.get("title"),
        "preview": item.get("preview"),
        "vendors": list(item.get("vendors") or []),
        "sessions": list(item.get("session_ids") or []),
        "runtime": dict(item.get("runtime") or {}),
        "usage": dict(item.get("usage") or {}),
        "warnings": list(item.get("warnings") or []),
        "cost_usd": item.get("cost_usd"),
        "pricing_confidence": item.get("pricing_confidence"),
    }


def _overview_activity(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    runtime_totals = {
        "execution_seconds": 0,
        "wait_seconds": 0,
        "turns": 0,
        "tool_calls": 0,
        "failed_tool_calls": 0,
    }
    usage_totals = {
        "processed_tokens": 0,
        "cost_usd": 0.0,
        "known_cost_count": 0,
        "missing_cost_count": 0,
    }
    project_stats: dict[str, dict[str, Any]] = {}
    warnings: list[dict[str, Any]] = []
    for item in items:
        runtime = item.get("runtime") or {}
        usage = item.get("usage") or {}
        for key in runtime_totals:
            runtime_totals[key] += _number(runtime.get(key))
        usage_totals["processed_tokens"] += int(_number(usage.get("processed_tokens")))
        cost = item.get("cost_usd")
        if isinstance(cost, int | float) and not isinstance(cost, bool):
            usage_totals["cost_usd"] += float(cost)
        confidence = item.get("pricing_confidence")
        confidence_key = (
            "known_cost_count" if confidence == "estimated" else "missing_cost_count"
        )
        usage_totals[confidence_key] += 1

        project = str(item.get("project") or "unknown")
        stats = project_stats.setdefault(
            project,
            {
                "project": project,
                "count": 0,
                "vendors": {},
                "execution_seconds": 0,
                "processed_tokens": 0,
                "cost_usd": 0.0,
                "known_cost_count": 0,
            },
        )
        stats["count"] += 1
        stats["execution_seconds"] += _number(runtime.get("execution_seconds"))
        stats["processed_tokens"] += int(_number(usage.get("processed_tokens")))
        if isinstance(cost, int | float) and not isinstance(cost, bool):
            stats["cost_usd"] += float(cost)
        if confidence == "estimated":
            stats["known_cost_count"] += 1
        for vendor in item.get("vendors") or []:
            vendor_key = str(vendor)
            stats["vendors"][vendor_key] = stats["vendors"].get(vendor_key, 0) + 1
        for warning in item.get("warnings") or []:
            warnings.append(
                {
                    "session_id": item.get("id"),
                    "project": project,
                    "message": str(warning),
                }
            )

    top_projects = sorted(
        (
            {**stats, "vendors": dict(sorted(stats["vendors"].items()))}
            for stats in project_stats.values()
        ),
        key=lambda stats: (-stats["count"], stats["project"]),
    )[:8]
    top_sessions = sorted(
        (
            _overview_session(item)
            for item in items
            if int(_number((item.get("usage") or {}).get("processed_tokens"))) > 0
        ),
        key=lambda item: item["processed_tokens"],
        reverse=True,
    )[:8]
    return {
        "runtime": runtime_totals,
        "usage": usage_totals,
        "top_projects": top_projects,
        "top_sessions": top_sessions,
        "warnings": warnings,
    }


def _overview_coverage(items: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {
        "total": len(items),
        "runtime": sum(
            bool((item.get("runtime") or {}).get("started_at")) for item in items
        ),
        "usage": sum(
            (item.get("usage") or {}).get("processed_tokens") is not None
            for item in items
        ),
        "pricing": sum(item.get("cost_usd") is not None for item in items),
    }


def _activity_buckets(
    items: Sequence[Mapping[str, Any]], *, grain: Literal["hour", "day"]
) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for item in items:
        runtime = item.get("runtime") or {}
        started_at = _parse_utc_datetime(runtime.get("started_at"))
        if started_at is None:
            continue
        key = (
            started_at.strftime("%Y-%m-%dT%H:00:00Z")
            if grain == "hour"
            else started_at.date().isoformat()
        )
        bucket = buckets.setdefault(
            key,
            {
                "bucket": key,
                "sessions": 0,
                "execution_seconds": 0,
                "processed_tokens": 0,
                "cost_usd": 0.0,
            },
        )
        bucket["sessions"] += 1
        bucket["execution_seconds"] += int(_number(runtime.get("execution_seconds")))
        bucket["processed_tokens"] += int(
            _number((item.get("usage") or {}).get("processed_tokens"))
        )
        bucket["cost_usd"] += float(_number(item.get("cost_usd")))
    return [buckets[key] for key in sorted(buckets)]


def _parse_utc_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _overview_session(item: Mapping[str, Any]) -> dict[str, Any]:
    runtime = item.get("runtime") or {}
    usage = item.get("usage") or {}
    vendors = item.get("vendors") or []
    return {
        "id": item.get("id"),
        "title": item.get("title"),
        "project": item.get("project"),
        "vendor": str(vendors[0]) if vendors else "unknown",
        "vendors": list(vendors),
        "started_at": runtime.get("started_at"),
        "execution_seconds": int(_number(runtime.get("execution_seconds"))),
        "wait_seconds": int(_number(runtime.get("wait_seconds"))),
        "turns": int(_number(runtime.get("turns"))),
        "tool_calls": int(_number(runtime.get("tool_calls"))),
        "failed_tool_calls": int(_number(runtime.get("failed_tool_calls"))),
        "processed_tokens": int(_number(usage.get("processed_tokens"))),
    }


def _number(value: Any) -> int | float:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int | float):
        return value
    return 0


def _row_kind(row: Any) -> str:
    if isinstance(row, ReadModelEntity):
        return row.entity_kind
    if isinstance(row, Mapping):
        return str(row.get("entity_kind") or row.get("kind") or "")
    return str(getattr(row, "entity_kind", "") or getattr(row, "kind", ""))


def _row_payload(row: Any) -> Mapping[str, Any]:
    if isinstance(row, ReadModelEntity):
        return row.payload
    payload = (
        row.get("payload")
        if isinstance(row, Mapping)
        else getattr(row, "payload", None)
    )
    return payload if isinstance(payload, Mapping) else {}


def _row_sort_key(row: Any) -> str:
    if isinstance(row, ReadModelEntity):
        return row.sort_key
    if isinstance(row, Mapping):
        return str(row.get("sort_key") or "")
    return str(getattr(row, "sort_key", "") or "")


def _recent_scope(since_days: int) -> str:
    return f"recent:{since_days}d"


def _project_scope_id(project_name: str, scope_id: str) -> str:
    return f"{quote(project_name, safe='')}:{scope_id}"


def _build_status(
    entities: Sequence[ReadModelEntity], issues: Sequence[BuildIssue]
) -> BuildStatus:
    if not issues:
        return "success"
    if entities:
        return "partial"
    if any(issue.disposition == "failed" for issue in issues):
        return "failed"
    return "inconclusive"
