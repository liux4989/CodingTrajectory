#!/usr/bin/env python3
"""Qualify bounded Chronicle artifacts against available local vendor sources.

The report contains aggregate counts only. It never records source paths,
project names, session identifiers, payload bodies, or exception messages.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from coding_trajectory.analysis.measurements import extract_item_measurements
from coding_trajectory.analysis.request_lineage import extract_user_request
from coding_trajectory.control_plane.chronicle import (
    MAX_CHRONICLE_ARTIFACT_BYTES,
    MAX_CHRONICLE_PUBLICATION_BYTES,
    ChronicleGraphArtifact,
    build_chronicle_graph_artifact,
)
from coding_trajectory.discovery import discover_store
from coding_trajectory.ingestion.indexes import build_session_graph_index
from coding_trajectory.ingestion.models import AgentMessageItem, SessionGraph
from coding_trajectory.query import DocumentError, DocumentStore
from coding_trajectory.service.handlers import dispatch
from coding_trajectory.service.store import IndexCache
from pydantic import BaseModel, ConfigDict, Field

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = REPO_ROOT / ".artifacts" / "chronicle-local-qualification.json"
VENDORS = ("codex_cli", "claude_code", "pi", "amp")
API_METHODS = (
    "project.sessions",
    "session.overview",
    "session.summary",
    "session.tree",
    "graph.overview",
    "session.stats",
    "graph.stats",
    "session.usage",
    "graph.usage",
    "session.model_usage",
    "session.request_usage",
    "session.tool_usage",
    "session.items",
)
NUMERIC_PARITY_METHODS = frozenset(
    {
        "session.stats",
        "graph.stats",
        "session.usage",
        "graph.usage",
        "session.model_usage",
        "session.request_usage",
        "session.tool_usage",
    }
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VendorQualification(StrictModel):
    vendor: str
    status: Literal["pass", "fail", "unavailable"]
    source_scope: Literal["project", "global"] | None = None
    window_days: int | None = None
    source_count: int = 0
    graph_count: int = 0
    session_count: int = 0
    turn_count: int = 0
    item_count: int = 0
    artifact_count: int = 0
    artifact_bytes_total: int = 0
    artifact_bytes_max: int = 0
    operational_detail_count: int = 0
    projection_link_count: int = 0
    user_request_preview_count: int = 0
    assistant_response_preview_count: int = 0
    api_calls: int = 0
    numeric_values_checked: int = 0
    checks: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)


class QualificationReport(StrictModel):
    schema_version: Literal[1] = 1
    generated_at: str
    status: Literal["pass", "fail", "partial"]
    source_policy: str
    vendors: list[VendorQualification]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=REPO_ROOT,
        help="Project path recorded by vendor logs; never written to the report.",
    )
    parser.add_argument("--since-days", type=int, default=7)
    parser.add_argument("--amp-since-days", type=int, default=30)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser.parse_args()


def _counts(graphs: list[SessionGraph]) -> tuple[int, int, int]:
    sessions = sum(len(graph.sessions) for graph in graphs)
    turns = sum(
        len(session.turns) for graph in graphs for session in graph.sessions
    )
    items = sum(
        len(turn.items)
        for graph in graphs
        for session in graph.sessions
        for turn in session.turns
    )
    return sessions, turns, items


def _discover(
    *, project_root: Path, vendor: str, since_days: int, global_scope: bool
) -> tuple[list[SessionGraph], int]:
    result = discover_store(
        current_dir=project_root,
        global_scope=global_scope,
        since_days=since_days,
        agent_vendor=vendor,
    )
    return list(result.store.session_graphs.values()), len(result.sources)


def _select_sources(
    *, project_root: Path, vendor: str, since_days: int, amp_since_days: int
) -> tuple[list[SessionGraph], int, Literal["project", "global"], int]:
    try:
        scoped_graphs, scoped_sources = _discover(
            project_root=project_root,
            vendor=vendor,
            since_days=since_days,
            global_scope=False,
        )
    except DocumentError:
        scoped_graphs, scoped_sources = [], 0
    if scoped_graphs and _counts(scoped_graphs)[2] > 0:
        return scoped_graphs, scoped_sources, "project", since_days

    global_days = amp_since_days if vendor == "amp" else since_days
    global_graphs, global_sources = _discover(
        project_root=project_root,
        vendor=vendor,
        since_days=global_days,
        global_scope=True,
    )
    return global_graphs, global_sources, "global", global_days


def _structure(graph: SessionGraph) -> tuple[Any, ...]:
    sessions = tuple(
        (
            str(session.session_id),
            str(session.parent_session_id) if session.parent_session_id else None,
            tuple(
                (
                    str(turn.turn_id),
                    tuple(str(item.item_id) for item in turn.items),
                )
                for turn in session.turns
            ),
        )
        for session in graph.sessions
    )
    edges = tuple(
        (
            str(edge.source_session_id),
            str(edge.target_session_id),
            edge.type,
            str(edge.source_turn_id) if edge.source_turn_id else None,
            str(edge.source_item_id) if edge.source_item_id else None,
        )
        for edge in graph.edges
    )
    return str(graph.root_session_id), sessions, edges


def _numeric_values(value: Any, path: str = "$") -> dict[str, int | float]:
    result: dict[str, int | float] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            result.update(_numeric_values(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            result.update(_numeric_values(child, f"{path}[{index}]"))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        result[path] = value
    return result


def _preview(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:280].strip() or None


def _validate_narrative_previews(
    graph: SessionGraph, artifact: ChronicleGraphArtifact
) -> tuple[int, int]:
    index = build_session_graph_index(graph)
    artifact_sessions = {
        session.session_id: session for session in artifact.sessions
    }
    user_requests = 0
    assistant_responses = 0
    for session in graph.sessions:
        artifact_session = artifact_sessions[session.session_id]
        artifact_turns = {turn.turn_id: turn for turn in artifact_session.turns}
        for turn in session.turns:
            artifact_turn = artifact_turns[turn.turn_id]
            request = extract_user_request(index, turn, session=session)
            expected_request = _preview(
                request.get("content") if request is not None else None
            )
            actual_request = (
                artifact_turn.user_request.content
                if artifact_turn.user_request is not None
                else None
            )
            if actual_request != expected_request:
                raise ValueError("user-request preview parity failed")
            user_requests += expected_request is not None

            artifact_items = {item.item_id: item for item in artifact_turn.items}
            for item in turn.items:
                if not isinstance(item, AgentMessageItem):
                    continue
                measurements = item.measurements or extract_item_measurements(item)
                expected_response = _preview(measurements.text_preview)
                actual_response = (
                    artifact_items[item.item_id].measurements.text_preview
                )
                if actual_response != expected_response:
                    raise ValueError("assistant-response preview parity failed")
                assistant_responses += expected_response is not None
    return user_requests, assistant_responses


def _api_params(method: str, graph: SessionGraph) -> dict[str, Any]:
    if method == "project.sessions":
        return {"include": ["usage", "runtime"]}
    params: dict[str, Any] = {"session_id": str(graph.root_session_id)}
    if method == "session.items":
        params["include_content"] = False
    return params


def _dispatch(
    method: str, params: dict[str, Any], graph: SessionGraph, project_root: Path
) -> Any:
    return dispatch(
        method,
        params,
        store=DocumentStore.from_session_graphs([graph]),
        global_scope=True,
        current_dir=project_root,
        discovery_note="local Chronicle qualification",
        cache=IndexCache(),
    )


def _validate_vendor(
    *, project_root: Path, vendor: str, since_days: int, amp_since_days: int
) -> VendorQualification:
    report = VendorQualification(vendor=vendor, status="fail")
    try:
        graphs, source_count, source_scope, window_days = _select_sources(
            project_root=project_root,
            vendor=vendor,
            since_days=since_days,
            amp_since_days=amp_since_days,
        )
    except DocumentError:
        report.status = "unavailable"
        report.failures.append("no representative local source")
        return report
    except Exception as exc:  # noqa: BLE001 - report a sanitized failure class
        report.failures.append(f"source discovery failed: {type(exc).__name__}")
        return report

    report.source_scope = source_scope
    report.window_days = window_days
    report.source_count = source_count
    report.graph_count = len(graphs)
    report.session_count, report.turn_count, report.item_count = _counts(graphs)
    publication_bytes: dict[str, int] = defaultdict(int)

    try:
        for graph in graphs:
            artifact = build_chronicle_graph_artifact(graph)
            encoded = artifact.canonical_bytes()
            replay_graph = artifact.to_session_graph()
            replay_artifact = build_chronicle_graph_artifact(replay_graph)
            if encoded != replay_artifact.canonical_bytes():
                raise ValueError("artifact replay changed canonical bytes")
            if _structure(graph) != _structure(replay_graph):
                raise ValueError("identity or topology changed during replay")
            request_previews, response_previews = _validate_narrative_previews(
                graph, artifact
            )
            report.user_request_preview_count += request_previews
            report.assistant_response_preview_count += response_previews
            if len(encoded) > MAX_CHRONICLE_ARTIFACT_BYTES:
                raise ValueError("artifact exceeded its byte limit")
            publication_bytes[graph.project_identifier or "unknown"] += len(encoded)

            report.artifact_count += 1
            report.artifact_bytes_total += len(encoded)
            report.artifact_bytes_max = max(report.artifact_bytes_max, len(encoded))
            report.operational_detail_count += sum(
                item.measurements.tool_summary is not None
                and item.measurements.tool_summary.detail is not None
                for session in artifact.sessions
                for turn in session.turns
                for item in turn.items
            )
            report.projection_link_count += sum(
                item.projection_parent_item_id is not None
                for session in artifact.sessions
                for turn in session.turns
                for item in turn.items
            )

            for method in API_METHODS:
                params = _api_params(method, graph)
                chronicle_result = _dispatch(
                    method, params, replay_graph, project_root
                )
                report.api_calls += 1
                if method in NUMERIC_PARITY_METHODS:
                    canonical_result = _dispatch(
                        method, params, graph, project_root
                    )
                    expected_numbers = _numeric_values(canonical_result)
                    actual_numbers = _numeric_values(chronicle_result)
                    if expected_numbers != actual_numbers:
                        raise ValueError(f"numeric API parity failed for {method}")
                    report.numeric_values_checked += len(expected_numbers)

        if any(total > MAX_CHRONICLE_PUBLICATION_BYTES for total in publication_bytes.values()):
            raise ValueError("one project publication exceeded its byte limit")
    except Exception as exc:  # noqa: BLE001 - keep private evidence out of output
        report.failures.append(f"qualification failed: {type(exc).__name__}")
        return report

    report.status = "pass"
    report.checks = [
        "strict bounded-history model validation",
        "artifact and per-project publication byte limits",
        "canonical byte and digest replay stability",
        "session, turn, item, and edge identity parity",
        "bounded user-request and assistant-response preview parity",
        "13 Chronicle API executions per graph",
        "numeric API parity against the full local graph",
    ]
    return report


def main() -> int:
    args = _parse_args()
    if args.since_days <= 0 or args.amp_since_days <= 0:
        raise SystemExit("source windows must be positive")
    project_root = args.project_root.resolve()
    vendors = [
        _validate_vendor(
            project_root=project_root,
            vendor=vendor,
            since_days=args.since_days,
            amp_since_days=args.amp_since_days,
        )
        for vendor in VENDORS
    ]
    if all(vendor.status == "pass" for vendor in vendors):
        status: Literal["pass", "fail", "partial"] = "pass"
    elif any(vendor.status == "fail" for vendor in vendors):
        status = "fail"
    else:
        status = "partial"
    report = QualificationReport(
        generated_at=datetime.now(UTC).isoformat(),
        status=status,
        source_policy=(
            "Use the project-scoped seven-day window when it contains operational "
            "items; otherwise use a global seven-day fallback, extended to thirty "
            "days for Amp."
        ),
        vendors=vendors,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for vendor in vendors:
        print(
            f"{vendor.status.upper()} {vendor.vendor}: sources={vendor.source_count} "
            f"graphs={vendor.graph_count} sessions={vendor.session_count} "
            f"turns={vendor.turn_count} items={vendor.item_count} "
            f"artifacts={vendor.artifact_count} api_calls={vendor.api_calls}"
        )
        for failure in vendor.failures:
            print(f"  {failure}")
    print(f"Chronicle local qualification: {status.upper()}")
    print(f"machine report: {args.report}")
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
