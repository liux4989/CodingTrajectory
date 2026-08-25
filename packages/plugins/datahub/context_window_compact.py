"""Compact reshapers from raw service API payloads to projection inputs."""

from __future__ import annotations

from typing import Any


def _compact_stats_api(payload: dict[str, Any]) -> dict[str, Any]:
    model = payload.get("model") or {}
    context = payload.get("context_window") or {}
    runtime = payload.get("runtime") or {}
    messages = payload.get("messages") or {}
    compaction = payload.get("compaction") or {}
    compact = _drop_none(
        {
            "id": payload.get("root_session_id") or payload.get("session_id"),
            "session_id": payload.get("session_id"),
            "role": payload.get("role"),
            "relationship": payload.get("relationship"),
            "parent": payload.get("parent_session_id"),
            "agent_name": payload.get("agent_name"),
            "title": payload.get("title"),
            "vendor": payload.get("vendor"),
            "model": _drop_none(
                {
                    "name": model.get("name"),
                    "context_window": model.get("context_window_tokens"),
                }
            )
            or None,
            "context": _drop_none(
                {
                    "used": context.get("used_tokens"),
                    "pct": context.get("used_percent"),
                    "categories": [
                        _compact_context_category(item)
                        for item in context.get("categories") or []
                    ]
                    or None,
                }
            )
            or None,
            "provider_usage_buckets": [
                _compact_context_category(item)
                for item in payload.get("provider_usage_buckets") or []
            ]
            or None,
            "runtime": _compact_stats_runtime(runtime),
            "compaction": _compact_compaction(compaction),
            "messages": _drop_none(
                {
                    "user": messages.get("user"),
                    "assistant": messages.get("assistant"),
                    "developer": messages.get("developer"),
                    "tools": messages.get("tool_outputs"),
                    "reasoning": messages.get("reasoning_items"),
                    "compacted": messages.get("compacted_contexts"),
                }
            )
            or None,
            "usage": _compact_usage_tokens(payload.get("usage"), include_cost=False),
            "billed_token_usage": _compact_usage_tokens(
                payload.get("billed_token_usage"), include_cost=False
            ),
            "warnings": payload.get("warnings") or None,
        }
    )
    if payload.get("scope"):
        compact["scope"] = payload["scope"]
    compact["sessions"] = [
        _compact_stats_api(item)
        for item in payload.get("sessions") or []
        if isinstance(item, dict)
    ] or None
    return _drop_none(compact)


def _compact_stats_runtime(runtime: dict[str, Any]) -> dict[str, Any] | None:
    return (
        _drop_none(
            {
                "status": runtime.get("status"),
                "start": runtime.get("started_at"),
                "end": runtime.get("ended_at"),
                "execution_seconds": runtime.get("execution_seconds"),
                "model_active_seconds": runtime.get("model_active_seconds"),
                "processed_tokens_per_second": runtime.get(
                    "processed_tokens_per_second"
                ),
                "wait_seconds": runtime.get("wait_seconds"),
                "turns": runtime.get("turns"),
                "items": runtime.get("items"),
                "tools": runtime.get("tool_calls"),
                "failed_tools": runtime.get("failed_tool_calls") or None,
                "subagents": runtime.get("subagent_sessions"),
                "compactions": runtime.get("compactions"),
                "interrupted_turns": runtime.get("interrupted_turns") or None,
                "rollbacks": runtime.get("rollbacks") or None,
                "average_ttft_ms": runtime.get("average_time_to_first_token_ms"),
            }
        )
        or None
    )


def _compact_context_category(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return _drop_none(
        {
            "key": value.get("key"),
            "label": value.get("label"),
            "tokens": value.get("tokens"),
            "usage": value.get("allocated_usage"),
            "estimated_cost": value.get("estimated_cost"),
            "pct": value.get("percent"),
            "chars": value.get("observed_chars"),
            "items": value.get("items"),
            "confidence": value.get("confidence"),
            "source": value.get("source"),
            "children": [
                _compact_context_category(child)
                for child in value.get("children") or []
            ]
            or None,
        }
    )


def _compact_overview_api(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": payload.get("root_session_id"),
        "sessions": [
            _drop_none(
                {
                    "id": session.get("session_id"),
                    "relationship": _compact_relationship(session.get("relationship")),
                    "vendor": session.get("vendor"),
                    "status": session.get("status"),
                    "agent": session.get("agent_name"),
                    "cwd": session.get("cwd"),
                    "compactions": session.get("compactions"),
                    "turns": [
                        _drop_none(
                            {
                                "id": turn.get("turn_id"),
                                "status": turn.get("status"),
                                "request": _compact_request(turn.get("user_request")),
                                "activity": [
                                    _compact_activity(activity)
                                    for activity in turn.get("activity") or []
                                ]
                                or None,
                                "teammate_summary": turn.get("teammate_summary"),
                                "items": (
                                    (turn.get("refs") or {}).get("item_ids")
                                    if isinstance(turn.get("refs"), dict)
                                    else None
                                ),
                            }
                        )
                        for turn in session.get("turns") or []
                        if isinstance(turn, dict)
                    ],
                }
            )
            for session in payload.get("sessions") or []
            if isinstance(session, dict)
        ],
    }


def _compact_relationship(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if value.get("role") == "main":
        return _drop_none({"role": "main", "forks": value.get("forked_session_ids")})
    return (
        _drop_none(
            {
                "type": value.get("relationship"),
                "parent": value.get("parent_session_id"),
                "forks": value.get("forked_session_ids"),
            }
        )
        or None
    )


def _compact_request(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return (
        _drop_none(
            {
                "text": value.get("content")
                or value.get("summary")
                or value.get("text"),
                "source": value.get("source"),
                "type": value.get("type")
                if value.get("type") not in {None, "message"}
                else None,
            }
        )
        or None
    )


def _compact_activity(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if "compaction" in value:
        return _drop_none(
            {
                "compaction": value.get("compaction"),
                "mechanism": value.get("mechanism"),
                "summary": value.get("summary"),
                "trigger": value.get("trigger"),
                "pre": value.get("pre_tokens"),
                "post": value.get("post_tokens"),
                "dropped": value.get("dropped_tokens"),
            }
        )
    if "tool" in value:
        compact = {
            "tool": value.get("tool"),
            "count": value.get("count"),
            "status": value.get("status"),
        }
        for key in ("cmd", "path", "query", "url", "text"):
            if value.get(key) is not None:
                compact[key] = value[key]
        for key in ("paths", "queries", "urls", "targets"):
            if value.get(key) is not None:
                compact[key] = value[key]
        if value.get("item_ids") is not None:
            compact["item_ids"] = value["item_ids"]
        if compact.get("count") == 1:
            compact.pop("count", None)
        return _drop_none(compact)
    if "text" in value:
        return _drop_none(
            {"text": value.get("text"), "item_ids": value.get("item_ids")}
        )
    if "teammate_summary" in value:
        return {"teammate_summary": value.get("teammate_summary")}
    return value


def _compact_usage_api(payload: dict[str, Any]) -> dict[str, Any]:
    runtime = payload.get("runtime") or {}
    effort_changes = payload.get("effort_changes") or {}
    return _drop_none(
        {
            "id": payload.get("session_id"),
            "scope": payload.get("scope"),
            "extra_billing": payload.get("extra_billing"),
            "runtime": _drop_none(
                {
                    "status": runtime.get("status"),
                    "start": runtime.get("started_at"),
                    "end": runtime.get("ended_at"),
                    "execution_seconds": runtime.get("execution_seconds"),
                    "model_active_seconds": runtime.get("model_active_seconds"),
                    "processed_tokens_per_second": runtime.get(
                        "processed_tokens_per_second"
                    ),
                    "wait_seconds": runtime.get("wait_seconds"),
                }
            )
            or None,
            "usage": _compact_usage_tokens(payload.get("total_usage")),
            "cost": _evidence_value(payload.get("estimated_cost")),
            "pricing": _evidence_pricing(payload.get("estimated_cost")),
            "models": _compact_usage_models(payload.get("models")),
            "compaction": _compact_compaction(payload.get("compaction")),
            "effort_changes": _compact_effort_changes(effort_changes),
            "turns": [
                _compact_usage_turn(turn)
                for turn in payload.get("turns") or []
                if isinstance(turn, dict)
            ],
            "sessions": [
                _compact_usage_session(session)
                for session in payload.get("sessions") or []
                if isinstance(session, dict)
            ]
            or None,
            "warnings": payload.get("warnings") or None,
        }
    )


def _compact_usage_tokens(
    value: Any, *, include_cost: bool = True
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return (
        _drop_none(
            {
                "prompt": value.get("prompt_tokens"),
                "uncached_prompt": value.get("uncached_prompt_tokens"),
                "cached_prompt": value.get("cached_prompt_tokens"),
                "cache_write": value.get("cache_write_tokens"),
                "completion": value.get("completion_tokens"),
                "reasoning": value.get("reasoning_tokens"),
                "reported_total": value.get("reported_total_tokens"),
                "processed": value.get("processed_tokens"),
                "prompt_completion": value.get("prompt_completion_tokens"),
                "cost": value.get("cost_usd") if include_cost else None,
            }
        )
        or None
    )


def _compact_usage_turn(value: dict[str, Any]) -> dict[str, Any]:
    runtime = value.get("runtime") or {}
    return _drop_none(
        {
            "id": value.get("turn_id"),
            "session": value.get("session_id"),
            "runtime": _drop_none(
                {
                    "start": runtime.get("started_at"),
                    "end": runtime.get("ended_at"),
                    "execution_seconds": runtime.get("execution_seconds"),
                    "model_active_seconds": runtime.get("model_active_seconds"),
                    "processed_tokens_per_second": runtime.get(
                        "processed_tokens_per_second"
                    ),
                    "wait_before_seconds": runtime.get("wait_before_seconds"),
                }
            )
            or None,
            "usage": _compact_usage_tokens(value.get("usage")),
            "cost": _evidence_value(value.get("estimated_cost")),
            "pricing": _evidence_pricing(value.get("estimated_cost")),
            "cache_break_waste_usd": value.get("cache_break_waste_usd"),
            "cache_break_re_read_tokens": value.get("cache_break_re_read_tokens"),
            "cache_boundary_loss_tokens": value.get("cache_boundary_loss_tokens"),
            "cache_first_call_cached_tokens": value.get(
                "cache_first_call_cached_tokens"
            ),
            "cache_intra_turn_loss_tokens": value.get("cache_intra_turn_loss_tokens"),
            "cache_intra_turn_waste_usd": value.get("cache_intra_turn_waste_usd"),
        }
    )


def _compact_usage_session(value: dict[str, Any]) -> dict[str, Any]:
    runtime = value.get("runtime") or {}
    return _drop_none(
        {
            "id": value.get("session_id"),
            "role": value.get("role"),
            "relationship": value.get("relationship"),
            "parent": value.get("parent_session_id"),
            "agent_name": value.get("agent_name"),
            "title": value.get("title"),
            "runtime": _drop_none(
                {
                    "status": runtime.get("status"),
                    "start": runtime.get("started_at"),
                    "end": runtime.get("ended_at"),
                    "execution_seconds": runtime.get("execution_seconds"),
                    "model_active_seconds": runtime.get("model_active_seconds"),
                    "processed_tokens_per_second": runtime.get(
                        "processed_tokens_per_second"
                    ),
                    "wait_seconds": runtime.get("wait_seconds"),
                    "turns": runtime.get("turns"),
                    "items": runtime.get("items"),
                    "tools": runtime.get("tool_calls"),
                    "failed_tools": runtime.get("failed_tool_calls") or None,
                }
            )
            or None,
            "usage": _compact_usage_tokens(value.get("total_usage")),
            "cost": _evidence_value(value.get("estimated_cost")),
            "pricing": _evidence_pricing(value.get("estimated_cost")),
            "models": _compact_usage_models(value.get("models")),
            "effort_changes": _compact_effort_changes(
                value.get("effort_changes") or {}
            ),
            "turns": [
                _compact_usage_turn(turn)
                for turn in value.get("turns") or []
                if isinstance(turn, dict)
            ],
        }
    )


def _compact_usage_models(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    rows = [
        _drop_none(
            {
                "provider": model.get("provider"),
                "model": model.get("model"),
                "turns": model.get("turns"),
                "model_active_seconds": model.get("model_active_seconds"),
                "processed_tokens_per_second": model.get("processed_tokens_per_second"),
                "usage": _compact_usage_tokens(model.get("usage")),
                "cost": _evidence_value(model.get("estimated_cost")),
                "pricing": _evidence_pricing(model.get("estimated_cost")),
            }
        )
        for model in value
        if isinstance(model, dict)
    ]
    return rows or None


def _compact_effort_changes(value: dict[str, Any]) -> dict[str, Any]:
    return _drop_none(
        {
            "count": value.get("count") or 0,
            "events": [
                _drop_none(
                    {
                        "timestamp": event.get("timestamp"),
                        "from": event.get("effort_from"),
                        "to": event.get("effort_to"),
                    }
                )
                for event in value.get("events") or []
                if isinstance(event, dict)
            ]
            or None,
        }
    )


def _compact_compaction(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return (
        _drop_none(
            {
                "count": value.get("count"),
                "cumulative_dropped": value.get("cumulative_dropped_tokens"),
                "last": _compact_compaction_event(value.get("last")),
                "events": [
                    _compact_compaction_event(event)
                    for event in value.get("events") or []
                    if isinstance(event, dict)
                ]
                or None,
            }
        )
        or None
    )


def _compact_compaction_event(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return (
        _drop_none(
            {
                "mechanism": value.get("mechanism"),
                "timestamp": value.get("timestamp"),
                "trigger": value.get("trigger"),
                "pre": value.get("pre_tokens"),
                "post": value.get("post_tokens"),
                "dropped": value.get("dropped_tokens"),
            }
        )
        or None
    )


def _evidence_value(value: Any) -> float | None:
    if not isinstance(value, dict):
        return None
    raw = value.get("value_usd")
    return (
        float(raw)
        if isinstance(raw, int | float) and not isinstance(raw, bool)
        else None
    )


def _evidence_pricing(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return (
        _drop_none(
            {
                "confidence": value.get("confidence"),
                "source": value.get("source"),
                "effective_date": value.get("effective_date"),
            }
        )
        or None
    )


def _drop_none(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}
