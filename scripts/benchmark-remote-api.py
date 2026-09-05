#!/usr/bin/env python3
"""Benchmark authenticated remote reads; retain only aggregate measurements.

Uses CT_SUPABASE_URL, CT_SUPABASE_ANON_KEY, CT_ACCESS_TOKEN and
CT_REMOTE_WORKSPACE_ID, or an explicitly selected Keychain credential profile.
Estimation is excluded because even its reads can persist comparisons.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from pydantic import BaseModel, Field

from coding_trajectory.contracts import SERVICE_CONTRACTS
from coding_trajectory.control_plane.http_service import RemoteRuntimeFactory


class Measurement(BaseModel):
    method: str
    variant: str = "default"
    status: str
    fresh_ms: float
    reused_ms: list[float] = Field(default_factory=list)
    reused_median_ms: float | None = None
    response_bytes: int = 0
    result_count: int | None = None


def forbid_local(*args, **kwargs):
    raise AssertionError("remote benchmark attempted local discovery")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", help="Refresh a configured Keychain profile")
    parser.add_argument("--project", required=True)
    parser.add_argument("--since-days", type=int, default=7)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--session-id", help="Optional canonical session in this scope")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".artifacts/benchmarks/remote-api.json"),
    )
    args = parser.parse_args()
    if args.repeat < 1 or args.since_days < 1:
        parser.error("repeat and since-days must be positive")
    if args.profile:
        from coding_trajectory_cli.collector_credentials import refresh_profile

        credentials = refresh_profile(args.profile)
        profile = credentials.profile
        url, key = str(profile.supabase_url), profile.supabase_api_key
        token, workspace = credentials.access_token, profile.workspace_id
    else:
        url = os.environ["CT_SUPABASE_URL"]
        key = os.environ["CT_SUPABASE_ANON_KEY"]
        token = os.environ["CT_ACCESS_TOKEN"]
        workspace = UUID(os.environ["CT_REMOTE_WORKSPACE_ID"])
    # Server authentication remains authoritative; reject privileged benchmark use.
    segment = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    if claims.get("role") != "authenticated":
        raise ValueError("benchmark requires an ordinary authenticated user")
    factory = RemoteRuntimeFactory(url=url, api_key=key, workspace_id=workspace)
    scoped = {"project_name": args.project, "since_days": args.since_days}
    with factory.build(token) as runtime:
        discovery = runtime.execute({"method": "project.sessions", "params": scoped})
        if not discovery["ok"]:
            raise RuntimeError("remote scope discovery failed")
        sequence = discovery["meta"]["snapshot_sequence"]
        sessions = discovery["result"]["items"]
    if not sessions:
        raise ValueError("no published graphs in selected scope")
    # Default is the first returned graph; record selection policy, never its ID.
    session_id = args.session_id or sessions[0]["root_session_id"]
    if session_id not in {
        value
        for item in sessions
        for value in [item["root_session_id"], *item.get("session_ids", [])]
    }:
        raise ValueError("session is outside the selected scope")
    queries = [("project.list", {}, "default"), ("project.sessions", scoped, "default")]
    for method in SERVICE_CONTRACTS:
        if method.startswith(("session.", "graph.")):
            params = {
                "root_session_id"
                if method.startswith("graph.")
                else "session_id": session_id
            }
            if method == "session.search":
                params["query"] = "validation"
            queries.append((method, params, "default"))
    queries.extend(
        [
            (
                "session.items",
                {"session_id": session_id, "include_content": True},
                "contentful",
            ),
            (
                "living.events",
                {"scope": {"session_id": session_id}, "limit": 50},
                "default",
            ),
            ("living.sessions", {"limit": 50}, "default"),
        ]
    )
    report = {
        "measured_at": datetime.now(UTC).isoformat(),
        "since_days": args.since_days,
        "graphs_in_scope": len(sessions),
        "selection": "explicit session" if args.session_id else "first returned graph",
        "local_discovery_disabled": True,
        "snapshot_pinned": True,
        "timing": "fresh includes runtime creation and snapshot RPC; reused excludes setup; both include execute and JSON serialization; no CLI startup or HTTP facade",
        "measurements": [],
        "skipped": {
            m: "May write state, or needs an existing job identifier"
            for m in SERVICE_CONTRACTS
            if m.startswith("estimate.")
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    failed = False
    for method, params, variant in queries:
        start = time.perf_counter()
        with factory.build(token, snapshot_sequence=sequence) as runtime:
            result = runtime.execute({"method": method, "params": params})
            size = len(json.dumps(result, separators=(",", ":")).encode())
            fresh = (time.perf_counter() - start) * 1000
            expected_rejection = (
                method in {"session.events", "session.search"}
                or variant == "contentful"
            )
            if result["ok"]:
                status = "unexpected_success" if expected_rejection else "passed"
            else:
                is_local_only = "local-only" in str(result.get("error", ""))
                status = (
                    "expected_local_only"
                    if expected_rejection and is_local_only
                    else "failed"
                )
            failed |= status in {"failed", "unexpected_success"}
            runs = []
            if status == "passed":
                for _ in range(args.repeat):
                    start = time.perf_counter()
                    again = runtime.execute({"method": method, "params": params})
                    json.dumps(again, separators=(",", ":")).encode()
                    runs.append((time.perf_counter() - start) * 1000)
                    if not again["ok"]:
                        status, failed = "failed", True
            payload = result.get("result", {})
            count = len(payload) if isinstance(payload, list) else None
            if isinstance(payload, dict) and isinstance(payload.get("items"), list):
                count = len(payload["items"])
            row = Measurement(
                method=method,
                variant=variant,
                status=status,
                fresh_ms=fresh,
                reused_ms=runs,
                reused_median_ms=statistics.median(runs) if runs else None,
                response_bytes=size,
                result_count=count,
            )
            report["measurements"].append(row.model_dump())
            args.output.write_text(json.dumps(report, indent=2) + "\n")
            print(row.model_dump_json(), flush=True)
    return int(failed)


if __name__ == "__main__":
    # Fail closed if a remote route unexpectedly tries the local log resolver.
    with patch("coding_trajectory.service.resolve_store", forbid_local):
        try:
            raise SystemExit(main())
        except Exception as exc:
            # Do not expose server messages, credentials, or resource identifiers.
            print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
            raise SystemExit(1) from None
