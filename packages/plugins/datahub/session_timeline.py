"""Chronological session evidence projected from retained canonical facts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TimelineKind = Literal["user", "assistant", "tool", "subagent", "compaction"]
ArtifactKind = Literal["file", "command", "check", "commit", "link"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimelineEntry(_StrictModel):
    id: str
    timestamp: datetime | None = None
    ended_at: datetime | None = None
    session_id: str
    turn_id: str
    turn_sequence: int = Field(ge=0)
    position: int = Field(ge=0)
    vendor: str | None = None
    agent_name: str | None = None
    kind: TimelineKind
    label: str
    summary: str | None = None
    status: str | None = None
    failed: bool = False
    artifact_kind: ArtifactKind | None = None
    item_ids: list[str] = Field(default_factory=list)
    event_ids: list[str] = Field(default_factory=list)
    target_session_id: str | None = None


class SessionEvidenceTimeline(_StrictModel):
    schema_version: Literal[1] = 1
    revision: int = Field(ge=0)
    root_session_id: str
    entrypoint_session_id: str
    entries: list[TimelineEntry] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def build_session_evidence_timeline(
    overview_payloads: Sequence[Mapping[str, Any]],
    *,
    revision: int,
    root_session_id: str,
    entrypoint_session_id: str,
    graph_overview: Mapping[str, Any] | None = None,
) -> SessionEvidenceTimeline:
    """Flatten retained overview turns without inventing evidence or timing."""

    target_by_item = _target_sessions_by_item(graph_overview or {})
    sessions: dict[str, Mapping[str, Any]] = {}
    for payload in overview_payloads:
        for raw_session in payload.get("sessions") or []:
            if not isinstance(raw_session, Mapping):
                continue
            session_id = str(raw_session.get("session_id") or "")
            if session_id:
                sessions[session_id] = raw_session

    entries: list[TimelineEntry] = []
    warnings: list[str] = []
    for session_id, session in sessions.items():
        vendor = _text(session.get("vendor"))
        agent_name = _text(session.get("agent_name"))
        for raw_turn in session.get("turns") or []:
            if not isinstance(raw_turn, Mapping):
                continue
            turn_id = str(raw_turn.get("turn_id") or "")
            if not turn_id:
                warnings.append(f"session {session_id} contains a turn without an id")
                continue
            common = {
                "timestamp": raw_turn.get("started_at"),
                "ended_at": raw_turn.get("ended_at"),
                "session_id": session_id,
                "turn_id": turn_id,
                "turn_sequence": int(raw_turn.get("sequence") or 0),
                "vendor": vendor,
                "agent_name": agent_name,
            }
            request = raw_turn.get("user_request")
            if isinstance(request, Mapping):
                refs = raw_turn.get("refs")
                event_id = _text(
                    request.get("event_id")
                    or (
                        refs.get("user_request_event_id")
                        if isinstance(refs, Mapping)
                        else None
                    )
                )
                entries.append(
                    TimelineEntry(
                        id=f"{session_id}:{turn_id}:request",
                        position=0,
                        kind="user",
                        label="User request",
                        summary=_request_summary(request),
                        status=_text(raw_turn.get("status")),
                        event_ids=[event_id] if event_id else [],
                        **common,
                    )
                )
            for position, activity in enumerate(raw_turn.get("activity") or [], 1):
                if not isinstance(activity, Mapping):
                    continue
                item_ids = [
                    str(value) for value in activity.get("item_ids") or [] if value
                ]
                target = next(
                    (
                        target_by_item[item_id]
                        for item_id in item_ids
                        if item_id in target_by_item
                    ),
                    None,
                )
                kind = _activity_kind(activity, target_session_id=target)
                artifact_kind = _artifact_kind(activity) if kind == "tool" else None
                status = _text(
                    activity.get("status")
                    or activity.get("outcome")
                    or activity.get("wrapper_status")
                    or raw_turn.get("status")
                )
                entries.append(
                    TimelineEntry(
                        id=f"{session_id}:{turn_id}:{position}",
                        position=position,
                        kind=kind,
                        label=_activity_label(
                            activity,
                            kind=kind,
                            artifact_kind=artifact_kind,
                        ),
                        summary=_activity_summary(activity),
                        status=status,
                        failed=_failed(status),
                        artifact_kind=artifact_kind,
                        item_ids=item_ids,
                        target_session_id=target,
                        **common,
                    )
                )

    entries.sort(
        key=lambda entry: (
            entry.timestamp is None,
            entry.timestamp.isoformat() if entry.timestamp else "",
            entry.session_id,
            entry.turn_sequence,
            entry.position,
        )
    )
    return SessionEvidenceTimeline(
        revision=revision,
        root_session_id=root_session_id,
        entrypoint_session_id=entrypoint_session_id,
        entries=entries,
        warnings=list(dict.fromkeys(warnings)),
    )


def _target_sessions_by_item(graph_overview: Mapping[str, Any]) -> dict[str, str]:
    targets: dict[str, str] = {}
    for edge in graph_overview.get("edges") or []:
        if not isinstance(edge, Mapping):
            continue
        source_item_id = _text(edge.get("source_item_id"))
        target_session_id = _text(edge.get("target_session_id"))
        if source_item_id and target_session_id:
            targets[source_item_id] = target_session_id
    return targets


def _activity_kind(
    activity: Mapping[str, Any], *, target_session_id: str | None
) -> TimelineKind:
    if target_session_id:
        return "subagent"
    if activity.get("compaction") is True:
        return "compaction"
    if _text(activity.get("text")):
        return "assistant"
    return "tool"


def _activity_label(
    activity: Mapping[str, Any],
    *,
    kind: TimelineKind,
    artifact_kind: ArtifactKind | None,
) -> str:
    if kind == "assistant":
        return "Assistant response"
    if kind == "subagent":
        return "Subagent activity"
    if kind == "compaction":
        return "Context compaction"
    if artifact_kind == "check":
        return "Check run"
    if artifact_kind == "commit":
        return "Git commit"
    return _text(activity.get("tool")) or "Tool activity"


def _request_summary(request: Mapping[str, Any]) -> str | None:
    return _text(
        request.get("content")
        or request.get("text")
        or request.get("summary")
        or request.get("title")
    )


def _activity_summary(activity: Mapping[str, Any]) -> str | None:
    direct = _text(
        activity.get("summary")
        or activity.get("description")
        or activity.get("command")
        or activity.get("cmd")
        or activity.get("path")
        or activity.get("url")
        or activity.get("text")
    )
    if direct:
        return direct
    for key in ("descriptions", "paths", "commands", "queries", "urls"):
        values = activity.get(key)
        if isinstance(values, list):
            text = ", ".join(str(value) for value in values[:4] if value)
            if text:
                return text
    return None


def _artifact_kind(activity: Mapping[str, Any]) -> ArtifactKind | None:
    tool = (_text(activity.get("tool")) or "").casefold()
    if tool in {"editfile", "writefile"}:
        return "file"
    if tool == "webfetch":
        return "link"
    if tool != "runcommand":
        return None
    command = _text(
        activity.get("cmd")
        or activity.get("command")
        or " && ".join(str(value) for value in activity.get("commands") or [])
    )
    if command is None:
        return "command"
    normalized = " ".join(command.casefold().split())
    if "git commit" in normalized:
        return "commit"
    if any(
        marker in normalized
        for marker in (
            "pytest",
            "ruff check",
            "py_compile",
            "mypy",
            "tsc ",
            "tsc -",
            "bun run build",
            "npm run build",
            "npm test",
            "pnpm test",
            "cargo test",
            "go test",
            "validate-metrics-baselines.py",
            "check-metrics-quality-gate.sh",
        )
    ):
        return "check"
    return "command"


def _failed(status: str | None) -> bool:
    return bool(
        status and any(value in status.casefold() for value in ("fail", "error"))
    )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "ArtifactKind",
    "SessionEvidenceTimeline",
    "TimelineEntry",
    "build_session_evidence_timeline",
]
