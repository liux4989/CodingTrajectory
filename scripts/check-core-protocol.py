#!/usr/bin/env python3
"""Check the frozen public Core and Chronicle protocol schemas."""

from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path
from typing import Any

from coding_trajectory.contracts import SERVICE_CONTRACTS
from coding_trajectory.contracts.envelope import (
    CORE_PROTOCOL,
    ApiErrorResponse,
    ApiSuccessResponse,
)
from coding_trajectory.contracts.registry import ServiceContract
from coding_trajectory.control_plane.chronicle import (
    CHRONICLE_GRAPH_SCHEMA_VERSION,
    ChronicleGraphArtifact,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "validation" / "core-protocol.json"


def build_snapshot() -> dict[str, Any]:
    methods = {
        method: {
            "method_version": contract.version,
            "request": contract.request_model.model_json_schema(),
            "result": contract.response_model.model_json_schema(),
        }
        for method, contract in sorted(SERVICE_CONTRACTS.items())
    }
    return {
        "snapshot_version": 1,
        "registry_schema_version": ServiceContract.schema_version,
        "method_count": len(methods),
        "methods": methods,
        "core_envelope": {
            "protocol": CORE_PROTOCOL,
            "success": ApiSuccessResponse[Any].model_json_schema(),
            "error": ApiErrorResponse.model_json_schema(),
        },
        "chronicle": {
            "schema_version": CHRONICLE_GRAPH_SCHEMA_VERSION,
            "schema": ChronicleGraphArtifact.model_json_schema(),
        },
    }


def snapshot_text() -> str:
    return json.dumps(build_snapshot(), indent=2, sort_keys=True) + "\n"


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
            f"({len(SERVICE_CONTRACTS)} methods, {CHRONICLE_GRAPH_SCHEMA_VERSION})"
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
