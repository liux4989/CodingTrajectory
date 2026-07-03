"""SessionGraph overview and narrative projections."""

from __future__ import annotations

from typing import Any

from coding_trajectory.analysis.activity_flow import build_overview_flows
from coding_trajectory.ingestion.common import prune_nones
from coding_trajectory.ingestion.indexes import SessionGraphIndex, build_session_graph_index, ordered_sessions
from coding_trajectory.ingestion.models import AgentMessageItem, Session, SessionGraph, Turn

from coding_trajectory.analysis.request_lineage import effective_user_request, extract_user_request, is_low_value_turn
from coding_trajectory.analysis.teammate_summary import (
    MemberSessionCandidate,
    build_member_session_lookup,
    build_teammate_summary,
    is_teammate_turn,
    merge_teammate_turn_nodes,
)


def build_session_graph_overview(
    session_graph: SessionGraph,
    *,
    num_turns: int | None = None,
    drop_turns: int | None = None,
) -> dict[str, Any]:
    index = build_session_graph_index(session_graph)
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
) -> dict[str, Any]:
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


def _session_connection(session: Session, *, index: SessionGraphIndex) -> dict[str, Any]:
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

    return prune_nones({
        "turn_id": str(turn.turn_id),
        "status": turn.status,
        "user_request": user_request,
        "assistant_responses": assistant_responses or None,
        "refs": {
            "item_ids": item_ids or None,
            "user_request_event_id": str(turn.user_request_event_id) if turn.user_request_event_id else None,
        },
    })


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

    return prune_nones({
        "session_id": str(session.session_id),
        "relationship": _session_connection(session, index=index),
        "vendor": session.vendor.value,
        "status": session.status,
        "agent_name": session.agent_name,
        "cwd": session.cwd,
        "turns": turns,
    })


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
        "activity": build_overview_flows(turn.items),
        "refs": {
            "item_ids": [str(item.item_id) for item in turn.items],
        },
    })
