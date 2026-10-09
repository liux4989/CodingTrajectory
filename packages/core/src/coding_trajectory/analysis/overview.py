"""Live canonical display projection, independent of preparation/transport."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from coding_trajectory.contracts.overview import OverviewSession, OverviewTurn


def _count(total: int, cap: int) -> dict[str, Any]:
    return {"total": total, "returned": min(total, cap), "truncated": total > cap}


def overview_rows(
    graph: Any, *, session_id: str | None = None, root_session_id: str | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Project the public narrative and topology, retaining semantic cell caps."""
    from coding_trajectory.analysis.activity_flow import build_overview_flows
    from coding_trajectory.analysis.graph_views import _graph_orchestration_summary
    from coding_trajectory.analysis.orchestration_runs import (
        orchestration_run_for_entrypoint,
    )
    from coding_trajectory.analysis.request_lineage import (
        effective_user_request,
        is_low_value_turn,
    )
    from coding_trajectory.analysis.session_stats import session_title
    from coding_trajectory.ingestion.indexes import (
        build_session_graph_index,
        event_for_turn_user_request,
        ordered_sessions,
    )
    from coding_trajectory.project_identity import graph_project_id
    from coding_trajectory.service.handlers import _canonical_item_record

    entrypoint = (
        UUID(session_id or root_session_id)
        if session_id or root_session_id
        else graph.root_session_id
    )
    run = orchestration_run_for_entrypoint(graph, entrypoint)
    index = build_session_graph_index(graph)
    members = {s.session_id for s in run.sessions}
    sessions = [
        s
        for s in ordered_sessions(index)
        if s.session_id in members
        and (not session_id or str(s.session_id) == session_id)
    ]
    turns: list[dict[str, Any]] = []
    nodes = []
    for rank, session in enumerate(sessions):
        visible = 0
        for source_ordinal, turn in enumerate(
            sorted(session.turns, key=lambda t: (t.sequence, str(t.turn_id)))
        ):
            request = effective_user_request(index, turn, session=session)
            if is_low_value_turn(turn.items, request):
                continue
            records = [
                _canonical_item_record(item, session_graph=graph, index=index)
                for item in turn.items
                if item.kind == "agent_message"
            ]
            assistants = [
                {"item_id": row["item_id"], "preview": row["preview"]}
                for row in records
                if row["kind"] == "agent_message" and row["preview"]
            ]
            activities = [
                flow
                for flow in build_overview_flows(turn.items, flatten_commands=True)
                if "tool" in flow
            ]
            text_trimmed = any(
                item.measurements
                and (
                    item.measurements.text_chars > 280
                    or (item.measurements.tool_summary or {}).get(
                        "description_truncated", False
                    )
                )
                for item in turn.items
            )
            request_event = event_for_turn_user_request(index, turn)
            text_trimmed = text_trimmed or bool(
                request_event and request_event.payload.get("preview_truncated")
            )
            row = OverviewTurn(
                global_ordinal=len(turns),
                session_id=str(session.session_id),
                session_narrative_ordinal=visible,
                source_turn_ordinal=source_ordinal,
                turn_id=str(turn.turn_id),
                source_sequence=turn.sequence,
                started_at=turn.started_at,
                ended_at=turn.ended_at,
                timestamp_state="complete"
                if turn.started_at and turn.ended_at
                else "partial"
                if turn.started_at or turn.ended_at
                else "missing",
                status=turn.status,
                user_request={
                    "content": request["content"][:280],
                    "source": request.get("source"),
                    "event_id": str(turn.user_request_event_id)
                    if turn.user_request_event_id
                    else None,
                }
                if request
                else None,
                assistant_responses=assistants[-8:],
                activities=activities[-8:],
                refs={
                    "item_ids": [str(item.item_id) for item in turn.items[:100]],
                    "user_request_event_id": str(turn.user_request_event_id)
                    if turn.user_request_event_id
                    else None,
                },
                content_coverage={
                    "assistant_responses": _count(len(assistants), 8),
                    "activities": _count(len(activities), 8),
                    "item_ids": _count(len(turn.items), 100),
                    "text_trimmed": bool(text_trimmed),
                },
            )
            turns.append(row.model_dump(mode="json"))
            visible += 1
        codex = session.extensions.codex if session.extensions else None
        parent = index.parent.get(session.session_id)
        edge_type = index.incoming_edge_type.get(session.session_id)
        nodes.append(
            OverviewSession(
                session_id=str(session.session_id),
                session_rank=rank,
                run_root_session_id=str(run.root_session_id),
                parent_session_id=str(parent) if parent else None,
                parent_in_run=parent in members,
                edge_type=edge_type,
                relationship=edge_type or "root",
                status=session.status,
                latest_turn_status=session.latest_turn_status,
                started_at=session.started_at,
                ended_at=session.ended_at,
                vendor=session.vendor.value,
                title=session_title(session),
                model=session.model,
                agent_name=session.agent_name,
                cwd=session.cwd,
                agent_path=codex.agent_path if codex else None,
                multi_agent_version=codex.multi_agent_version if codex else None,
                multi_agent_mode=codex.multi_agent_mode if codex else None,
                source_turn_total=len(session.turns),
                narrative_turn_total=visible,
                filtered_turn_total=len(session.turns) - visible,
            ).model_dump(mode="json")
        )
    selected = {s.session_id for s in sessions}
    edges = [
        edge.model_dump(mode="json")
        for edge in graph.edges
        if edge.source_session_id in selected or edge.target_session_id in selected
    ]
    origin = next(
        (
            edge.model_dump(mode="json")
            for edge in graph.edges
            if edge.type == "forked_from"
            and edge.target_session_id == run.root_session_id
        ),
        None,
    )
    base = {
        "graph_id": str(run.root_session_id),
        "root_session_id": str(run.root_session_id),
        "lineage_root_session_id": str(graph.root_session_id),
        "entrypoint_id": str(entrypoint),
        "project": {
            "project_id": graph_project_id(graph),
            "display_name": graph.project_identifier,
        },
        "orchestration": _graph_orchestration_summary(run),
        "fork_origin": origin,
        "totals": {
            "source_turns": sum(node["source_turn_total"] for node in nodes),
            "narrative_turns": len(turns),
            "filtered_turns": sum(node["filtered_turn_total"] for node in nodes),
            "forks": sum(edge.type == "forked_from" for edge in graph.edges),
        },
        "sessions": nodes,
        "edges": edges,
        "coverage": {
            "retention": "preview",
            "measurement": "complete",
            "searchable": None,
            "trimmed": any(
                row["content_coverage"]["text_trimmed"]
                or any(
                    row["content_coverage"][field]["truncated"]
                    for field in ("activities", "assistant_responses", "item_ids")
                )
                for row in turns
            ),
        },
    }
    base["orchestration"].pop("agent_paths", None)
    return base, turns


def build_overview(
    graph: Any, *, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    """Page backwards through live canonical source keys, not frozen ordinals."""
    from coding_trajectory.service.pagination import paginate

    base, rows = overview_rows(
        graph,
        session_id=params.get("session_id"),
        root_session_id=params.get("root_session_id"),
    )
    from coding_trajectory.ingestion.indexes import build_session_graph_index

    index = build_session_graph_index(graph)
    session_keys = {}
    # Preserve canonical breadth-first hierarchy order using ancestry IDs, not
    # display ranks (which shift when live sibling sessions are inserted).
    for source_position, session_id in enumerate(index.session_ids_in_order):
        path = [session_id]
        parent = index.parent[session_id]
        while parent in index.sessions_by_id and parent not in path:
            path.insert(0, parent)
            parent = index.parent[parent]
        session_keys[str(session_id)] = (
            (0, len(path) - 1, "/".join(map(str, path)))
            if parent is None
            else (1, source_position, str(session_id))
        )

    def key(row: dict[str, Any]) -> tuple:
        return (
            *session_keys[row["session_id"]],
            row["source_sequence"],
            row["turn_id"],
        )

    rows.sort(key=key)
    for ordinal, row in enumerate(rows):
        row["global_ordinal"] = ordinal
    page, cursor = paginate(rows, method=method, params=params, key=key, older=True)
    base["turns"] = page
    base["page"] = {
        "direction": "older",
        "requested_limit": params["limit"],
        "start_ordinal": page[0]["global_ordinal"] if page else 0,
        "end_ordinal_exclusive": page[-1]["global_ordinal"] + 1 if page else 0,
        "returned": len(page),
        "total": len(rows),
        "has_more": cursor is not None,
        "next_cursor": cursor,
    }
    base["coverage"]["trimmed"] = bool(
        base["coverage"]["trimmed"] or len(page) < len(rows)
    )
    return base
