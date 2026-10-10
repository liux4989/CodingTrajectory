"""Stateless public projections of request-scoped retained orchestration runs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coding_trajectory.analysis.activity_flow import build_overview_flows
from coding_trajectory.analysis.item_details import build_item_details
from coding_trajectory.analysis.request_lineage import effective_user_request
from coding_trajectory.contracts.living import (
    LivingContextCheckpointResource,
    LivingItemResource,
    LivingSessionEdgeResource,
    LivingSessionResource,
    LivingTurnResource,
)
from coding_trajectory.ingestion.common import canonical_json, stable_uuid
from coding_trajectory.ingestion.indexes import build_session_graph_index
from coding_trajectory.ingestion.models import (
    COMPACTION_MECHANISMS,
    AgentMessageItem,
    PlanItem,
    RuntimeObservation,
    Session,
    SessionEdge,
    SessionGraph,
    Turn,
    Vendor,
)
from coding_trajectory.query import DocumentStore, ResourceNotFoundError
from coding_trajectory.service.pagination import paginate
from coding_trajectory.service.serializers import _parse_user_id

SCHEMA_VERSION = "ct.living_events.v2"
_RESOURCE_MODELS = {
    "session": LivingSessionResource,
    "turn": LivingTurnResource,
    "item": LivingItemResource,
    "context_checkpoint": LivingContextCheckpointResource,
    "session_edge": LivingSessionEdgeResource,
}


@dataclass(frozen=True)
class ProjectedResource:
    kind: str
    path: dict[str, str]
    sort_key: tuple
    details: dict[str, Any]


def query_living_events(
    params: dict[str, Any],
    *,
    document_store: DocumentStore,
    cache: Any,
    current_dir: Path,
    global_scope: bool,
) -> dict[str, Any]:
    """Project current resources, never publish or preserve a historical version."""
    scope = params["scope"]
    field = "session_id" if "session_id" in scope else "root_session_id"
    raw_id = scope[field]
    resource_id = _parse_user_id(raw_id)
    if field == "session_id":
        graph = document_store.get_session_graph_for_session(resource_id)
    else:
        graph = document_store.get_session_graph(resource_id)
    index = build_session_graph_index(graph)
    turn = (
        index.turns_by_id.get(_parse_user_id(scope["turn_id"]))
        if "turn_id" in scope
        else None
    )
    item = (
        index.items_by_id.get(_parse_user_id(scope["item_id"]))
        if "item_id" in scope
        else None
    )
    if (
        ("turn_id" in scope and turn is None)
        or ("item_id" in scope and item is None)
        or (
            turn is not None
            and field == "session_id"
            and turn.session_id != resource_id
        )
        or (
            item is not None
            and field == "session_id"
            and item.session_id != resource_id
        )
        or (item is not None and turn is not None and item.turn_id != turn.turn_id)
    ):
        raise ResourceNotFoundError("resource not found in selected scope")
    rows = [
        row
        for row in _project_graph(graph)
        if all(_in_scope(row, key, value) for key, value in scope.items())
    ]
    page, cursor = paginate(
        rows,
        method="living.events",
        params=params,
        scope={"global_scope": global_scope, "current_dir": str(current_dir.resolve())},
        key=lambda row: row.sort_key,
    )
    resources = []
    for row in page:
        # Include the same defaults/nulls/datetime formatting clients see on the
        # wire. View truncation never changes the details-based content digest.
        details = (
            _RESOURCE_MODELS[row.kind]
            .model_validate(row.details)
            .model_dump(mode="json")
        )
        resource = dict(details)
        if (
            params["mode"] == "view"
            and row.kind == "item"
            and resource["type"] != "assistant_response"
            and resource["shape"]
        ):
            resource["shape"] = {
                key: _view_value(
                    value,
                    path=row.path,
                    event_ids=resource["event_ids"],
                    field_path=f"shape.{key}",
                )
                for key, value in resource["shape"].items()
            }
        resources.append(
            {
                "resource_kind": row.kind,
                "path": row.path,
                "resource": resource,
                "digest": hashlib.sha256(
                    canonical_json(details).encode("utf-8")
                ).hexdigest(),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": params["mode"],
        "resources": resources,
        "total": len(rows),
        "returned": len(resources),
        "next_cursor": cursor,
        "issues": [],
    }


def _in_scope(row: ProjectedResource, field: str, value: str) -> bool:
    if row.path.get(field) == value:
        return True
    if row.kind != "session_edge":
        return False
    if field == "session_id":
        return value in (
            row.path.get("source_session_id"),
            row.path.get("target_session_id"),
        )
    return row.details.get(f"source_{field}") == value


def _project_graph(graph: SessionGraph) -> list[ProjectedResource]:
    root_id = str(graph.root_session_id)
    index = build_session_graph_index(graph)
    resources = []
    for session in sorted(
        graph.sessions, key=lambda value: (value.started_at, str(value.session_id))
    ):
        session_id = str(session.session_id)
        path = {"root_session_id": root_id, "session_id": session_id}
        # Hierarchy keys stay stable when resources are appended or removed.
        base = (0, session.started_at.isoformat(), session_id)
        checkpoints = _context_checkpoints(session)
        resources.append(
            ProjectedResource(
                "session",
                path,
                (*base, 0, 0, 0, session_id),
                {
                    "session_id": session_id,
                    "root_session_id": root_id,
                    "vendor": session.vendor.value,
                    "model": session.model,
                    "reasoning_effort": session.reasoning_effort,
                    "status": session.status.value,
                    "latest_turn_status": session.latest_turn_status,
                    "agent_name": session.agent_name,
                    "cwd": session.cwd,
                    "started_at": session.started_at,
                    "ended_at": session.ended_at,
                    "turn_count": len(session.turns),
                    "item_count": sum(len(turn.items) for turn in session.turns),
                    "context_checkpoint_count": len(checkpoints),
                },
            )
        )
        for turn in sorted(
            session.turns, key=lambda value: (value.sequence, str(value.turn_id))
        ):
            turn_id = str(turn.turn_id)
            turn_path = {**path, "turn_id": turn_id}
            preceding = [
                value for value in checkpoints if value["timestamp"] <= turn.started_at
            ]
            resources.append(
                ProjectedResource(
                    "turn",
                    turn_path,
                    (*base, 1, turn.sequence, -1, turn_id),
                    {
                        "turn_id": turn_id,
                        "session_id": session_id,
                        "sequence": turn.sequence,
                        "status": turn.status.value,
                        "started_at": turn.started_at,
                        "ended_at": turn.ended_at,
                        "preceding_context_checkpoint_id": preceding[-1][
                            "context_checkpoint_id"
                        ]
                        if preceding
                        else None,
                        "user_request": _request_content(
                            effective_user_request(index, turn, session=session)
                        ),
                        "assistant_responses": [
                            _inline_content(item.text)
                            for item in turn.items
                            if isinstance(item, AgentMessageItem) and item.text
                        ],
                        "activity": [
                            value
                            for value in build_overview_flows(
                                turn.items,
                                flatten_commands=session.vendor == Vendor.CODEX_CLI,
                            )
                            if "text" not in value
                        ],
                        "item_count": len(turn.items),
                    },
                )
            )
            for item in sorted(
                turn.items, key=lambda value: (value.sequence, str(value.item_id))
            ):
                item_id = str(item.item_id)
                details = build_item_details(
                    item,
                    session_graph=graph,
                    include_content=True,
                    index=index if isinstance(item, PlanItem) else None,
                )
                # Empty-output completion must remain observable on the SAME
                # item. Historical item-details and metric contracts stay alone.
                details["status"] = item.status
                details.setdefault("event_ids", [])
                resources.append(
                    ProjectedResource(
                        "item",
                        {**turn_path, "item_id": item_id},
                        (*base, 1, turn.sequence, item.sequence, item_id),
                        details,
                    )
                )
        for checkpoint in checkpoints:
            checkpoint_id = checkpoint["context_checkpoint_id"]
            resources.append(
                ProjectedResource(
                    "context_checkpoint",
                    {**path, "context_checkpoint_id": checkpoint_id},
                    (*base, 2, checkpoint["sequence"], 0, checkpoint_id),
                    checkpoint,
                )
            )
    for edge in graph.edges:
        details = _edge_resource(graph, edge)
        edge_id = details["edge_id"]
        resources.append(
            ProjectedResource(
                "session_edge",
                {
                    "root_session_id": root_id,
                    "edge_id": edge_id,
                    "source_session_id": str(edge.source_session_id),
                    "target_session_id": str(edge.target_session_id),
                },
                (1, "", "", 0, 0, 0, edge_id),
                details,
            )
        )
    return resources


def _context_checkpoints(session: Session) -> list[dict[str, Any]]:
    observations = sorted(
        (
            value
            for value in session.runtime_observations
            if value.kind in COMPACTION_MECHANISMS
        ),
        key=lambda value: (value.timestamp, value.kind, value.trace_id or ""),
    )
    turns = sorted(session.turns, key=lambda value: (value.started_at, value.sequence))
    result = []
    for sequence, observation in enumerate(observations, start=1):
        checkpoint_id = str(
            stable_uuid(
                session.vendor,
                session.session_id,
                resource="context_checkpoint",
                sequence=sequence,
                timestamp=observation.timestamp,
                kind=observation.kind,
                trace_id=observation.trace_id,
            )
        )
        after, before = _checkpoint_turn_bounds(observation, turns)
        dropped = (
            max(observation.pre_tokens - observation.post_tokens, 0)
            if observation.pre_tokens is not None
            and observation.post_tokens is not None
            else None
        )
        result.append(
            {
                "context_checkpoint_id": checkpoint_id,
                "session_id": str(session.session_id),
                "sequence": sequence,
                "timestamp": observation.timestamp,
                "mechanism": COMPACTION_MECHANISMS[observation.kind],
                "trigger": observation.trigger,
                "pre_tokens": observation.pre_tokens,
                "post_tokens": observation.post_tokens,
                "dropped_tokens": dropped,
                "effective_after_turn_id": after,
                "effective_before_turn_id": before,
                "source_event_ids": [],
            }
        )
    return result


def _checkpoint_turn_bounds(
    observation: RuntimeObservation, turns: list[Turn]
) -> tuple[str | None, str | None]:
    before = next(
        (turn for turn in turns if turn.started_at > observation.timestamp), None
    )
    eligible_after = [
        turn
        for turn in turns
        if (turn.ended_at or turn.started_at) <= observation.timestamp
    ]
    after = eligible_after[-1] if eligible_after else None
    return str(after.turn_id) if after else None, str(
        before.turn_id
    ) if before else None


def _request_content(request: dict[str, Any] | None) -> dict[str, Any] | None:
    if not request:
        return None
    result = {key: value for key, value in request.items() if key != "content"}
    if isinstance(request.get("content"), str):
        result["content"] = _inline_content(request["content"])
    return result


def _inline_content(value: Any) -> dict[str, Any]:
    text = value if isinstance(value, str) else canonical_json(value)
    return {"state": "inline", "value": value, "size_chars": len(text), "ref": None}


def _view_value(
    value: Any, *, path: dict[str, str], event_ids: list[str], field_path: str
) -> Any:
    serialized = value if isinstance(value, str) else canonical_json(value)
    if len(serialized) > (500 if isinstance(value, str) else 2000):
        return {
            "$type": "content_ref",
            "size_chars": len(serialized),
            "ref": {
                "session_id": path["session_id"],
                "turn_id": path["turn_id"],
                "item_id": path["item_id"],
                "event_ids": event_ids,
                "field_path": field_path,
            },
        }
    if isinstance(value, dict):
        return {
            key: _view_value(
                child, path=path, event_ids=event_ids, field_path=f"{field_path}.{key}"
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _view_value(
                child,
                path=path,
                event_ids=event_ids,
                field_path=f"{field_path}[{index}]",
            )
            for index, child in enumerate(value)
        ]
    return value


def _edge_resource(graph: SessionGraph, edge: SessionEdge) -> dict[str, Any]:
    edge_id = str(
        stable_uuid(
            "ct",
            graph.root_session_id,
            resource="session_edge",
            type=edge.type,
            source_session_id=edge.source_session_id,
            target_session_id=edge.target_session_id,
            source_turn_id=edge.source_turn_id,
            source_item_id=edge.source_item_id,
            source_event_id=edge.source_event_id,
        )
    )
    return {
        "edge_id": edge_id,
        "type": edge.type,
        "source_session_id": str(edge.source_session_id),
        "target_session_id": str(edge.target_session_id),
        "source_turn_id": str(edge.source_turn_id) if edge.source_turn_id else None,
        "source_item_id": str(edge.source_item_id) if edge.source_item_id else None,
        "source_event_id": str(edge.source_event_id) if edge.source_event_id else None,
        "provenance": edge.provenance,
        "confidence": edge.confidence,
        "evidence_event_ids": [str(value) for value in edge.evidence_event_ids],
        "metadata": edge.metadata,
    }


__all__ = ["SCHEMA_VERSION", "query_living_events"]
