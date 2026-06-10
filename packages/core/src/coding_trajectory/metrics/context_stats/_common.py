"""Shared helpers for per-vendor session context stats."""

from __future__ import annotations

from typing import Any

from coding_trajectory.ingestion.models import EventType, Session, SessionGraph
from coding_trajectory.metrics.models import (
    MessageStatsFlat,
    RuntimeStatsFlat,
    TokenUsage,
)
from coding_trajectory.metrics.pricing import get_model_context_window


def root_session(session_graph: SessionGraph) -> Session:
    for session in session_graph.sessions:
        if session.session_id == session_graph.root_session_id:
            return session
    return session_graph.sessions[0]


def runtime_stats(session_graph: SessionGraph) -> RuntimeStatsFlat:
    started = min((session.started_at for session in session_graph.sessions), default=None)
    ended = max(
        (session.ended_at or session.started_at for session in session_graph.sessions),
        default=None,
    )
    status_value = root_session(session_graph).status
    status = status_value.value if status_value else None
    tool_calls = sum(
        1
        for session in session_graph.sessions
        for event in session.events
        if event.type == EventType.TOOL_CALL_REQUESTED
    )
    failed_tool_calls = sum(
        1
        for session in session_graph.sessions
        for event in session.events
        if event.type == EventType.TOOL_CALL_FAILED
    )
    compactions = sum(
        1
        for session in session_graph.sessions
        for observation in session.runtime_observations
        if observation.kind == "context_compacted"
    )
    runtime_observations = [
        observation
        for session in session_graph.sessions
        for observation in session.runtime_observations
    ]
    observed_durations = [
        observation.duration_ms
        for observation in runtime_observations
        if observation.kind in {"turn_completed", "turn_aborted"}
        and observation.duration_ms is not None
    ]
    first_token_durations = [
        observation.time_to_first_token_ms
        for observation in runtime_observations
        if observation.kind == "turn_completed"
        and observation.time_to_first_token_ms is not None
    ]
    return RuntimeStatsFlat(
        status=status,
        started_at=started,
        ended_at=ended,
        duration_seconds=round((ended - started).total_seconds()) if started and ended else None,
        turns=sum(len(session.turns) for session in session_graph.sessions),
        items=sum(len(turn.items) for session in session_graph.sessions for turn in session.turns),
        tool_calls=tool_calls,
        failed_tool_calls=failed_tool_calls,
        subagent_sessions=sum(
            1 for session in session_graph.sessions if session.parent_session_id is not None
        ),
        compactions=compactions,
        interrupted_turns=sum(
            1 for observation in runtime_observations if observation.kind == "turn_aborted"
        ),
        rollbacks=sum(
            observation.num_turns or 0
            for observation in runtime_observations
            if observation.kind == "thread_rolled_back"
        ),
        observed_turn_duration_ms=sum(observed_durations) if observed_durations else None,
        average_time_to_first_token_ms=(
            round(sum(first_token_durations) / len(first_token_durations))
            if first_token_durations
            else None
        ),
    )


def message_stats(session_graph: SessionGraph) -> MessageStatsFlat:
    return MessageStatsFlat(
        user=sum(
            1
            for session in session_graph.sessions
            for event in session.events
            if event.type == EventType.USER_PROMPT_SUBMITTED
        ),
        assistant=sum(
            1
            for session in session_graph.sessions
            for event in session.events
            if event.type == EventType.LLM_RESPONSE
        ),
        developer=sum(
            1
            for session in session_graph.sessions
            for _source in session.context_sources
        ),
        tool_outputs=sum(
            1
            for session in session_graph.sessions
            for event in session.events
            if event.type in {EventType.TOOL_CALL_SUCCEEDED, EventType.TOOL_CALL_FAILED}
        ),
        reasoning_items=sum(
            1
            for session in session_graph.sessions
            for observation in session.runtime_observations
            if observation.kind == "reasoning"
        ),
        compacted_contexts=sum(
            1
            for session in session_graph.sessions
            for observation in session.runtime_observations
            if observation.kind == "context_compacted"
        ),
    )


def percent(value: int, denominator: int | None) -> float | None:
    if not denominator or denominator <= 0:
        return None
    return round((value / denominator) * 100, 1)


def model_context_window(model: str | None, *, provider: str | None = None) -> int | None:
    """Lookup a model's context window via the cached models.dev catalog."""
    return get_model_context_window(model, provider=provider)


def token_usage_from_mapping(value: dict[str, Any] | None) -> TokenUsage:
    if not isinstance(value, dict):
        return TokenUsage()
    return TokenUsage(
        input_tokens=_as_int(value.get("input_tokens") or value.get("inputTokens")),
        cached_input_tokens=_as_int(value.get("cached_input_tokens") or value.get("cachedInputTokens")),
        cache_creation_input_tokens=_as_int(
            value.get("cache_creation_input_tokens") or value.get("cacheCreationInputTokens")
        ),
        output_tokens=_as_int(value.get("output_tokens") or value.get("outputTokens")),
        reasoning_output_tokens=_as_int(
            value.get("reasoning_output_tokens") or value.get("reasoningOutputTokens")
        ),
        total_tokens=_as_int(value.get("total_tokens") or value.get("totalTokens")),
    )


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
