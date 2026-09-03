"""One shared compact canonical boundary for remote CT observations."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from coding_trajectory.analysis.measurements import attach_measurements
from coding_trajectory.discovery import DiscoveryCandidate, stabilize_session
from coding_trajectory.ingestion.graph import build_session_graph
from coding_trajectory.ingestion.models import (
    ClaudeCodeExtensions,
    CodexExtensions,
    EventType,
    Session,
    VendorExtensions,
)
from coding_trajectory.token_counter import counter_for_session_graph, scoped_counter

_REMOTE_TOOL_EVENT_KEYS = frozenset({"tool_name"})
_MAX_REMOTE_STRING_LENGTH = 512


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
    """Project a measurements session onto the compact-v2 privacy boundary."""

    events = [
        event.model_copy(
            update={
                "actor": None,
                "payload": (
                    {"tool_name": event.payload["tool_name"]}
                    if event.type != EventType.USER_PROMPT_SUBMITTED
                    and isinstance(event.payload.get("tool_name"), str)
                    and event.payload["tool_name"]
                    else {}
                ),
            }
        )
        for event in session.events
    ]
    turns = []
    for turn in session.turns:
        items = []
        for item in turn.items:
            update: dict[str, Any] = {}
            for field in ("text", "input", "output", "command", "path", "tool_call_id"):
                if hasattr(item, field):
                    update[field] = None
            update["vendor_data"] = {}
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
                    "team_state": None,
                }
            )
        )

    measurements = session.measurements
    if measurements is not None:
        measurements = measurements.model_copy(
            update={
                "context_sources": [
                    source.model_copy(
                        update={
                            "key": f"context-source-{index}",
                            "label": f"Context source {index}",
                        }
                    )
                    for index, source in enumerate(measurements.context_sources, 1)
                ]
            }
        )
    return session.model_copy(
        update={
            "cwd": None,
            "agent_name": None,
            "events": events,
            "turns": turns,
            "measurements": measurements,
            "runtime_observations": [
                observation.model_copy(
                    update={
                        "turn_id_raw": None,
                        "trace_id": None,
                        "reason": None,
                        "trigger": None,
                    }
                )
                for observation in session.runtime_observations
            ],
            "extensions": _scrub_extensions(session),
        }
    )


def validate_remote_compact_session(session: Session) -> None:
    """Fail closed unless the session is exactly the compact-v2 projection."""

    _reject_embedded_content(session.model_dump(mode="json"))
    scrubbed = scrub_remote_session(session)
    if scrubbed.model_dump(mode="json") != session.model_dump(mode="json"):
        raise ValueError("remote compact session contains non-canonical private data")

    if (
        session.cwd is not None
        or session.agent_name is not None
        or session.context_sources
    ):
        raise ValueError(
            "remote compact session retained a host location or context body"
        )
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
        if turn.team_state is not None:
            raise ValueError("remote compact turn retained team state")
    if any(
        observation.turn_id_raw
        or observation.trace_id
        or observation.reason
        or observation.trigger
        for observation in session.runtime_observations
    ):
        raise ValueError("remote compact session retained runtime text")


def _reject_embedded_content(value: Any, *, field: str = "") -> None:
    """Reject inline content even if it appears in an otherwise allowed field."""

    if isinstance(value, dict):
        for key, child in value.items():
            normalized = key.lower().replace("-", "_")
            if child not in (None, "", [], {}) and (
                "data_uri" in normalized
                or "blob" in normalized
                or "media" in normalized
            ):
                raise ValueError(f"remote compact session retained {key}")
            _reject_embedded_content(child, field=key)
    elif isinstance(value, list):
        for child in value:
            _reject_embedded_content(child, field=field)
    elif isinstance(value, str):
        if len(value) > _MAX_REMOTE_STRING_LENGTH:
            raise ValueError(
                f"remote compact session retained unbounded string in {field}"
            )
        if value.lstrip().lower().startswith("data:"):
            raise ValueError(f"remote compact session retained data URI in {field}")


def _scrub_extensions(session: Session) -> VendorExtensions | None:
    extensions = session.extensions
    if extensions is None:
        return None
    claude = extensions.claude_code
    if claude is not None:
        claude = ClaudeCodeExtensions(
            is_sidechain=claude.is_sidechain,
            spawn_depth=claude.spawn_depth,
        )
    codex = extensions.codex
    if codex is not None:
        codex = CodexExtensions(
            forked_from_id=codex.forked_from_id,
            spawn_parent_thread_id=codex.spawn_parent_thread_id,
            spawn_depth=codex.spawn_depth,
            spawn_links={
                child_session_id: "" for child_session_id in codex.spawn_links
            },
        )
    if claude is None and codex is None:
        return None
    return VendorExtensions(claude_code=claude, codex=codex, pi=None)
