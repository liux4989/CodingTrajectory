#!/usr/bin/env python3
"""Fail-closed Wrangler realtime trace analyzer for prepared API staging runs."""

from __future__ import annotations

import argparse
import json
import math
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON field: {key}")
        result[key] = value
    return result


DECODER = json.JSONDecoder(object_pairs_hook=unique_object)
CORRELATION_FIELDS = ("traceId", "trace_id", "traceID", "invocationId", "invocation_id")


def decode_objects(path: Path) -> list[dict[str, Any]]:
    text = path.read_text()
    result: list[dict[str, Any]] = []
    position = 0
    while True:
        while position < len(text) and text[position].isspace():
            position += 1
        if position == len(text):
            break
        try:
            value, end = DECODER.raw_decode(text, position)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"malformed or truncated Wrangler realtime JSON at character {error.pos}"
            ) from error
        require(isinstance(value, dict), "trace record must be an object")
        result.append(value)
        position = end
    require(result, "trace capture is empty")
    return result


def nested_values(value: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for name, item in value.items():
            if name == key:
                found.append(item)
            found.extend(nested_values(item, key))
    elif isinstance(value, list):
        for item in value:
            found.extend(nested_values(item, key))
    return found


def request_id(event: dict[str, Any]) -> str | None:
    values = nested_values(event.get("event"), "x-ct-stage-request-id")
    values += nested_values(event.get("event"), "X-CT-Stage-Request-Id")
    strings = {value for value in values if isinstance(value, str) and value}
    require(len(strings) <= 1, "conflicting request ID headers")
    return next(iter(strings), None)


def correlations(event: dict[str, Any]) -> set[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    for field in CORRELATION_FIELDS:
        for value in nested_values(event, field):
            if isinstance(value, str) and value:
                result.add((field, value))
    return result


def timing(event: dict[str, Any], name: str) -> int:
    value = event.get(name)
    require(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0,
        f"{name} must be a non-negative integer millisecond value",
    )
    return value


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def expected_classes(plan: dict[str, Any], mode: str) -> dict[tuple[str, str], int]:
    shapes = [row["shape"] for row in plan["fixtures"]]
    if mode == "preflight":
        return {
            (shape, "trace-preflight"): plan["preflight"]["requests_per_shape"]
            for shape in shapes
        }
    result: dict[tuple[str, str], int] = {}
    for shape in shapes:
        result[(shape, "first-read-after-publication")] = 1
        result[(shape, "sequential")] = plan["full"]["sequential_per_shape"]
        result[(shape, "concurrent")] = (
            plan["full"]["concurrent_batches_per_shape"] * plan["full"]["concurrency"]
        )
    return result


def shape_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    models: dict[str, int] = defaultdict(int)
    top_fields: set[str] = set()
    event_fields: set[str] = set()
    header_fields: set[str] = set()
    correlation_fields: set[str] = set()
    for event in events:
        top_fields.update(event)
        model = event.get("executionModel")
        models[str(model)] += 1
        payload = event.get("event")
        if isinstance(payload, dict):
            event_fields.update(payload)
        for field in CORRELATION_FIELDS:
            if nested_values(event, field):
                correlation_fields.add(field)
        for headers in nested_values(payload, "headers"):
            if isinstance(headers, dict):
                header_fields.update(
                    key
                    for key in headers
                    if key.lower() not in ("authorization", "cookie")
                )
    return {
        "records": len(events),
        "execution_models": dict(sorted(models.items())),
        "top_level_fields": sorted(top_fields),
        "event_fields": sorted(event_fields),
        "safe_request_header_names": sorted(header_fields),
        "correlation_fields": sorted(correlation_fields),
    }


def analyze(trace_path: Path, http_path: Path, plan_path: Path) -> dict[str, Any]:
    events = decode_objects(trace_path)
    http = json.loads(http_path.read_text(), object_pairs_hook=unique_object)
    plan = json.loads(plan_path.read_text(), object_pairs_hook=unique_object)
    mode = http.get("mode")
    require(mode in ("preflight", "full"), "HTTP evidence mode")
    require(
        http.get("status") == "PASS" and http.get("failure") is None,
        "HTTP evidence failed",
    )
    require(http.get("source_head") == plan.get("source_head"), "source head mismatch")
    require(http.get("source_tree") == plan.get("source_tree"), "source tree mismatch")
    classes = expected_classes(plan, mode)
    rows = http.get("requests")
    require(
        isinstance(rows, list) and len(rows) == sum(classes.values()),
        "HTTP request count",
    )
    expected: dict[str, dict[str, Any]] = {}
    observed_classes: dict[tuple[str, str], int] = defaultdict(int)
    for row in rows:
        require(isinstance(row, dict), "HTTP request row")
        request = row.get("request_id")
        key = (row.get("shape"), row.get("phase"))
        require(
            isinstance(request, str) and request and request not in expected,
            "HTTP request ID",
        )
        require(key in classes, "unexpected HTTP class")
        require(
            row.get("outcome") == "success" and row.get("http_status") == 200,
            "HTTP failure",
        )
        expected[request] = row
        observed_classes[key] += 1
    require(dict(observed_classes) == classes, "HTTP class totals")

    relevant = [
        event
        for event in events
        if event.get("executionModel") in ("stateless", "durableObject")
        and event.get("outcome") == "ok"
    ]
    fetch_by_request: dict[str, dict[str, Any]] = {}
    authority_events: list[dict[str, Any]] = []
    for event in relevant:
        model = event.get("executionModel")
        if model == "stateless":
            identifier = request_id(event)
            if identifier not in expected:
                continue
            require(identifier not in fetch_by_request, "duplicate fetch trace")
            fetch_by_request[identifier] = event
        else:
            authority_events.append(event)
    require(
        set(fetch_by_request) == set(expected), "incomplete fetch request correlation"
    )

    authority_by_request: dict[str, dict[str, Any]] = {}
    authority_pool = [(event, correlations(event)) for event in authority_events]
    for identifier, fetch_event in fetch_by_request.items():
        direct = request_id(fetch_event)
        require(direct == identifier, "fetch request correlation mismatch")
        fetch_keys = correlations(fetch_event)
        matches = [
            event for event, keys in authority_pool if fetch_keys and fetch_keys & keys
        ]
        require(
            len(matches) == 1, f"authority correlation unavailable for {identifier}"
        )
        authority_by_request[identifier] = matches[0]
    require(
        len({id(event) for event in authority_by_request.values()}) == len(expected),
        "authority event reused across requests",
    )

    samples: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    walls: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    latency: dict[tuple[str, str], list[float]] = defaultdict(list)
    response_bytes: dict[tuple[str, str], list[int]] = defaultdict(list)
    for identifier, row in expected.items():
        class_key = (row["shape"], row["phase"])
        latency[class_key].append(float(row["elapsed_ms"]))
        response_bytes[class_key].append(int(row["response_bytes"]))
        for component, event in (
            ("fetch", fetch_by_request[identifier]),
            ("authority", authority_by_request[identifier]),
        ):
            samples[(*class_key, component)].append(timing(event, "cpuTime"))
            walls[(*class_key, component)].append(timing(event, "wallTime"))

    result_rows: list[dict[str, Any]] = []
    fetch_pass = True
    for class_key, count in classes.items():
        for component in ("fetch", "authority"):
            values = samples[(*class_key, component)]
            require(len(values) == count, "trace class count")
            p50 = percentile(values, 0.50)
            p95 = percentile(values, 0.95)
            p99 = percentile(values, 0.99)
            over_nine = sum(value > 9 for value in values)
            gate = p95 <= 6 and p99 <= 7.5
            if mode == "full" and component == "fetch":
                fetch_pass &= gate
            result_rows.append(
                {
                    "shape": class_key[0],
                    "phase": class_key[1],
                    "component": component,
                    "samples": len(values),
                    "cpu_p50_ms": None if mode == "preflight" else p50,
                    "cpu_p95_ms": None if mode == "preflight" else p95,
                    "cpu_p99_ms": None if mode == "preflight" else p99,
                    "cpu_over_9ms_count": over_nine,
                    "cpu_max_ms": max(values),
                    "wall_p95_ms": None
                    if mode == "preflight"
                    else percentile(walls[(*class_key, component)], 0.95),
                    "gate": "PREFLIGHT_ONLY"
                    if mode == "preflight"
                    else "PASS"
                    if gate
                    else "FAIL",
                }
            )
        result_rows.append(
            {
                "shape": class_key[0],
                "phase": class_key[1],
                "component": "http",
                "samples": len(latency[class_key]),
                "latency_p50_ms": percentile(latency[class_key], 0.50),
                "latency_p95_ms": percentile(latency[class_key], 0.95),
                "latency_p99_ms": percentile(latency[class_key], 0.99),
                "response_bytes_min": min(response_bytes[class_key]),
                "response_bytes_max": max(response_bytes[class_key]),
            }
        )
    return {
        "schema_version": "ct.prepared-api-staging-trace-analysis.v1",
        "run_id": http["run_id"],
        "source_head": plan["source_head"],
        "source_tree": plan["source_tree"],
        "deployed_version": http["deployed_version"],
        "mode": mode,
        "trace_source": "wrangler-realtime-4.129.1",
        "trace_shape": shape_summary(events),
        "capture_status": "AVAILABLE" if mode == "full" else "AVAILABLE_PREFLIGHT",
        "fetch_cpu_status": "PREFLIGHT_ONLY"
        if mode == "preflight"
        else "PASS"
        if fetch_pass
        else "FAIL",
        "memory_status": "UNQUALIFIED",
        "r2_observation": {
            "estimated_reads": http["estimated_r2_reads"],
            "estimated_body_bytes": http["estimated_r2_body_bytes"],
            "observed_binding_reads": None,
            "observed_binding_bytes": None,
        },
        "rows": result_rows,
    }


def self_test() -> None:
    requests = []
    events = []
    for shape in ("representative", "near-budget"):
        for sequence in range(4):
            identifier = f"{shape}-{sequence}"
            trace = f"trace-{shape}-{sequence}"
            requests.append(
                {
                    "request_id": identifier,
                    "shape": shape,
                    "phase": "trace-preflight",
                    "outcome": "success",
                    "http_status": 200,
                    "elapsed_ms": 5 + sequence,
                    "response_bytes": 100 + sequence,
                }
            )
            events.extend(
                [
                    {
                        "outcome": "ok",
                        "executionModel": "durableObject",
                        "cpuTime": 0,
                        "wallTime": 1,
                        "traceId": trace,
                        "event": {"rpcMethod": "invoke"},
                    },
                    {
                        "outcome": "ok",
                        "executionModel": "stateless",
                        "cpuTime": 3 + sequence,
                        "wallTime": 5 + sequence,
                        "traceId": trace,
                        "event": {
                            "request": {
                                "headers": {"x-ct-stage-request-id": identifier}
                            }
                        },
                    },
                ]
            )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        plan = {
            "source_head": "h",
            "source_tree": "t",
            "fixtures": [{"shape": "representative"}, {"shape": "near-budget"}],
            "preflight": {"requests_per_shape": 4},
        }
        http = {
            "mode": "preflight",
            "status": "PASS",
            "failure": None,
            "source_head": "h",
            "source_tree": "t",
            "run_id": "run",
            "deployed_version": "version",
            "estimated_r2_reads": 28,
            "estimated_r2_body_bytes": 1,
            "requests": requests,
        }
        (root / "trace").write_text(
            "\n".join(json.dumps(row, indent=2) for row in events)
        )
        (root / "http").write_text(json.dumps(http))
        (root / "plan").write_text(json.dumps(plan))
        result = analyze(root / "trace", root / "http", root / "plan")
        require(result["capture_status"] == "AVAILABLE_PREFLIGHT", "self-test result")
        events[0].pop("traceId")
        (root / "trace").write_text(
            "\n".join(json.dumps(row, indent=2) for row in events)
        )
        try:
            analyze(root / "trace", root / "http", root / "plan")
        except ValueError as error:
            require(
                "authority correlation unavailable" in str(error),
                "fail-closed self-test",
            )
        else:
            raise AssertionError("missing authority correlation was accepted")
    print(
        "PASS trace analyzer: pretty framing, exact fetch/authority joins, CPU fields, fail-closed missing correlation"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path, nargs="?")
    parser.add_argument("http", type=Path, nargs="?")
    parser.add_argument("plan", type=Path, nargs="?")
    parser.add_argument("output", type=Path, nargs="?")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    require(
        all((args.trace, args.http, args.plan, args.output)),
        "trace, http, plan, output required",
    )
    try:
        report = analyze(args.trace, args.http, args.plan)
    except (ValueError, KeyError, TypeError) as error:
        events: list[dict[str, Any]] = []
        try:
            events = decode_objects(args.trace)
        except ValueError:
            pass
        report = {
            "schema_version": "ct.prepared-api-staging-trace-analysis.v1",
            "capture_status": "UNQUALIFIED",
            "fetch_cpu_status": "UNQUALIFIED",
            "memory_status": "UNQUALIFIED",
            "error": str(error),
            "trace_shape": shape_summary(events) if events else None,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return (
        0 if report.get("capture_status") in ("AVAILABLE", "AVAILABLE_PREFLIGHT") else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
