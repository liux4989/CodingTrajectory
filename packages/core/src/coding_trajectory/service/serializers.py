"""Serialization helpers and id parsing for the service layer."""

from __future__ import annotations

from collections import Counter
from typing import Any
from uuid import UUID

from coding_trajectory.analysis.session_stats import (
    session_graph_preview,
    session_graph_title,
)
from coding_trajectory.ingestion.common import format_datetime, prune_nones
from coding_trajectory.ingestion.execution_models import (
    ToolScriptExecution,
    TurnExecutionLayer,
)
from coding_trajectory.ingestion.models import (
    Event,
    EventType,
    Item,
    SessionGraph,
    Vendor,
)


def attach_execution_layer(
    graph: SessionGraph, payload: dict[str, Any], *, turn_id: str | None = None
) -> dict[str, Any]:
    """Project canonical parents for exactly the evidence returned by a view."""
    from coding_trajectory.ingestion.indexes import build_session_graph_index

    index = build_session_graph_index(graph)
    selected_ids: set[str] | None = None
    for key in ("items", "events", "tool_items", "matches"):
        if isinstance(payload.get(key), list):
            selected_ids = set()
            for row in payload[key]:
                refs = row.get("references", row)
                identity = refs.get("item_id")
                if identity:
                    selected_ids.add(str(identity))
                script = (
                    index.script_by_item_id.get(UUID(identity)) if identity else None
                )
                if script:
                    refs["script_id"] = script.unit_id
            break
    visible_turns = (
        {str(t["turn_id"]) for t in payload["turns"]}
        if isinstance(payload.get("turns"), list) and payload["turns"]
        else None
    )
    if isinstance(payload.get("requests"), list):
        visible_turns = {str(row["turn_id"]) for row in payload["requests"]}
    needed_parents = {
        index.script_by_item_id[UUID(identity)].unit_id
        for identity in selected_ids or set()
        if UUID(identity) in index.script_by_item_id
    }
    layers = []
    for session in graph.sessions:
        for turn in session.turns:
            turn_visible = (not turn_id or str(turn.turn_id) == str(turn_id)) and (
                visible_turns is None or str(turn.turn_id) in visible_turns
            )
            units = [
                u
                for u in turn.execution_units
                if u.unit_id in needed_parents
                or turn_visible
                and (
                    selected_ids is None
                    or selected_ids.intersection(
                        u.item_ids
                        if isinstance(u, ToolScriptExecution)
                        else [u.item_id]
                    )
                )
            ]
            if units:
                layers.append(
                    TurnExecutionLayer(
                        session_id=str(session.session_id),
                        turn_id=str(turn.turn_id),
                        units=units,
                    ).model_dump(mode="json")
                )
    payload["execution_units"] = layers
    observed: Counter[str] = Counter()
    candidates: Counter[str] = Counter()
    coverage: Counter[str] = Counter()
    scripts = 0
    for layer in layers:
        for unit in layer["units"]:
            if unit["kind"] == "tool_script":
                scripts += 1
                coverage[unit["inventory_coverage"]] += 1
                observed.update(c["tool_name"] for c in unit["observed_external_calls"])
                candidates.update(c["tool_name"] for c in unit["source_only_calls"])
            else:
                item = index.items_by_id.get(UUID(unit["item_id"]))
                if item is not None and item.kind in {
                    "tool_call",
                    "command_execution",
                    "file_change",
                    "plan",
                }:
                    observed[item.tool_name or item.kind] += 1
    payload["execution_composition"] = {
        "scope": "returned_execution_units",
        "script_executions": scripts,
        "script_inventory_coverage": dict(coverage),
        "observed_calls": [
            {"tool_name": name, "count": count}
            for name, count in sorted(observed.items())
        ],
        "source_only_sites": [
            {"tool_name": name, "count": count}
            for name, count in sorted(candidates.items())
        ],
        "attribution": "membership_does_not_allocate_content_or_usage",
    }
    return payload


def _optional_positive_int(params: dict[str, Any], key: str) -> int | None:
    value = params.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{key} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a positive integer") from exc
    if parsed < 1:
        raise ValueError(f"{key} must be a positive integer")
    return parsed


def _public_output_for_session_graph(session_graph: SessionGraph, payload: Any) -> Any:
    """Attach source coverage without presenting absent Amp usage as zero."""
    amp_sessions = [s for s in session_graph.sessions if s.vendor == Vendor.AMP]
    if not amp_sessions:
        return payload
    coverage = {
        "source": "amp_live_plugin",
        "provider_usage": "unavailable"
        if len(amp_sessions) == len(session_graph.sessions)
        else "partial",
        "timestamps": "observed_not_provider_execution",
        "relationships": "explicit_creation_results_only",
        "content_tokens": "estimated_not_billed",
    }
    if isinstance(payload, dict):
        payload["measurement_coverage"] = coverage
        payload.setdefault("warnings", []).append(
            "Amp capture contains observed activity, not provider usage or exact inference timing; "
            "request counts describe recorded usage observations, not all inference requests."
        )
        if coverage["provider_usage"] == "unavailable":
            _omit_unavailable_amp_usage(payload)
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                item["measurement_coverage"] = coverage
                if coverage["provider_usage"] == "unavailable":
                    _omit_unavailable_amp_usage(item)
    return payload


def _omit_unavailable_amp_usage(payload: dict[str, Any]) -> None:
    # Walk projection containers only, never raw tool/message bodies.
    for key in ("usage", "total_usage", "token_usage", "billed_token_usage"):
        if key in payload:
            payload[key] = {"availability": "unavailable", "source": "amp_live_plugin"}
    for key in (
        "estimated_cost",
        "allocated_real_token_cost",
        "item_real_token_costs",
        "model_active_seconds",
        "processed_tokens_per_second",
        "output_tokens_per_second",
    ):
        if key in payload:
            payload[key] = None
    runtime = payload.get("runtime")
    if isinstance(runtime, dict):
        _omit_unavailable_amp_usage(runtime)
    for key in ("turns", "sessions", "models", "requests", "items"):
        entries = payload.get(key)
        if not isinstance(entries, list):
            continue
        for item in entries:
            if isinstance(item, dict):
                _omit_unavailable_amp_usage(item)


def serialize_session_graph_detail(session_graph: SessionGraph) -> dict[str, Any]:
    vendors = sorted(
        {session.vendor.value for session in session_graph.sessions if session.vendor}
    )
    return prune_nones(
        {
            "graph_id": str(session_graph.root_session_id),
            "root_session_id": str(session_graph.root_session_id),
            "title": session_graph_title(session_graph),
            "preview": session_graph_preview(session_graph),
            "vendors": vendors or None,
            "session_ids": [
                str(session.session_id) for session in session_graph.sessions
            ],
        }
    )


def serialize_event_detail(
    event: Event,
    *,
    related_item: Item | None = None,
) -> dict[str, Any]:
    return prune_nones(
        {
            "event_id": str(event.event_id),
            "session_id": str(event.session_id),
            "timestamp": format_datetime(event.timestamp),
            "type": event.type.value,
            "tool_call": serialize_tool_call_detail(
                event,
                related_item=related_item,
            ),
            "llm": serialize_llm_detail(event),
            "usage": serialize_usage_detail(event),
            "text": serialize_text_detail(event),
        }
    )


def serialize_tool_call_detail(
    event: Event,
    *,
    related_item: Item | None = None,
) -> dict[str, Any] | None:
    if event.type not in {
        EventType.TOOL_CALL_REQUESTED,
        EventType.TOOL_CALL_SUCCEEDED,
        EventType.TOOL_CALL_FAILED,
    }:
        return None

    payload = event.payload
    status_by_type = {
        EventType.TOOL_CALL_REQUESTED: "in_progress",
        EventType.TOOL_CALL_SUCCEEDED: "done",
        EventType.TOOL_CALL_FAILED: "failed",
    }
    item_result = (
        getattr(related_item, "output", None)
        if related_item is not None
        and event.type
        in {
            EventType.TOOL_CALL_SUCCEEDED,
            EventType.TOOL_CALL_FAILED,
        }
        else None
    )
    result = next(
        (
            value
            for value in (
                payload.get("result"),
                payload.get("tool_output"),
                payload.get("tool_text"),
                item_result,
            )
            if value is not None
        ),
        None,
    )
    return (
        prune_nones(
            {
                "tool_call_id": payload.get("tool_call_id"),
                "tool_name": payload.get("tool_name"),
                "input": payload.get("tool_args") or payload.get("input"),
                "result": result,
                "status": status_by_type.get(event.type),
            }
        )
        or None
    )


def serialize_usage_detail(event: Event) -> dict[str, Any] | None:
    if (
        event.type != EventType.VENDOR_RAW
        or event.payload.get("transcript_kind") != "usage"
    ):
        return None
    metrics = event.payload.get("metrics")
    usage = (
        metrics.get("usage") or metrics.get("last_token_usage")
        if isinstance(metrics, dict)
        else None
    )
    if not isinstance(usage, dict):
        return None
    return prune_nones(
        {
            "provider": (
                metrics.get("provider") if isinstance(metrics, dict) else None
            ),
            "model": metrics.get("model") if isinstance(metrics, dict) else None,
            **usage,
        }
    )


def serialize_llm_detail(event: Event) -> dict[str, Any] | None:
    if event.type != EventType.LLM_RESPONSE:
        return None

    usage = (
        event.payload.get("usage")
        if isinstance(event.payload.get("usage"), dict)
        else {}
    )
    return (
        prune_nones(
            {
                "model": event.payload.get("model")
                or event.payload.get("model_version"),
                "prompt_tokens": usage.get("prompt_tokens")
                or usage.get("input_tokens"),
                "completion_tokens": usage.get("completion_tokens")
                or usage.get("output_tokens"),
                "reported_total_tokens": usage.get("reported_total_tokens")
                or usage.get("total_tokens"),
                "stop_reason": event.payload.get("stop_reason"),
            }
        )
        or None
    )


def serialize_text_detail(event: Event) -> dict[str, Any] | None:
    text = event.payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return {"text": text.strip()}


def _parse_user_id(raw_id: str) -> UUID:
    try:
        return UUID(raw_id)
    except ValueError as exc:
        raise ValueError(f"invalid id: {raw_id!r} is not a valid UUID") from exc


def _normalize_user_id(raw_id: str) -> str:
    return str(_parse_user_id(raw_id))
