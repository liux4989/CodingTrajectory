from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field


ErrorKind = Literal[
    "abort_coding_session",
    "abrupt_coding_mid_session",
    "fail_tool_coverage",
]
Confidence = Literal["direct", "inferred"]
Severity = Literal["info", "warning", "critical"]


class ErrorCollectionFilters(BaseModel):
    since_days: int = Field(default=7, ge=1)
    project_name: str | None = None


def build_projection(
    *,
    ct_json: Callable[[list[str]], dict[str, Any]],
    since_days: int = 7,
    project_name: str | None = None,
) -> dict[str, Any]:
    filters = ErrorCollectionFilters(since_days=since_days, project_name=project_name)
    projects_payload = ct_json(
        ["project", "list", "--params", "{}", "--output", "json"]
    )
    session_params: dict[str, Any] = {
        "since_days": filters.since_days,
        "include": ["runtime", "usage"],
    }
    if filters.project_name:
        session_params["project_name"] = filters.project_name
    sessions_payload = _api_call(ct_json, "project.sessions", session_params)
    sessions = [
        item for item in sessions_payload.get("items") or [] if isinstance(item, dict)
    ]
    tool_usage_by_session = _tool_usage_batch(ct_json, _session_ids_with_tools(sessions))
    errors = [
        error
        for session in sessions
        for error in _session_errors(session, tool_usage_by_session.get(_session_id(session)))
    ]
    return {
        "schema_version": 1,
        "filters": filters.model_dump(mode="json"),
        "project_options": _project_options(projects_payload),
        "summary": _summary(sessions, errors),
        "errors": sorted(
            errors,
            key=lambda row: (
                _severity_rank(row["severity"]),
                row.get("started_at") or "",
                row.get("session_id") or "",
            ),
            reverse=True,
        ),
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
        raise RuntimeError(f"ct api request returned invalid result: {method}")
    return result


def _tool_usage_batch(
    ct_json: Callable[[list[str]], dict[str, Any]],
    session_ids: list[str],
) -> dict[str, dict[str, Any]]:
    if not session_ids:
        return {}
    requests = [
        {
            "id": session_id,
            "method": "session.tool_usage",
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
            session_id = str(result.get("root_session_id") or item.get("id") or "")
            if session_id:
                rows[session_id] = result
    return rows


def _session_errors(
    session: dict[str, Any],
    tool_usage: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    runtime = session.get("runtime") if isinstance(session.get("runtime"), dict) else {}
    session_id = _session_id(session)
    project = session.get("project")
    title = session.get("title")
    started_at = runtime.get("started_at") or session.get("started_at")
    ended_at = runtime.get("ended_at") or session.get("updated_at")
    errors: list[dict[str, Any]] = []

    interrupted_turns = _int(runtime.get("interrupted_turns"))
    if interrupted_turns:
        errors.append(
            _error_row(
                session_id=session_id,
                project=project,
                title=title,
                started_at=started_at,
                ended_at=ended_at,
                kind="abort_coding_session",
                severity="warning",
                confidence="direct",
                title_text="Abort coding session",
                detail=(
                    f"{interrupted_turns} turn"
                    f"{'' if interrupted_turns == 1 else 's'} recorded as aborted."
                ),
                evidence=[
                    f"runtime.interrupted_turns={interrupted_turns}",
                    "Codex turn_aborted runtime observation",
                ],
            )
        )

    status = str(runtime.get("status") or "").lower()
    if status == "incomplete":
        errors.append(
            _error_row(
                session_id=session_id,
                project=project,
                title=title,
                started_at=started_at,
                ended_at=ended_at,
                kind="abrupt_coding_mid_session",
                severity="critical",
                confidence="inferred",
                title_text="Abrupt coding mid-session",
                detail="The final turn did not reach a completed terminal state.",
                evidence=[
                    "runtime.status=incomplete",
                    "Last turn projected without task_complete or turn_aborted closure",
                ],
            )
        )

    tool_calls = _int(runtime.get("tool_calls"))
    failed_tool_calls = _int(runtime.get("failed_tool_calls"))
    tool_result_count = _int(
        (tool_usage or {}).get("tool_call_count")
        or (tool_usage or {}).get("tool_item_count")
    )
    missing_projection = tool_calls > 0 and tool_result_count == 0
    if failed_tool_calls or missing_projection:
        evidence = [
            f"runtime.tool_calls={tool_calls}",
            f"runtime.failed_tool_calls={failed_tool_calls}",
            f"session.tool_usage.tool_call_count={tool_result_count}",
        ]
        if missing_projection:
            evidence.append("tool_usage_projection=missing")
        errors.append(
            _error_row(
                session_id=session_id,
                project=project,
                title=title,
                started_at=started_at,
                ended_at=ended_at,
                kind="fail_tool_coverage",
                severity="warning" if not missing_projection else "critical",
                confidence="direct" if not missing_projection else "inferred",
                title_text="Fail tool coverage",
                detail=_tool_coverage_detail(
                    failed_tool_calls=failed_tool_calls,
                    missing_projection=missing_projection,
                ),
                evidence=evidence,
            )
        )
    return errors


def _error_row(
    *,
    session_id: str,
    project: Any,
    title: Any,
    started_at: Any,
    ended_at: Any,
    kind: ErrorKind,
    severity: Severity,
    confidence: Confidence,
    title_text: str,
    detail: str,
    evidence: list[str],
) -> dict[str, Any]:
    return {
        "id": f"{session_id}:{kind}",
        "session_id": session_id,
        "project": project,
        "session_title": title,
        "started_at": started_at,
        "ended_at": ended_at,
        "kind": kind,
        "severity": severity,
        "confidence": confidence,
        "title": title_text,
        "detail": detail,
        "evidence": evidence,
    }


def _tool_coverage_detail(*, failed_tool_calls: int, missing_projection: bool) -> str:
    parts: list[str] = []
    if failed_tool_calls:
        parts.append(
            f"{failed_tool_calls} failed tool result"
            f"{'' if failed_tool_calls == 1 else 's'}"
        )
    if missing_projection:
        parts.append("tool calls exist but session.tool_usage produced no tool rows")
    return "; ".join(parts) + "."


def _summary(
    sessions: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    by_kind: dict[str, int] = {
        "abort_coding_session": 0,
        "abrupt_coding_mid_session": 0,
        "fail_tool_coverage": 0,
    }
    by_project: dict[str, int] = {}
    by_severity: dict[str, int] = {"critical": 0, "warning": 0, "info": 0}
    affected_sessions: set[str] = set()
    for error in errors:
        by_kind[error["kind"]] += 1
        by_severity[error["severity"]] += 1
        affected_sessions.add(str(error["session_id"]))
        project = str(error.get("project") or "unknown")
        by_project[project] = by_project.get(project, 0) + 1
    return {
        "sessions": len(sessions),
        "affected_sessions": len(affected_sessions),
        "total_errors": len(errors),
        "by_kind": by_kind,
        "by_severity": by_severity,
        "top_projects": [
            {"project": project, "errors": count}
            for project, count in sorted(
                by_project.items(), key=lambda item: (-item[1], item[0])
            )[:8]
        ],
        "generated_at": datetime.now(UTC).isoformat(),
    }


def _project_options(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("items") or {}
    if not isinstance(items, dict):
        return []
    return [
        {
            "name": name,
            "path": item.get("path") if isinstance(item, dict) else None,
            "vendors": item.get("vendors") if isinstance(item, dict) else [],
        }
        for name, item in sorted(items.items())
    ]


def _session_ids_with_tools(sessions: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for session in sessions:
        runtime = session.get("runtime") if isinstance(session.get("runtime"), dict) else {}
        if _int(runtime.get("tool_calls")) <= 0:
            continue
        session_id = _session_id(session)
        if session_id:
            ids.append(session_id)
    return ids


def _session_id(session: dict[str, Any]) -> str:
    return str(session.get("root_session_id") or session.get("id") or "")


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _severity_rank(value: str) -> int:
    return {"critical": 3, "warning": 2, "info": 1}.get(value, 0)
