from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Callable

from pydantic import BaseModel, Field

try:
    from . import context_window as context_window_mod
    from .context_window import CacheBreakSummary
except ImportError:
    import context_window as context_window_mod
    from context_window import CacheBreakSummary

# Re-export so the classifier is the single source of truth for both the
# per-session context-window view and this aggregate. The classifier
# (``_project_cache_breaks`` + its helpers) classifies a re-read into
# ttl_confirmed / ttl_likely / effort_switch / model_switch / unattributed; we
# reuse it verbatim per session rather than re-deriving the heuristic here. It
# is form-agnostic: it reads both the CLI display form (``id``, ``start``,
# short usage keys) and the raw ``ct api call session.usage`` form
# (``turn_id``, ``started_at``, long usage keys) - so this aggregate uses the
# fast batched api call directly, with no dependency on the CLI's
# display-layer reshaping.
classify_cache_breaks = context_window_mod._project_cache_breaks
project_compaction = context_window_mod._project_compaction
select_session_usage = context_window_mod._selected_session_usage


class CacheBreaksFilters(BaseModel):
    since_days: int = Field(default=7, ge=1)
    project_name: str | None = None


def build_projection(
    *,
    ct_json: Callable[[list[str]], dict[str, Any]],
    since_days: int = 7,
    project_name: str | None = None,
) -> dict[str, Any]:
    filters = CacheBreaksFilters(since_days=since_days, project_name=project_name)
    projects_payload = ct_json(["project", "list", "--params", "{}", "--output", "json"])
    session_params: dict[str, Any] = {
        "since_days": filters.since_days,
        "include": ["runtime"],
    }
    if filters.project_name:
        session_params["project_name"] = filters.project_name
    sessions_payload = _api_call(ct_json, "project.sessions", session_params)
    sessions = [
        item for item in sessions_payload.get("items") or [] if isinstance(item, dict)
    ]
    usage_by_session = _usage_batch(
        ct_json,
        [
            str(item.get("root_session_id") or item.get("id") or "")
            for item in sessions
            if item.get("root_session_id") or item.get("id")
        ],
    )
    # Core's ``session.usage`` api always emits ``effort_changes`` (even
    # ``{"count": 0}``) now that ``effort_change_stats`` returns an empty stats
    # object rather than None. Its absence on every fetched session therefore
    # means the installed ct is stale and lacks the effort_change-ingestion
    # capability. We don't raise (the per-session page does that loudly on
    # drill-through); a warning is emitted below instead.
    any_effort_changes = any(
        "effort_changes" in payload for payload in usage_by_session.values()
    )

    breaks: list[dict[str, Any]] = []
    warnings: list[str] = []
    for session in sessions:
        session_id = str(session.get("root_session_id") or session.get("id") or "")
        if not session_id:
            continue
        usage = usage_by_session.get(session_id)
        if not usage:
            continue
        # Match the per-session context-window scope: classify the selected
        # (main) sub-session, not the merged graph aggregate, so subagent
        # context windows don't create spurious floor-collapses.
        selected_usage = select_session_usage(usage, session_id)
        vendors = session.get("vendors") or []
        vendor = vendors[0] if vendors else "unknown"
        try:
            compaction = project_compaction(selected_usage)
            summary = classify_cache_breaks(
                selected_usage,
                vendor=vendor,
                compaction=compaction,
                require_effort_changes=False,
            )
        except Exception as exc:  # pragma: no cover - defensive per-session skip
            warnings.append(f"{session_id[:8]}: {exc}")
            continue
        if summary is None or not summary.events:
            continue
        session_meta = _session_meta(session, session_id, vendor)
        breaks.extend(_enrich_breaks(summary, selected_usage, session_meta))

    if breaks and not any_effort_changes:
        warnings.append(
            "No effort_change observations surfaced (ct may be stale); breaks "
            "are shown without confirmed effort-switch attribution."
        )

    return _projection_payload(
        filters=filters,
        projects=_project_options(projects_payload),
        breaks=breaks,
        warnings=warnings,
    )


def _projection_payload(
    *,
    filters: CacheBreaksFilters,
    projects: list[dict[str, Any]],
    breaks: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    by_type: dict[str, int] = defaultdict(int)
    by_vendor_rows: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"breaks": 0, "re_read_tokens": 0, "waste_usd": 0.0, "has_waste": False}
    )
    by_project_rows: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"breaks": 0, "re_read_tokens": 0, "waste_usd": 0.0, "has_waste": False}
    )
    session_rows: dict[str, dict[str, Any]] = {}
    total_re_read = 0
    total_waste = 0.0
    has_waste = False
    confirmed = 0
    cost_values: list[float] = []
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"breaks": 0, "re_read_tokens": 0, "waste_usd": 0.0, "has_waste": False,
                 "by_type": defaultdict(int)}
    )
    for record in breaks:
        by_type[record["type"]] += 1
        if record["effort_to"]:
            confirmed += 1
        re_read = int(record.get("re_read_tokens") or 0)
        total_re_read += re_read
        cost = record.get("est_cost_usd")
        if cost is not None:
            total_waste += float(cost)
            has_waste = True
            cost_values.append(float(cost))
        vendor = record["vendor"]
        project = record["project"] or "unknown"
        _bump(by_vendor_rows[vendor], re_read, cost)
        _bump(by_project_rows[project], re_read, cost)
        _bump_session(session_rows, record, re_read, cost)
        bucket = record.get("timestamp") or "unknown"
        bucket_day = bucket[:10] if isinstance(bucket, str) and bucket else "unknown"
        _bump(buckets[bucket_day], re_read, cost)
        buckets[bucket_day]["by_type"][record["type"]] += 1

    session_list = sorted(
        session_rows.values(),
        key=lambda row: (row["waste_usd"], row["re_read_tokens"]),
        reverse=True,
    )
    return {
        "schema_version": 1,
        "filters": filters.model_dump(mode="json"),
        "project_options": projects,
        "summary": {
            "sessions_with_breaks": len(session_rows),
            "total_breaks": len(breaks),
            "by_type": {
                "effort_switch": by_type.get("effort_switch", 0),
                "model_switch": by_type.get("model_switch", 0),
                "ttl_confirmed": by_type.get("ttl_confirmed", 0),
                "ttl_likely": by_type.get("ttl_likely", 0),
                "unattributed": by_type.get("unattributed", 0),
            },
            "total_re_read_tokens": total_re_read,
            "estimated_waste_usd": round(total_waste, 4) if has_waste else None,
            "confirmed_effort_switches": confirmed,
            "affected_projects": len({r["project"] for r in breaks if r["project"]}),
            "avg_break_cost_usd": round(sum(cost_values) / len(cost_values), 4)
            if cost_values
            else None,
        },
        "top_sessions": session_list[:12],
        "by_vendor": sorted(
            (
                {"vendor": v, **row}
                for v, row in by_vendor_rows.items()
            ),
            key=lambda r: (r["waste_usd"], r["re_read_tokens"]),
            reverse=True,
        ),
        "by_project": sorted(
            (
                {"project": p, **row}
                for p, row in by_project_rows.items()
            ),
            key=lambda r: (r["waste_usd"], r["re_read_tokens"]),
            reverse=True,
        ),
        "time_buckets": sorted(
            (
                {"bucket": b, **{k: v for k, v in row.items() if k != "by_type"},
                 "by_type": dict(row["by_type"])}
                for b, row in buckets.items()
            ),
            key=lambda r: r["bucket"],
        ),
        "breaks": sorted(
            breaks,
            key=lambda r: (r.get("est_cost_usd") or 0, r.get("re_read_tokens") or 0),
            reverse=True,
        ),
        "warnings": warnings,
    }


def _bump(row: dict[str, Any], re_read: int, cost: float | None) -> None:
    row["breaks"] += 1
    row["re_read_tokens"] += re_read
    if cost is not None:
        row["waste_usd"] += float(cost)
        row["has_waste"] = True


def _bump_session(
    rows: dict[str, dict[str, Any]],
    record: dict[str, Any],
    re_read: int,
    cost: float | None,
) -> None:
    session_id = record["session_id"]
    row = rows.get(session_id)
    if row is None:
        row = {
            "session_id": session_id,
            "project": record["project"],
            "vendor": record["vendor"],
            "title": record["title"],
            "started_at": record["started_at"],
            "breaks": 0,
            "re_read_tokens": 0,
            "waste_usd": 0.0,
            "has_waste": False,
            "confirmed": 0,
            "by_type": defaultdict(int),
        }
        rows[session_id] = row
    row["breaks"] += 1
    row["re_read_tokens"] += re_read
    if record["effort_to"]:
        row["confirmed"] += 1
    if cost is not None:
        row["waste_usd"] += float(cost)
        row["has_waste"] = True
    row["by_type"][record["type"]] += 1


def _enrich_breaks(
    summary: CacheBreakSummary,
    usage: dict[str, Any],
    session_meta: dict[str, Any],
) -> list[dict[str, Any]]:
    """Attach session metadata + a per-break timestamp/turn index to each
    classified break. The timestamp comes from the turn's ``runtime`` start
    (the cache-rebuild turn), bucketed to a day for the time series. Reads
    both the api form (``turn_id``/``started_at``) and the CLI form
    (``id``/``start``).
    """
    turn_meta: dict[str, dict[str, Any]] = {}
    for index, turn in enumerate(usage.get("turns") or []):
        if not isinstance(turn, dict):
            continue
        turn_id = str(turn.get("id") or turn.get("turn_id") or "")
        if not turn_id:
            continue
        runtime = turn.get("runtime") or {}
        turn_meta[turn_id] = {
            "index": index,
            "timestamp": _optional_text(
                runtime.get("start") or runtime.get("started_at")
            ),
        }
    enriched: list[dict[str, Any]] = []
    for record in summary.events:
        meta = turn_meta.get(record.turn_id, {})
        data = record.model_dump(mode="json")
        data["session_id"] = session_meta["session_id"]
        data["project"] = session_meta["project"]
        data["vendor"] = session_meta["vendor"]
        data["title"] = session_meta["title"]
        data["started_at"] = session_meta["started_at"]
        data["turn_index"] = meta.get("index")
        data["timestamp"] = meta.get("timestamp")
        enriched.append(data)
    return enriched


def _session_meta(
    session: dict[str, Any],
    session_id: str,
    vendor: str,
) -> dict[str, Any]:
    runtime = session.get("runtime") or {}
    return {
        "session_id": session_id,
        "project": _optional_text(session.get("project")) or "unknown",
        "vendor": vendor,
        "title": _optional_text(session.get("title")) or "",
        "started_at": _optional_text(runtime.get("started_at")),
    }


def _api_call(
    ct_json: Callable[[list[str]], dict[str, Any]],
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    payload = ct_json(
        [
            "api",
            "call",
            method,
            "--global-scope",
            "--params",
            json.dumps(params),
        ]
    )
    if not payload.get("ok"):
        error = payload.get("error") or {}
        raise RuntimeError(
            str(error.get("message") or f"ct api request failed: {method}")
        )
    result = payload.get("result") or {}
    if not isinstance(result, dict):
        raise RuntimeError(f"ct api call returned a non-object result: {method}")
    return result


def _usage_batch(
    ct_json: Callable[[list[str]], dict[str, Any]],
    session_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Fetch ``ct api call session.usage`` for all sessions in one batched call.

    Core's ``session.usage`` api is the single source of truth: it emits turn
    ids, ``wait_before_seconds``, the cached/uncached prompt split,
    ``cache_break_waste_usd``, ``compaction``, and ``effort_changes`` (always,
    even ``{"count": 0}``). The classifier is form-agnostic (reads both the
    api's ``turn_id``/``started_at``/long keys and the CLI's ``id``/``start``/
    short keys), so this uses the fast batched api call rather than fanning out
    one ``ct session usage`` subprocess per session.
    """
    if not session_ids:
        return {}
    requests = [
        {
            "id": session_id,
            "method": "session.usage",
            "params": {"session_id": session_id},
        }
        for session_id in session_ids
    ]
    payload = ct_json(
        [
            "api",
            "batch",
            "--global-scope",
            "--requests",
            json.dumps(requests),
        ]
    )
    rows: dict[str, dict[str, Any]] = {}
    for item in payload.get("items") or []:
        if not isinstance(item, dict) or not item.get("ok"):
            continue
        result = item.get("result")
        if isinstance(result, dict):
            session_id = str(result.get("session_id") or item.get("id") or "")
            if session_id:
                rows[session_id] = result
    return rows


def _project_options(payload: dict[str, Any]) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if name:
            options.append({"name": name, "path": item.get("path")})
    return options


def _optional_text(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None
