"""Shared helpers for per-vendor session context stats."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from coding_trajectory.analysis.request_lineage import is_low_value_turn
from coding_trajectory.ingestion.models import EventType, Session, SessionGraph
from coding_trajectory.metrics.models import (
    CompactionEventFlat,
    CompactionStatsFlat,
    EffortChangeEventFlat,
    EffortChangeStatsFlat,
    MessageStatsFlat,
    RuntimeStatsFlat,
    SessionMetrics,
    TokenUsage,
)
from coding_trajectory.metrics.throughput import processed_tokens_per_second


def root_session(session_graph: SessionGraph) -> Session:
    for session in session_graph.sessions:
        if session.session_id == session_graph.root_session_id:
            return session
    return session_graph.sessions[0]


# Evicting-compaction observation kinds. Codex emits ``context_compacted`` (a
# full eviction with no pre/post delta in the event); Claude Code emits
# ``claude_compact_boundary`` (a full eviction). Both count as a compaction for
# stats — previously only Codex's kind was counted, so Claude Code sessions
# reported 0 compactions despite showing a ``Compacted history`` composition row.
_COMPACTION_KINDS = frozenset({"context_compacted", "claude_compact_boundary"})

# Map provider observation kinds to a compaction mechanism label. Same concept,
# different mechanisms: Claude Code's ``claude_compact_boundary`` is a discrete
# eviction (preserved messages + dropped totals); Codex's ``context_compacted``
# carries no eviction metadata in the event. The label drives per-provider
# rendering so a bare Codex compaction doesn't show as empty pre→post / dropped
# cells. Mirrored in ``analysis/session_graph_views.py`` for overview activities.
_COMPACTION_MECHANISMS = {
    "claude_compact_boundary": "eviction_boundary",
    "context_compacted": "context_compacted",
}


def runtime_stats(
    session_graph: SessionGraph,
    *,
    session_metrics: Iterable[SessionMetrics],
) -> RuntimeStatsFlat:
    """Return primary-session runtime plus graph-wide complexity counts.

    Subagent turn intervals commonly overlap the primary session while the
    primary agent waits for their results. Adding those durations reports
    agent-seconds, not the elapsed execution experienced by the user. Runtime
    duration and latency fields therefore come exclusively from the graph's
    root session. Structural counts below remain graph-wide by design.
    """
    primary = root_session(session_graph)
    primary_metrics = next(
        (
            metrics
            for metrics in session_metrics
            if metrics.session_id == primary.session_id
        ),
        None,
    )
    if primary_metrics is None:
        raise ValueError("runtime metrics omit the graph's root session")
    started = primary.started_at
    ended = primary.ended_at or primary.started_at
    status_value = primary.status
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
        if observation.kind in _COMPACTION_KINDS
    )
    runtime_observations = [
        observation
        for session in session_graph.sessions
        for observation in session.runtime_observations
    ]
    execution_seconds = sum(
        value
        for turn in primary.turns
        if (value := _turn_duration_seconds(turn)) is not None
    )
    wait_seconds = sum(_turn_wait_seconds(primary))
    model_active = primary_metrics.model_active_seconds
    processed_tokens = primary_metrics.token_usage.processed_token_total()
    first_token_durations = [
        observation.time_to_first_token_ms
        for observation in primary.runtime_observations
        if observation.kind == "turn_completed"
        and observation.time_to_first_token_ms is not None
    ]
    return RuntimeStatsFlat(
        status=status,
        started_at=started,
        ended_at=ended,
        execution_seconds=execution_seconds,
        model_active_seconds=model_active,
        processed_tokens_per_second=processed_tokens_per_second(
            processed_tokens or 0,
            model_active,
        ),
        wait_seconds=wait_seconds,
        # Exclude low-value turns (no items, e.g. a compaction-only lifecycle)
        # so the count matches `session overview`, which filters them via
        # is_low_value_turn. Codex wraps a context compaction in its own
        # task_started/task_complete pair; the projector keeps that empty turn
        # (so overview can attach the compaction as activity) but it is not a
        # real executed turn.
        turns=sum(
            1
            for session in session_graph.sessions
            for turn in session.turns
            if not is_low_value_turn(turn.items, None)
        ),
        items=sum(
            len(turn.items)
            for session in session_graph.sessions
            for turn in session.turns
        ),
        tool_calls=tool_calls,
        failed_tool_calls=failed_tool_calls,
        subagent_sessions=sum(
            1
            for session in session_graph.sessions
            if session.parent_session_id is not None
        ),
        compactions=compactions,
        interrupted_turns=sum(
            1
            for observation in runtime_observations
            if observation.kind == "turn_aborted"
        ),
        rollbacks=sum(
            observation.num_turns or 0
            for observation in runtime_observations
            if observation.kind == "thread_rolled_back"
        ),
        average_time_to_first_token_ms=(
            round(sum(first_token_durations) / len(first_token_durations))
            if first_token_durations
            else None
        ),
    )


def compaction_stats(session_graph: SessionGraph) -> CompactionStatsFlat | None:
    """Aggregate compaction observations across a session graph.

    Returns ``None`` when the session never compacted. Claude Code's
    ``claude_compact_boundary`` carries pre/post/dropped/trigger metadata;
    Codex's ``context_compacted`` does not, so pre/post/dropped are derived
    from the bracketing ``context_usage`` observations (per-call input token
    count) instead of staying ``None``.
    """
    # Pair each compaction with its own session's context_usage observations so
    # Codex compactions can derive pre/post from the per-call
    # input token count before/after the eviction.
    entries: list[tuple[Any, list[Any]]] = []
    for session in session_graph.sessions:
        context_obs = [
            obs
            for obs in sorted(session.context_usage, key=lambda o: o.timestamp)
            if obs.used_input_tokens > 0
        ]
        for observation in session.runtime_observations:
            if observation.kind in _COMPACTION_KINDS:
                entries.append((observation, context_obs))
    entries.sort(key=lambda entry: entry[0].timestamp)
    if not entries:
        return None
    # ``cumulative_dropped_tokens`` is cumulative (Claude Code reports the
    # running total per compaction), so the latest non-None value is the total.
    cumulative = next(
        (
            observation.cumulative_dropped_tokens
            for observation, _ in reversed(entries)
            if observation.cumulative_dropped_tokens is not None
        ),
        None,
    )
    events = [
        _event_from_observation(observation, context_obs)
        for observation, context_obs in entries
    ]
    # When no vendor-reported cumulative (Codex), derive it from per-event drops.
    if cumulative is None:
        cumulative = (
            sum(
                event.dropped_tokens
                for event in events
                if event.dropped_tokens is not None
            )
            or None
        )
    # ``last`` mirrors the final timeline entry; deriving it from ``events``
    # keeps the two in sync instead of constructing the same object twice.
    return CompactionStatsFlat(
        count=len(entries),
        cumulative_dropped_tokens=cumulative,
        last=events[-1],
        events=events,
    )


def _event_from_observation(
    observation: Any,
    context_observations: list[Any] | None = None,
) -> CompactionEventFlat:
    pre = observation.pre_tokens
    post = observation.post_tokens
    dropped = (
        pre - post
        if pre is not None and post is not None
        else observation.cumulative_dropped_tokens
    )
    # Codex compactions carry no eviction metadata in the event; derive
    # pre/post from the bracketing context_usage observations (the per-call
    # input token count). The last call before compaction is the
    # pre-compaction context size; the first call after is the post size.
    if pre is None and context_observations:
        pre = _nearest_context_tokens(
            context_observations, observation.timestamp, before=True
        )
    if post is None and context_observations:
        post = _nearest_context_tokens(
            context_observations, observation.timestamp, before=False
        )
    if dropped is None and pre is not None and post is not None:
        dropped = max(pre - post, 0)
    return CompactionEventFlat(
        timestamp=observation.timestamp,
        mechanism=_COMPACTION_MECHANISMS.get(observation.kind, observation.kind),
        trigger=observation.trigger,
        pre_tokens=pre,
        post_tokens=post,
        dropped_tokens=dropped,
    )


def _nearest_context_tokens(
    observations: list[Any],
    timestamp: Any,
    *,
    before: bool,
) -> int | None:
    if before:
        for observation in reversed(observations):
            if observation.timestamp < timestamp:
                return observation.used_input_tokens or None
        return None
    for observation in observations:
        if observation.timestamp > timestamp:
            return observation.used_input_tokens or None
    return None


def effort_change_stats(session_graph: SessionGraph) -> EffortChangeStatsFlat:
    """Aggregate reasoning-effort change observations across a session graph.

    Always returns a stats object (``count=0`` when no change was observed) so
    the ``session.usage`` api always emits the ``effort_changes`` key - its
    presence is the capability marker that distinguishes a fresh ct (always
    emits, even ``{"count": 0}``) from a stale install that lacks effort-change
    ingestion entirely (key absent). Codex emits ``effort_changed`` per turn
    whose ``effort`` differs from the prior turn (``effort_from`` always set);
    Claude Code emits it on each ``/effort`` switch (``effort_from`` is ``None``
    on the first, whose baseline is unknown). The ``timestamp`` marks the turn
    using the new effort.
    """
    changes = sorted(
        (
            observation
            for session in session_graph.sessions
            for observation in session.runtime_observations
            if observation.kind == "effort_changed"
        ),
        key=lambda observation: observation.timestamp,
    )
    if not changes:
        return EffortChangeStatsFlat(count=0)
    events = [
        EffortChangeEventFlat(
            timestamp=observation.timestamp,
            effort_from=observation.effort_from,
            effort_to=observation.effort_to,
        )
        for observation in changes
    ]
    return EffortChangeStatsFlat(count=len(changes), events=events)


def message_stats(session_graph: SessionGraph) -> MessageStatsFlat:
    def _assistant_count(session: Any) -> int:
        measurements = getattr(session, "measurements", None)
        if measurements is not None:
            return measurements.llm_response_count
        return sum(
            1 for event in session.events if event.type == EventType.LLM_RESPONSE
        )

    def _developer_count(session: Any) -> int:
        measurements = getattr(session, "measurements", None)
        if measurements is not None:
            return len(measurements.context_sources)
        return len(session.context_sources)

    return MessageStatsFlat(
        user=sum(
            1
            for session in session_graph.sessions
            for event in session.events
            if event.type == EventType.USER_PROMPT_SUBMITTED
        ),
        assistant=sum(_assistant_count(session) for session in session_graph.sessions),
        developer=sum(_developer_count(session) for session in session_graph.sessions),
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
            if observation.kind in _COMPACTION_KINDS
        ),
    )


def percent(value: int, denominator: int | None) -> float | None:
    if not denominator or denominator <= 0:
        return None
    return round((value / denominator) * 100, 1)


def token_usage_from_mapping(value: dict[str, Any] | None) -> TokenUsage:
    if not isinstance(value, dict):
        return TokenUsage()
    uncached_raw = value.get("uncached_input_tokens") or value.get(
        "uncachedInputTokens"
    )
    return TokenUsage(
        input_tokens=_as_int(value.get("input_tokens") or value.get("inputTokens")),
        cached_input_tokens=_as_int(
            value.get("cached_input_tokens") or value.get("cachedInputTokens")
        ),
        cache_creation_input_tokens=_as_int(
            value.get("cache_creation_input_tokens")
            or value.get("cacheCreationInputTokens")
        ),
        output_tokens=_as_int(value.get("output_tokens") or value.get("outputTokens")),
        reasoning_output_tokens=_as_int(
            value.get("reasoning_output_tokens") or value.get("reasoningOutputTokens")
        ),
        total_tokens=_as_int(value.get("total_tokens") or value.get("totalTokens")),
        uncached_input_tokens=(
            uncached_raw
            if isinstance(uncached_raw, int) and not isinstance(uncached_raw, bool)
            else None
        ),
    )


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _turn_duration_seconds(turn: Any) -> int | None:
    started_at = getattr(turn, "started_at", None)
    ended_at = getattr(turn, "ended_at", None)
    if started_at is None or ended_at is None:
        return None
    return max(round((ended_at - started_at).total_seconds()), 0)


def _turn_wait_seconds(session: Session) -> list[int]:
    values: list[int] = []
    previous_ended_at = None
    for turn in session.turns:
        if previous_ended_at is not None and turn.started_at is not None:
            values.append(
                max(round((turn.started_at - previous_ended_at).total_seconds()), 0)
            )
        if turn.ended_at is not None:
            previous_ended_at = turn.ended_at
    return values
