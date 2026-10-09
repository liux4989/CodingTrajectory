"""Direct method handlers over the local retained canonical document store."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from coding_trajectory import debug
from coding_trajectory.contracts import service_contract
from coding_trajectory.ingestion.models import SessionGraph
from coding_trajectory.query import DocumentStore, ResourceNotFoundError
from coding_trajectory.service.pagination import LocalQueryError, paginate
from coding_trajectory.service.serializers import (
    _parse_user_id,
    _public_output_for_session_graph,
)
from coding_trajectory.service.store import (
    IndexCache,
    _resolve_session_graph,
    project_list_metadata,
    project_sessions_metadata,
)


@dataclass(frozen=True)
class ServiceContext:
    store: DocumentStore
    global_scope: bool
    current_dir: Path
    discovery_note: str
    cache: IndexCache


ServiceHandler = Callable[[dict[str, Any], ServiceContext], Any]


def dispatch(
    method: str,
    params: dict[str, Any],
    *,
    store: DocumentStore,
    global_scope: bool,
    current_dir: Path,
    discovery_note: str,
    cache: IndexCache,
) -> Any:
    contract = service_contract(method)
    try:
        params = contract.validate_request(params)
    except ValidationError as exc:
        if any(error["loc"] == ("cursor",) for error in exc.errors()):
            raise LocalQueryError("invalid_cursor") from exc
        raise
    if not isinstance(store, DocumentStore):
        raise TypeError("dispatch requires a canonical document store")
    # Direct callers (including metric validation) can supply transient graphs.
    # Normalize without mutating their store or involving publication authority.
    # Living methods keep their existing incremental behavioral contract.
    if method.startswith(("session.", "graph.")):
        from coding_trajectory.ingestion.retained import retain_session_graph

        store = DocumentStore.from_session_graphs(
            [retain_session_graph(graph) for graph in store.session_graphs.values()]
        )
    context = ServiceContext(
        store=store,
        global_scope=global_scope,
        current_dir=current_dir,
        discovery_note=discovery_note,
        cache=cache,
    )
    context.cache.index_store(context.store)
    try:
        handler = SERVICE_HANDLERS[method]
    except KeyError as exc:
        raise KeyError(f"no service handler registered for {method}") from exc
    result = handler(params, context)
    return contract.validate_response(result)


def _select_session_graph(session_graph: SessionGraph, session_id: str) -> SessionGraph:
    """Select one canonical session from a graph for ``session.*`` methods."""
    from coding_trajectory.ingestion.indexes import build_session_graph_index

    index = build_session_graph_index(session_graph)
    selected = index.sessions_by_id.get(_parse_user_id(session_id))
    if selected is None:
        raise ResourceNotFoundError(f"session not found in graph: {session_id}")
    return SessionGraph(
        root_session_id=selected.session_id,
        project_identifier=session_graph.project_identifier,
        summary=None,
        sessions=[selected],
    )


def _resolve_historical_graph(store: DocumentStore, raw_id: str | None) -> SessionGraph:
    return _resolve_session_graph(store, raw_id)


def _graph_handler(
    build: Callable[[dict[str, Any], SessionGraph], Any],
) -> ServiceHandler:
    """Resolve the graph from its required ``root_session_id`` entry point."""

    @wraps(build)
    def wrapper(params: dict[str, Any], context: ServiceContext) -> Any:
        root_session_id = params["root_session_id"]
        session_graph = _resolve_historical_graph(context.store, root_session_id)
        from coding_trajectory.analysis.orchestration_runs import (
            orchestration_run_for_entrypoint,
        )

        session_graph = orchestration_run_for_entrypoint(
            session_graph, _parse_user_id(root_session_id)
        )
        return _public_output_for_session_graph(
            session_graph, build(params, session_graph)
        )

    return wrapper


def _session_handler(
    build: Callable[[dict[str, Any], SessionGraph], Any],
) -> ServiceHandler:
    """Resolve one session via its required ``session_id`` entry point."""

    @wraps(build)
    def wrapper(params: dict[str, Any], context: ServiceContext) -> Any:
        session_id = params["session_id"]
        session_graph = _resolve_historical_graph(context.store, session_id)
        selected_graph = _select_session_graph(session_graph, session_id)
        return _public_output_for_session_graph(
            selected_graph, build(params, selected_graph)
        )

    return wrapper


def _handle_project_sessions(
    params: dict[str, Any], context: ServiceContext
) -> dict[str, Any]:
    """Metadata-only inventory; never build metric graphs for cards."""
    result = project_sessions_metadata(
        params,
        global_scope=context.global_scope,
        current_dir=context.current_dir,
        cache=context.cache,
    )
    return _inventory_page(result["items"], "project.sessions", params, context)


def _handle_project_list(
    params: dict[str, Any], context: ServiceContext
) -> dict[str, Any]:
    """Use the same metadata-only implementation as local runtime calls."""
    result = project_list_metadata(
        params,
        global_scope=context.global_scope,
        current_dir=context.current_dir,
        cache=context.cache,
    )
    return _inventory_page(
        list(result["items"].values()), "project.list", params, context
    )


def _inventory_page(
    rows: list[dict[str, Any]],
    method: str,
    params: dict[str, Any],
    context: ServiceContext,
) -> dict[str, Any]:
    page, cursor = paginate(
        rows,
        method=method,
        params=params,
        scope={
            "global_scope": context.global_scope,
            "current_dir": str(context.current_dir.resolve()),
        },
        key=lambda row: (row["project_id"], row.get("root_session_id", "")),
    )
    return {
        "items": page,
        "total": len(rows),
        "returned": len(page),
        "next_cursor": cursor,
    }


def _handle_living_events(
    params: dict[str, Any], context: ServiceContext
) -> dict[str, Any]:
    from coding_trajectory.living_events import query_living_events

    if not isinstance(context.store, DocumentStore):
        raise TypeError("living.events requires a canonical document store")
    return query_living_events(
        params,
        document_store=context.store,
        cache=context.cache,
        current_dir=context.current_dir,
        global_scope=context.global_scope,
    )


def _handle_session_overview(params: dict[str, Any], context: ServiceContext) -> Any:
    from coding_trajectory.analysis.overview import build_overview

    return build_overview(
        _resolve_historical_graph(context.store, params["session_id"]),
        method="session.overview",
        params=params,
    )


@_session_handler
def _handle_session_summary(params: dict[str, Any], session_graph: SessionGraph) -> Any:
    from coding_trajectory.analysis.session_retrieval import build_session_summary

    return build_session_summary(session_graph, turn_id=params.get("turn_id"))


@_session_handler
def _handle_session_search(params: dict[str, Any], session_graph: SessionGraph) -> Any:
    from coding_trajectory.analysis.session_retrieval import search_session

    result = search_session(
        session_graph,
        query=params["query"],
        mode=params["mode"],
        kinds=params["kinds"],
        limit=2**31 - 1,
        turn_id=params.get("turn_id"),
    )

    def key(row: dict[str, Any]) -> tuple:
        refs = row["references"]
        identity = refs.get("item_id") or (refs.get("event_ids") or [""])[0]
        return (-float(row["score"]), row["timestamp"], str(identity), row["kind"])

    page, cursor = paginate(
        result["matches"], method="session.search", params=params, key=key
    )
    result.update(
        matches=page,
        next_cursor=cursor,
        truncated=cursor is not None,
    )
    result["coverage"]["trimmed"] = len(page) < result["total"]
    return result


def _handle_session_tree(params: dict[str, Any], context: ServiceContext) -> Any:
    from coding_trajectory.analysis.orchestration_runs import (
        build_conversation_tree,
        orchestration_run_for_entrypoint,
    )

    session_id = params["session_id"]
    session_graph = _resolve_historical_graph(context.store, session_id)
    tree = build_conversation_tree(session_graph)
    run = orchestration_run_for_entrypoint(session_graph, _parse_user_id(session_id))
    tree["selected_branch_id"] = str(run.root_session_id)
    return tree


@_session_handler
def _handle_session_stats(params: dict[str, Any], session_graph: SessionGraph) -> Any:
    return _build_stats_response(session_graph)


@_session_handler
def _handle_session_usage(params: dict[str, Any], session_graph: SessionGraph) -> Any:
    from coding_trajectory.metrics import build_session_graph_usage

    return _native_metric_costs(
        build_session_graph_usage(session_graph, turn_id=params.get("turn_id"))
    )


@_session_handler
def _handle_session_model_usage(
    params: dict[str, Any], session_graph: SessionGraph
) -> Any:
    from coding_trajectory.metrics import build_session_graph_model_usage

    return _native_metric_costs(
        build_session_graph_model_usage(
            session_graph,
            turn_id=params.get("turn_id"),
        )
    )


@_session_handler
def _handle_session_request_usage(
    params: dict[str, Any], session_graph: SessionGraph
) -> Any:
    from coding_trajectory.metrics import build_session_graph_request_usage

    result = build_session_graph_request_usage(
        session_graph, turn_id=params.get("turn_id")
    )
    index = _usage_index(session_graph)
    rows = result["requests"]
    page, cursor = paginate(
        rows,
        method="session.request_usage",
        params=params,
        key=lambda row: (
            index.turns_by_id[_parse_user_id(row["turn_id"])].sequence,
            row["turn_id"],
            row["sequence"],
            row["timestamp"],
            row.get("usage_event_id") or "",
        ),
    )
    result.update(
        requests=page, total=len(rows), returned=len(page), next_cursor=cursor
    )
    return result


@_session_handler
def _handle_session_tool_usage(
    params: dict[str, Any], session_graph: SessionGraph
) -> Any:
    from coding_trajectory.metrics import build_session_graph_tool_usage

    result = build_session_graph_tool_usage(
        session_graph, turn_id=params.get("turn_id")
    )
    items, costs = result["tool_items"], result.get("item_real_token_costs")
    # Preserve independent, potentially unequal ledgers by pairing positions,
    # not joining IDs or recomputing attribution (same public ledger semantics).
    rows = [
        {
            "tool": items[i] if i < len(items) else None,
            "cost": costs[i] if costs is not None and i < len(costs) else None,
        }
        for i in range(max(len(items), len(costs or [])))
    ]
    index = _usage_index(session_graph)

    def key(row: dict[str, Any]) -> tuple:
        value = row["cost"] if row["cost"] is not None else row["tool"]
        turn = index.turns_by_id[_parse_user_id(value["turn_id"])]
        # The all-item ledger includes synthetic user-prompt entries (sequence
        # -1) that deliberately do not exist in the canonical item index.
        sequence = (
            value["sequence"]
            if row["cost"] is not None
            else index.items_by_id[_parse_user_id(value["item_id"])].sequence
        )
        return (turn.sequence, str(turn.turn_id), sequence, value["item_id"])

    page, cursor = paginate(rows, method="session.tool_usage", params=params, key=key)
    result["tool_items"] = [row["tool"] for row in page if row["tool"] is not None]
    if "item_real_token_costs" in result and costs is not None:
        result["item_real_token_costs"] = [
            row["cost"] for row in page if row["cost"] is not None
        ]
    result.update(total=len(rows), returned=len(page), next_cursor=cursor)
    return result


def _usage_index(session_graph: SessionGraph) -> Any:
    from coding_trajectory.ingestion.indexes import build_session_graph_index

    return build_session_graph_index(session_graph)


def _handle_graph_overview(params: dict[str, Any], context: ServiceContext) -> Any:
    from coding_trajectory.analysis.overview import build_overview

    return build_overview(
        _resolve_historical_graph(context.store, params["root_session_id"]),
        method="graph.overview",
        params=params,
    )


@_graph_handler
def _handle_graph_stats(params: dict[str, Any], session_graph: SessionGraph) -> Any:
    return _build_stats_response(session_graph)


def _build_stats_response(session_graph: SessionGraph) -> dict[str, Any]:
    from coding_trajectory.metrics import build_session_graph_stats

    return _native_metric_costs(
        build_session_graph_stats(
            session_graph,
            include_session_composition=True,
        )
    )


@_graph_handler
def _handle_graph_usage(params: dict[str, Any], session_graph: SessionGraph) -> Any:
    from coding_trajectory.metrics import build_session_graph_usage

    return _native_metric_costs(
        build_session_graph_usage(session_graph, include_graph_turns=True)
    )


def _native_metric_costs(value: Any) -> Any:
    """Preserve metric evidence produced by the separate pricing authority."""

    return value


def _handle_session_events(
    params: dict[str, Any], context: ServiceContext
) -> dict[str, Any]:
    """Return minimal normalized event envelopes with typed filters only.

    Events never carry raw payloads. Output content is referenced through
    ``item_id``/``output_evidence_id`` and lives once on the owning item's
    processed output evidence. Usage observations resolve to native numeric
    measurements retained on the session's request facts.
    """
    from coding_trajectory.ingestion.indexes import build_session_graph_index

    session_graph = _resolve_historical_graph(context.store, params["session_id"])
    session_graph = _select_session_graph(session_graph, params["session_id"])
    selected_turn_id = params.get("turn_id")
    index = build_session_graph_index(session_graph)
    allowed_event_ids = _event_ids_for_turn(session_graph, selected_turn_id)
    requested_ids = (
        {_parse_user_id(value) for value in params["event_ids"]}
        if params.get("event_ids")
        else None
    )
    item_filter = _parse_user_id(params["item_id"]) if params.get("item_id") else None
    types_filter = _event_types_filter(params.get("types"))
    status_filter = params.get("status")
    tool_name_filter = params.get("tool_name")

    usage_by_event: dict[Any, Any] = {}
    for session in session_graph.sessions:
        for observation in session.context_usage:
            usage_by_event[observation.source_event_id] = observation

    rows: list[dict[str, Any]] = []
    for session in session_graph.sessions:
        for source_sequence, event in enumerate(session.events):
            if (
                allowed_event_ids is not None
                and event.event_id not in allowed_event_ids
            ):
                continue
            if requested_ids is not None and event.event_id not in requested_ids:
                continue
            record = _event_record(
                event,
                session_graph=session_graph,
                index=index,
                source_sequence=source_sequence,
                usage_by_event=usage_by_event,
            )
            if types_filter is not None:
                if "usage" in types_filter:
                    if event.type.value not in types_filter and record["usage"] is None:
                        continue
                elif event.type.value not in types_filter:
                    continue
            if status_filter is not None and record["status"] != status_filter:
                continue
            if item_filter is not None:
                related = record["item_id"]
                if related is None or _parse_user_id(related) != item_filter:
                    continue
            if tool_name_filter is not None:
                if record["item_id"] is None:
                    continue
                item = index.items_by_id.get(_parse_user_id(record["item_id"]))
                if item is None or _item_tool_name(item) != tool_name_filter:
                    continue
            rows.append(record)

    page, next_cursor = paginate(
        rows,
        method="session.events",
        params=params,
        key=lambda row: (row["source_order_key"], row["event_id"]),
    )
    missing = (
        sorted(str(value) for value in requested_ids - set(index.events_by_id))
        if requested_ids
        else []
    )
    for event_id in missing:
        debug.warn(
            f"skipping unresolved event id {event_id!r}",
            code="session.events.event_id_unresolved",
            event_id=event_id,
        )
    return _public_output_for_session_graph(
        session_graph,
        {
            "root_session_id": str(session_graph.root_session_id),
            "events": page,
            "total": len(rows),
            "returned": len(page),
            "unresolved_ids": missing,
            "next_cursor": next_cursor,
            "coverage": {
                "retention": "not_retained",
                "measurement": "complete",
                "searchable": None,
                "trimmed": len(page) < len(rows),
            },
        },
    )


def _event_types_filter(types: list[str] | None) -> set[str] | None:
    if not types:
        return None
    from coding_trajectory.ingestion.models import EventType

    valid = {event.value for event in EventType} | {"usage"}
    unknown = sorted(set(types) - valid)
    if unknown:
        raise ValueError(
            f"unknown event types {unknown!r}. Valid types: usage, "
            f"{', '.join(sorted(valid - {'usage'}))}"
        )
    return set(types)


def _event_record(
    event: Any,
    *,
    session_graph: SessionGraph,
    index: Any,
    source_sequence: int,
    usage_by_event: dict[Any, Any],
) -> dict[str, Any]:
    from coding_trajectory.ingestion.indexes import item_for_event

    related_item = item_for_event(index, event.event_id)
    related_turn = _turn_for_event(session_graph, event.event_id, related_item)
    payload = event.payload if isinstance(event.payload, dict) else {}
    status = payload.get("status")
    if not isinstance(status, str) or not status:
        status = _event_lifecycle_status(event.type)
    evidence = _item_output_evidence(related_item) if related_item is not None else None
    observation = usage_by_event.get(event.event_id)
    return {
        "event_id": str(event.event_id),
        "session_id": str(event.session_id),
        "turn_id": str(related_turn.turn_id) if related_turn else None,
        "item_id": str(related_item.item_id) if related_item else None,
        "timestamp": event.timestamp,
        "type": event.type.value,
        "status": status,
        "source_sequence": source_sequence,
        "source_order_key": _event_source_order_key(
            index.sessions_by_id[event.session_id].started_at,
            event.session_id,
            source_sequence,
            event.timestamp,
            event.event_id,
        ),
        "provenance": {
            "source": "retained_canonical",
            "method": "retained_event_envelope.v1",
            "confidence": "high",
        },
        "coverage": {
            "retention": "not_retained",
            "measurement": "complete" if observation is not None else "none",
            "searchable": "none",
            "hierarchy": "complete" if related_turn else "partial",
            "lifecycle": "complete" if status else "partial",
        },
        "output_evidence_id": (
            str(related_item.item_id) if evidence is not None else None
        ),
        "usage": _event_usage(observation) if observation is not None else None,
    }


def _event_usage(observation: Any) -> dict[str, Any]:
    usage = dict(observation.usage or {})
    cost = usage.pop("cost_usd", None)
    return {
        "model": observation.model,
        "provider": observation.provider,
        "source": observation.source,
        "context_window_tokens": observation.context_window_tokens,
        "used_input_tokens": observation.used_input_tokens,
        "input_tokens": usage.get("input_tokens", 0),
        "cached_input_tokens": usage.get("cached_input_tokens", 0),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "reasoning_output_tokens": usage.get("reasoning_output_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "uncached_input_tokens": usage.get("uncached_input_tokens"),
        "cost_usd": cost,
    }


def _handle_session_items(
    params: dict[str, Any], context: ServiceContext
) -> dict[str, Any]:
    """Return bounded metadata, measurements, and processed output evidence."""

    session_graph = _resolve_historical_graph(context.store, params["session_id"])
    session_graph = _select_session_graph(session_graph, params["session_id"])
    selected_turn_id = params.get("turn_id")
    types_filter = set(params["types"]) if params.get("types") else None
    requested_ids = (
        {_parse_user_id(value) for value in params["item_ids"]}
        if params.get("item_ids")
        else None
    )
    from coding_trajectory.ingestion.indexes import build_session_graph_index

    index = build_session_graph_index(session_graph)
    rows: list[dict[str, Any]] = []
    for session in session_graph.sessions:
        for turn in session.turns:
            if selected_turn_id is not None and str(turn.turn_id) != str(
                selected_turn_id
            ):
                continue
            for item in turn.items:
                if requested_ids is not None and item.item_id not in requested_ids:
                    continue
                if types_filter and item.kind not in types_filter:
                    continue
                rows.append(
                    _canonical_item_record(
                        item, session_graph=session_graph, index=index
                    )
                )
    missing = (
        sorted(str(value) for value in requested_ids - set(index.items_by_id))
        if requested_ids
        else []
    )
    if requested_ids:
        for item_id in missing:
            debug.warn(
                f"skipping unresolved item id {item_id!r}",
                code="session.items.item_id_unresolved",
                item_id=item_id,
            )
    page, next_cursor = paginate(
        rows,
        method="session.items",
        params=params,
        key=lambda row: (row["source_order_key"], row["item_id"]),
    )
    trimmed = len(page) < len(rows)
    return _public_output_for_session_graph(
        session_graph,
        {
            "root_session_id": str(session_graph.root_session_id),
            "items": page,
            "total": len(rows),
            "returned": len(page),
            "unresolved_ids": missing,
            "next_cursor": next_cursor,
            "coverage": {
                "retention": _items_retention(rows),
                "measurement": "complete",
                "searchable": _items_searchable(rows),
                "trimmed": trimmed,
            },
        },
    )


def _items_retention(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "not_applicable"
    values = {row["coverage"]["retention"] for row in rows}
    if values == {"not_applicable"}:
        return "not_applicable"
    if "preview" in values or "complete" in values:
        return "preview"
    return "not_retained"


def _items_searchable(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "none"
    order = {"complete": 3, "preview": 2, "facts_only": 1, "none": 0}
    return min((row["coverage"]["searchable"] for row in rows), key=lambda v: order[v])


def _canonical_item_record(
    item: Any,
    *,
    session_graph: SessionGraph,
    index: Any,
) -> dict[str, Any]:
    from coding_trajectory.analysis.item_details import _classify_item
    from coding_trajectory.analysis.measurements import is_projection_only_item

    source_session = index.sessions_by_id[item.session_id]
    source_turn = index.turns_by_id[item.turn_id]
    source_order_key = _item_source_order_key(
        item,
        session_started_at=source_session.started_at,
        turn_sequence=source_turn.sequence,
    )
    concept = _classify_item(item)
    operations = _item_operations(item, concept=concept)
    evidence = _item_output_evidence(item)
    measurements = item.measurements
    preview = _item_preview(item, evidence)
    projection_parent_item_id, nested_index, projection_method = (
        _item_projection_origin(item, index=index)
    )
    projection_only = (
        bool(measurements.projection_only)
        if measurements is not None
        else is_projection_only_item(item)
    )
    return {
        "item_id": str(item.item_id),
        "session_id": str(item.session_id),
        "turn_id": str(item.turn_id),
        "event_ids": [str(event_id) for event_id in item.event_ids],
        "source_sequence": item.sequence,
        "source_order_key": source_order_key,
        "started_at": item.started_at,
        "completed_at": item.completed_at,
        "kind": item.kind,
        "operation": operations[0] if operations else None,
        "status": _normalized_value(item.status),
        "projection_parent_item_id": projection_parent_item_id,
        "nested_index": nested_index,
        "projection_only": projection_only,
        "projection_provenance": (
            {
                "source": "reconstructed_evidence",
                "method": projection_method,
                "confidence": "medium",
            }
            if projection_parent_item_id is not None
            else None
        ),
        "provenance": {
            "source": "retained_canonical",
            "method": "retained_item_record.v1",
            "confidence": "high",
        },
        "coverage": _item_coverage(item, evidence),
        "type": str(concept),
        "operations": operations or None,
        "preview": preview,
        "detail": _item_detail(item, concept=concept, index=index),
        "measurements": (
            {
                "input_chars": measurements.input_chars,
                "input_tokens": measurements.input_tokens,
                "output_chars": measurements.output_chars,
                "output_tokens": measurements.output_tokens,
                "text_chars": measurements.text_chars,
                "text_tokens": measurements.text_tokens,
            }
            if measurements is not None
            else None
        ),
        "output_evidence": evidence,
    }


def _item_projection_origin(
    item: Any, *, index: Any
) -> tuple[str | None, int | None, str | None]:
    """Return retained reconstruction evidence without inventing a parent."""

    vendor_data = getattr(item, "vendor_data", None)
    projection = (
        vendor_data.get("chronicle_projection")
        if isinstance(vendor_data, dict)
        and isinstance(vendor_data.get("chronicle_projection"), dict)
        else {}
    )
    parent_item_id = projection.get("parent_item_id")
    nested_index = projection.get("nested_index")
    method: str | None = None
    if parent_item_id is not None:
        method = "chronicle_projection.v1"
    else:
        activity = (
            vendor_data.get("activity")
            if isinstance(vendor_data, dict)
            and isinstance(vendor_data.get("activity"), dict)
            else {}
        )
        provenance = (
            activity.get("provenance")
            if isinstance(activity.get("provenance"), dict)
            else {}
        )
        parent_tool_call_id = provenance.get("parent_tool_call_id")
        if isinstance(parent_tool_call_id, str):
            parent = index.items_by_tool_call_id.get(parent_tool_call_id)
            if parent is not None:
                parent_item_id = parent.item_id
                nested_index = provenance.get("nested_index")
                extractor = provenance.get("extractor")
                basis = provenance.get("relationship_basis")
                method = (
                    ".".join(
                        str(value)
                        for value in (extractor, basis)
                        if isinstance(value, str) and value
                    )
                    or "canonical_projection.v1"
                )
    return (
        str(parent_item_id) if parent_item_id is not None else None,
        nested_index
        if isinstance(nested_index, int) and not isinstance(nested_index, bool)
        else None,
        method,
    )


def _item_operations(item: Any, *, concept: Any) -> list[str] | None:
    from coding_trajectory.analysis.concepts import ItemKind
    from coding_trajectory.ingestion.models import (
        AgentMessageItem,
        CommandExecutionItem,
        FileChangeItem,
        PlanItem,
        ReasoningItem,
    )

    if isinstance(item, AgentMessageItem):
        return ["text_reply"]
    if isinstance(item, ReasoningItem):
        return ["reason"]
    if isinstance(item, FileChangeItem):
        return [item.operation or (item.tool_name or "edit")]
    if isinstance(item, CommandExecutionItem):
        return ["execute"]
    if isinstance(item, PlanItem):
        if concept == ItemKind.PLAN_SUBAGENT:
            return ["spawn", "collect_result"]
        if concept == ItemKind.SESSION_HANDOFF:
            return ["handoff"]
        return ["update"]
    return [item.tool_name] if getattr(item, "tool_name", None) else None


def _item_tool_name(item: Any) -> str | None:
    name = getattr(item, "tool_name", None)
    if name:
        return str(name)
    measurements = getattr(item, "measurements", None)
    summary = getattr(measurements, "tool_summary", None) if measurements else None
    if isinstance(summary, dict) and summary.get("name"):
        return str(summary["name"])
    return None


def _item_output_evidence(item: Any) -> dict[str, Any] | None:
    vendor_data = getattr(item, "vendor_data", None) or {}
    value = vendor_data.get("chronicle_output_evidence")
    return value if isinstance(value, dict) else None


def _item_preview(item: Any, evidence: dict[str, Any] | None) -> str | None:
    measurements = getattr(item, "measurements", None)
    preview = getattr(measurements, "text_preview", None) if measurements else None
    if isinstance(preview, str) and preview:
        return preview
    if evidence is not None:
        value = evidence.get("preview")
        if isinstance(value, str) and value:
            return value
    return None


def _item_coverage(item: Any, evidence: dict[str, Any] | None) -> dict[str, Any]:
    from coding_trajectory.ingestion.models import AgentMessageItem, ReasoningItem

    measurements = getattr(item, "measurements", None)
    lifecycle = (
        "complete"
        if getattr(item, "completed_at", None) is not None
        and getattr(item, "status", None)
        else "partial"
    )
    if evidence is not None:
        return {
            "retention": evidence["retention"],
            "measurement": (
                "complete" if evidence.get("output_chars") is not None else "partial"
            ),
            "searchable": evidence["searchable"],
            "hierarchy": "complete",
            "lifecycle": lifecycle,
        }
    if isinstance(item, (AgentMessageItem, ReasoningItem)):
        preview = getattr(measurements, "text_preview", None) if measurements else None
        if measurements is None:
            retention = "complete"
            searchable = "complete"
        elif isinstance(preview, str) and preview:
            text_chars = getattr(measurements, "text_chars", 0)
            retention = "preview"
            searchable = "complete" if text_chars <= 280 else "preview"
        else:
            retention = "not_retained"
            searchable = "none"
        return {
            "retention": retention,
            "measurement": "complete" if measurements is not None else "none",
            "searchable": searchable,
            "hierarchy": "complete",
            "lifecycle": lifecycle,
        }
    return {
        "retention": "not_applicable",
        "measurement": "complete" if measurements is not None else "none",
        "searchable": "facts_only",
        "hierarchy": "complete",
        "lifecycle": lifecycle,
    }


def _item_detail(item: Any, *, concept: Any, index: Any) -> dict[str, Any] | None:
    from coding_trajectory.ingestion.indexes import target_session_id_for_item
    from coding_trajectory.ingestion.models import (
        CommandExecutionItem,
        FileChangeItem,
        PlanItem,
        ToolCallItem,
    )

    semantics = item.vendor_data.get("chronicle_semantics") or {}
    tool_name = _item_tool_name(item)
    measurements = getattr(item, "measurements", None)
    summary = getattr(measurements, "tool_summary", None) if measurements else None
    detail: dict[str, Any] = {
        "tool_name": tool_name,
        "concept": summary.get("name")
        if isinstance(summary, dict)
        else getattr(concept, "value", concept),
    }
    summary_detail = summary.get("detail") if isinstance(summary, dict) else None
    if isinstance(summary_detail, dict):
        detail["target_kind"] = summary_detail.get("kind")
        detail["target"] = summary_detail.get("target")
    if isinstance(item, FileChangeItem):
        detail["path"] = item.path
        detail["operation"] = item.operation or item.tool_name or "edit"
    if isinstance(item, CommandExecutionItem):
        detail["exit_code"] = item.exit_code
    verification_kind = semantics.get("verification_kind")
    if isinstance(verification_kind, str):
        detail["verification_kind"] = verification_kind
    resolution_key = semantics.get("resolution_key")
    if isinstance(resolution_key, str):
        detail["resolution_key"] = resolution_key
    if isinstance(item, (PlanItem, ToolCallItem)):
        for edge_type in ("spawned_subagent", "handoff_to"):
            target = target_session_id_for_item(index, item, edge_type=edge_type)
            if target is not None:
                detail["target_session_id"] = str(target)
                break
    return {key: value for key, value in detail.items() if value is not None} or None


def _item_source_order_key(
    item: Any,
    *,
    session_started_at: datetime,
    turn_sequence: int,
) -> str:
    return (
        f"item:{_time_key(session_started_at)}:{item.session_id}:"
        f"{turn_sequence:012d}:{item.turn_id}:{item.sequence:012d}:"
        f"{_time_key(item.started_at)}:{item.item_id}"
    )


def _event_source_order_key(
    session_started_at: datetime,
    session_id: UUID,
    source_sequence: int,
    timestamp: datetime,
    event_id: UUID,
) -> str:
    return (
        f"event:{_time_key(session_started_at)}:{session_id}:"
        f"{source_sequence:012d}:{_time_key(timestamp)}:{event_id}"
    )


def _time_key(value: datetime) -> str:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).isoformat(timespec="microseconds")


def _normalized_value(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value)).lower()


def _event_lifecycle_status(value: Any) -> str | None:
    from coding_trajectory.ingestion.models import EventType

    return {
        EventType.TOOL_CALL_REQUESTED: "requested",
        EventType.TOOL_CALL_SUCCEEDED: "completed",
        EventType.TOOL_CALL_FAILED: "failed",
    }.get(value)


def _turn_for_event(
    session_graph: SessionGraph,
    event_id: UUID,
    related_item: Any,
) -> Any:
    if related_item is not None:
        for session in session_graph.sessions:
            for turn in session.turns:
                if turn.turn_id == related_item.turn_id:
                    return turn
    for session in session_graph.sessions:
        for turn in session.turns:
            if event_id == turn.user_request_event_id or event_id in turn.event_ids:
                return turn
    return None


def _event_ids_for_turn(
    session_graph: SessionGraph,
    turn_id: str | None,
) -> set[UUID] | None:
    if turn_id is None:
        return None
    parsed_turn_id = _parse_user_id(turn_id)
    for session in session_graph.sessions:
        for turn in session.turns:
            if turn.turn_id != parsed_turn_id:
                continue
            return {
                *turn.event_ids,
                *(
                    [turn.user_request_event_id]
                    if turn.user_request_event_id is not None
                    else []
                ),
                *(event_id for item in turn.items for event_id in item.event_ids),
            }
    raise ResourceNotFoundError(f"turn not found in selected session: {turn_id}")


SERVICE_HANDLERS: dict[str, ServiceHandler] = {
    "project.list": _handle_project_list,
    "project.sessions": _handle_project_sessions,
    "living.events": _handle_living_events,
    "session.overview": _handle_session_overview,
    "session.summary": _handle_session_summary,
    "session.search": _handle_session_search,
    "session.tree": _handle_session_tree,
    "graph.overview": _handle_graph_overview,
    "session.stats": _handle_session_stats,
    "graph.stats": _handle_graph_stats,
    "session.usage": _handle_session_usage,
    "graph.usage": _handle_graph_usage,
    "session.model_usage": _handle_session_model_usage,
    "session.request_usage": _handle_session_request_usage,
    "session.tool_usage": _handle_session_tool_usage,
    "session.events": _handle_session_events,
    "session.items": _handle_session_items,
}
