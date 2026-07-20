"""User-request extraction and lineage helpers for analysis views."""

from __future__ import annotations

from datetime import datetime
import re
from uuid import UUID

from coding_trajectory.ingestion.indexes import (
    SessionGraphIndex,
    event_for_turn_user_request,
    incoming_edge,
)
from coding_trajectory.ingestion.models import Session, Turn

_COMMAND_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)

_LOW_VALUE_COMMANDS: frozenset[str] = frozenset(
    {
        "/clear",
        "/reset",
        "/new",
        "/compact",
        "/context",
        "/cost",
        "/usage",
        "/stats",
        "/exit",
        "/quit",
        "/help",
        "/release-notes",
        "/config",
        "/settings",
        "/model",
        "/fast",
        "/effort",
        "/vim",
        "/theme",
        "/color",
        "/statusline",
        "/keybindings",
        "/terminal-setup",
        "/login",
        "/logout",
        "/status",
        "/stickers",
        "/mobile",
        "/ios",
        "/android",
        "/upgrade",
        "/privacy-settings",
        "/personality",
        "/debug-config",
    }
)


def extract_user_request(
    index: SessionGraphIndex,
    turn: Turn,
    *,
    session: Session | None = None,
) -> dict[str, str] | None:
    event = event_for_turn_user_request(index, turn)
    if event is None:
        return None
    team_request_summary = event.payload.get("team_request_summary")
    if isinstance(team_request_summary, str) and team_request_summary.strip():
        source = (
            "parent_agent"
            if session and session.parent_session_id is not None
            else "team_lead"
        )
        return {
            "type": "message",
            "source": source,
            "content": team_request_summary.strip(),
        }
    for key in ("text", "message", "content"):
        value = event.payload.get(key)
        if isinstance(value, str) and value.strip():
            return _parse_user_request_info(value)
    return None


def latest_human_user_request(
    index: SessionGraphIndex,
    session: Session,
    *,
    before: datetime | None = None,
) -> dict[str, str] | None:
    turns = sorted(
        session.turns, key=lambda item: (item.started_at, item.sequence), reverse=True
    )
    for turn in turns:
        if before is not None and turn.started_at > before:
            continue
        request = extract_user_request(index, turn, session=session)
        if request and request.get("source") == "human_user":
            return request
    return None


def resolve_originating_human_request(
    index: SessionGraphIndex,
    session: Session,
) -> dict[str, str] | None:
    current_session = session
    visited: set[UUID] = set()
    cutoff: datetime | None = None

    while current_session.session_id not in visited:
        visited.add(current_session.session_id)
        parent_id = index.parent.get(current_session.session_id)
        if parent_id is None:
            return None
        parent_session = index.sessions_by_id.get(parent_id)
        if parent_session is None:
            return None

        edge = incoming_edge(index, current_session.session_id)
        if edge is not None and edge.source_turn_id is not None:
            source_turn = index.turns_by_id.get(edge.source_turn_id)
            if source_turn is not None:
                request = extract_user_request(
                    index, source_turn, session=parent_session
                )
                if request and request.get("source") == "human_user":
                    return request
                cutoff = source_turn.started_at
            else:
                cutoff = current_session.started_at
        else:
            cutoff = current_session.started_at

        request = latest_human_user_request(index, parent_session, before=cutoff)
        if request is not None:
            return request
        current_session = parent_session

    return None


def effective_user_request(
    index: SessionGraphIndex,
    turn: Turn,
    *,
    session: Session,
) -> dict[str, str] | None:
    request = extract_user_request(index, turn, session=session)
    if request is None:
        return None
    if request.get("source") != "parent_agent":
        return request
    return resolve_originating_human_request(index, session)


def is_low_value_turn(items: list, user_request: dict[str, str] | None) -> bool:
    if not items:
        return True
    if user_request and user_request.get("type") == "command":
        return user_request.get("content") in _LOW_VALUE_COMMANDS
    return False


def _parse_user_request_info(raw: str) -> dict[str, str] | None:
    match = _COMMAND_NAME_RE.search(raw)
    if match:
        return {
            "type": "command",
            "source": "human_user",
            "content": match.group(1).strip(),
        }
    stripped = raw.strip()
    if not stripped:
        return None
    return {"type": "message", "source": "human_user", "content": stripped}
