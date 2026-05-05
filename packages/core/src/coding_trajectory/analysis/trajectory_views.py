"""Trajectory overview and narrative projections."""

from __future__ import annotations

from typing import Any

from coding_trajectory.ingestion.common import prune_nones
from coding_trajectory.ingestion.indexes import TrajectoryIndex, build_trajectory_index, ordered_sessions
from coding_trajectory.ingestion.models import Session, StepTextItem, Trajectory, Turn

from coding_trajectory.analysis.request_lineage import effective_user_request, extract_user_request, is_low_value_turn
from coding_trajectory.analysis.teammate_summary import (
    MemberSessionCandidate,
    build_flows,
    build_member_session_lookup,
    build_teammate_summary,
    is_teammate_turn,
    merge_teammate_turn_nodes,
)


def build_trajectory_overview(
    trajectory: Trajectory,
    *,
    num_turns: int | None = None,
    drop_turns: int | None = None,
) -> dict[str, Any]:
    index = build_trajectory_index(trajectory)
    member_session_lookup = build_member_session_lookup(trajectory)

    ordered = [
        _session_nav_node(
            session,
            index=index,
            member_session_lookup=member_session_lookup,
            num_turns=num_turns,
            drop_turns=drop_turns,
        )
        for session in ordered_sessions(index)
        if _include_session_in_overview(session, index=index)
    ]

    return {
        "trajectory_id": str(trajectory.trajectory_id),
        "sessions": ordered,
    }


def build_trajectory_narrative(
    trajectory: Trajectory,
    *,
    num_turns: int | None = None,
    drop_turns: int | None = None,
) -> dict[str, Any]:
    index = build_trajectory_index(trajectory)

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
        "trajectory_id": str(trajectory.trajectory_id),
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


def _session_connection(session: Session, *, index: TrajectoryIndex) -> dict[str, Any]:
    parent = index.parent.get(session.session_id)
    edge_type = index.incoming_edge_type.get(session.session_id)
    forked_session_ids = [
        str(child_id)
        for child_id in index.children.get(session.session_id, [])
        if index.incoming_edge_type.get(child_id) == "forked_from"
    ]
    if parent is None and not edge_type:
        return prune_nones({
            "role": "main",
            "forked_session_ids": forked_session_ids or None,
        })
    return prune_nones({
        "relationship": edge_type,
        "parent_session_id": str(parent) if parent else None,
        "forked_session_ids": forked_session_ids or None,
    })


def _include_session_in_overview(session: Session, *, index: TrajectoryIndex) -> bool:
    return index.incoming_edge_type.get(session.session_id) != "spawned_subagent"


def _session_narrative_node(
    session: Session,
    *,
    index: TrajectoryIndex,
    num_turns: int | None = None,
    drop_turns: int | None = None,
) -> dict[str, Any]:
    turns = [
        turn_node
        for turn in session.turns
        if (turn_node := _turn_narrative_node(turn, session=session, index=index)) is not None
    ]
    turns = _apply_turn_window(turns, num_turns=num_turns, drop_turns=drop_turns)

    return prune_nones({
        "session_id": str(session.session_id),
        "relationship": _session_connection(session, index=index),
        "vendor": session.vendor.value,
        "status": session.status,
        "agent_name": session.agent_name,
        "cwd": session.cwd,
        "turns": turns,
    })


def _turn_narrative_node(
    turn: Turn,
    *,
    session: Session,
    index: TrajectoryIndex,
) -> dict[str, Any] | None:
    user_request = extract_user_request(index, turn, session=session)
    if is_low_value_turn(turn.steps, user_request):
        return None

    assistant_responses: list[str] = []
    step_ids: list[str] = []
    for step in turn.steps:
        step_ids.append(str(step.step_id))
        for item in step.items:
            if isinstance(item, StepTextItem) and item.text:
                assistant_responses.append(item.text)

    return prune_nones({
        "turn_id": str(turn.turn_id),
        "status": turn.status,
        "user_request": user_request,
        "assistant_responses": assistant_responses or None,
        "refs": {
            "step_ids": step_ids or None,
            "user_request_event_id": str(turn.user_request_event_id) if turn.user_request_event_id else None,
        },
    })


def _session_nav_node(
    session: Session,
    *,
    index: TrajectoryIndex,
    member_session_lookup: dict[str, list[MemberSessionCandidate]],
    num_turns: int | None = None,
    drop_turns: int | None = None,
) -> dict[str, Any]:
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
    turns = _apply_turn_window(turns, num_turns=num_turns, drop_turns=drop_turns)

    return prune_nones({
        "session_id": str(session.session_id),
        "relationship": _session_connection(session, index=index),
        "vendor": session.vendor.value,
        "status": session.status,
        "agent_name": session.agent_name,
        "cwd": session.cwd,
        "turns": turns,
    })


def _turn_nav_node(
    turn: Turn,
    *,
    session: Session,
    index: TrajectoryIndex,
    member_session_lookup: dict[str, list[MemberSessionCandidate]],
) -> dict[str, Any] | None:
    user_request = effective_user_request(index, turn, session=session)
    visible_user_request = user_request
    if isinstance(user_request, dict) and user_request.get("source") == "team_lead":
        visible_user_request = None
    if is_low_value_turn(turn.steps, user_request):
        return None
    if is_teammate_turn(session, turn, user_request=user_request):
        return prune_nones({
            "turn_id": str(turn.turn_id),
            "status": turn.status,
            "user_request": visible_user_request,
            "teammate_summary": build_teammate_summary(
                turn,
                user_request=user_request,
                member_session_lookup=member_session_lookup,
            ),
        })
    return prune_nones({
        "turn_id": str(turn.turn_id),
        "status": turn.status,
        "user_request": user_request,
        "activity": build_flows(turn.steps),
        "step_ids": [str(step.step_id) for step in turn.steps],
    })
