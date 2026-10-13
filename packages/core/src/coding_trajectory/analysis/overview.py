"""Live canonical display projection, independent of preparation/transport."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

from coding_trajectory.contracts.overview import (
    NarrativeActivity,
    NarrativeAssistant,
    NarrativeToolCall,
    NarrativeTurn,
    OverviewSession,
    OverviewTurn,
    SessionNarrativeResponse,
)


def _narrative_items(turn: Any) -> list[NarrativeAssistant | NarrativeActivity]:
    from coding_trajectory.analysis.activity_flow import (
        build_flows,
        public_activity_outcome,
    )
    from coding_trajectory.analysis.process_summary import process_summary
    from coding_trajectory.analysis.projection_utils import truncate_text_preview
    from coding_trajectory.analysis.tool_arguments import tool_argument_detail
    from coding_trajectory.analysis.tool_summary import summarize_tool_call
    from coding_trajectory.ingestion.execution_models import ProcessTrace

    items = {str(item.item_id): item for item in turn.items}
    entries = []
    for flow in build_flows(turn.items, execution_units=turn.execution_units):
        item = items[flow["item_id"]]
        if flow["type"] == "assistant_response":
            text = flow["text"]
            entries.append(
                NarrativeAssistant(
                    text=truncate_text_preview(text, max_len=280),
                    item_ids=[flow["item_id"]],
                    truncated=len(" ".join(text.split())) > 280
                    or bool(item.measurements and item.measurements.text_chars > 280),
                )
            )
            continue
        script = flow.get("script")
        tool_calls = None
        if script:
            tool_calls = []
            for call in [
                *script["observed_external_calls"],
                *script["source_only_calls"],
            ]:
                child = items.get(call.get("item_id"))
                summary = summarize_tool_call(child) if child else None
                text = (summary or {}).get("description") or ""
                if not text and call["observation"] == "observed":
                    detail = tool_argument_detail(
                        SimpleNamespace(
                            kind="tool_call", input=call.get("arguments_preview")
                        )
                    )
                    text = detail.description if detail else ""
                else:
                    detail = None
                process = (
                    ProcessTrace.model_validate(call["process"])
                    if call.get("process")
                    else None
                )
                summary_basis = None
                if process:
                    text, summary_basis = process_summary(process)
                tool_calls.append(
                    NarrativeToolCall(
                        tool=call["tool_name"],
                        text=truncate_text_preview(text, max_len=280),
                        status=call.get("outcome")
                        if call["observation"] == "observed"
                        else None,
                        observation=call["observation"],
                        item_id=call.get("item_id"),
                        truncated=bool((summary or {}).get("description_truncated"))
                        or bool(detail and detail.truncated)
                        or call.get("arguments_coverage") == "truncated",
                        process=process,
                        summary_basis=summary_basis,
                    )
                )
            preview = flow.get("name") or "Tool script"
            activity_type = "tool_script"
            status = script["result"]["lifecycle"]
            # Script lifecycle describes its wrapper, not nested call outcomes.
            outcome = None
            truncated = False
            item_ids = script["item_ids"]
        else:
            activity_type = flow["name"]
            preview = flow.get("description") or activity_type
            status = getattr(item, "status", None)
            outcome = public_activity_outcome(flow.get("activity_outcome"))
            truncated = bool(flow.get("description_truncated"))
            item_ids = [flow["item_id"]]
            if flow.get("activity_wrapper_status") == "failed":
                preview = f"[wrapper failed] {preview}"
        entries.append(
            NarrativeActivity(
                tool=activity_type,
                text=truncate_text_preview(preview, max_len=280),
                status=status,
                outcome=outcome,
                item_ids=item_ids,
                truncated=truncated or len(" ".join(preview.split())) > 280,
                calls=tool_calls,
            )
        )
    return entries


def build_session_overview(graph: Any, *, params: dict[str, Any]) -> dict[str, Any]:
    """One session's ordered narrative, with detail kept behind stable references."""
    from coding_trajectory.analysis.projection_utils import truncate_text_preview
    from coding_trajectory.analysis.request_lineage import (
        extract_user_request,
        is_low_value_turn,
    )
    from coding_trajectory.analysis.session_stats import session_title
    from coding_trajectory.ingestion.indexes import (
        build_session_graph_index,
        event_for_turn_user_request,
    )
    from coding_trajectory.service.selection import select_rows

    index = build_session_graph_index(graph)
    session = index.sessions_by_id[UUID(params["session_id"])]
    rows = []
    for turn in sorted(session.turns, key=lambda t: (t.sequence, str(t.turn_id))):
        request = extract_user_request(index, turn, session=session)
        if is_low_value_turn(turn.items, request):
            continue
        event = event_for_turn_user_request(index, turn)
        rows.append(
            NarrativeTurn(
                turn_id=str(turn.turn_id),
                sequence=turn.sequence,
                started_at=turn.started_at,
                status=turn.status,
                request={
                    "text": truncate_text_preview(request["content"], max_len=280),
                    "event_id": str(event.event_id) if event else None,
                    "truncated": len(" ".join(request["content"].split())) > 280
                    or bool(event and event.payload.get("preview_truncated")),
                }
                if request
                else None,
                items=_narrative_items(turn),
            ).model_dump(mode="json")
        )
    selected = select_rows(
        rows,
        limit=params.get("limit"),
        key=lambda row: (row["sequence"], row["turn_id"]),
        latest=True,
    )
    return SessionNarrativeResponse(
        session_id=str(session.session_id),
        title=session_title(session),
        turns=selected,
        coverage={"truncated": len(selected) < len(rows)},
    ).model_dump(mode="json")


def overview_rows(
    graph: Any, *, session_id: str | None = None, root_session_id: str | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Project the public narrative and topology, including every semantic activity."""
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
                for flow in build_overview_flows(
                    turn.items,
                    flatten_commands=True,
                    execution_units=turn.execution_units,
                )
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
                assistant_responses=assistants,
                activities=activities,
                execution_units=turn.execution_units,
                refs={
                    "item_ids": [str(item.item_id) for item in turn.items],
                    "user_request_event_id": str(turn.user_request_event_id)
                    if turn.user_request_event_id
                    else None,
                },
                text_trimmed=bool(
                    text_trimmed or (request and len(request["content"]) > 280)
                ),
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
            "text_trimmed": any(row["text_trimmed"] for row in turns),
            "selection_trimmed": False,
        },
    }
    base["orchestration"].pop("agent_paths", None)
    return base, turns


def build_overview(
    graph: Any, *, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    """Return the narrative, optionally selecting its latest turns."""
    from coding_trajectory.service.selection import select_rows

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
    selected = select_rows(rows, limit=params.get("limit"), key=key, latest=True)
    base["turns"] = selected
    base["coverage"]["selection_trimmed"] = len(selected) < len(rows)
    base["coverage"]["text_trimmed"] = any(row["text_trimmed"] for row in selected)
    return base
