"""Prepare immutable high-level API results once, without request-time graphs.

The wire objects are ordinary canonical JSON. Each paged method has one bounded
index, one topology/base object, and independently hashed source-order packs.
The same reader operates on local objects and authenticated remote locators.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from coding_trajectory.analysis.tool_summary_shared import scoped_shell_tokens
from coding_trajectory.contracts.prepared_api import (
    API_ENVELOPE_RESERVE,
    MAX_API_INDEX_BYTES,
    MAX_API_PACK_BYTES,
    MAX_API_RESPONSE_BYTES,
    MAX_API_TOPOLOGY_BYTES,
    PREPARED_API_SCHEMA,
    OverviewSession,
    OverviewTurn,
    PreparedMethod,
    PreparedObject,
)
from coding_trajectory.ingestion.common import canonical_json

_JSON = TypeAdapter(Any)


class PreparedApiError(ValueError):
    def __init__(self, code: str, status: int = 400):
        self.code = code
        self.status = status
        super().__init__(code)


def json_value(value: Any) -> Any:
    return _JSON.dump_python(value, mode="json")


def encoded(value: Any) -> bytes:
    return canonical_json(json_value(value)).encode()


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def method_key(method: str, scope: str, turn_id: str | None = None) -> str:
    return canonical_json([method, scope, turn_id])


class PreparedApi(BaseModel):
    model_config = ConfigDict(extra="forbid")
    methods: list[PreparedMethod] = Field(default_factory=list)
    objects: dict[str, str] = Field(default_factory=dict)

    def put(self, value: Any, bound: int) -> dict[str, Any]:
        body = encoded(value)
        if len(body) > bound:
            raise PreparedApiError("remote_result_too_large", 413)
        digest = sha256(body)
        self.objects[digest] = body.decode()
        return PreparedObject(sha256=digest, bytes=len(body)).model_dump()

    def prepare(
        self,
        method: str,
        scope: str,
        *,
        base: dict[str, Any],
        rows: list[dict[str, Any]] | None = None,
        field: str | None = None,
        turn_id: str | None = None,
        postings: dict[str, dict[str, list[int]]] | None = None,
        source_digest: str,
    ) -> None:
        from coding_trajectory.contracts import service_contract

        version = service_contract(method).version
        descriptor = PreparedMethod(
            method=method, method_version=version, scope=scope, turn_id=turn_id
        )
        header = {
            "schema_version": PREPARED_API_SCHEMA,
            "method": method,
            "method_version": version,
            "scope": scope,
            "turn_id": turn_id,
            "source_manifest_sha256": source_digest,
        }
        try:
            if rows is None:
                result = self.put(
                    {**header, "data": base},
                    MAX_API_RESPONSE_BYTES - API_ENVELOPE_RESERVE,
                )
                index = {**header, "mode": "exact", "result": result}
            else:
                topology = self.put({**header, "data": base}, MAX_API_TOPOLOGY_BYTES)
                packs: list[dict[str, Any]] = []
                sizes = [len(encoded(row)) for row in rows]
                start = 0
                # Header and array punctuation are included in the pack bound.
                overhead = len(encoded({**header, "start": len(rows), "rows": []}))
                while start < len(rows):
                    end = start
                    size = overhead
                    while (
                        end < len(rows) and size + sizes[end] + 1 <= MAX_API_PACK_BYTES
                    ):
                        size += sizes[end] + 1
                        end += 1
                    if end == start:
                        raise PreparedApiError("remote_result_too_large", 413)
                    reference = self.put(
                        {**header, "start": start, "rows": rows[start:end]},
                        MAX_API_PACK_BYTES,
                    )
                    packs.append({"start": start, "end": end, "object": reference})
                    start = end
                index = {
                    **header,
                    "mode": "page",
                    "field": field,
                    "topology": topology,
                    "total": len(rows),
                    "sizes": sizes,
                    "packs": packs,
                    "postings": postings or {},
                }
            descriptor.index = PreparedObject.model_validate(
                self.put(index, MAX_API_INDEX_BYTES)
            )
        except PreparedApiError as exc:
            if exc.code != "remote_result_too_large":
                raise
            descriptor.error = "remote_result_too_large"
        self.methods.append(descriptor)

    def referenced_objects(self) -> set[str]:
        """Discard intermediate packs belonging to an unrepresentable method."""
        refs: set[str] = set()
        for method in self.methods:
            if method.index is None:
                continue
            refs.add(method.index.sha256)
            index = json.loads(self.objects[method.index.sha256])
            if index["mode"] == "exact":
                refs.add(index["result"]["sha256"])
            else:
                refs.add(index["topology"]["sha256"])
                refs.update(pack["object"]["sha256"] for pack in index["packs"])
        return refs


def _count(total: int, cap: int) -> dict[str, Any]:
    return {"total": total, "returned": min(total, cap), "truncated": total > cap}


def overview_rows(
    graph: Any, *, session_id: str | None = None, root_session_id: str | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from uuid import UUID

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
        for source_ordinal, turn in enumerate(session.turns):
            request = effective_user_request(index, turn, session=session)
            if is_low_value_turn(turn.items, request):
                continue
            records = [
                _canonical_item_record(item, session_graph=graph, index=index)
                for item in turn.items
            ]
            assistants = [
                {"item_id": row["item_id"], "preview": row["preview"]}
                for row in records
                if row["kind"] == "agent_message" and row["preview"]
            ]
            activities = []
            for row in records:
                if row["kind"] not in {
                    "tool_call",
                    "command_execution",
                    "file_change",
                    "plan",
                }:
                    continue
                detail, evidence = row["detail"] or {}, row["output_evidence"] or {}
                activities.append(
                    {
                        "item_id": row["item_id"],
                        "kind": row["kind"],
                        **{
                            key: detail.get(key)
                            for key in (
                                "tool_name",
                                "concept",
                                "target_kind",
                                "target",
                                "path",
                                "operation",
                                "exit_code",
                            )
                        },
                        "status": row["status"],
                        "outcome": evidence.get("outcome"),
                    }
                )
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


def detail_postings(
    rows: list[dict[str, Any]], *, field: str, tool_names: dict[str, str | None]
) -> dict[str, dict[str, list[int]]]:
    postings: dict[str, dict[str, list[int]]] = {}
    for position, row in enumerate(rows):
        values = {
            "turn_id": row.get("turn_id"),
            "item_id": row.get("item_id") if field == "events" else None,
            "id": row.get("event_id" if field == "events" else "item_id"),
            "types": row.get("type" if field == "events" else "kind"),
            "status": row.get("status"),
            "tool_name": tool_names.get(row.get("item_id", "")),
        }
        for key, value in values.items():
            if value is not None:
                postings.setdefault(key, {}).setdefault(str(value), []).append(position)
        if field == "events" and row.get("usage"):
            posting = postings.setdefault("types", {}).setdefault("usage", [])
            if not posting or posting[-1] != position:
                posting.append(position)
    return postings


@scoped_shell_tokens()
def prepare_graph_api(index: Any) -> PreparedApi:
    """Compute supported methods using the canonical handlers, never raw logs."""
    from coding_trajectory.analysis.orchestration_runs import orchestration_runs
    from coding_trajectory.control_plane.published_facts import (
        compute_fact_set_digest,
        session_graph_from_fact_index,
    )
    from coding_trajectory.service.handlers import SERVICE_HANDLERS, ServiceContext
    from coding_trajectory.service.store import IndexCache

    (graph_id,) = index.graph_ids
    graph = session_graph_from_fact_index(index, graph_id)
    source = compute_fact_set_digest(graph_id, list(index.rows_for_graph(graph_id)))
    from coding_trajectory.query import DocumentStore

    context = ServiceContext(
        DocumentStore.from_session_graphs([graph]),
        True,
        Path.cwd(),
        "prepared API",
        IndexCache(),
    )
    result = PreparedApi()

    def compute(method: str, params: dict[str, Any]) -> dict[str, Any]:
        return json_value(SERVICE_HANDLERS[method](params, context))

    for session in graph.sessions:
        scope = str(session.session_id)
        base, rows = overview_rows(graph, session_id=scope)
        result.prepare(
            "session.overview",
            scope,
            base=base,
            rows=rows,
            field="turns",
            source_digest=source,
        )
        items = compute("session.items", {"session_id": scope, "limit": 2**31 - 1})
        tools = {
            row["item_id"]: (row["detail"] or {}).get("tool_name")
            for row in items["items"]
        }
        for method, field in (("session.items", "items"), ("session.events", "events")):
            data = (
                items
                if field == "items"
                else compute(method, {"session_id": scope, "limit": 2**31 - 1})
            )
            rows = data.pop(field)
            data.pop("next_cursor", None)
            result.prepare(
                method,
                scope,
                base=data,
                rows=rows,
                field=field,
                postings=detail_postings(rows, field=field, tool_names=tools),
                source_digest=source,
            )
        for method in (
            "session.summary",
            "session.tree",
            "session.stats",
            "session.usage",
            "session.model_usage",
            "session.request_usage",
            "session.tool_usage",
        ):
            turns = (
                [None, *[str(turn.turn_id) for turn in session.turns]]
                if method
                in {
                    "session.summary",
                    "session.usage",
                    "session.model_usage",
                    "session.request_usage",
                    "session.tool_usage",
                }
                else [None]
            )
            for turn_id in turns:
                params = {
                    "session_id": scope,
                    **({"turn_id": turn_id} if turn_id else {}),
                }
                result.prepare(
                    method,
                    scope,
                    turn_id=turn_id,
                    base=compute(method, params),
                    source_digest=source,
                )
    for run in orchestration_runs(graph):
        scope = str(run.root_session_id)
        # Keep lineage/fork facts while selecting exactly this orchestration run.
        base, rows = overview_rows(graph, root_session_id=scope)
        result.prepare(
            "graph.overview",
            scope,
            base=base,
            rows=rows,
            field="turns",
            source_digest=source,
        )
        for method in ("graph.stats", "graph.usage"):
            result.prepare(
                method,
                scope,
                base=compute(method, {"root_session_id": scope}),
                source_digest=source,
            )
    keep = result.referenced_objects()
    result.objects = {key: body for key, body in result.objects.items() if key in keep}
    return result


def prepare_inventory_api(
    projects: list[dict[str, Any]], sessions: list[dict[str, Any]]
) -> tuple[PreparedApi, str]:
    from coding_trajectory.ingestion.common import normalize_project_key

    result = PreparedApi()
    projects = sorted(json_value(projects), key=lambda row: row["project_id"])
    sessions = sorted(json_value(sessions), key=lambda row: row["root_session_id"])
    source = sha256(encoded({"projects": projects, "sessions": sessions}))
    for method, rows in (("project.list", projects), ("project.sessions", sessions)):
        postings: dict[str, dict[str, list[int]]] = {}
        for position, row in enumerate(rows):
            values = {
                "project_id": [row["project_id"]],
                "project_name": [
                    normalize_project_key(
                        row.get("project") or row.get("display_name") or ""
                    )
                ],
                "modified": [row.get("modified") or row.get("modified_at")],
                "agent_vendor": row.get("vendors", []),
            }
            for field, entries in values.items():
                for value in entries:
                    if value is not None:
                        postings.setdefault(field, {}).setdefault(
                            str(value), []
                        ).append(position)
        result.prepare(
            method,
            "workspace",
            base={},
            rows=rows,
            field="items",
            postings=postings,
            source_digest=source,
        )
    keep = result.referenced_objects()
    result.objects = {key: body for key, body in result.objects.items() if key in keep}
    return result, source
