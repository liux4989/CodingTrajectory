#!/usr/bin/env python3
"""Check the frozen public Core and published-fact protocol schemas."""

from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path
from typing import Any

from coding_trajectory.contracts import SERVICE_CONTRACTS
from coding_trajectory.contracts.envelope import (
    API_PROTOCOL,
    ApiErrorResponse,
    ApiSuccessResponse,
)
from coding_trajectory.contracts.registry import ServiceContract
from coding_trajectory.control_plane.published_facts import (
    FACT_SET_SCHEMA_VERSION,
    PublishedFactSet,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "validation" / "core-protocol.json"


def compact_schema(
    schema: dict[str, Any], shared_defs: dict[str, Any]
) -> dict[str, Any]:
    """Remove generated noise and hoist reusable definitions."""
    root_title = schema.get("title")

    def compact(value: Any, *, schema_map: bool = False) -> Any:
        if isinstance(value, list):
            return [compact(item) for item in value]
        if not isinstance(value, dict):
            return value
        if schema_map:
            return {name: compact(item) for name, item in value.items()}

        local_defs = value.get("$defs", {})
        result = {
            key: compact(item, schema_map=key in {"properties", "patternProperties"})
            for key, item in value.items()
            if key not in {"$defs", "title"}
        }
        for name, definition in local_defs.items():
            definition = compact(definition)
            if name in shared_defs and shared_defs[name] != definition:
                raise ValueError(f"conflicting JSON Schema definition: {name}")
            shared_defs[name] = definition

        alternatives = result.get("anyOf")
        if isinstance(alternatives, list) and {"type": "null"} in alternatives:
            non_null = [item for item in alternatives if item != {"type": "null"}]
            if len(alternatives) == 2 and len(non_null) == 1:
                alternative = non_null[0]
                if not alternative:
                    result.pop("anyOf")
                elif "type" in alternative and "enum" not in alternative:
                    result.pop("anyOf")
                    result.update(alternative)
                    types = alternative["type"]
                    result["type"] = (
                        [types, "null"] if isinstance(types, str) else [*types, "null"]
                    )
        return result

    compacted = compact(schema)
    if root_title is not None:
        compacted["title"] = root_title
    return compacted


def build_snapshot() -> dict[str, Any]:
    shared_defs: dict[str, Any] = {}
    methods = {
        method: {
            "method_version": contract.version,
            "request": compact_schema(
                contract.request_model.model_json_schema(), shared_defs
            ),
            "result": compact_schema(
                contract.response_model.model_json_schema(), shared_defs
            ),
        }
        for method, contract in sorted(SERVICE_CONTRACTS.items())
    }
    snapshot = {
        "snapshot_version": 2,
        "registry_schema_version": ServiceContract.schema_version,
        "method_count": len(methods),
        "methods": methods,
        "core_envelope": {
            "protocol": API_PROTOCOL,
            "success": compact_schema(
                ApiSuccessResponse[Any].model_json_schema(), shared_defs
            ),
            "error": compact_schema(ApiErrorResponse.model_json_schema(), shared_defs),
        },
        "published_facts": {
            "schema_version": FACT_SET_SCHEMA_VERSION,
            "schema": compact_schema(PublishedFactSet.model_json_schema(), shared_defs),
        },
    }
    snapshot["$defs"] = shared_defs
    return snapshot


def snapshot_text() -> str:
    return (
        json.dumps(
            build_snapshot(),
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--update",
        action="store_true",
        help="Write the current schemas after an approved protocol proposal.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    current = snapshot_text()
    if args.update:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(current, encoding="utf-8")
        print(f"updated Core protocol baseline: {args.baseline}")
        return 0
    try:
        expected = args.baseline.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(
            f"Core protocol baseline is missing: {args.baseline}\n"
            "After an approved protocol proposal, create it with --update."
        )
        return 1
    if expected == current:
        print(
            "Core protocol freeze: PASS "
            f"({len(SERVICE_CONTRACTS)} methods, {FACT_SET_SCHEMA_VERSION})"
        )
        return 0
    print("Core protocol freeze: FAIL\n")
    print(
        "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                current.splitlines(keepends=True),
                fromfile=str(args.baseline),
                tofile="generated Core protocol",
            )
        ),
        end="",
    )
    print("Protocol changes require an approved proposal and baseline update.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
