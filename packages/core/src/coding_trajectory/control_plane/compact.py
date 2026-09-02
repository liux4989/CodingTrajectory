"""One shared compact canonical boundary for remote CT observations."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from coding_trajectory.analysis.measurements import attach_measurements
from coding_trajectory.discovery import DiscoveryCandidate, stabilize_session
from coding_trajectory.ingestion.graph import build_session_graph
from coding_trajectory.ingestion.models import (
    EventType,
    Session,
    TeamMemberState,
    TeamTaskState,
    TeamTurnState,
    VendorExtensions,
)
from coding_trajectory.token_counter import counter_for_session_graph, scoped_counter

_REMOTE_TOOL_EVENT_KEYS = frozenset(
    {
        "tool_call_id",
        "tool_name",
        "name",
        "status",
        "exit_code",
        "child_session_id",
        "thread_id",
    }
)


def build_remote_compact_session(
    candidate: DiscoveryCandidate,
    *,
    source: Path,
    records: Iterable[dict[str, Any]],
) -> Session:
    """Build measurements retention and attach body-derived facts once."""

    materialized = list(records)
    full = candidate.adapter_cls().build_canonical_session(source, materialized)
    full = stabilize_session(full, vendor=candidate.vendor, source=source)
    compact = candidate.adapter_cls().build_canonical_session(
        source, materialized, retention="measurements"
    )
    graph = build_session_graph(
        root_session_id=compact.session_id,
        project_identifier="remote-compact-source",
        sessions=[compact],
    )
    with scoped_counter(counter_for_session_graph(graph)):
        attach_measurements(compact, full)
    compact = scrub_remote_session(compact)
    validate_remote_compact_session(compact)
    return compact


def scrub_remote_session(session: Session) -> Session:
    """Remove the few private fields retained by measurements mode."""

    events = [
        event.model_copy(update={"payload": {}})
        if event.type == EventType.USER_PROMPT_SUBMITTED
        else event
        for event in session.events
    ]
    turns = []
    for turn in session.turns:
        items = []
        for item in turn.items:
            update: dict[str, Any] = {}
            if hasattr(item, "path"):
                update["path"] = None
            if item.measurements is not None:
                update["measurements"] = item.measurements.model_copy(
                    update={
                        "input_summary": None,
                        "text_preview": None,
                        "tool_summary": None,
                    }
                )
            items.append(item.model_copy(update=update) if update else item)
        turns.append(
            turn.model_copy(
                update={
                    "items": items,
                    "team_state": _scrub_team_state(turn.team_state),
                }
            )
        )

    measurements = session.measurements
    if measurements is not None:
        measurements = measurements.model_copy(
            update={
                "context_sources": [
                    source.model_copy(update={"key": "", "label": ""})
                    for source in measurements.context_sources
                ]
            }
        )
    return session.model_copy(
        update={
            "cwd": None,
            "events": events,
            "turns": turns,
            "measurements": measurements,
            "runtime_observations": [
                observation.model_copy(update={"reason": None, "trigger": None})
                for observation in session.runtime_observations
            ],
            "extensions": _scrub_extensions(session),
        }
    )


def validate_remote_compact_session(session: Session) -> None:
    """Reject a compact payload if a known body or host location survived."""

    if session.cwd is not None or session.context_sources:
        raise ValueError("remote compact session retained a host location or context body")
    for event in session.events:
        if event.type == EventType.USER_PROMPT_SUBMITTED and event.payload:
            raise ValueError("remote compact session retained a user prompt")
        if event.type != EventType.USER_PROMPT_SUBMITTED and (
            event.type
            not in {
                EventType.TOOL_CALL_REQUESTED,
                EventType.TOOL_CALL_SUCCEEDED,
                EventType.TOOL_CALL_FAILED,
            }
            or set(event.payload) - _REMOTE_TOOL_EVENT_KEYS
        ):
            raise ValueError("remote compact session retained an unbounded event")
    for turn in session.turns:
        for item in turn.items:
            for field in ("text", "input", "output", "command", "path"):
                if hasattr(item, field) and getattr(item, field) is not None:
                    raise ValueError(f"remote compact item retained {field}")
            if item.vendor_data:
                raise ValueError("remote compact item retained vendor data")
            measurements = item.measurements
            if measurements is not None and any(
                value
                for value in (
                    measurements.input_summary,
                    measurements.text_preview,
                    measurements.tool_summary,
                )
            ):
                raise ValueError("remote compact item retained a content summary")
        if turn.team_state is not None and (
            any(
                member.name or member.team_name or member.summary
                for member in turn.team_state.members
            )
            or any(task.title or task.summary for task in turn.team_state.tasks)
        ):
            raise ValueError("remote compact turn retained team-state text")
    if any(
        observation.reason or observation.trigger
        for observation in session.runtime_observations
    ):
        raise ValueError("remote compact session retained runtime text")
    if session.measurements is not None and any(
        source.key or source.label for source in session.measurements.context_sources
    ):
        raise ValueError("remote compact session retained context-source labels")
    extensions = session.extensions
    if extensions is not None:
        claude = extensions.claude_code
        codex = extensions.codex
        pi = extensions.pi
        if claude is not None and any(
            (claude.description, claude.title, claude.last_prompt)
        ):
            raise ValueError("remote compact session retained Claude text")
        if codex is not None and any(
            (codex.agent_path, codex.cwd, codex.preview, codex.title)
        ):
            raise ValueError("remote compact session retained Codex location text")
        if pi is not None and any((pi.session_file, pi.cwd, pi.title)):
            raise ValueError("remote compact session retained Pi location text")


def _scrub_team_state(team_state: TeamTurnState | None) -> TeamTurnState | None:
    if team_state is None:
        return None
    return team_state.model_copy(
        update={
            "members": [
                TeamMemberState(
                    member_id=member.member_id,
                    session_id=member.session_id,
                    color=member.color,
                    agent_type=member.agent_type,
                )
                for member in team_state.members
            ],
            "tasks": [
                TeamTaskState(
                    task_id=task.task_id,
                    status=task.status,
                    member_id=task.member_id,
                    blocked_by=task.blocked_by,
                    updated_fields=task.updated_fields,
                )
                for task in team_state.tasks
            ],
        }
    )


def _scrub_extensions(session: Session) -> VendorExtensions | None:
    extensions = session.extensions
    if extensions is None:
        return None
    claude = extensions.claude_code
    if claude is not None:
        claude = claude.model_copy(
            update={
                "description": None,
                "title": None,
                "last_prompt": None,
            }
        )
    codex = extensions.codex
    if codex is not None:
        codex = codex.model_copy(
            update={"agent_path": None, "cwd": None, "preview": None, "title": None}
        )
    pi = extensions.pi
    if pi is not None:
        pi = pi.model_copy(update={"session_file": None, "cwd": None, "title": None})
    return extensions.model_copy(
        update={"claude_code": claude, "codex": codex, "pi": pi}
    )
