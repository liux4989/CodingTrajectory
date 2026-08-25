"""SessionGraph overview and narrative projections."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from coding_trajectory.analysis.activity_flow import build_overview_flows
from coding_trajectory.analysis.request_lineage import (
    effective_user_request,
    extract_user_request,
    is_low_value_turn,
)
from coding_trajectory.analysis.teammate_summary import (
    MemberSessionCandidate,
    build_member_session_lookup,
    build_teammate_summary,
    is_teammate_turn,
    merge_teammate_turn_nodes,
)
from coding_trajectory.ingestion.common import prune_nones
from coding_trajectory.ingestion.indexes import (
    SessionGraphIndex,
    build_session_graph_index,
    ordered_sessions,
)
from coding_trajectory.ingestion.models import (
    AgentMessageItem,
    Session,
    SessionGraph,
    Turn,
)

# Vendor-reported compaction observation kinds. Codex emits
# ``context_compacted`` (full eviction, no pre/post delta in the event); Claude
# Code emits ``claude_compact_boundary`` (full eviction with pre/post/trigger
# metadata).
_COMPACTION_KINDS = frozenset({"context_compacted", "claude_compact_boundary"})

# Map provider observation kinds to a compaction mechanism label, mirrored from
# ``metrics/context_stats/_common`` so overview activity entries carry the same
# discriminator as the stats/usage payloads. ``eviction_boundary`` (Claude Code)
# carries pre/post/dropped/trigger; ``context_compacted`` (Codex) does not.
_COMPACTION_MECHANISMS = {
    "claude_compact_boundary": "eviction_boundary",
    "context_compacted": "context_compacted",
}


def build_session_graph_overview(
    session_graph: SessionGraph,
    *,
    num_turns: int | None = None,
    drop_turns: int | None = None,
    index: SessionGraphIndex | None = None,
) -> dict[str, Any]:
    index = index or build_session_graph_index(session_graph)
    member_session_lookup = build_member_session_lookup(session_graph)

    ordered: list[dict[str, Any]] = []
    for session in ordered_sessions(index):
        if not _include_session_in_overview(session, index=index):
            continue
        node = _session_nav_node(
            session,
            index=index,
            member_session_lookup=member_session_lookup,
            num_turns=num_turns,
            drop_turns=drop_turns,
        )
        if node is not None:
            ordered.append(node)

    return {
        "root_session_id": str(session_graph.root_session_id),
        "sessions": ordered,
    }


def session_graph_has_visible_overview_content(session_graph: SessionGraph) -> bool:
    index = build_session_graph_index(session_graph)
    member_session_lookup = build_member_session_lookup(session_graph)
    return any(
        _include_session_in_overview(session, index=index)
        and bool(
            build_session_overview_turns(
                session,
                index=index,
                member_session_lookup=member_session_lookup,
            )
        )
        for session in ordered_sessions(index)
    )


def build_session_graph_narrative(
    session_graph: SessionGraph,
    *,
    num_turns: int | None = None,
    drop_turns: int | None = None,
    index: SessionGraphIndex | None = None,
) -> dict[str, Any]:
    if index is None:
        index = build_session_graph_index(session_graph)

    ordered = [
        _session_narrative_node(
            session,
            index=index,
            num_turns=num_turns,
            drop_turns=drop_turns,
        )
        for session in ordered_sessions(index)
    ]

    return {
        "root_session_id": str(session_graph.root_session_id),
        "sessions": ordered,
    }


def _apply_turn_window(
    turns: list[dict[str, Any]],
    *,
    num_turns: int | None = None,
    drop_turns: int | None = None,
) -> list[dict[str, Any]]:
    if drop_turns is not None:
        turns = turns[:-drop_turns]
    if num_turns is not None:
        turns = turns[-num_turns:]
    return turns


def _session_connection(
    session: Session, *, index: SessionGraphIndex
) -> dict[str, Any]:
    parent = index.parent.get(session.session_id)
    edge_type = index.incoming_edge_type.get(session.session_id)
    forked_session_ids = [
        str(child_id)
        for child_id in index.children.get(session.session_id, [])
        if index.incoming_edge_type.get(child_id) == "forked_from"
    ]
    if parent is None and not edge_type:
        return prune_nones(
            {
                "role": "main",
                "forked_session_ids": forked_session_ids or None,
            }
        )
    return prune_nones(
        {
            "relationship": edge_type,
            "parent_session_id": str(parent) if parent else None,
            "forked_session_ids": forked_session_ids or None,
        }
    )


def _include_session_in_overview(session: Session, *, index: SessionGraphIndex) -> bool:
    return index.incoming_edge_type.get(session.session_id) != "spawned_subagent"


def _session_narrative_node(
    session: Session,
    *,
    index: SessionGraphIndex,
    num_turns: int | None = None,
    drop_turns: int | None = None,
) -> dict[str, Any]:
    turns = [
        turn_node
        for turn in session.turns
        if (turn_node := _turn_narrative_node(turn, session=session, index=index))
        is not None
    ]
    turns = _apply_turn_window(turns, num_turns=num_turns, drop_turns=drop_turns)

    return prune_nones(
        {
            "session_id": str(session.session_id),
            "relationship": _session_connection(session, index=index),
            "vendor": session.vendor.value,
            "status": session.status,
            "latest_turn_status": session.latest_turn_status,
            "agent_name": session.agent_name,
            "cwd": session.cwd,
            "turns": turns,
        }
    )


def _turn_narrative_node(
    turn: Turn,
    *,
    session: Session,
    index: SessionGraphIndex,
) -> dict[str, Any] | None:
    user_request = extract_user_request(index, turn, session=session)
    if is_low_value_turn(turn.items, user_request):
        return None

    assistant_responses: list[str] = []
    item_ids: list[str] = []
    for item in turn.items:
        item_ids.append(str(item.item_id))
        if isinstance(item, AgentMessageItem) and item.text:
            assistant_responses.append(item.text)

    return prune_nones(
        {
            "turn_id": str(turn.turn_id),
            "sequence": turn.sequence,
            "started_at": turn.started_at,
            "ended_at": turn.ended_at,
            "status": turn.status,
            "user_request": user_request,
            "assistant_responses": assistant_responses or None,
            "refs": {
                "item_ids": item_ids or None,
                "user_request_event_id": str(turn.user_request_event_id)
                if turn.user_request_event_id
                else None,
            },
        }
    )


def _session_nav_node(
    session: Session,
    *,
    index: SessionGraphIndex,
    member_session_lookup: dict[str, list[MemberSessionCandidate]],
    num_turns: int | None = None,
    drop_turns: int | None = None,
) -> dict[str, Any] | None:
    turns = build_session_overview_turns(
        session,
        index=index,
        member_session_lookup=member_session_lookup,
        num_turns=num_turns,
        drop_turns=drop_turns,
    )
    if not turns:
        return None

    compaction_activities = _compaction_activities_for_session(session)
    if compaction_activities:
        turns = _attach_compaction_activities(turns, compaction_activities, session)

    return prune_nones(
        {
            "session_id": str(session.session_id),
            "relationship": _session_connection(session, index=index),
            "vendor": session.vendor.value,
            "status": session.status,
            "latest_turn_status": session.latest_turn_status,
            "agent_name": session.agent_name,
            "cwd": session.cwd,
            "compactions": len(compaction_activities)
            if compaction_activities
            else None,
            "turns": turns,
        }
    )


def _compaction_activities_for_session(session: Session) -> list[dict[str, Any]]:
    """Build activity entries for compaction observations on a session.

    Each entry mirrors the shape emitted by ``build_overview_flows`` so the
    overview renderer can treat it like any other activity. Compactions have a
    timestamp but no turn id; they are matched to a turn by timestamp interval
    in ``_attach_compaction_activities``. Unmatched compactions (timestamp
    outside every turn interval — e.g. before the first turn or after the last)
    are returned with ``turn_id=None`` so the caller can decide where to drop
    them.
    """
    activities: list[dict[str, Any]] = []
    for observation in session.runtime_observations:
        if observation.kind not in _COMPACTION_KINDS:
            continue
        pre = observation.pre_tokens
        post = observation.post_tokens
        trigger = observation.trigger
        if pre is not None and post is not None:
            delta = f"{_format_tokens(pre)} → {_format_tokens(post)}"
            dropped = pre - post
            summary = (
                f"{trigger or 'auto'}, {delta} ({_format_tokens(dropped)} dropped)"
            )
        elif observation.cumulative_dropped_tokens is not None:
            summary = f"{trigger or 'auto'}, {_format_tokens(observation.cumulative_dropped_tokens)} dropped"
        else:
            summary = trigger or "compaction"
        activities.append(
            {
                "compaction": True,
                "mechanism": _COMPACTION_MECHANISMS.get(
                    observation.kind, observation.kind
                ),
                "timestamp": observation.timestamp,
                "trigger": trigger,
                "pre_tokens": pre,
                "post_tokens": post,
                "dropped_tokens": (
                    pre - post
                    if pre is not None and post is not None
                    else observation.cumulative_dropped_tokens
                ),
                "summary": summary,
            }
        )
    return activities


def _attach_compaction_activities(
    turns: list[dict[str, Any]],
    compaction_activities: list[dict[str, Any]],
    session: Session,
) -> list[dict[str, Any]]:
    """Splice compaction activity entries into the turn whose ``[started_at,
    ended_at]`` interval contains the compaction timestamp.

    Compactions are appended to the turn's activity list (after existing
    tool/assistant entries) so the ordering of the original flow is preserved.
    The session's original turns (with ``started_at``/``ended_at``) are used
    to build the timestamp windows — the overview turn nodes themselves don't
    carry timing fields.
    """
    if not compaction_activities:
        return turns

    # Map visible overview turn ids back to their original timing on the
    # session so compaction timestamps can be matched by interval.
    turn_timing: dict[str, tuple[datetime | None, datetime | None]] = {}
    for turn in session.turns:
        turn_timing[str(turn.turn_id)] = (turn.started_at, turn.ended_at)
    turn_windows = [
        (
            turn_timing.get(str(turn.get("turn_id") or "")) or (None, None),
            str(turn.get("turn_id") or ""),
        )
        for turn in turns
    ]

    for activity in compaction_activities:
        ts = activity.get("timestamp")
        if not isinstance(ts, datetime):
            continue
        target_turn_id = _match_turn_for_timestamp(ts, turn_windows)
        if target_turn_id is None:
            continue
        for turn in turns:
            if str(turn.get("turn_id") or "") == target_turn_id:
                activity_list = turn.setdefault("activity", [])
                activity_list.append(
                    {k: v for k, v in activity.items() if k != "timestamp"}
                )
                break
    return turns


def _match_turn_for_timestamp(
    timestamp: datetime,
    turn_windows: list[tuple[tuple[datetime | None, datetime | None], str]],
) -> str | None:
    """Return the turn id whose ``[started_at, ended_at]`` interval contains
    ``timestamp``. Falls back to the nearest turn by timestamp when no
    interval matches (e.g. the compaction observation landed between turns).
    """
    for (started, ended), turn_id in turn_windows:
        if started is None:
            continue
        if ended is None:
            if timestamp >= started:
                return turn_id
            continue
        if started <= timestamp <= ended:
            return turn_id
    # Nearest-turn fallback: pick the turn whose started_at is closest to the
    # compaction timestamp. Keeps compactions visible when the log's turn
    # boundaries don't bracket the observation (common with Codex).
    best_turn_id: str | None = None
    best_delta: float | None = None
    for (started, _ended), turn_id in turn_windows:
        if started is None:
            continue
        delta = abs((timestamp - started).total_seconds())
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best_turn_id = turn_id
    return best_turn_id


def _format_tokens(value: int | None) -> str:
    if value is None:
        return "-"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def build_session_overview_turns(
    session: Session,
    *,
    index: SessionGraphIndex,
    member_session_lookup: dict[str, list[MemberSessionCandidate]],
    num_turns: int | None = None,
    drop_turns: int | None = None,
) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    pending_teammate: dict[str, Any] | None = None

    for turn in session.turns:
        node = _turn_nav_node(
            turn,
            session=session,
            index=index,
            member_session_lookup=member_session_lookup,
        )
        if node is None:
            continue

        if "teammate_summary" not in node:
            if pending_teammate is not None:
                turns.append(pending_teammate)
                pending_teammate = None
            turns.append(node)
            continue

        request = node.get("user_request")
        request_source = request.get("source") if isinstance(request, dict) else None
        if pending_teammate is None:
            pending_teammate = node
            continue

        if request_source == "human_user":
            turns.append(pending_teammate)
            pending_teammate = node
            continue

        pending_teammate = merge_teammate_turn_nodes(pending_teammate, node)

    if pending_teammate is not None:
        turns.append(pending_teammate)
    return _apply_turn_window(turns, num_turns=num_turns, drop_turns=drop_turns)


def _turn_nav_node(
    turn: Turn,
    *,
    session: Session,
    index: SessionGraphIndex,
    member_session_lookup: dict[str, list[MemberSessionCandidate]],
) -> dict[str, Any] | None:
    user_request = effective_user_request(index, turn, session=session)
    visible_user_request = user_request
    if isinstance(user_request, dict) and user_request.get("source") == "team_lead":
        visible_user_request = None
    if is_low_value_turn(turn.items, user_request):
        return None
    if is_teammate_turn(session, turn, user_request=user_request):
        return prune_nones(
            {
                "turn_id": str(turn.turn_id),
                "sequence": turn.sequence,
                "started_at": turn.started_at,
                "ended_at": turn.ended_at,
                "status": turn.status,
                "user_request": visible_user_request,
                "teammate_summary": build_teammate_summary(
                    turn,
                    user_request=user_request,
                    member_session_lookup=member_session_lookup,
                ),
            }
        )
    return prune_nones(
        {
            "turn_id": str(turn.turn_id),
            "sequence": turn.sequence,
            "started_at": turn.started_at,
            "ended_at": turn.ended_at,
            "status": turn.status,
            "user_request": user_request,
            "activity": build_overview_flows(turn.items),
            "refs": {
                "item_ids": [str(item.item_id) for item in turn.items],
                "user_request_event_id": str(turn.user_request_event_id)
                if turn.user_request_event_id
                else None,
            },
        }
    )
