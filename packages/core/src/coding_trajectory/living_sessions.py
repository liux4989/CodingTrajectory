"""Topology-only live session inventory; no transcripts, history or writes."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from coding_trajectory.contracts.living import LivingSessionMetadata
from coding_trajectory.ingestion.common import LIVING_ACTIVITY_SECONDS, canonical_json
from coding_trajectory.service.pagination import paginate
from coding_trajectory.service.store import IndexCache, _filter_runs, _refresh_topology

SCHEMA_VERSION = "ct.living_sessions.v3"


def serve_living_sessions(
    params: dict[str, Any],
    *,
    current_dir: Path,
    global_scope: bool,
    cache: IndexCache | None = None,
) -> dict[str, Any]:
    """Include whole runs within the horizon, with per-session source liveness."""
    cache = cache if cache is not None else IndexCache()
    entrypoint = params.get("root_session_id") or params.get("session_id")
    runs = _refresh_topology(cache, current_dir, [entrypoint] if entrypoint else None)
    if entrypoint:
        runs = [
            run
            for run in runs
            if entrypoint == str(run.root_session_id)
            or entrypoint in {str(sid) for sid in run.session_ids}
        ]
    else:
        runs = _filter_runs(
            runs, params, current_dir=current_dir, global_scope=global_scope
        )
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=params["horizon_days"])
    rows = []
    issues = []
    for run in runs:
        sources = []
        for path in run.source_paths:
            try:
                stat = Path(path).stat()
                sources.append((cache._topologies[path], stat))
            except OSError as exc:
                issues.append(
                    {
                        "severity": "warning",
                        "code": "living.sessions.source_unavailable",
                        "message": str(exc),
                        "path": {"root_session_id": str(run.root_session_id)},
                    }
                )
        if not entrypoint and not any(
            datetime.fromtimestamp(stat.st_mtime, UTC) >= cutoff for _, stat in sources
        ):
            continue
        for sid in run.session_ids:
            segments = [
                (header, stat) for header, stat in sources if header.session_id == sid
            ]
            if not segments:
                continue
            header, latest = max(segments, key=lambda value: value[1].st_mtime_ns)
            modified = datetime.fromtimestamp(latest.st_mtime, UTC)
            payload = LivingSessionMetadata(
                session_id=str(sid),
                root_session_id=str(run.root_session_id),
                lineage_root_session_id=str(run.lineage_root_session_id),
                vendor=header.vendor.value,
                project=header.project or str(sid),
                cwd=header.cwd,
                modified=modified,
                size=sum(stat.st_size for _, stat in segments),
                state="living"
                if (now - modified).total_seconds() <= LIVING_ACTIVITY_SECONDS
                else "inactive",
            ).model_dump(mode="json")
            rows.append(
                {
                    **payload,
                    "digest": hashlib.sha256(
                        canonical_json(payload).encode("utf-8")
                    ).hexdigest(),
                }
            )
    page, cursor = paginate(
        rows,
        method="living.sessions",
        params=params,
        scope={"global_scope": global_scope, "current_dir": str(current_dir.resolve())},
        key=lambda row: (row["root_session_id"], row["session_id"]),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "items": page,
        "total": len(rows),
        "returned": len(page),
        "next_cursor": cursor,
        "issues": issues,
    }


__all__ = ["SCHEMA_VERSION", "serve_living_sessions"]
