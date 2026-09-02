"""Method handlers and dispatch for the session-api.json contract."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any
from uuid import UUID

from coding_trajectory import debug
from coding_trajectory.contracts import service_contract
from coding_trajectory.ingestion.models import Session, SessionGraph
from coding_trajectory.query import DocumentStore, ResourceNotFoundError
from coding_trajectory.service.serializers import (
    _optional_positive_int,
    _parse_user_id,
    _public_output_for_session_graph,
    serialize_event_detail,
    serialize_session_graph_detail,
)
from coding_trajectory.service.store import (
    IndexCache,
    _resolve_session_graph,
    _session_graph_entrypoint_id,
    project_list_metadata,
    resolve_collection,
    resolve_resource,
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
    params = contract.validate_request(params)
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


def _graph_handler(
    build: Callable[[dict[str, Any], SessionGraph], Any],
) -> ServiceHandler:
    """Resolve the graph and wrap the graph-level build result.

    Every ``graph.*`` projection follows the same preamble: resolve the graph
    from the entry-point id, build the projection, and pass it through the
    public output seam. Entry-point caching is centralized in :func:`dispatch`.
    """

    @wraps(build)
    def wrapper(params: dict[str, Any], context: ServiceContext) -> Any:
        session_graph = _resolve_session_graph(
            context.store, _session_graph_entrypoint_id(params)
        )
        from coding_trajectory.analysis.orchestration_runs import (
            orchestration_run_for_entrypoint,
        )

        entrypoint = _session_graph_entrypoint_id(params)
        session_graph = orchestration_run_for_entrypoint(
            session_graph,
            _parse_user_id(entrypoint) if entrypoint else None,
        )
        return _public_output_for_session_graph(
            session_graph, build(params, session_graph)
        )

    return wrapper


def _single_session_graph(
    session_graph: SessionGraph, entrypoint_id: str | None
) -> SessionGraph:
    """Select one canonical session from a graph for ``session.*`` methods."""
    from coding_trajectory.ingestion.indexes import build_session_graph_index

    index = build_session_graph_index(session_graph)
    selected: Session | None = None
    if entrypoint_id:
        resource_id = _parse_user_id(entrypoint_id)
        selected = index.sessions_by_id.get(resource_id)
        if selected is None:
            turn = index.turns_by_id.get(resource_id)
            if turn is not None:
                selected = index.sessions_by_id.get(turn.session_id)
    if selected is None:
        selected = index.sessions_by_id.get(session_graph.root_session_id)
    if selected is None:
        selected = min(
            session_graph.sessions,
            key=lambda item: (item.started_at, str(item.session_id)),
            default=None,
        )
    if selected is None:
        raise ValueError("session_graph has no sessions")
    return SessionGraph(
        root_session_id=selected.session_id,
        project_identifier=session_graph.project_identifier,
        summary=None,
        sessions=[selected],
    )


def _single_session_handler(
    build: Callable[[dict[str, Any], SessionGraph], Any],
) -> ServiceHandler:
    """Resolve one thread while retaining the source graph for cache lookup."""

    @wraps(build)
    def wrapper(params: dict[str, Any], context: ServiceContext) -> Any:
        session_graph = _resolve_session_graph(
            context.store, _session_graph_entrypoint_id(params)
        )
        selected_graph = _single_session_graph(
            session_graph, _session_graph_entrypoint_id(params)
        )
        return _public_output_for_session_graph(
            selected_graph, build(params, selected_graph)
        )

    return wrapper


def _canonical_session_handler(
    build: Callable[[dict[str, Any], SessionGraph], Any],
) -> ServiceHandler:
    """Resolve an exact canonical session ID for strict retrieval methods."""

    @wraps(build)
    def wrapper(params: dict[str, Any], context: ServiceContext) -> Any:
        session_id = params["session_id"]
        session = context.store.get_session(_parse_user_id(session_id))
        session_graph = context.store.get_session_graph_for_session(session.session_id)
        selected_graph = _single_session_graph(session_graph, session_id)
        return _public_output_for_session_graph(
            selected_graph, build(params, selected_graph)
        )

    return wrapper


def _handle_project_sessions(
    params: dict[str, Any], context: ServiceContext
) -> dict[str, Any]:
    from coding_trajectory.analysis.session_graph_views import (
        session_graph_has_visible_overview_content,
    )

    session_graphs = resolve_collection(
        context.store,
        "session_graph",
        global_scope=context.global_scope,
        current_dir=context.current_dir,
        project_name=params.get("project_name"),
        agent_vendor=params.get("agent_vendor"),
    )
    include = set(params.get("include") or [])
    items: list[dict[str, Any]] = []
    from coding_trajectory.analysis.orchestration_runs import orchestration_runs

    for lineage_graph in session_graphs:
        for graph in orchestration_runs(lineage_graph):
            if not session_graph_has_visible_overview_content(graph):
                continue
            item = {
                **serialize_session_graph_detail(graph),
                "project": graph.project_identifier,
                "lineage_root_session_id": str(lineage_graph.root_session_id),
            }
            if "usage" in include:
                from coding_trajectory.metrics import build_session_graph_usage

                usage = build_session_graph_usage(graph)
                item["usage"] = usage.get("total_usage") or {}
                item["warnings"] = usage.get("warnings") or []
                if "runtime" in include:
                    item["runtime"] = usage.get("runtime") or {}
            elif "runtime" in include:
                from coding_trajectory.metrics import build_session_graph_runtime

                item["runtime"] = build_session_graph_runtime(graph)
            items.append(_public_output_for_session_graph(graph, item))
    return {"items": items}


def _handle_project_list(
    params: dict[str, Any], context: ServiceContext
) -> dict[str, Any]:
    """Dispatch adapter for ``project.list``.

    Production traffic is short-circuited in :meth:`ServiceRuntime.call` to
    :func:`project_list_metadata` (which never builds a store). This handler
    keeps ``dispatch("project.list", ...)`` consistent with that path by
    delegating to the same canonical implementation, so the contract registry
    and the handler registry agree.
    """
    return project_list_metadata(
        params,
        global_scope=context.global_scope,
        current_dir=context.current_dir,
    )


def _handle_living_events(
    params: dict[str, Any], context: ServiceContext
) -> dict[str, Any]:
    from coding_trajectory.living_events import query_living_events

    return query_living_events(
        params,
        document_store=context.store,
        cache=context.cache,
        current_dir=context.current_dir,
        global_scope=context.global_scope,
    )


@_single_session_handler
def _handle_session_overview(
    params: dict[str, Any], session_graph: SessionGraph
) -> Any:
    from coding_trajectory.analysis.projections import build_session_graph_overview

    return build_session_graph_overview(
        session_graph,
        num_turns=_optional_positive_int(params, "num_turns"),
        drop_turns=_optional_positive_int(params, "drop_turns"),
    )


@_canonical_session_handler
def _handle_session_summary(params: dict[str, Any], session_graph: SessionGraph) -> Any:
    from coding_trajectory.analysis.session_retrieval import build_session_summary

    return build_session_summary(session_graph, turn_id=params.get("turn_id"))


@_canonical_session_handler
def _handle_session_search(params: dict[str, Any], session_graph: SessionGraph) -> Any:
    from coding_trajectory.analysis.session_retrieval import search_session

    return search_session(
        session_graph,
        query=params["query"],
        mode=params["mode"],
        kinds=params["kinds"],
        limit=params["limit"],
        turn_id=params.get("turn_id"),
    )


def _handle_session_tree(params: dict[str, Any], context: ServiceContext) -> Any:
    from coding_trajectory.analysis.orchestration_runs import (
        build_conversation_tree,
        orchestration_run_for_entrypoint,
    )

    entrypoint = _session_graph_entrypoint_id(params)
    session_graph = _resolve_session_graph(context.store, entrypoint)
    tree = build_conversation_tree(session_graph)
    run = orchestration_run_for_entrypoint(
        session_graph,
        _parse_user_id(entrypoint) if entrypoint else None,
    )
    tree["selected_branch_id"] = str(run.root_session_id)
    return tree


@_single_session_handler
def _handle_session_stats(params: dict[str, Any], session_graph: SessionGraph) -> Any:
    return _build_stats_response(session_graph)


@_single_session_handler
def _handle_session_usage(params: dict[str, Any], session_graph: SessionGraph) -> Any:
    from coding_trajectory.metrics import build_session_graph_usage

    return build_session_graph_usage(session_graph, turn_id=params.get("turn_id"))


@_single_session_handler
def _handle_session_model_usage(
    params: dict[str, Any], session_graph: SessionGraph
) -> Any:
    from coding_trajectory.metrics import build_session_graph_model_usage

    return build_session_graph_model_usage(
        session_graph,
        turn_id=params.get("turn_id"),
    )


@_single_session_handler
def _handle_session_request_usage(
    params: dict[str, Any], session_graph: SessionGraph
) -> Any:
    from coding_trajectory.metrics import build_session_graph_request_usage

    include = set(params.get("include") or [])
    return build_session_graph_request_usage(
        session_graph,
        turn_id=params.get("turn_id"),
        include_causality="causality" in include,
        include_context_diagnostics="context" in include,
    )


@_single_session_handler
def _handle_session_tool_usage(
    params: dict[str, Any], session_graph: SessionGraph
) -> Any:
    from coding_trajectory.metrics import build_session_graph_tool_usage

    include = set(params.get("include") or [])
    return build_session_graph_tool_usage(
        session_graph,
        turn_id=params.get("turn_id"),
        include_item_real_token_costs="item_costs" in include,
        include_advanced_causality="causality" in include,
    )


@_graph_handler
def _handle_graph_overview(params: dict[str, Any], session_graph: SessionGraph) -> Any:
    from coding_trajectory.analysis.graph_views import build_graph_overview

    return build_graph_overview(
        session_graph,
        num_turns=_optional_positive_int(params, "num_turns"),
        drop_turns=_optional_positive_int(params, "drop_turns"),
        include_narrative="narrative" in set(params.get("include") or []),
    )


@_graph_handler
def _handle_graph_stats(params: dict[str, Any], session_graph: SessionGraph) -> Any:
    return _build_stats_response(
        session_graph,
        include_session_composition=(
            "session_composition" in set(params.get("include") or [])
        ),
    )


def _build_stats_response(
    session_graph: SessionGraph,
    *,
    include_session_composition: bool = True,
) -> dict[str, Any]:
    from coding_trajectory.metrics import build_session_graph_stats

    return build_session_graph_stats(
        session_graph,
        include_session_composition=include_session_composition,
    )


@_graph_handler
def _handle_graph_usage(params: dict[str, Any], session_graph: SessionGraph) -> Any:
    from coding_trajectory.metrics import build_session_graph_usage

    include = set(params.get("include") or [])
    return build_session_graph_usage(
        session_graph,
        turn_id=params.get("turn_id"),
        include_graph_turns="flat_turns" in include,
    )


def _handle_session_events(
    params: dict[str, Any], context: ServiceContext
) -> dict[str, Any]:
    from coding_trajectory.analysis.projections import build_event_scan
    from coding_trajectory.ingestion.indexes import (
        SessionGraphIndex,
        build_session_graph_index,
        item_for_event,
    )

    event_ids = params.get("event_ids")
    selected_turn_id = params.get("turn_id")
    if event_ids:
        matches: list[dict[str, Any]] = []
        root_session_id: str | None = None
        indexes_by_graph_id: dict[UUID, SessionGraphIndex] = {}
        for eid in event_ids:
            try:
                event = resolve_resource(context.store, "event", eid)
                session_graph = context.store.get_session_graph_for_session(
                    event.session_id
                )
                selected_graph = _single_session_graph(
                    session_graph,
                    params.get("session_id")
                    or params.get("root_session_id")
                    or selected_turn_id
                    or str(event.session_id),
                )
                allowed_event_ids = _event_ids_for_turn(
                    selected_graph,
                    selected_turn_id,
                )
                if (
                    allowed_event_ids is not None
                    and event.event_id not in allowed_event_ids
                ):
                    continue
                if root_session_id is None:
                    root_session_id = str(selected_graph.root_session_id)
                index = indexes_by_graph_id.get(selected_graph.root_session_id)
                if index is None:
                    index = build_session_graph_index(selected_graph)
                    indexes_by_graph_id[selected_graph.root_session_id] = index
                related_item = item_for_event(index, event.event_id)
                detail = _public_output_for_session_graph(
                    selected_graph,
                    serialize_event_detail(event, related_item=related_item),
                )
                matches.append(detail)
            except (ResourceNotFoundError, ValueError) as exc:
                debug.warn(
                    f"skipping unresolved event id {eid!r}: {exc}",
                    code="session.events.event_id_unresolved",
                    event_id=eid,
                )
                continue
        return {
            "root_session_id": root_session_id,
            "type": params.get("type"),
            "matches": matches,
        }

    entrypoint_id = _session_graph_entrypoint_id(params)
    session_graph = _resolve_session_graph(context.store, entrypoint_id)
    session_graph = _single_session_graph(session_graph, entrypoint_id)
    allowed_event_ids = _event_ids_for_turn(session_graph, selected_turn_id)

    event_type = params.get("type")
    if not event_type:
        all_events: list[dict[str, Any]] = []
        for session in session_graph.sessions:
            for event in session.events:
                if (
                    allowed_event_ids is not None
                    and event.event_id not in allowed_event_ids
                ):
                    continue
                detail = serialize_event_detail(event)
                all_events.append(
                    _public_output_for_session_graph(session_graph, detail)
                )
        limit = params.get("limit")
        if limit:
            all_events = all_events[:limit]
        return {
            "root_session_id": str(session_graph.root_session_id),
            "type": None,
            "matches": all_events,
        }

    result = build_event_scan(
        session_graph,
        event_type=event_type,
        filters=params.get("filters") or [],
        event_ids=allowed_event_ids,
    )
    limit = params.get("limit")
    if limit:
        result["matches"] = result["matches"][:limit]
    return _public_output_for_session_graph(session_graph, result)


def _handle_session_items(
    params: dict[str, Any], context: ServiceContext
) -> list[dict[str, Any]]:
    from coding_trajectory.analysis.projections import build_item_details
    from coding_trajectory.ingestion.indexes import (
        SessionGraphIndex,
        build_session_graph_index,
    )
    from coding_trajectory.ingestion.models import PlanItem

    item_ids = params.get("item_ids")
    selected_turn_id = params.get("turn_id")
    include_content = bool(params.get("include_content"))
    if item_ids:
        result: list[dict[str, Any]] = []
        indexes_by_graph_id: dict[UUID, SessionGraphIndex] = {}
        for item_id in item_ids:
            try:
                item = resolve_resource(context.store, "item", item_id)
                session_graph = context.store.get_session_graph_for_session(
                    item.session_id
                )
                if selected_turn_id is not None and str(item.turn_id) != str(
                    selected_turn_id
                ):
                    continue
                index = None
                if isinstance(item, PlanItem):
                    index = indexes_by_graph_id.get(session_graph.root_session_id)
                    if index is None:
                        index = build_session_graph_index(session_graph)
                        indexes_by_graph_id[session_graph.root_session_id] = index
                result.append(
                    _public_output_for_session_graph(
                        session_graph,
                        build_item_details(
                            item,
                            session_graph=session_graph,
                            include_content=include_content,
                            index=index,
                        ),
                    )
                )
            except (ResourceNotFoundError, ValueError) as exc:
                debug.warn(
                    f"skipping unresolved item id {item_id!r}: {exc}",
                    code="session.items.item_id_unresolved",
                    item_id=item_id,
                )
                continue
        return result

    entrypoint_id = params.get("session_id") or params.get("root_session_id")
    session_graph = _resolve_session_graph(context.store, entrypoint_id)
    session_graph = _single_session_graph(session_graph, entrypoint_id)

    types_filter = set(params["types"]) if params.get("types") else None
    projection_index: SessionGraphIndex | None = None
    result = []
    for session in session_graph.sessions:
        for turn in session.turns:
            if selected_turn_id is not None and str(turn.turn_id) != str(
                selected_turn_id
            ):
                continue
            for item in turn.items:
                if types_filter and item.kind not in types_filter:
                    continue
                if isinstance(item, PlanItem) and projection_index is None:
                    projection_index = build_session_graph_index(session_graph)
                result.append(
                    _public_output_for_session_graph(
                        session_graph,
                        build_item_details(
                            item,
                            session_graph=session_graph,
                            include_content=include_content,
                            index=projection_index,
                        ),
                    )
                )
    return result


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


def _estimate_handler(method: str) -> ServiceHandler:
    """Dispatch adapter for ``estimate.*`` methods.

    Production traffic is short-circuited in :meth:`ServiceRuntime.call` (the
    ledger-only methods never build a store). This handler keeps
    ``dispatch(method, ...)`` consistent with that path, the same pattern as
    ``project.list``.
    """

    def handler(params: dict[str, Any], context: ServiceContext) -> Any:
        from coding_trajectory.estimation import serve_estimate

        return serve_estimate(
            method,
            params,
            global_scope=context.global_scope,
            current_dir=context.current_dir,
            cache=context.cache,
        )

    return handler


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
    "estimate.predict": _estimate_handler("estimate.predict"),
    "estimate.bind": _estimate_handler("estimate.bind"),
    "estimate.get": _estimate_handler("estimate.get"),
    "estimate.list": _estimate_handler("estimate.list"),
    "estimate.calibration": _estimate_handler("estimate.calibration"),
    "estimate.backfill.start": _estimate_handler("estimate.backfill.start"),
    "estimate.backfill.status": _estimate_handler("estimate.backfill.status"),
}
