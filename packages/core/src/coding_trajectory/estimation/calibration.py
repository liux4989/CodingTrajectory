"""Deterministic cohort calibration statistics.

Forecast kinds and estimator/prompt/retrieval versions are never merged
implicitly: every cohort key carries all of them, and rollups only happen
inside one cohort. Statistics that lack sufficient positive, non-constant
observations are ``undefined`` with a reason.
"""

from __future__ import annotations

import math
import random
from typing import Any

from coding_trajectory.estimation.comparison import DURATION_BUCKETS

CALIBRATION_POLICY: dict[str, Any] = {
    "version": "ct.estimation.calibration.v1",
    "min_samples": 3,
    "min_compression_samples": 5,
    "within_factor": 1.5,
    "bootstrap_samples": 2000,
    "bootstrap_seed": 20260814,
    "duration_buckets": [label for label, _ in DURATION_BUCKETS],
}

_UNDEFINED = "undefined"


def compute_calibration(
    forecasts: list[dict[str, Any]],
    *,
    filters: dict[str, Any],
    include_buckets: bool = True,
) -> dict[str, Any]:
    selected = [record for record in forecasts if _matches(record, filters)]

    cohorts: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for record in selected:
        cohorts.setdefault(_cohort_key(record), []).append(record)

    results = [
        _cohort_response(key, records, include_buckets=include_buckets)
        for key, records in sorted(cohorts.items(), key=lambda item: str(item[0]))
    ]
    return {"policy": dict(CALIBRATION_POLICY), "cohorts": results}


def _cohort_key(record: dict[str, Any]) -> tuple[Any, ...]:
    estimator = record.get("estimator") or {}
    retrieval = record.get("retrieval") or {}
    return (
        record.get("forecast_kind"),
        estimator.get("provider"),
        estimator.get("model"),
        estimator.get("effort"),
        estimator.get("prompt_version"),
        estimator.get("schema_version"),
        retrieval.get("policy_version"),
    )


def _cohort_response(
    key: tuple[Any, ...],
    records: list[dict[str, Any]],
    *,
    include_buckets: bool,
) -> dict[str, Any]:
    (
        kind,
        provider,
        model,
        effort,
        prompt_version,
        schema_version,
        retrieval_policy_version,
    ) = key
    exclusions: dict[str, int] = {}
    usable: list[dict[str, Any]] = []
    for record in records:
        reason = _exclusion_reason(record)
        if reason is not None:
            exclusions[reason] = exclusions.get(reason, 0) + 1
            continue
        usable.append(record)

    pairs = sorted(
        (
            (
                float(record["p50_minutes"]),
                float(record["p80_minutes"]),
                float(record["comparison"]["actual_execution_seconds"]) / 60.0,
            )
            for record in usable
        ),
        key=lambda pair: (pair[2], pair[0]),
    )

    response: dict[str, Any] = {
        "cohort": {
            "forecast_kind": kind,
            "estimator_provider": provider,
            "estimator_model": model,
            "estimator_effort": effort,
            "prompt_version": prompt_version,
            "schema_version": schema_version,
            "retrieval_policy_version": retrieval_policy_version,
        },
        "eligible_count": len(records),
        "primary_count": sum(
            1 for record in records if record.get("role") == "primary"
        ),
        "exclusions": exclusions,
        "statistics": _statistics(pairs),
    }
    if include_buckets:
        response["buckets"] = _bucket_breakdown(usable)
    return response


def _exclusion_reason(record: dict[str, Any]) -> str | None:
    if record.get("role") != "primary":
        return "duplicate_non_primary"
    if record.get("status") == "unbound":
        return "unbound"
    comparison = record.get("comparison")
    if comparison is None:
        return "uncompared"
    if comparison.get("exclusion"):
        return str(comparison["exclusion"])
    actual = comparison.get("actual_execution_seconds")
    if not actual or actual <= 0:
        return "zero_duration"
    if record.get("p50_minutes") is None or record.get("p80_minutes") is None:
        return "missing_forecast"
    return None


def _statistics(pairs: list[tuple[float, float, float]]) -> dict[str, Any]:
    n = len(pairs)
    min_samples = CALIBRATION_POLICY["min_samples"]
    result: dict[str, Any] = {"sample_count": n}
    if n < min_samples:
        result["calibration_ratio"] = {
            "value": _UNDEFINED,
            "reason": f"fewer than {min_samples} usable observations",
        }
        result["median_absolute_log_error"] = _UNDEFINED
        result["within_1_5x_share"] = _UNDEFINED
        result["p80_coverage"] = _UNDEFINED
        result["compression_exponent"] = {
            "value": _UNDEFINED,
            "reason": "insufficient samples",
        }
        return result

    log_ratios = [math.log(p50 / actual) for p50, _, actual in pairs]
    ratios = [math.exp(value) for value in log_ratios]
    geo_mean = math.exp(sum(log_ratios) / n)
    result["calibration_ratio"] = {
        "value": round(geo_mean, 4),
        "interval_95": _bootstrap_interval(ratios),
    }
    abs_log_errors = sorted(abs(value) for value in log_ratios)
    result["median_absolute_log_error"] = round(_median(abs_log_errors), 4)
    within = CALIBRATION_POLICY["within_factor"]
    result["within_1_5x_share"] = round(
        sum(1 for p50, _, actual in pairs if max(p50 / actual, actual / p50) <= within)
        / n,
        4,
    )
    result["p80_coverage"] = round(
        sum(1 for _, p80, actual in pairs if actual <= p80) / n,
        4,
    )
    result["compression_exponent"] = _compression_exponent(pairs)
    return result


def _compression_exponent(pairs: list[tuple[float, float, float]]) -> Any:
    min_samples = CALIBRATION_POLICY["min_compression_samples"]
    if len(pairs) < min_samples:
        return {
            "value": _UNDEFINED,
            "reason": f"fewer than {min_samples} usable observations",
        }
    xs = [math.log(actual) for _, _, actual in pairs]
    ys = [math.log(p50) for p50, _, _ in pairs]
    mean_x = sum(xs) / len(xs)
    variance_x = sum((x - mean_x) ** 2 for x in xs)
    if variance_x == 0:
        return {"value": _UNDEFINED, "reason": "constant actual durations"}
    mean_y = sum(ys) / len(ys)
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    return {"value": round(covariance / variance_x, 4)}


def _bucket_breakdown(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[tuple[float, float, float]]] = {
        label: [] for label, _ in DURATION_BUCKETS
    }
    for record in records:
        comparison = record.get("comparison") or {}
        label = comparison.get("duration_bucket")
        if label in buckets:
            actual = comparison["actual_execution_seconds"] / 60.0
            buckets[label].append(
                (
                    float(record["p50_minutes"]),
                    float(record["p80_minutes"]),
                    float(actual),
                )
            )
    breakdown: list[dict[str, Any]] = []
    within = CALIBRATION_POLICY["within_factor"]
    for label, _ in DURATION_BUCKETS:
        pairs = buckets[label]
        entry: dict[str, Any] = {"bucket": label, "sample_count": len(pairs)}
        if pairs:
            log_ratios = [math.log(p50 / actual) for p50, _, actual in pairs]
            entry["calibration_ratio"] = round(
                math.exp(sum(log_ratios) / len(pairs)), 4
            )
            entry["within_1_5x_share"] = round(
                sum(
                    1
                    for p50, _, actual in pairs
                    if max(p50 / actual, actual / p50) <= within
                )
                / len(pairs),
                4,
            )
            entry["outcome"] = "unknown"
        breakdown.append(entry)
    return breakdown


def _bootstrap_interval(ratios: list[float]) -> list[float]:
    """Deterministic bootstrap 95% interval for the geometric mean ratio."""

    rng = random.Random(CALIBRATION_POLICY["bootstrap_seed"])
    n = len(ratios)
    samples = CALIBRATION_POLICY["bootstrap_samples"]
    estimates: list[float] = []
    log_ratios = [math.log(value) for value in ratios]
    for _ in range(samples):
        draw = [log_ratios[rng.randrange(n)] for _ in range(n)]
        estimates.append(math.exp(sum(draw) / n))
    estimates.sort()
    lower = estimates[max(0, round(0.025 * samples) - 1)]
    upper = estimates[min(samples - 1, round(0.975 * samples) - 1)]
    return [round(lower, 4), round(upper, 4)]


def _median(sorted_values: list[float]) -> float:
    n = len(sorted_values)
    middle = n // 2
    if n % 2:
        return sorted_values[middle]
    return (sorted_values[middle - 1] + sorted_values[middle]) / 2


def _matches(record: dict[str, Any], filters: dict[str, Any]) -> bool:
    target = record.get("target") or {}
    estimator = record.get("estimator") or {}
    retrieval = record.get("retrieval") or {}
    checks = {
        "forecast_kind": record.get("forecast_kind"),
        "project_name": record.get("project_name"),
        "target_harness_name": target.get("harness_name"),
        "target_model": target.get("model"),
        "estimator_model": estimator.get("model"),
        "prompt_version": estimator.get("prompt_version"),
        "retrieval_policy_version": retrieval.get("policy_version"),
    }
    for key, actual in checks.items():
        expected = filters.get(key)
        if expected is not None and actual != expected:
            return False
    return True
