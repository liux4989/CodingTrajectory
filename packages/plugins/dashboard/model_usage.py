from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from pydantic import BaseModel, Field

try:
    from . import token_pricing
except ImportError:
    import token_pricing

TOKEN_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


class ModelUsageFilters(BaseModel):
    since_days: int = Field(default=7, ge=1)
    project_name: str | None = None
    model_key: str | None = None


def build_projection(
    *,
    ct_json: Callable[[list[str]], dict[str, Any]],
    since_days: int = 7,
    project_name: str | None = None,
    model_key: str | None = None,
) -> dict[str, Any]:
    filters = ModelUsageFilters(
        since_days=since_days,
        project_name=project_name,
        model_key=model_key,
    )
    projects_payload = ct_json(
        ["project", "list", "--params", "{}", "--output", "json"]
    )
    projects = _project_options(projects_payload)
    session_params: dict[str, Any] = {
        "since_days": filters.since_days,
        "include": ["runtime", "usage"],
    }
    if filters.project_name:
        session_params["project_name"] = filters.project_name
    sessions_payload = _api_call(ct_json, "project.sessions", session_params)
    session_items = [
        item for item in sessions_payload.get("items") or [] if isinstance(item, dict)
    ]
    session_ids = [
        str(item.get("root_session_id") or item.get("id"))
        for item in session_items
        if item.get("root_session_id") or item.get("id")
    ]
    usage_payloads = _model_usage_batch(ct_json, session_ids)
    all_session_rows = [_session_row(payload) for payload in usage_payloads]
    model_options = _model_options(_model_rows(all_session_rows))
    session_rows = _filter_session_rows_by_model(
        all_session_rows,
        filters.model_key,
    )
    model_rows = _model_rows(session_rows)
    turn_rows = _turn_rows(session_rows)
    total_cost = sum(_number(row["estimated_cost_usd"]) for row in session_rows)
    total_tokens = sum(_usage_total(row.get("usage")) for row in session_rows)
    total_elapsed_seconds = sum(
        int(_number(row.get("elapsed_seconds"))) for row in session_rows
    )

    return {
        "schema_version": 1,
        "filters": filters.model_dump(mode="json"),
        "project_options": projects,
        "model_options": model_options,
        "summary": {
            "sessions": len(session_rows),
            "turns": len(turn_rows),
            "models": len(model_rows),
            "total_tokens": total_tokens,
            "total_elapsed_seconds": total_elapsed_seconds,
            "avg_tokens_per_session": _safe_div(total_tokens, len(session_rows)),
            "avg_tokens_per_turn": _safe_div(total_tokens, len(turn_rows)),
            "avg_elapsed_seconds_per_session": _safe_div(
                total_elapsed_seconds,
                len(session_rows),
            ),
            "estimated_cost_usd": round(total_cost, 8),
            "missing_price_count": sum(
                1
                for row in model_rows
                if row["pricing"].get("confidence") == "missing_price"
            ),
            "top_model_by_cost": model_rows[0]["model_key"] if model_rows else None,
            "top_model_by_sessions": _top_model_by_sessions(model_rows),
        },
        "models": model_rows,
        "sessions": sorted(
            session_rows,
            key=lambda row: _number(row["estimated_cost_usd"]),
            reverse=True,
        ),
        "turns": sorted(
            turn_rows,
            key=lambda row: _number(row["estimated_cost_usd"]),
            reverse=True,
        )[:200],
        "time_buckets": _time_buckets(turn_rows),
        "warnings": [
            {"session_id": row["id"], "message": warning}
            for row in session_rows
            for warning in row.get("warnings") or []
        ],
    }


def _api_call(
    ct_json: Callable[[list[str]], dict[str, Any]],
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    payload = ct_json(
        [
            "api",
            "call",
            method,
            "--global-scope",
            "--params",
            json.dumps(params),
        ]
    )
    if not payload.get("ok"):
        error = payload.get("error") or {}
        raise RuntimeError(
            str(error.get("message") or f"ct api request failed: {method}")
        )
    result = payload.get("result") or {}
    if not isinstance(result, dict):
        raise RuntimeError(f"ct api request returned invalid result: {method}")
    return result


def _model_usage_batch(
    ct_json: Callable[[list[str]], dict[str, Any]],
    session_ids: list[str],
) -> list[dict[str, Any]]:
    if not session_ids:
        return []
    requests = [
        {
            "id": session_id,
            "method": "session.model_usage",
            "params": {"session_id": session_id},
        }
        for session_id in session_ids
    ]
    payload = ct_json(
        [
            "api",
            "batch",
            "--global-scope",
            "--requests",
            json.dumps(requests),
        ]
    )
    rows: list[dict[str, Any]] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict) or not item.get("ok"):
            continue
        result = item.get("result")
        if isinstance(result, dict):
            rows.append(result)
    return rows


def _session_row(payload: dict[str, Any]) -> dict[str, Any]:
    session_id = str(payload.get("id") or payload.get("root_session_id") or "")
    models = [_priced_model(row) for row in payload.get("models") or []]
    total_cost = sum(_number(row["estimated_cost_usd"]) for row in models)
    turns = [
        _priced_turn(row, session_id=session_id) for row in payload.get("turns") or []
    ]
    return {
        "id": session_id,
        "project": payload.get("project"),
        "title": payload.get("title"),
        "vendor": payload.get("vendor"),
        "started_at": payload.get("started_at"),
        "completed_at": payload.get("completed_at"),
        "elapsed_seconds": _elapsed_seconds(
            payload.get("started_at"),
            payload.get("completed_at"),
        ),
        "usage": payload.get("usage") or {},
        "context": payload.get("context"),
        "dominant_model": payload.get("dominant_model"),
        "estimated_cost_usd": round(total_cost, 8),
        "models": models,
        "turns": turns,
        "warnings": payload.get("warnings") or [],
    }


def _filter_session_rows_by_model(
    session_rows: list[dict[str, Any]],
    model_key: str | None,
) -> list[dict[str, Any]]:
    if not model_key:
        return session_rows
    rows: list[dict[str, Any]] = []
    for session in session_rows:
        models = [row for row in session["models"] if row.get("model_key") == model_key]
        if not models:
            continue
        turns = [row for row in session["turns"] if row.get("model_key") == model_key]
        usage = _empty_usage()
        total_cost = 0.0
        for row in models:
            _add_usage(usage, row.get("usage") or {})
            total_cost += _number(row.get("estimated_cost_usd"))
        rows.append(
            {
                **session,
                "usage": usage,
                "dominant_model": _dominant_model(models),
                "estimated_cost_usd": round(total_cost, 8),
                "models": models,
                "turns": turns,
            }
        )
    return rows


def _priced_model(row: dict[str, Any]) -> dict[str, Any]:
    provider = row.get("provider")
    model = row.get("model")
    usage = row.get("usage") or {}
    estimate = token_pricing.estimate_cost(usage, model=model, provider=provider)
    return {
        "provider": provider,
        "model": model,
        "model_key": _model_key(provider, model),
        "turns": int(_number(row.get("turns"))),
        "usage": usage,
        "estimated_cost_usd": estimate.amount_usd if estimate else 0.0,
        "pricing": _pricing_payload(estimate),
    }


def _priced_turn(row: dict[str, Any], *, session_id: str) -> dict[str, Any]:
    provider = row.get("provider")
    model = row.get("model")
    usage = row.get("usage") or {}
    estimate = token_pricing.estimate_cost(usage, model=model, provider=provider)
    return {
        "session_id": session_id,
        "turn_id": row.get("turn_id"),
        "sequence": row.get("sequence"),
        "started_at": row.get("started_at"),
        "provider": provider,
        "model": model,
        "model_key": _model_key(provider, model),
        "usage": usage,
        "context": row.get("context"),
        "estimated_cost_usd": estimate.amount_usd if estimate else 0.0,
        "pricing": _pricing_payload(estimate),
    }


def _model_rows(session_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    session_sets: dict[str, set[str]] = defaultdict(set)
    for session in session_rows:
        for row in session["models"]:
            key = row["model_key"]
            target = grouped.setdefault(
                key,
                {
                    "provider": row.get("provider"),
                    "model": row.get("model"),
                    "model_key": key,
                    "sessions": 0,
                    "turns": 0,
                    "usage": _empty_usage(),
                    "estimated_cost_usd": 0.0,
                    "elapsed_seconds": 0.0,
                    "pricing": row.get("pricing") or {},
                    "avg_session_cost_usd": 0.0,
                    "avg_turn_cost_usd": 0.0,
                    "avg_session_elapsed_seconds": 0.0,
                    "avg_turn_elapsed_seconds": 0.0,
                },
            )
            session_sets[key].add(str(session["id"]))
            target["turns"] += int(_number(row.get("turns")))
            target["estimated_cost_usd"] += _number(row.get("estimated_cost_usd"))
            target["elapsed_seconds"] += _allocated_model_elapsed_seconds(
                session,
                row,
            )
            _add_usage(target["usage"], row.get("usage") or {})
            if target["pricing"].get("confidence") == "missing_price":
                target["pricing"] = row.get("pricing") or {}
    for key, target in grouped.items():
        target["sessions"] = len(session_sets[key])
        target["estimated_cost_usd"] = round(target["estimated_cost_usd"], 8)
        target["avg_session_cost_usd"] = _safe_div(
            target["estimated_cost_usd"], target["sessions"]
        )
        target["avg_turn_cost_usd"] = _safe_div(
            target["estimated_cost_usd"], target["turns"]
        )
        target["elapsed_seconds"] = round(_number(target["elapsed_seconds"]), 3)
        target["avg_session_elapsed_seconds"] = _safe_div(
            target["elapsed_seconds"], target["sessions"]
        )
        target["avg_turn_elapsed_seconds"] = _safe_div(
            target["elapsed_seconds"], target["turns"]
        )
        target["usage"]["total_tokens"] = _usage_total(target["usage"])
    return sorted(
        grouped.values(),
        key=lambda row: (
            _number(row["estimated_cost_usd"]),
            _usage_total(row["usage"]),
        ),
        reverse=True,
    )


def _model_options(model_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "model_key": row["model_key"],
            "provider": row.get("provider"),
            "model": row.get("model"),
            "sessions": row.get("sessions"),
            "turns": row.get("turns"),
            "usage": row.get("usage") or {},
            "estimated_cost_usd": row.get("estimated_cost_usd"),
            "elapsed_seconds": row.get("elapsed_seconds"),
        }
        for row in model_rows
    ]


def _allocated_model_elapsed_seconds(
    session: dict[str, Any],
    model_row: dict[str, Any],
) -> float:
    elapsed = _number(session.get("elapsed_seconds"))
    model_turns = _number(model_row.get("turns"))
    total_turns = sum(_number(row.get("turns")) for row in session.get("models") or [])
    if elapsed <= 0 or model_turns <= 0 or total_turns <= 0:
        return 0.0
    return elapsed * (model_turns / total_turns)


def _dominant_model(models: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not models:
        return None
    row = max(models, key=lambda item: _usage_total(item.get("usage")))
    return {
        "provider": row.get("provider"),
        "model": row.get("model"),
        "basis": "filtered_usage",
    }


def _turn_rows(session_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for session in session_rows:
        for turn in session["turns"]:
            rows.append(
                {
                    **turn,
                    "project": session.get("project"),
                    "session_title": session.get("title"),
                    "vendor": session.get("vendor"),
                }
            )
    return rows


def _time_buckets(turn_rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        "five_hour": _bucket_turns(turn_rows, "five_hour"),
        "daily": _bucket_turns(turn_rows, "daily"),
        "weekly": _bucket_turns(turn_rows, "weekly"),
        "monthly": _bucket_turns(turn_rows, "monthly"),
    }


def _bucket_turns(turn_rows: list[dict[str, Any]], grain: str) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for turn in turn_rows:
        started_at = _parse_datetime(turn.get("started_at"))
        if started_at is None:
            continue
        if (
            _usage_total(turn.get("usage")) == 0
            and _number(turn.get("estimated_cost_usd")) == 0
        ):
            continue
        bucket = _bucket_key(started_at, grain)
        model_key = str(turn.get("model_key") or "unknown")
        target = grouped.setdefault(
            (bucket, model_key),
            {
                "bucket": bucket,
                "model_key": model_key,
                "provider": turn.get("provider"),
                "model": turn.get("model"),
                "turns": 0,
                "estimated_cost_usd": 0.0,
                "usage": _empty_usage(),
            },
        )
        target["turns"] += 1
        target["estimated_cost_usd"] += _number(turn.get("estimated_cost_usd"))
        _add_usage(target["usage"], turn.get("usage") or {})
    rows = []
    for row in grouped.values():
        row["estimated_cost_usd"] = round(row["estimated_cost_usd"], 8)
        row["usage"]["total_tokens"] = _usage_total(row["usage"])
        rows.append(row)
    return sorted(rows, key=lambda row: (row["bucket"], row["model_key"]))


def _project_options(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("items") or {}
    if not isinstance(items, dict):
        return []
    return [
        {
            "name": name,
            "path": item.get("path") if isinstance(item, dict) else None,
            "vendors": item.get("vendors") if isinstance(item, dict) else [],
        }
        for name, item in sorted(items.items())
    ]


def _pricing_payload(estimate: Any | None) -> dict[str, Any]:
    if estimate is None:
        return {
            "confidence": "missing_price",
            "source": None,
            "effective_date": None,
        }
    return {
        "confidence": "estimated",
        "source": estimate.pricing_source,
        "effective_date": estimate.pricing_effective_date,
        "breakdown": estimate.breakdown.model_dump(mode="json"),
    }


def _bucket_key(value: datetime, grain: str) -> str:
    if grain == "five_hour":
        hour = (value.hour // 5) * 5
        return value.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()
    if grain == "weekly":
        start = value - timedelta(days=value.weekday())
        return (
            start.replace(hour=0, minute=0, second=0, microsecond=0).date().isoformat()
        )
    if grain == "monthly":
        return f"{value.year:04d}-{value.month:02d}"
    return value.date().isoformat()


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _elapsed_seconds(started_at: Any, completed_at: Any) -> int:
    start = _parse_datetime(started_at)
    end = _parse_datetime(completed_at)
    if start is None or end is None:
        return 0
    seconds = int((end - start).total_seconds())
    return max(0, seconds)


def _top_model_by_sessions(rows: list[dict[str, Any]]) -> str | None:
    if not rows:
        return None
    return max(rows, key=lambda row: int(_number(row.get("sessions"))))["model_key"]


def _model_key(provider: Any, model: Any) -> str:
    if provider and model:
        return f"{provider}/{model}"
    if model:
        return str(model)
    if provider:
        return f"{provider}/unknown"
    return "unknown"


def _empty_usage() -> dict[str, int]:
    return {key: 0 for key in (*TOKEN_KEYS, "total_tokens")}


def _add_usage(target: dict[str, int], usage: dict[str, Any]) -> None:
    for key in TOKEN_KEYS:
        target[key] = int(target.get(key) or 0) + int(_number(usage.get(key)))
    target["total_tokens"] = _usage_total(target)


def _usage_total(usage: Any) -> int:
    if not isinstance(usage, dict):
        return 0
    total = int(_number(usage.get("total_tokens")))
    if total:
        return total
    return sum(int(_number(usage.get(key))) for key in TOKEN_KEYS)


def _safe_div(numerator: float, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 8)


def _number(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    return 0.0
