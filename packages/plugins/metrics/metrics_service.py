from __future__ import annotations

import json
import math
import os
import shutil
import statistics
import subprocess
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

TOKEN_KEYS = (
    "prompt_tokens",
    "uncached_prompt_tokens",
    "cached_prompt_tokens",
    "cache_write_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "processed_tokens",
)


class CohortRequest(BaseModel):
    since_days: int = Field(default=7, ge=1, le=365)


class CohortSummary(BaseModel):
    since_days: int
    session_graph_count: int
    turn_count: int
    usage_eligible: int
    pricing_eligible: int
    runtime_eligible: int
    cache_eligible: int
    generated_at: datetime


class FilterOption(BaseModel):
    value: str
    label: str
    count: int


class OptionsResponse(BaseModel):
    schema_version: Literal[1] = 1
    since_days: int
    projects: list[FilterOption]
    vendors: list[FilterOption]
    models: list[FilterOption]
    session_graph_count: int


class Highlight(BaseModel):
    key: str
    label: str
    value: float | int | None
    format: Literal["integer", "tokens", "percent", "usd", "duration", "ratio"]
    detail: str


class ChartPoint(BaseModel):
    key: str
    label: str
    primary: float
    secondary: float | None = None
    tertiary: float | None = None
    sample_count: int


class ComparisonRow(BaseModel):
    key: str
    label: str
    provider: str | None = None
    model: str | None = None
    graphs: int
    turns: int
    processed_tokens: int
    processed_tokens_per_second: float | None
    cache_hit_rate: float | None
    cost_usd: float | None
    pricing_coverage: int
    active_seconds: int
    wait_seconds: int


class SessionRow(BaseModel):
    session_graph_id: str
    project: str | None
    title: str | None
    vendor: str | None
    model_label: str
    mixed_models: bool
    turns: int
    processed_tokens: int | None
    processed_tokens_per_second: float | None
    cost_usd: float | None
    cost_confidence: str | None
    active_seconds: int | None
    wait_seconds: int | None


class CategoryResponse(BaseModel):
    schema_version: Literal[1] = 1
    category: Literal["tokens", "cost", "execution"]
    chart: str
    cohort: CohortSummary
    highlights: list[Highlight]
    chart_points: list[ChartPoint]
    comparison_rows: list[ComparisonRow]
    sessions: list[SessionRow]
    warnings: list[str]


class ValidationCheck(BaseModel):
    session_graph_id: str
    processed_tokens: int | None
    direct_processed_tokens: int | None
    cost_usd: float | None
    direct_cost_usd: float | None
    passed: bool


class ValidationResponse(BaseModel):
    schema_version: Literal[1] = 1
    since_days: int
    checked_graphs: int
    passed: bool
    checks: list[ValidationCheck]


@dataclass(frozen=True, slots=True)
class GraphRecord:
    session_graph_id: str
    project: str | None
    title: str | None
    vendor: str | None
    usage: dict[str, Any]
    runtime: dict[str, Any]
    cost: dict[str, Any] | None
    models: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]


class MetricsService:
    def __init__(self, *, cache_ttl_seconds: float = 30) -> None:
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cache: dict[int, tuple[float, tuple[GraphRecord, ...]]] = {}
        self._lock = threading.Lock()

    def refresh(self) -> dict[str, str]:
        with self._lock:
            self._cache.clear()
        return {"status": "refreshed"}

    def options(self, since_days: int = 7) -> OptionsResponse:
        request = CohortRequest(since_days=since_days)
        graphs = self._cohort(request.since_days)
        project_counts: dict[str, int] = defaultdict(int)
        vendor_counts: dict[str, int] = defaultdict(int)
        model_counts: dict[str, int] = defaultdict(int)
        for graph in graphs:
            project_counts[graph.project or "Unknown project"] += 1
            vendor_counts[graph.vendor or "Unknown vendor"] += 1
            for model_key in _graph_model_keys(graph):
                model_counts[model_key] += 1
        return OptionsResponse(
            since_days=request.since_days,
            projects=_options(project_counts),
            vendors=_options(vendor_counts),
            models=_options(model_counts),
            session_graph_count=len(graphs),
        )

    def category(
        self,
        category: Literal["tokens", "cost", "execution"],
        *,
        since_days: int = 7,
        chart: str | None = None,
    ) -> CategoryResponse:
        request = CohortRequest(since_days=since_days)
        graphs = self._cohort(request.since_days)
        chart_mode = _chart_mode(category, chart)
        groups = _comparison_groups(graphs, execution=category == "execution")
        groups.sort(key=lambda group: _group_sort_value(category, group), reverse=True)
        cohort = _cohort_summary(graphs, request.since_days)
        warnings = list(dict.fromkeys(warning for graph in graphs for warning in graph.warnings))[:20]
        return CategoryResponse(
            category=category,
            chart=chart_mode,
            cohort=cohort,
            highlights=_highlights(category, graphs, cohort),
            chart_points=_chart_points(category, chart_mode, groups),
            comparison_rows=[_comparison_row(group) for group in groups[:50]],
            sessions=_session_rows(category, graphs),
            warnings=warnings,
        )

    def validate(self, *, since_days: int = 7, sample_size: int = 5) -> ValidationResponse:
        request = CohortRequest(since_days=since_days)
        graphs = self._cohort(request.since_days)
        sample = list(graphs[: max(0, sample_size)])
        direct = _batch_results(
            [
                {
                    "id": graph.session_graph_id,
                    "method": "session.usage",
                    "params": {"session_id": graph.session_graph_id},
                }
                for graph in sample
            ]
        )
        checks: list[ValidationCheck] = []
        for graph in sample:
            payload = direct.get(graph.session_graph_id) or {}
            usage = _usage(payload)
            direct_cost = _cost_value(payload.get("estimated_cost"))
            processed = _optional_int(graph.usage.get("processed_tokens"))
            direct_processed = _optional_int(usage.get("processed_tokens"))
            cost = _cost_value(graph.cost)
            passed = processed == direct_processed and _equal_optional_float(cost, direct_cost)
            checks.append(
                ValidationCheck(
                    session_graph_id=graph.session_graph_id,
                    processed_tokens=processed,
                    direct_processed_tokens=direct_processed,
                    cost_usd=cost,
                    direct_cost_usd=direct_cost,
                    passed=passed,
                )
            )
        return ValidationResponse(
            since_days=request.since_days,
            checked_graphs=len(checks),
            passed=all(check.passed for check in checks),
            checks=checks,
        )

    def _cohort(self, since_days: int) -> tuple[GraphRecord, ...]:
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(since_days)
            if cached and now - cached[0] < self._cache_ttl_seconds:
                return cached[1]
        graphs = tuple(_load_graphs(since_days))
        with self._lock:
            self._cache[since_days] = (time.monotonic(), graphs)
        return graphs


def _load_graphs(since_days: int) -> list[GraphRecord]:
    session_result = _api_call(
        "project.sessions",
        {"since_days": since_days, "include": ["runtime", "usage"]},
    )
    session_items = [item for item in session_result.get("items") or [] if isinstance(item, dict)]
    graph_ids = [
        str(item.get("root_session_id") or item.get("id"))
        for item in session_items
        if item.get("root_session_id") or item.get("id")
    ]
    usage_results: dict[str, dict[str, Any]] = {}
    model_results: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(graph_ids), 50):
        chunk = graph_ids[offset : offset + 50]
        requests = [
            {
                "id": f"usage:{graph_id}",
                "method": "session.usage",
                "params": {"session_id": graph_id},
            }
            for graph_id in chunk
        ] + [
            {
                "id": f"models:{graph_id}",
                "method": "session.model_usage",
                "params": {"session_id": graph_id},
            }
            for graph_id in chunk
        ]
        results = _batch_results(requests)
        for graph_id in chunk:
            usage_results[graph_id] = results.get(f"usage:{graph_id}") or {}
            model_results[graph_id] = results.get(f"models:{graph_id}") or {}

    records: list[GraphRecord] = []
    for item in session_items:
        graph_id = str(item.get("root_session_id") or item.get("id") or "")
        if not graph_id:
            continue
        usage_payload = usage_results.get(graph_id) or {}
        model_payload = model_results.get(graph_id) or {}
        vendors = item.get("vendors") or []
        warnings = [str(value) for value in item.get("warnings") or []]
        warnings.extend(str(value) for value in usage_payload.get("warnings") or [])
        warnings.extend(str(value) for value in model_payload.get("warnings") or [])
        records.append(
            GraphRecord(
                session_graph_id=graph_id,
                project=_optional_str(model_payload.get("project") or item.get("project")),
                title=_optional_str(model_payload.get("title") or item.get("title")),
                vendor=_optional_str(model_payload.get("vendor") or (vendors[0] if vendors else None)),
                usage=_usage(usage_payload) or dict(item.get("usage") or {}),
                runtime=dict(usage_payload.get("graph_runtime") or usage_payload.get("runtime") or item.get("runtime") or {}),
                cost=usage_payload.get("estimated_cost") if isinstance(usage_payload.get("estimated_cost"), dict) else None,
                models=tuple(row for row in model_payload.get("models") or [] if isinstance(row, dict)),
                warnings=tuple(dict.fromkeys(warnings)),
            )
        )
    return records


def _api_call(method: str, params: dict[str, Any]) -> dict[str, Any]:
    payload = _ct_json(
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
        raise RuntimeError(str(error.get("message") or f"ct api request failed: {method}"))
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"ct api request returned invalid result: {method}")
    return result


def _batch_results(requests: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not requests:
        return {}
    payload = _ct_json(
        [
            "api",
            "batch",
            "--global-scope",
            "--requests",
            json.dumps(requests),
        ]
    )
    results: dict[str, dict[str, Any]] = {}
    for item in payload.get("items") or []:
        if not isinstance(item, dict) or not item.get("ok"):
            continue
        result = item.get("result")
        if isinstance(result, dict):
            results[str(item.get("id"))] = result
    return results


def _ct_json(args: list[str]) -> dict[str, Any]:
    command = os.environ.get("CT_COMMAND") or shutil.which("ct")
    if not command:
        raise RuntimeError("ct executable not found; set CT_COMMAND to the ct command path")
    completed = subprocess.run(
        [command, *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "ct command failed")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ct command returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("ct command returned a non-object JSON payload")
    return payload


def _cohort_summary(graphs: tuple[GraphRecord, ...], since_days: int) -> CohortSummary:
    return CohortSummary(
        since_days=since_days,
        session_graph_count=len(graphs),
        turn_count=sum(_int(graph.runtime.get("turns")) for graph in graphs),
        usage_eligible=sum(graph.usage.get("processed_tokens") is not None for graph in graphs),
        pricing_eligible=sum(_cost_value(graph.cost) is not None for graph in graphs),
        runtime_eligible=sum(graph.runtime.get("execution_seconds") is not None for graph in graphs),
        cache_eligible=sum(_cache_rate(graph.usage) is not None for graph in graphs),
        generated_at=datetime.now(UTC),
    )


def _highlights(
    category: str,
    graphs: tuple[GraphRecord, ...],
    cohort: CohortSummary,
) -> list[Highlight]:
    if category == "tokens":
        processed = [_int(graph.usage.get("processed_tokens")) for graph in graphs if graph.usage.get("processed_tokens") is not None]
        cached = sum(_int(graph.usage.get("cached_prompt_tokens")) for graph in graphs)
        uncached = sum(_int(graph.usage.get("uncached_prompt_tokens")) for graph in graphs)
        prompt = sum(_int(graph.usage.get("prompt_tokens")) for graph in graphs)
        output = sum(_int(graph.usage.get("completion_tokens")) + _int(graph.usage.get("reasoning_tokens")) for graph in graphs)
        return [
            Highlight(key="processed", label="Processed tokens", value=sum(processed), format="tokens", detail=f"{cohort.usage_eligible}/{len(graphs)} graphs with usage"),
            Highlight(key="median", label="Median per graph", value=_median(processed), format="tokens", detail=f"{len(processed)} eligible graphs"),
            Highlight(key="cache", label="Cache hit rate", value=_safe_div(cached, cached + uncached) * 100 if cached + uncached else None, format="percent", detail=f"{cohort.cache_eligible}/{len(graphs)} graphs with cache telemetry"),
            Highlight(key="output_input", label="Output / input", value=_safe_div(output, prompt) if prompt else None, format="ratio", detail="Completion plus reasoning over prompt tokens"),
        ]
    if category == "cost":
        costs = [value for graph in graphs if (value := _cost_value(graph.cost)) is not None]
        confidence_counts: dict[str, int] = defaultdict(int)
        for graph in graphs:
            if graph.cost:
                confidence_counts[str(graph.cost.get("confidence") or "unknown")] += 1
        return [
            Highlight(key="total", label="Supported cost", value=round(sum(costs), 8), format="usd", detail=f"{len(costs)}/{len(graphs)} priced graphs"),
            Highlight(key="median", label="Median per graph", value=_median(costs), format="usd", detail=f"{len(costs)} priced graphs"),
            Highlight(key="reported", label="Reported coverage", value=confidence_counts.get("reported", 0), format="integer", detail=f"of {len(graphs)} graphs"),
            Highlight(key="estimated", label="Estimated coverage", value=confidence_counts.get("estimated", 0), format="integer", detail=f"of {len(graphs)} graphs"),
        ]
    active = [_int(graph.runtime.get("execution_seconds")) for graph in graphs if graph.runtime.get("execution_seconds") is not None]
    wait = [_int(graph.runtime.get("wait_seconds")) for graph in graphs if graph.runtime.get("wait_seconds") is not None]
    turns = [_int(graph.runtime.get("turns")) for graph in graphs if graph.runtime.get("turns") is not None]
    return [
        Highlight(key="active", label="Active execution", value=sum(active), format="duration", detail=f"{len(active)}/{len(graphs)} graphs with runtime"),
        Highlight(key="median_active", label="Median active time", value=_median(active), format="duration", detail=f"{len(active)} eligible graphs"),
        Highlight(key="median_wait", label="Median wait time", value=_median(wait), format="duration", detail=f"{len(wait)} eligible graphs"),
        Highlight(key="median_turns", label="Median turns", value=_median(turns), format="integer", detail="Workflow complexity indicator"),
    ]


def _comparison_groups(graphs: tuple[GraphRecord, ...], *, execution: bool) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for graph in graphs:
        if execution:
            model_keys = _graph_model_keys(graph)
            if len(model_keys) == 1:
                key = label = model_keys[0]
            elif model_keys:
                key, label = "mixed-models", "Mixed models"
            else:
                key, label = "unknown-model", "Unknown model"
            group = _group(groups, key, label, None, None)
            _add_graph_runtime(group, graph)
            _add_usage(group["usage"], graph.usage)
            group["turns"] += _int(graph.runtime.get("turns"))
            continue
        for model in graph.models:
            provider = _optional_str(model.get("provider"))
            model_name = _optional_str(model.get("model"))
            key = _model_key(provider, model_name)
            group = _group(groups, key, key, provider, model_name)
            group["graph_ids"].add(graph.session_graph_id)
            group["turns"] += _int(model.get("turns"))
            usage = model.get("usage") if isinstance(model.get("usage"), dict) else {}
            _add_usage(group["usage"], usage)
            group["processed_values"].append(_int(usage.get("processed_tokens")))
            model_active = model.get("model_active_seconds")
            if model_active is None:
                group["model_rate_complete"] = False
            else:
                group["model_active_values"].append(float(model_active))
            group["cache_rates"].append(_cache_rate(usage))
            cost = _cost_value(model.get("estimated_cost"))
            if cost is not None:
                group["cost_values"].append(cost)
    return list(groups.values())


def _group_sort_value(category: str, group: dict[str, Any]) -> float:
    if category == "cost":
        return float(sum(group["cost_values"]))
    if category == "execution":
        return float(sum(group["active_values"]))
    return float(_int(group["usage"].get("processed_tokens")))


def _group(
    groups: dict[str, dict[str, Any]],
    key: str,
    label: str,
    provider: str | None,
    model: str | None,
) -> dict[str, Any]:
    if key not in groups:
        groups[key] = {
            "key": key,
            "label": label,
            "provider": provider,
            "model": model,
            "graph_ids": set(),
            "turns": 0,
            "usage": {token_key: 0 for token_key in TOKEN_KEYS},
            "processed_values": [],
            "model_active_values": [],
            "model_rate_complete": True,
            "cache_rates": [],
            "cost_values": [],
            "active_values": [],
            "wait_values": [],
            "tool_call_values": [],
        }
    return groups[key]


def _add_graph_runtime(group: dict[str, Any], graph: GraphRecord) -> None:
    group["graph_ids"].add(graph.session_graph_id)
    group["active_values"].append(_int(graph.runtime.get("execution_seconds")))
    group["wait_values"].append(_int(graph.runtime.get("wait_seconds")))
    group["tool_call_values"].append(_int(graph.runtime.get("tool_calls")))
    group["processed_values"].append(_int(graph.usage.get("processed_tokens")))
    group["cache_rates"].append(_cache_rate(graph.usage))
    cost = _cost_value(graph.cost)
    if cost is not None:
        group["cost_values"].append(cost)


def _chart_points(category: str, chart: str, groups: list[dict[str, Any]]) -> list[ChartPoint]:
    points: list[ChartPoint] = []
    for group in groups[:12]:
        sample_count = len(group["graph_ids"])
        if category == "tokens":
            if chart == "distribution":
                primary, secondary, tertiary = _median(group["processed_values"]), _percentile(group["processed_values"], 75), _percentile(group["processed_values"], 90)
            elif chart == "cache-hit-rate":
                rates = [value for value in group["cache_rates"] if value is not None]
                primary, secondary, tertiary = (_mean(rates) * 100 if rates else 0), None, None
            elif chart == "input-output":
                primary = _safe_div(_int(group["usage"].get("prompt_tokens")), sample_count)
                secondary = _safe_div(_int(group["usage"].get("completion_tokens")) + _int(group["usage"].get("reasoning_tokens")), sample_count)
                tertiary = None
            else:
                primary = _safe_div(_int(group["usage"].get("processed_tokens")), sample_count)
                secondary = _safe_div(_int(group["usage"].get("cached_prompt_tokens")), sample_count)
                tertiary = _safe_div(_int(group["usage"].get("completion_tokens")) + _int(group["usage"].get("reasoning_tokens")), sample_count)
        elif category == "cost":
            costs = group["cost_values"]
            if chart == "distribution":
                primary, secondary, tertiary = _median(costs), _percentile(costs, 75), _percentile(costs, 90)
            elif chart == "total":
                primary, secondary, tertiary = sum(costs), None, None
            else:
                primary, secondary, tertiary = _mean(costs), _median(costs), None
        else:
            active = group["active_values"]
            wait = group["wait_values"]
            if chart == "distribution":
                primary, secondary, tertiary = _median(active), _percentile(active, 75), _percentile(active, 90)
            elif chart == "active-wait":
                primary, secondary, tertiary = _mean(active), _mean(wait), None
            elif chart == "turns":
                primary = _safe_div(group["turns"], sample_count)
                secondary = _mean(group["tool_call_values"])
                tertiary = None
            else:
                primary, secondary, tertiary = _mean(active), _median(active), None
        points.append(ChartPoint(key=group["key"], label=group["label"], primary=primary or 0, secondary=secondary, tertiary=tertiary, sample_count=sample_count))
    return points


def _comparison_row(group: dict[str, Any]) -> ComparisonRow:
    rates = [value for value in group["cache_rates"] if value is not None]
    costs = group["cost_values"]
    return ComparisonRow(
        key=group["key"],
        label=group["label"],
        provider=group["provider"],
        model=group["model"],
        graphs=len(group["graph_ids"]),
        turns=group["turns"],
        processed_tokens=_int(group["usage"].get("processed_tokens")),
        processed_tokens_per_second=(
            _safe_div(
                _int(group["usage"].get("processed_tokens")),
                sum(group["model_active_values"]),
            )
            if group["model_rate_complete"] and group["model_active_values"]
            else None
        ),
        cache_hit_rate=_mean(rates) * 100 if rates else None,
        cost_usd=round(sum(costs), 8) if costs else None,
        pricing_coverage=len(costs),
        active_seconds=sum(group["active_values"]),
        wait_seconds=sum(group["wait_values"]),
    )


def _session_rows(category: str, graphs: tuple[GraphRecord, ...]) -> list[SessionRow]:
    rows = [
        SessionRow(
            session_graph_id=graph.session_graph_id,
            project=graph.project,
            title=graph.title,
            vendor=graph.vendor,
            model_label=_graph_model_label(graph),
            mixed_models=len(_graph_model_keys(graph)) > 1,
            turns=_int(graph.runtime.get("turns")),
            processed_tokens=_optional_int(graph.usage.get("processed_tokens")),
            processed_tokens_per_second=(
                float(graph.runtime["processed_tokens_per_second"])
                if graph.runtime.get("processed_tokens_per_second") is not None
                else None
            ),
            cost_usd=_cost_value(graph.cost),
            cost_confidence=_optional_str(graph.cost.get("confidence")) if graph.cost else None,
            active_seconds=_optional_int(graph.runtime.get("execution_seconds")),
            wait_seconds=_optional_int(graph.runtime.get("wait_seconds")),
        )
        for graph in graphs
    ]
    key = {
        "tokens": lambda row: row.processed_tokens if row.processed_tokens is not None else -1,
        "cost": lambda row: row.cost_usd if row.cost_usd is not None else -1,
        "execution": lambda row: row.active_seconds if row.active_seconds is not None else -1,
    }[category]
    return sorted(rows, key=key, reverse=True)[:100]


def _chart_mode(category: str, chart: str | None) -> str:
    modes = {
        "tokens": ("usage", "distribution", "cache-hit-rate", "input-output"),
        "cost": ("per-session", "distribution", "total"),
        "execution": ("active", "distribution", "active-wait", "turns"),
    }[category]
    return chart if chart in modes else modes[0]


def _options(counts: dict[str, int]) -> list[FilterOption]:
    return [FilterOption(value=value, label=value, count=count) for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


def _graph_model_keys(graph: GraphRecord) -> list[str]:
    return list(dict.fromkeys(_model_key(_optional_str(model.get("provider")), _optional_str(model.get("model"))) for model in graph.models))


def _graph_model_label(graph: GraphRecord) -> str:
    keys = _graph_model_keys(graph)
    if not keys:
        return "Unknown model"
    if len(keys) > 1:
        return "Mixed models"
    return keys[0]


def _model_key(provider: str | None, model: str | None) -> str:
    return f"{provider or 'unknown'}/{model or 'unknown'}"


def _usage(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("graph_total_usage", "total_usage", "graph_usage", "usage"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _add_usage(target: dict[str, int], usage: dict[str, Any]) -> None:
    for key in TOKEN_KEYS:
        target[key] = target.get(key, 0) + _int(usage.get(key))


def _cache_rate(usage: dict[str, Any]) -> float | None:
    if usage.get("cached_prompt_tokens") is None or usage.get("uncached_prompt_tokens") is None:
        return None
    cached = _int(usage.get("cached_prompt_tokens"))
    uncached = _int(usage.get("uncached_prompt_tokens"))
    return _safe_div(cached, cached + uncached) if cached + uncached else None


def _cost_value(value: Any) -> float | None:
    if not isinstance(value, dict) or value.get("value_usd") is None:
        return None
    return float(value["value_usd"])


def _median(values: list[int] | list[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _mean(values: list[int] | list[float]) -> float:
    return float(statistics.mean(values)) if values else 0.0


def _percentile(values: list[int] | list[float], percentile: int) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = (len(ordered) - 1) * percentile / 100
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def _safe_div(numerator: float | int, denominator: float | int) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def _int(value: Any) -> int:
    return int(value or 0)


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _equal_optional_float(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    return math.isclose(left, right, rel_tol=0, abs_tol=1e-9)
