#!/usr/bin/env python3
"""Qualify the local Cloudflare fact authority with synthetic Chronicle facts.

Start ``wrangler dev --local`` on port 8794 with ``CT_PRINCIPALS`` containing
the synthetic token digests declared below and a synthetic 32-byte
``CT_CURSOR_KEY``. This script refuses a non-loopback target and never reads
provider logs or user data.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4, uuid5

import httpx
from coding_trajectory.analysis.tool_summary_shared import RUN_COMMAND
from coding_trajectory.contracts import SERVICE_CONTRACTS, service_contract
from coding_trajectory.control_plane.collector import (
    CloudflareCollectorRemote,
    _fact_row_batches,
)
from coding_trajectory.control_plane.collector_protocol import (
    CollectorRecoveryRequest,
    LeaseHeartbeatRequest,
    LivingObservationRequest,
    ObservationRequest,
    ProjectRegistrationRequest,
    SourceRegistrationRequest,
    SourceVectorEntry,
)
from coding_trajectory.control_plane.fact_projection import (
    ChronicleContextSourceMeasurement,
    ChronicleCoverage,
    ChronicleEvent,
    ChronicleGraphSummary,
    ChronicleItem,
    ChronicleItemMeasurements,
    ChronicleRequestUsage,
    ChronicleRuntimeObservation,
    ChronicleSession,
    ChronicleSessionMeasurements,
    ChronicleToolOutputEvidence,
    ChronicleToolSummary,
    ChronicleTurn,
    ChronicleUsage,
    ChronicleUserRequest,
    _tool_detail,
    build_published_fact_set,
)
from coding_trajectory.control_plane.fact_protocol import (
    FACT_PUBLICATION_MAX_BYTES,
    FACT_PUBLICATION_MAX_GRAPHS,
    FactGraphPublication,
    FactPublicationRequest,
    StageFactRowsRequest,
)
from coding_trajectory.control_plane.fact_repository import CloudflareFactRepository
from coding_trajectory.control_plane.published_facts import (
    MAX_FACT_READ_PAGE_BYTES,
    MAX_FACT_ROW_BYTES,
    MAX_FACT_SET_BYTES,
    FactIndex,
    PublishedFactSet,
    compute_row_hash,
    session_graph_from_fact_index,
)
from coding_trajectory.control_plane.remote import CloudflareRpcClient
from coding_trajectory.ingestion.common import canonical_json
from coding_trajectory.ingestion.models import (
    SessionEdge,
    SessionGraph,
    SessionGraphSummary,
)
from coding_trajectory.service.handlers import dispatch
from coding_trajectory.service.store import IndexCache
from pydantic import ValidationError

URL = os.environ.get("CT_QUALIFY_URL", "http://127.0.0.1:8794")
TOKENS = {
    "owner": "local-qualification-owner-token-0000000001",
    "reader": "local-qualification-reader-token-000000001",
    "worker": "local-qualification-worker-token-000000001",
    "other": "local-qualification-other-token-0000000001",
    "owner_b": "local-qualification-second-owner-token-000001",
}
WORKSPACE = "00000000-0000-0000-0000-000000000001"
AGENT = "00000000-0000-0000-0000-000000000003"
SECOND_WORKSPACE = "00000000-0000-0000-0000-000000000002"
SECOND_AGENT = "00000000-0000-0000-0000-000000000004"
checks = 0


def check(condition: object, label: str) -> None:
    global checks
    if not condition:
        raise AssertionError(label)
    checks += 1


def verify_restart_receipt(path: Path) -> None:
    """Verify one synthetic committed publication after local Worker restart."""

    state = json.loads(path.read_text())
    remote = CloudflareCollectorRemote(url=URL, access_token=TOKENS["owner"])
    try:
        request = FactPublicationRequest.model_validate(state["request"])
        receipt = remote.publish_facts(request, idempotency_key=state["key"])
        check(
            receipt.model_dump(mode="json") == state["receipt"],
            "process restart preserves publication retry receipt",
        )
        recovery = remote.recover(
            CollectorRecoveryRequest(
                workspace_id=WORKSPACE,
                agent_id=AGENT,
                project_id=request.project_id,
                publication_idempotency_key=state["key"],
            )
        )
        check(
            recovery.publication_receipt == state["receipt"],
            "process restart recovery finds publication receipt",
        )
        conflicting = request.model_copy(update={"replacement_scope": "upsert"})
        rpc(
            "ct_collector_publish_facts",
            conflicting.wire_payload(),
            status=409,
            key=state["key"],
        )
    finally:
        remote.close()
    print(json.dumps({"status": "ok", "checks": checks, "restart_recovery": True}))


def rpc(
    method: str,
    request: dict[str, Any],
    *,
    role: str = "owner",
    status: int = 200,
    key: str | None = None,
    workspace_id: str = WORKSPACE,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "protocol": "ct.core.v1",
        "id": None,
        "method": method,
        "params": {**request, "workspace_id": workspace_id},
    }
    if key is not None:
        body["idempotency_key"] = key
    response = httpx.post(
        URL + "/v1/core",
        json=body,
        headers={"Authorization": "Bearer " + TOKENS[role]},
        timeout=30,
    )
    check(
        response.status_code == status,
        f"{method}: {response.status_code} {response.text[:400]}",
    )
    envelope = response.json()
    return envelope["data"] if envelope.get("ok") else envelope


def synthetic_fact_set(
    *,
    seed: str,
    project: str,
    include_unknown: bool = True,
    revised: bool = False,
    command_description: str | None = None,
    large_measurement: bool = False,
) -> PublishedFactSet:
    root = uuid5(UUID(WORKSPACE), seed + ":session")
    turn_id = uuid5(root, "turn")
    command_id = uuid5(root, "command")
    unknown_id = uuid5(root, "unknown")
    request_id = uuid5(turn_id, "request")
    started = datetime(2026, 9, 15, 12, tzinfo=UTC)
    command_events = [uuid5(command_id, "call"), uuid5(command_id, "result")]
    unknown_events = [uuid5(unknown_id, "call"), uuid5(unknown_id, "result")]
    command = ChronicleItem(
        item_id=command_id,
        event_ids=command_events,
        sequence=0,
        kind="command_execution",
        started_at=started + timedelta(seconds=1),
        completed_at=started + timedelta(seconds=2),
        status="failed",
        tool_name="shell_command",
        exit_code=23,
        measurements=ChronicleItemMeasurements(
            output_chars=14,
            output_tokens=4,
            output_original_tokens=4,
            tool_summary=(
                ChronicleToolSummary(
                    name="shell_command",
                    detail=_tool_detail(
                        RUN_COMMAND,
                        command_description,
                        cwd=None,
                    ),
                )
                if command_description is not None
                else None
            ),
        ),
        output_evidence=ChronicleToolOutputEvidence(
            processor="ct.output_evidence.command.v1",
            lifecycle="failed",
            outcome="failed",
            exit_code=23,
            duration_ms=1000,
            output_chars=14,
            output_tokens=4,
            token_method="tokenizer_estimate",
            tokenizer="cl100k_base",
            truncated=False,
            original_tokens=4,
            facts={"exited_zero": False},
            source_event_ids=command_events,
            retention="not_retained",
            searchable="facts_only",
        ),
    )
    items = [command]
    events = [
        ChronicleEvent(
            event_id=request_id,
            timestamp=started,
            type="user.prompt.submitted",
            sequence=0,
            turn_id=turn_id,
        ),
        ChronicleEvent(
            event_id=command_events[0],
            timestamp=started + timedelta(seconds=1),
            type="tool.call.requested",
            sequence=1,
            turn_id=turn_id,
            item_id=command_id,
            status="running",
        ),
        ChronicleEvent(
            event_id=command_events[1],
            timestamp=started + timedelta(seconds=2),
            type="tool.call.failed",
            sequence=2,
            turn_id=turn_id,
            item_id=command_id,
            status="failed",
        ),
    ]
    if include_unknown:
        items.append(
            ChronicleItem(
                item_id=unknown_id,
                event_ids=unknown_events,
                sequence=1,
                kind="tool_call",
                started_at=started + timedelta(seconds=3),
                completed_at=started + timedelta(seconds=4),
                status="completed",
                tool_name="synthetic_unknown_tool",
                projection_parent_item_id=command_id,
                nested_index=0,
                measurements=ChronicleItemMeasurements(
                    output_chars=9,
                    output_tokens=3,
                    output_original_tokens=3,
                    projection_only=True,
                ),
                output_evidence=ChronicleToolOutputEvidence(
                    processor="ct.output_evidence.unknown.v1",
                    lifecycle="completed",
                    output_chars=9,
                    output_tokens=3,
                    token_method="tokenizer_estimate",
                    tokenizer="cl100k_base",
                    truncated=False,
                    original_tokens=3,
                    source_event_ids=unknown_events,
                    retention="not_retained",
                    searchable="none",
                ),
            )
        )
        events.extend(
            [
                ChronicleEvent(
                    event_id=unknown_events[0],
                    timestamp=started + timedelta(seconds=3),
                    type="tool.call.requested",
                    sequence=3,
                    turn_id=turn_id,
                    item_id=unknown_id,
                    status="running",
                ),
                ChronicleEvent(
                    event_id=unknown_events[1],
                    timestamp=started + timedelta(seconds=4),
                    type="tool.call.succeeded",
                    sequence=4,
                    turn_id=turn_id,
                    item_id=unknown_id,
                    status="completed",
                ),
            ]
        )
    turn = ChronicleTurn(
        turn_id=turn_id,
        sequence=0,
        started_at=started,
        completed_at=started + timedelta(seconds=5),
        status="completed",
        user_request=ChronicleUserRequest(
            request_id=request_id,
            content="Synthetic qualification request",
            chars=31,
            tokens=4,
        ),
        requests=[
            ChronicleRequestUsage(
                request_id=request_id,
                timestamp=started,
                source="synthetic",
                model="synthetic-model",
                provider="synthetic-provider",
                used_input_tokens=11,
                usage=ChronicleUsage(input_tokens=11, output_tokens=7, total_tokens=18),
                cumulative_usage=ChronicleUsage(
                    input_tokens=21, output_tokens=9, total_tokens=30
                ),
            )
        ],
        items=items,
    )
    session = ChronicleSession(
        session_id=root,
        vendor="amp",
        started_at=started,
        ended_at=started + timedelta(seconds=5),
        status="not_living",
        title="Synthetic revised session" if revised else "Synthetic session",
        measurements=(
            ChronicleSessionMeasurements(
                context_sources=[
                    ChronicleContextSourceMeasurement(
                        timestamp=started,
                        key=f"large-{index}",
                        label=("large row evidence " * 28)[:480],
                        chars=1,
                        tokens=1,
                    )
                    for index in range(850)
                ]
            )
            if large_measurement
            else ChronicleSessionMeasurements()
        ),
        events=events,
        runtime=[
            ChronicleRuntimeObservation(
                timestamp=started,
                kind="compaction",
                duration_ms=17,
                pre_tokens=21,
                post_tokens=11,
            )
        ],
        turns=[turn],
    )
    from coding_trajectory.control_plane.published_facts import (
        _assemble_published_fact_set,
    )

    return _assemble_published_fact_set(
        summary=ChronicleGraphSummary(
            root_session_id=root,
            project=project,
            started_at=started,
            ended_at=started + timedelta(seconds=5),
            status="not_living",
            session_count=1,
            turn_count=1,
            item_count=len(items),
        ),
        sessions=[session],
        edges=[],
        coverage=ChronicleCoverage(),
    )


def large_fact_set(*, seed: str, project: str) -> PublishedFactSet:
    fact_set = synthetic_fact_set(seed=seed, project=project, large_measurement=True)
    largest = max(
        len(canonical_json(row.model_dump(mode="json", exclude_none=True)).encode())
        for row in fact_set.rows
    )
    check(
        MAX_FACT_ROW_BYTES * 9 // 10 <= largest <= MAX_FACT_ROW_BYTES,
        "large synthetic row is near the row boundary",
    )
    return fact_set


def synthetic_edge_fact_set(*, seed: str, project: str) -> PublishedFactSet:
    parent_facts = synthetic_fact_set(seed=seed + ":parent", project=project)
    child_facts = synthetic_fact_set(seed=seed + ":child", project=project)
    facts = FactIndex.from_fact_sets([parent_facts, child_facts])
    parent = session_graph_from_fact_index(facts, parent_facts.graph_id)
    child = session_graph_from_fact_index(facts, child_facts.graph_id)
    parent_session = parent.sessions[0]
    child_session = child.sessions[0].model_copy(
        update={"parent_session_id": parent_session.session_id}
    )
    origin_turn = parent_session.turns[0]
    origin_item = origin_turn.items[0]
    graph = SessionGraph(
        root_session_id=parent_session.session_id,
        project_identifier=project,
        summary=SessionGraphSummary(
            root_session_id=parent_session.session_id,
            started_at=parent.summary.started_at,
            ended_at=child.summary.ended_at,
            session_count=2,
            turn_count=2,
            vendors=[parent_session.vendor, child_session.vendor],
        ),
        sessions=[parent_session, child_session],
        edges=[
            SessionEdge(
                type="spawned_subagent",
                source_session_id=parent_session.session_id,
                target_session_id=child_session.session_id,
                source_turn_id=origin_turn.turn_id,
                source_item_id=origin_item.item_id,
                source_event_id=origin_item.event_ids[0],
                evidence_event_ids=[origin_item.event_ids[0]],
                provenance="observed",
                confidence="high",
            )
        ],
    )
    return build_published_fact_set(graph)


def sized_row_fact_set(
    *, seed: str, project: str, target_row_bytes: int
) -> PublishedFactSet:
    rows = raw_rows(synthetic_fact_set(seed=seed, project=project))
    measurement = next(row for row in rows if row["kind"] == "measurement")
    timestamp = "2026-09-15T12:00:00Z"

    def apply_count(count: int) -> int:
        measurement["payload"]["context_sources"] = [
            {
                "timestamp": timestamp,
                "key": f"sized-{index}",
                "label": "x",
                "chars": 1,
                "tokens": 1,
            }
            for index in range(count)
        ]
        measurement["row_hash"] = compute_row_hash(
            {key: value for key, value in measurement.items() if key != "row_hash"}
        )
        return len(canonical_json(measurement).encode())

    low, high = 0, 1
    while apply_count(high) <= target_row_bytes:
        high *= 2
    while low + 1 < high:
        middle = (low + high) // 2
        if apply_count(middle) <= target_row_bytes:
            low = middle
        else:
            high = middle
    current = apply_count(low)
    remaining = target_row_bytes - current
    if remaining:
        measurement["payload"]["context_sources"][-1]["label"] += "x" * remaining
        measurement["row_hash"] = compute_row_hash(
            {key: value for key, value in measurement.items() if key != "row_hash"}
        )
    check(
        len(canonical_json(measurement).encode()) == target_row_bytes,
        "synthetic row reaches its exact canonical byte target",
    )
    digest = recalculate(rows)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1
    return PublishedFactSet.model_validate(
        raw_fact_set(rows, digest=digest, kind_counts=counts)
    )


def stage(remote: CloudflareCollectorRemote, request: StageFactRowsRequest) -> None:
    receipt = remote.stage_fact_rows(request)
    check(not receipt.missing_batches, "staged all fact rows")


def stage_fact_set(
    remote: CloudflareCollectorRemote,
    *,
    fact_set: PublishedFactSet,
    workspace_id: str = WORKSPACE,
    agent_id: str = AGENT,
) -> None:
    batches = _fact_row_batches(fact_set)
    receipt = None
    for batch_index, chunk in enumerate(batches):
        receipt = remote.stage_fact_rows(
            StageFactRowsRequest(
                workspace_id=workspace_id,
                agent_id=agent_id,
                graph_id=fact_set.graph_id,
                fact_set_digest=fact_set.fact_set_digest,
                batch_index=batch_index,
                batch_count=len(batches),
                rows=chunk,
            )
        )
    check(receipt is not None and not receipt.missing_batches, "staged all fact rows")


def manifest(
    fact_set: PublishedFactSet,
    *,
    source_id: UUID,
    observed_at: datetime,
) -> FactGraphPublication:
    return FactGraphPublication(
        graph_id=fact_set.graph_id,
        fact_set_digest=fact_set.fact_set_digest,
        fact_count=len(fact_set.rows),
        schema_version=fact_set.schema_version,
        kind_counts=fact_set.kind_counts,
        source_ids=[source_id],
        observed_at=observed_at,
    )


def raw_rows(fact_set: PublishedFactSet) -> list[dict[str, Any]]:
    return [row.model_dump(mode="json", exclude_none=True) for row in fact_set.rows]


def recalculate(rows: list[dict[str, Any]]) -> str:
    for row in rows:
        row["row_hash"] = compute_row_hash(
            {key: value for key, value in row.items() if key != "row_hash"}
        )
    return digest_for_rows(rows)


def digest_for_rows(rows: list[dict[str, Any]]) -> str:
    basis = {
        "schema_version": "ct.published_facts.v1",
        "graph_id": rows[0]["graph_id"],
        "rows": [
            [row["kind"], row["fact_id"], row["row_hash"]]
            for row in sorted(rows, key=lambda row: (row["kind"], row["fact_id"]))
        ],
    }
    return hashlib.sha256(canonical_json(basis).encode()).hexdigest()


def exact_graph_fact_set(
    *, seed: str, project: str, target_bytes: int
) -> PublishedFactSet:
    template = sized_row_fact_set(
        seed=seed + ":template",
        project=project,
        target_row_bytes=390_000,
    )
    template_context = deepcopy(
        next(row for row in raw_rows(template) if row["kind"] == "measurement")[
            "payload"
        ]["context_sources"]
    )
    source_count = max(2, (target_bytes + 399_999) // 400_000)
    source_sets = [
        synthetic_fact_set(seed=f"{seed}:{index}", project=project)
        for index in range(source_count)
    ]
    adjustable_id = str(
        next(row for row in source_sets[-1].rows if row.kind == "measurement").fact_id
    )
    graph_id = str(source_sets[0].graph_id)
    combined: list[dict[str, Any]] = []
    for index, fact_set in enumerate(source_sets):
        for row in raw_rows(fact_set):
            if row["kind"] == "graph":
                if index == 0:
                    combined.append(row)
                continue
            prior_graph_id = row["graph_id"]
            row["graph_id"] = graph_id
            if (
                row["kind"] in {"session", "model", "edge"}
                and row.get("parent_id") == prior_graph_id
            ):
                row["parent_id"] = graph_id
            if row["kind"] == "measurement" and index < source_count - 1:
                row["payload"]["context_sources"] = deepcopy(template_context)
            combined.append(row)
    graph = next(row for row in combined if row["kind"] == "graph")
    graph["payload"]["summary"].update(
        {
            "session_count": source_count,
            "turn_count": source_count,
            "item_count": source_count * 2,
        }
    )
    combined.sort(key=lambda row: (row["kind"], row["fact_id"]))
    recalculate(combined)
    counts: dict[str, int] = {}
    for row in combined:
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1
    adjustable = next(row for row in combined if row["fact_id"] == adjustable_id)
    timestamp = "2026-09-15T12:00:00Z"

    def apply_count(count: int) -> tuple[int, str]:
        adjustable["payload"]["context_sources"] = [
            {
                "timestamp": timestamp,
                "key": f"graph-sized-{index}",
                "label": "x",
                "chars": 1,
                "tokens": 1,
            }
            for index in range(count)
        ]
        adjustable["row_hash"] = compute_row_hash(
            {key: value for key, value in adjustable.items() if key != "row_hash"}
        )
        digest = digest_for_rows(combined)
        view = raw_fact_set(combined, digest=digest, kind_counts=counts)
        return len(canonical_json(view).encode()), digest

    low, high = 0, 1
    while apply_count(high)[0] <= target_bytes:
        high *= 2
    while low + 1 < high:
        middle = (low + high) // 2
        if apply_count(middle)[0] <= target_bytes:
            low = middle
        else:
            high = middle
    current, digest = apply_count(low)
    remaining = target_bytes - current
    if remaining:
        adjustable["payload"]["context_sources"][-1]["label"] += "x" * remaining
        adjustable["row_hash"] = compute_row_hash(
            {key: value for key, value in adjustable.items() if key != "row_hash"}
        )
        digest = digest_for_rows(combined)
    view = raw_fact_set(combined, digest=digest, kind_counts=counts)
    check(
        len(canonical_json(view).encode()) == target_bytes,
        "synthetic graph reaches its exact canonical byte target",
    )
    return PublishedFactSet.model_validate(view)


def raw_stage(
    rows: list[dict[str, Any]],
    *,
    digest: str,
    status: int,
    batch_index: int = 0,
    batch_count: int = 1,
) -> None:
    rpc(
        "ct_collector_stage_fact_rows",
        {
            "agent_id": AGENT,
            "graph_id": rows[0]["graph_id"],
            "fact_set_digest": digest,
            "batch_index": batch_index,
            "batch_count": batch_count,
            "rows": rows,
        },
        status=status,
    )


def raw_stage_fact_set(
    rows: list[dict[str, Any]], *, digest: str, status: int = 200
) -> None:
    chunks: list[list[dict[str, Any]]] = []
    chunk: list[dict[str, Any]] = []
    for row in rows:
        candidate = [*chunk, row]
        if chunk and len(canonical_json(candidate).encode()) > 2 * 1024 * 1024:
            chunks.append(chunk)
            chunk = [row]
        else:
            chunk = candidate
    if chunk:
        chunks.append(chunk)
    for index, rows_chunk in enumerate(chunks):
        raw_stage(
            rows_chunk,
            digest=digest,
            status=status,
            batch_index=index,
            batch_count=len(chunks),
        )


def raw_fact_set(
    rows: list[dict[str, Any]], *, digest: str, kind_counts: dict[str, int]
) -> dict[str, Any]:
    return {
        "schema_version": "ct.published_facts.v1",
        "graph_id": rows[0]["graph_id"],
        "fact_set_digest": digest,
        "kind_counts": kind_counts,
        "rows": rows,
    }


def check_client_rejects(
    rows: list[dict[str, Any]], *, digest: str, kind_counts: dict[str, int], label: str
) -> None:
    try:
        PublishedFactSet.model_validate(
            raw_fact_set(rows, digest=digest, kind_counts=kind_counts)
        )
    except ValidationError:
        check(True, label)
    else:
        raise AssertionError(label)


def historical_params(method: str, *, project: str, root: str) -> dict[str, Any]:
    if method == "project.list":
        return {}
    if method == "project.sessions":
        return {"project_name": project}
    if method == "graph.overview":
        return {"root_session_id": root, "limit": 100}
    if method.startswith("graph."):
        return {"root_session_id": root}
    if method == "session.search":
        return {"session_id": root, "query": "command", "limit": 100}
    if method in {"session.items", "session.events", "session.overview"}:
        return {"session_id": root, "limit": 100}
    return {"session_id": root}


def qualify_parity(
    fact_sets: list[PublishedFactSet], *, snapshot: int, project: str
) -> None:
    root = str(fact_sets[0].graph_id)
    local_store = FactIndex.from_fact_sets(fact_sets)
    repository = CloudflareFactRepository(
        client=CloudflareRpcClient(url=URL, access_token=TOKENS["reader"]),
        workspace_id=UUID(WORKSPACE),
        snapshot_sequence=snapshot,
    )
    cache = IndexCache()
    try:
        methods = [name for name in SERVICE_CONTRACTS if not name.startswith("living.")]
        for method in methods:
            params = historical_params(method, project=project, root=root)
            validated = service_contract(method).validate_request(params)
            remote_store, _ = repository.store_for(method, params)
            local_result = dispatch(
                method,
                validated,
                store=local_store,
                global_scope=False,
                current_dir=Path.cwd(),
                discovery_note="synthetic facts",
                cache=cache,
            )
            remote_result = dispatch(
                method,
                validated,
                store=remote_store,
                global_scope=False,
                current_dir=Path.cwd(),
                discovery_note="synthetic facts",
                cache=cache,
            )
            check(local_result == remote_result, f"local/remote parity: {method}")
    finally:
        repository.close()


def main() -> None:
    parsed = urlparse(URL)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("qualification refuses a non-loopback CT_QUALIFY_URL")
    restart_verify = os.environ.get("CT_QUALIFY_RESTART_VERIFY")
    if restart_verify:
        verify_restart_receipt(Path(restart_verify))
        return
    tag = uuid4().hex
    captured = datetime.now(UTC).replace(microsecond=0)
    remote = CloudflareCollectorRemote(url=URL, access_token=TOKENS["owner"])
    project = remote.register_project(
        ProjectRegistrationRequest(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            display_name="Qualification-" + tag,
        )
    )
    source = remote.register_source(
        SourceRegistrationRequest(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            project_id=project.project_id,
            vendor="amp",
            native_session_id=tag,
        ),
        idempotency_key="source:" + tag,
    )
    rpc(
        "ct_project_register",
        {"agent_id": AGENT, "display_name": "denied"},
        role="reader",
        status=403,
    )
    rpc("ct_workspace_snapshot", {}, role="other", status=403)
    unauthenticated = httpx.post(
        URL + "/v1/core",
        json={
            "protocol": "ct.core.v1",
            "id": None,
            "method": "ct_workspace_snapshot",
            "params": {"workspace_id": WORKSPACE},
        },
    )
    check(unauthenticated.status_code == 401, "missing authentication denied")

    heartbeat = LeaseHeartbeatRequest(
        workspace_id=WORKSPACE,
        agent_id=AGENT,
        agent_instance_id=uuid4(),
        observation_sequence=1,
        observed_at=captured,
    )
    lease = remote.heartbeat(heartbeat)
    check(remote.heartbeat(heartbeat) == lease, "heartbeat is idempotent")

    project_name = "Qualification-" + tag
    bearer_secret = "synthetic-bearer-secret-value"
    positional_secret = "synthetic-positional-secret"
    first_a = synthetic_fact_set(
        seed=tag + ":a",
        project=project_name,
        command_description=(
            f"curl -H 'Authorization: Bearer {bearer_secret}' https://example.test"
        ),
    )
    first_b = synthetic_fact_set(
        seed=tag + ":b",
        project=project_name,
        command_description=f"python deploy.py {positional_secret}",
    )
    bounded_sets = canonical_json(
        [
            fact_set.model_dump(mode="json", exclude_none=True)
            for fact_set in (first_a, first_b)
        ]
    )
    check(
        bearer_secret not in bounded_sets and positional_secret not in bounded_sets,
        "command arguments never enter PublishedFactSet",
    )
    checkpoint_payload_0 = {
        "kind": "ct.source_checkpoint.v1",
        "source_checkpoint": {"segments": [100]},
        "session_digest": first_a.fact_set_digest,
    }
    checkpoint_digest_0 = hashlib.sha256(
        canonical_json(checkpoint_payload_0).encode()
    ).hexdigest()
    remote.publish_observation(
        ObservationRequest(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            source_id=source.source_id,
            source_epoch=source.source_epoch,
            source_sequence=0,
            event_id="checkpoint:" + checkpoint_digest_0,
            parser_version="qualification.v1",
            content_sha256=checkpoint_digest_0,
            observed_at=captured,
            payload=checkpoint_payload_0,
        ),
        idempotency_key="checkpoint:" + checkpoint_digest_0,
    )
    for fact_set in (first_a, first_b):
        stage_fact_set(remote, fact_set=fact_set)
    publication_0_request = FactPublicationRequest(
        workspace_id=WORKSPACE,
        agent_id=AGENT,
        project_id=project.project_id,
        publication_sequence=0,
        source_vector=[
            SourceVectorEntry(
                source_id=source.source_id,
                source_epoch=source.source_epoch,
                source_sequence=0,
                content_sha256=checkpoint_digest_0,
            )
        ],
        graphs=[
            manifest(first_a, source_id=source.source_id, observed_at=captured),
            manifest(first_b, source_id=source.source_id, observed_at=captured),
        ],
    )
    publication_0_key = "publication:" + tag + ":0"
    publication_0 = remote.publish_facts(
        publication_0_request, idempotency_key=publication_0_key
    )
    check(publication_0.details["graphs_published"] == 2, "initial fact publication")
    check(
        remote.publish_facts(publication_0_request, idempotency_key=publication_0_key)
        == publication_0,
        "lost publication response retries immediately without restaging",
    )
    conflicting_publication = publication_0_request.model_copy(
        update={"replacement_scope": "upsert"}
    )
    rpc(
        "ct_collector_publish_facts",
        conflicting_publication.wire_payload(),
        status=409,
        key=publication_0_key,
    )
    recovered_publication = remote.recover(
        CollectorRecoveryRequest(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            project_id=project.project_id,
            publication_idempotency_key=publication_0_key,
        )
    )
    check(
        recovered_publication.publication_receipt
        == publication_0.model_dump(mode="json"),
        "collector recovery returns the atomically committed publication receipt",
    )
    restart_state = os.environ.get("CT_QUALIFY_RESTART_STATE")
    if restart_state:
        Path(restart_state).write_text(
            json.dumps(
                {
                    "request": publication_0_request.wire_payload(),
                    "key": publication_0_key,
                    "receipt": publication_0.model_dump(mode="json"),
                }
            )
        )
    pinned_sequence = publication_0.committed_sequence
    assert pinned_sequence is not None

    first_page = rpc(
        "ct_fact_read",
        {"graph_id": str(first_a.graph_id), "limit": 1},
        role="reader",
    )
    check(first_page["next_cursor"], "fact cursor emitted")
    second_page = rpc(
        "ct_fact_read",
        {
            "graph_id": str(first_a.graph_id),
            "limit": 1,
            "cursor": first_page["next_cursor"],
            "snapshot_sequence": first_page["snapshot_sequence"],
        },
        role="reader",
    )
    check(
        first_page["rows"][0]["fact_id"] != second_page["rows"][0]["fact_id"],
        "fact cursor advances deterministically",
    )
    check(
        second_page["snapshot_sequence"] == first_page["snapshot_sequence"],
        "fact cursor remains pinned",
    )
    remote_b = CloudflareCollectorRemote(url=URL, access_token=TOKENS["owner_b"])
    try:
        project_b = remote_b.register_project(
            ProjectRegistrationRequest(
                workspace_id=SECOND_WORKSPACE,
                agent_id=SECOND_AGENT,
                display_name=project_name,
            )
        )
        source_b = remote_b.register_source(
            SourceRegistrationRequest(
                workspace_id=SECOND_WORKSPACE,
                agent_id=SECOND_AGENT,
                project_id=project_b.project_id,
                vendor="amp",
                native_session_id=tag,
            ),
            idempotency_key="source:" + tag,
        )
        remote_b.heartbeat(
            LeaseHeartbeatRequest(
                workspace_id=SECOND_WORKSPACE,
                agent_id=SECOND_AGENT,
                agent_instance_id=uuid4(),
                observation_sequence=1,
                observed_at=captured,
            )
        )
        remote_b.publish_observation(
            ObservationRequest(
                workspace_id=SECOND_WORKSPACE,
                agent_id=SECOND_AGENT,
                source_id=source_b.source_id,
                source_epoch=source_b.source_epoch,
                source_sequence=0,
                event_id="checkpoint:" + checkpoint_digest_0,
                parser_version="qualification.v1",
                content_sha256=checkpoint_digest_0,
                observed_at=captured,
                payload=checkpoint_payload_0,
            ),
            idempotency_key="checkpoint:" + checkpoint_digest_0,
        )
        stage_fact_set(
            remote_b,
            fact_set=first_a,
            workspace_id=SECOND_WORKSPACE,
            agent_id=SECOND_AGENT,
        )
        publication_b = remote_b.publish_facts(
            FactPublicationRequest(
                workspace_id=SECOND_WORKSPACE,
                agent_id=SECOND_AGENT,
                project_id=project_b.project_id,
                publication_sequence=0,
                source_vector=[
                    SourceVectorEntry(
                        source_id=source_b.source_id,
                        source_epoch=source_b.source_epoch,
                        source_sequence=0,
                        content_sha256=checkpoint_digest_0,
                    )
                ],
                graphs=[
                    manifest(
                        first_a, source_id=source_b.source_id, observed_at=captured
                    )
                ],
            ),
            idempotency_key="publication:" + tag + ":workspace-b",
        )
        check(
            publication_b.committed_sequence == first_page["snapshot_sequence"],
            "cross-workspace cursor probe aligns pinned authority sequences",
        )
        rpc(
            "ct_fact_read",
            {
                "graph_id": str(first_a.graph_id),
                "limit": 1,
                "cursor": first_page["next_cursor"],
                "snapshot_sequence": first_page["snapshot_sequence"],
            },
            role="owner_b",
            workspace_id=SECOND_WORKSPACE,
            status=409,
        )
        workspace_b_first = rpc(
            "ct_fact_read",
            {
                "graph_id": str(first_a.graph_id),
                "limit": 1,
                "snapshot_sequence": first_page["snapshot_sequence"],
            },
            role="owner_b",
            workspace_id=SECOND_WORKSPACE,
        )
        check(
            workspace_b_first["rows"][0]["fact_id"] == first_page["rows"][0]["fact_id"],
            "rejected cross-workspace cursor does not skip workspace rows",
        )
    finally:
        remote_b.close()
    cursor_payload, cursor_signature = first_page["next_cursor"].split(".")
    decoded_cursor = json.loads(base64.b64decode(cursor_payload))
    decoded_cursor["after"] = ["zzzz", "zzzz", "zzzz"]
    tampered_cursor = (
        base64.b64encode(canonical_json(decoded_cursor).encode()).decode()
        + "."
        + cursor_signature
    )
    rpc(
        "ct_fact_read",
        {
            "graph_id": str(first_a.graph_id),
            "limit": 1,
            "cursor": tampered_cursor,
            "snapshot_sequence": first_page["snapshot_sequence"],
        },
        role="reader",
        status=400,
    )
    malformed_signature = cursor_payload + "." + base64.b64encode(bytes(32)).decode()
    rpc(
        "ct_fact_read",
        {
            "graph_id": str(first_a.graph_id),
            "limit": 1,
            "cursor": malformed_signature,
            "snapshot_sequence": first_page["snapshot_sequence"],
        },
        role="reader",
        status=400,
    )
    seen_fact_tuples = {
        (
            first_page["rows"][0]["graph_id"],
            first_page["rows"][0]["kind"],
            first_page["rows"][0]["fact_id"],
        )
    }
    page = second_page
    while True:
        for row in page["rows"]:
            fact_tuple = (row["graph_id"], row["kind"], row["fact_id"])
            check(
                fact_tuple not in seen_fact_tuples, "fact cursor never duplicates a row"
            )
            seen_fact_tuples.add(fact_tuple)
        if page["next_cursor"] is None:
            break
        page = rpc(
            "ct_fact_read",
            {
                "graph_id": str(first_a.graph_id),
                "limit": 1,
                "cursor": page["next_cursor"],
                "snapshot_sequence": first_page["snapshot_sequence"],
            },
            role="reader",
        )
    check(len(seen_fact_tuples) == len(first_a.rows), "fact cursor never skips a row")
    rpc(
        "ct_fact_read",
        {
            "graph_id": str(first_a.graph_id),
            "kinds": ["session"],
            "limit": 1,
            "cursor": first_page["next_cursor"],
            "snapshot_sequence": first_page["snapshot_sequence"],
        },
        role="reader",
        status=409,
    )
    published_commands = canonical_json(
        [
            *rpc(
                "ct_fact_read",
                {"graph_id": str(first_a.graph_id), "limit": 512},
                role="reader",
            )["rows"],
            *rpc(
                "ct_fact_read",
                {"graph_id": str(first_b.graph_id), "limit": 512},
                role="reader",
            )["rows"],
        ]
    )
    check(
        bearer_secret not in published_commands
        and positional_secret not in published_commands,
        "command arguments never enter SQL facts",
    )
    qualify_parity([first_a, first_b], snapshot=pinned_sequence, project=project_name)

    bad_hash = raw_rows(first_a)
    bad_hash[0]["row_hash"] = "f" * 64
    raw_stage(
        bad_hash,
        digest=first_a.fact_set_digest,
        status=400,
    )
    body_row = raw_rows(first_a)
    body_row[0]["payload"]["body"] = "forbidden raw body"
    body_digest = recalculate(body_row)
    raw_stage(body_row, digest=body_digest, status=400)

    staged_once = raw_rows(first_a)
    raw_stage(staged_once, digest=first_a.fact_set_digest, status=200)
    raw_stage(
        staged_once[: len(staged_once) // 2],
        digest=first_a.fact_set_digest,
        status=409,
        batch_count=2,
    )
    staged_replacement = raw_rows(first_a)
    graph_payload = next(row for row in staged_replacement if row["kind"] == "graph")
    graph_payload["payload"]["summary"]["status"] = "replacement staged"
    replacement_digest = recalculate(staged_replacement)
    split = len(staged_replacement) // 2
    raw_stage(
        staged_replacement[:split],
        digest=replacement_digest,
        status=200,
        batch_count=2,
    )
    replacement_missing = rpc(
        "ct_collector_missing_fact_rows",
        {
            "agent_id": AGENT,
            "graph_id": str(first_a.graph_id),
            "fact_set_digest": replacement_digest,
            "batch_count": 2,
        },
    )
    check(
        replacement_missing["missing_batches"] == [1],
        "new digest can replace staging with a different batch count",
    )

    bad_parent = raw_rows(first_a)
    child = next(row for row in bad_parent if row["parent_id"] is not None)
    child["parent_id"] = str(uuid4())
    parent_digest = recalculate(bad_parent)
    raw_stage(bad_parent, digest=parent_digest, status=200)
    invalid_parent = {
        "agent_id": AGENT,
        "publication_sequence": 1,
        "captured_at": captured.isoformat().replace("+00:00", "Z"),
        "source_vector": [
            {
                "source_id": str(source.source_id),
                "source_epoch": source.source_epoch,
                "source_sequence": 0,
                "content_sha256": checkpoint_digest_0,
            }
        ],
        "fact_sets": [
            {
                "graph_id": str(first_a.graph_id),
                "fact_set_digest": parent_digest,
                "fact_count": len(bad_parent),
                "schema_version": first_a.schema_version,
                "kind_counts": first_a.kind_counts,
                "source_ids": [str(source.source_id)],
                "observed_at": captured.isoformat().replace("+00:00", "Z"),
            }
        ],
    }
    invalid_parent["project_id"] = str(project.project_id)
    invalid_parent["graphs"] = invalid_parent.pop("fact_sets")
    invalid_parent.pop("captured_at")
    check_client_rejects(
        bad_parent,
        digest=parent_digest,
        kind_counts=first_a.kind_counts,
        label="client rejects recomputed dangling parent",
    )
    rpc("ct_collector_publish_facts", invalid_parent, status=400)
    check(
        rpc("ct_workspace_snapshot", {})["snapshot_sequence"] == pinned_sequence,
        "invalid parent publication rolls back",
    )
    valid_before_late_failure = synthetic_fact_set(
        seed=tag + ":valid-before-late-failure", project=project_name
    )
    stage_fact_set(remote, fact_set=valid_before_late_failure)
    late_invalid_publication = deepcopy(invalid_parent)
    late_invalid_publication["graphs"].insert(
        0,
        manifest(
            valid_before_late_failure,
            source_id=source.source_id,
            observed_at=captured,
        ).model_dump(mode="json"),
    )
    rpc("ct_collector_publish_facts", late_invalid_publication, status=400)
    check(
        rpc("ct_workspace_snapshot", {})["snapshot_sequence"] == pinned_sequence,
        "malformed late graph prevents visibility of an earlier valid graph",
    )

    adversarial_rows: list[tuple[str, list[dict[str, Any]]]] = []
    unsafe_command = raw_rows(first_a)
    unsafe_item = next(
        row
        for row in unsafe_command
        if row["kind"] == "item"
        and row["payload"]["measurements"].get("tool_summary") is not None
    )
    unsafe_item["payload"]["measurements"]["tool_summary"]["detail"]["target"] = (
        f"python deploy.py {positional_secret}"
    )
    adversarial_rows.append(("unsafe command detail", unsafe_command))

    mismatched_identity = raw_rows(first_a)
    session_row = next(row for row in mismatched_identity if row["kind"] == "session")
    session_row["payload"]["session_id"] = str(uuid4())
    adversarial_rows.append(("payload identity mismatch", mismatched_identity))

    dangling_reference = raw_rows(first_a)
    item_row = next(row for row in dangling_reference if row["kind"] == "item")
    item_row["payload"]["event_ids"][0] = str(uuid4())
    adversarial_rows.append(("dangling payload reference", dangling_reference))

    duplicate_item_sequence = raw_rows(first_a)
    ordered_items = sorted(
        (row for row in duplicate_item_sequence if row["kind"] == "item"),
        key=lambda row: row["payload"]["sequence"],
    )
    ordered_items[1]["payload"]["sequence"] = ordered_items[0]["payload"]["sequence"]
    adversarial_rows.append(("duplicate item sequence", duplicate_item_sequence))

    for label, rows in adversarial_rows:
        adversarial_digest = recalculate(rows)
        check_client_rejects(
            rows,
            digest=adversarial_digest,
            kind_counts=first_a.kind_counts,
            label=f"client rejects recomputed {label}",
        )
        raw_stage(rows, digest=adversarial_digest, status=200)
        invalid = deepcopy(invalid_parent)
        invalid["graphs"][0]["fact_set_digest"] = adversarial_digest
        rpc("ct_collector_publish_facts", invalid, status=400)
        check(
            rpc("ct_workspace_snapshot", {})["snapshot_sequence"] == pinned_sequence,
            f"server rejects recomputed {label} before commit",
        )

    edge_set = synthetic_edge_fact_set(seed=tag + ":edge", project=project_name)
    cross_item_edge = raw_rows(edge_set)
    edge_row = next(row for row in cross_item_edge if row["kind"] == "edge")
    origin_item_id = edge_row["payload"]["origin"]["item_id"]
    other_item = next(
        row
        for row in cross_item_edge
        if row["kind"] == "item"
        and row["parent_id"] == edge_row["payload"]["origin"]["turn_id"]
        and row["fact_id"] != origin_item_id
    )
    foreign_event_id = other_item["payload"]["event_ids"][0]
    edge_row["payload"]["origin"]["event_id"] = foreign_event_id
    edge_row["payload"]["evidence_event_ids"] = [foreign_event_id]
    cross_item_digest = recalculate(cross_item_edge)
    check_client_rejects(
        cross_item_edge,
        digest=cross_item_digest,
        kind_counts=edge_set.kind_counts,
        label="client rejects recomputed cross-item edge event",
    )
    raw_stage(cross_item_edge, digest=cross_item_digest, status=200)
    invalid_edge = deepcopy(invalid_parent)
    invalid_edge["graphs"][0].update(
        {
            "graph_id": str(edge_set.graph_id),
            "fact_set_digest": cross_item_digest,
            "fact_count": len(cross_item_edge),
            "kind_counts": edge_set.kind_counts,
        }
    )
    rpc("ct_collector_publish_facts", invalid_edge, status=400)
    check(
        rpc("ct_workspace_snapshot", {})["snapshot_sequence"] == pinned_sequence,
        "server rejects recomputed cross-item edge event before commit",
    )

    valid_rows = raw_rows(first_a)
    raw_stage(
        valid_rows,
        digest="f" * 64,
        status=200,
    )
    invalid_digest = deepcopy(invalid_parent)
    invalid_digest["graphs"][0]["fact_set_digest"] = "f" * 64
    rpc("ct_collector_publish_facts", invalid_digest, status=400)
    check(
        rpc("ct_workspace_snapshot", {})["snapshot_sequence"] == pinned_sequence,
        "invalid digest publication rolls back",
    )

    second_a = synthetic_fact_set(
        seed=tag + ":a",
        project=project_name,
        include_unknown=False,
        revised=True,
    )
    checkpoint_payload_1 = {
        "kind": "ct.source_checkpoint.v1",
        "source_checkpoint": {"segments": [80]},
        "session_digest": second_a.fact_set_digest,
    }
    checkpoint_digest_1 = hashlib.sha256(
        canonical_json(checkpoint_payload_1).encode()
    ).hexdigest()
    remote.publish_observation(
        ObservationRequest(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            source_id=source.source_id,
            source_epoch=source.source_epoch,
            source_sequence=1,
            event_id="checkpoint:" + checkpoint_digest_1,
            parser_version="qualification.v1",
            content_sha256=checkpoint_digest_1,
            observed_at=captured + timedelta(seconds=1),
            payload=checkpoint_payload_1,
        ),
        idempotency_key="checkpoint:" + checkpoint_digest_1,
    )
    stage_fact_set(remote, fact_set=second_a)
    publication_1 = remote.publish_facts(
        FactPublicationRequest(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            project_id=project.project_id,
            publication_sequence=1,
            source_vector=[
                SourceVectorEntry(
                    source_id=source.source_id,
                    source_epoch=source.source_epoch,
                    source_sequence=1,
                    content_sha256=checkpoint_digest_1,
                )
            ],
            graphs=[
                manifest(
                    second_a,
                    source_id=source.source_id,
                    observed_at=captured + timedelta(seconds=1),
                )
            ],
        ),
        idempotency_key="publication:" + tag + ":1",
    )
    details = publication_1.details
    check(details["rows_reused"] > 0, "unchanged fact rows reused")
    check(details["rows_inserted"] > 0, "revised fact rows inserted")
    check(details["rows_closed"] > 0, "removed fact rows closed")
    check(details["omitted_graphs"] == 1, "omitted graph tombstoned")

    pinned_a = rpc(
        "ct_fact_read",
        {
            "graph_id": str(first_a.graph_id),
            "snapshot_sequence": pinned_sequence,
            "limit": 512,
        },
        role="reader",
    )
    current_a = rpc(
        "ct_fact_read",
        {"graph_id": str(first_a.graph_id), "limit": 512},
        role="reader",
    )
    check(
        len(pinned_a["rows"]) > len(current_a["rows"]),
        "pinned read retains removed facts",
    )
    tombstoned = rpc(
        "ct_fact_read",
        {"graph_id": str(first_b.graph_id), "limit": 512},
        role="reader",
    )
    check(not tombstoned["rows"], "tombstoned graph absent from current read")
    check(
        rpc(
            "ct_fact_read",
            {
                "graph_id": str(first_b.graph_id),
                "snapshot_sequence": pinned_sequence,
                "limit": 512,
            },
            role="reader",
        )["rows"],
        "tombstoned graph retained at pinned snapshot",
    )
    encoded = canonical_json(current_a)
    check("forbidden raw body" not in encoded, "standard fact read has no raw body")
    evidence = [
        row["payload"] for row in pinned_a["rows"] if row["kind"] == "output_evidence"
    ]
    check(
        any(
            row["processor"] == "ct.output_evidence.command.v1"
            and row["exit_code"] == 23
            for row in evidence
        ),
        "known tool evidence retained",
    )
    check(
        any(
            row["processor"] == "ct.output_evidence.unknown.v1"
            and row["searchable"] == "none"
            and row.get("facts") is None
            for row in evidence
        ),
        "unknown tool evidence fails closed",
    )

    raw_stage(raw_rows(second_a), digest=second_a.fact_set_digest, status=200)
    completed_replacement_rows = raw_rows(second_a)
    completed_graph = next(
        row for row in completed_replacement_rows if row["kind"] == "graph"
    )
    completed_graph["payload"]["summary"]["status"] = "replacement completed"
    completed_replacement_digest = recalculate(completed_replacement_rows)
    completed_replacement = PublishedFactSet.model_validate(
        raw_fact_set(
            completed_replacement_rows,
            digest=completed_replacement_digest,
            kind_counts=second_a.kind_counts,
        )
    )
    replacement_split = len(completed_replacement_rows) // 2
    raw_stage(
        completed_replacement_rows[:replacement_split],
        digest=completed_replacement_digest,
        status=200,
        batch_count=2,
    )
    raw_stage(
        completed_replacement_rows[replacement_split:],
        digest=completed_replacement_digest,
        status=200,
        batch_index=1,
        batch_count=2,
    )
    completed_replacement_receipt = remote.publish_facts(
        FactPublicationRequest(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            project_id=project.project_id,
            publication_sequence=2,
            source_vector=[
                SourceVectorEntry(
                    source_id=source.source_id,
                    source_epoch=source.source_epoch,
                    source_sequence=1,
                    content_sha256=checkpoint_digest_1,
                )
            ],
            graphs=[
                manifest(
                    completed_replacement,
                    source_id=source.source_id,
                    observed_at=captured + timedelta(seconds=2),
                )
            ],
        ),
        idempotency_key="publication:" + tag + ":replacement-complete",
    )
    replacement_read = rpc(
        "ct_fact_read",
        {
            "graph_id": str(completed_replacement.graph_id),
            "snapshot_sequence": completed_replacement_receipt.committed_sequence,
            "limit": 512,
        },
        role="reader",
    )
    check(
        replacement_read["graph_digests"][str(completed_replacement.graph_id)]
        == completed_replacement_digest,
        "new-digest two-batch replacement completes and publishes",
    )

    living_before = rpc("ct_workspace_snapshot", {})["snapshot_sequence"]
    rpc(
        "ct_collector_publish_living_observation",
        LivingObservationRequest(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            agent_instance_id=heartbeat.agent_instance_id,
            observation_sequence=2,
            observed_at=captured + timedelta(seconds=2),
            kind="living.sessions",
            payload={
                "cursor": "local-only",
                "revision": 0,
                "operation": "upsert",
                "resource_kind": "session",
                "path": {
                    "root_session_id": str(second_a.graph_id),
                    "session_id": str(second_a.graph_id),
                },
                "resource": None,
            },
        ).model_dump(mode="json", exclude={"workspace_id"}),
    )
    living = rpc(
        "ct_remote_living",
        {"calls": [{"method": "living.sessions", "params": {"limit": 10}}]},
        role="reader",
    )["results"][0]["result"]
    check(living["changes"], "living observation queryable separately")
    fact_after_living = rpc(
        "ct_fact_read",
        {
            "graph_id": str(second_a.graph_id),
            "snapshot_sequence": living_before,
            "limit": 512,
        },
        role="reader",
    )
    check(
        all(row["kind"] != "living" for row in fact_after_living["rows"]),
        "living observations never enter historical facts",
    )

    exact_row_set = sized_row_fact_set(
        seed=tag + ":row-exact",
        project=project_name,
        target_row_bytes=MAX_FACT_ROW_BYTES,
    )
    below_row_set = sized_row_fact_set(
        seed=tag + ":row-below",
        project=project_name,
        target_row_bytes=MAX_FACT_ROW_BYTES - 1,
    )
    oversized_row = raw_rows(exact_row_set)
    oversized_measurement = next(
        row for row in oversized_row if row["kind"] == "measurement"
    )
    oversized_measurement["payload"]["context_sources"][-1]["label"] += "x"
    oversized_row_digest = recalculate(oversized_row)
    check_client_rejects(
        oversized_row,
        digest=oversized_row_digest,
        kind_counts=exact_row_set.kind_counts,
        label="client rejects a canonical row one byte above 512 KiB",
    )
    raw_stage(
        raw_rows(below_row_set),
        digest=below_row_set.fact_set_digest,
        status=200,
    )
    raw_stage(
        raw_rows(exact_row_set),
        digest=exact_row_set.fact_set_digest,
        status=200,
    )
    raw_stage(oversized_row, digest=oversized_row_digest, status=413)

    boundary_tag = uuid4().hex
    boundary_project_name = "Fact-boundary-" + boundary_tag
    boundary_project = remote.register_project(
        ProjectRegistrationRequest(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            display_name=boundary_project_name,
        )
    )
    boundary_source = remote.register_source(
        SourceRegistrationRequest(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            project_id=boundary_project.project_id,
            vendor="amp",
            native_session_id=boundary_tag,
        ),
        idempotency_key="boundary-source:" + boundary_tag,
    )
    boundary_sets = [
        large_fact_set(seed=f"{boundary_tag}:{index}", project=boundary_project_name)
        for index in range(160)
    ]
    staged_sizes = [
        len(
            canonical_json(fact_set.model_dump(mode="json", exclude_none=True)).encode()
        )
        for fact_set in boundary_sets
    ]
    check(
        71_239_324 < sum(staged_sizes) < FACT_PUBLICATION_MAX_BYTES,
        "synthetic publication covers the measured maximum below the new bound",
    )
    boundary_checkpoint = {
        "kind": "ct.source_checkpoint.v1",
        "source_checkpoint": {"segments": [sum(staged_sizes)]},
        "session_digest": hashlib.sha256(
            "".join(fact_set.fact_set_digest for fact_set in boundary_sets).encode()
        ).hexdigest(),
    }
    boundary_checkpoint_digest = hashlib.sha256(
        canonical_json(boundary_checkpoint).encode()
    ).hexdigest()
    remote.publish_observation(
        ObservationRequest(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            source_id=boundary_source.source_id,
            source_epoch=boundary_source.source_epoch,
            source_sequence=0,
            event_id="checkpoint:" + boundary_checkpoint_digest,
            parser_version="fact-boundary.v1",
            content_sha256=boundary_checkpoint_digest,
            observed_at=captured,
            payload=boundary_checkpoint,
        ),
        idempotency_key="boundary-checkpoint:" + boundary_checkpoint_digest,
    )
    old_plus_one_graph = exact_graph_fact_set(
        seed=boundary_tag + ":old-plus-one-graph",
        project=boundary_project_name,
        target_bytes=8 * 1024 * 1024 + 1,
    )
    raw_stage_fact_set(
        raw_rows(old_plus_one_graph), digest=old_plus_one_graph.fact_set_digest
    )
    boundary_vector = [
        SourceVectorEntry(
            source_id=boundary_source.source_id,
            source_epoch=boundary_source.source_epoch,
            source_sequence=0,
            content_sha256=boundary_checkpoint_digest,
        )
    ]
    old_plus_one_receipt = remote.publish_facts(
        FactPublicationRequest(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            project_id=boundary_project.project_id,
            publication_sequence=0,
            source_vector=boundary_vector,
            graphs=[
                manifest(
                    old_plus_one_graph,
                    source_id=boundary_source.source_id,
                    observed_at=captured,
                )
            ],
        ),
        idempotency_key="old-plus-one-graph:" + boundary_tag,
    )
    check(
        old_plus_one_receipt.details["graphs_published"] == 1,
        "legitimate-shaped graph one byte above the old 8 MiB bound commits",
    )
    exact_graph = exact_graph_fact_set(
        seed=boundary_tag + ":exact-graph",
        project=boundary_project_name,
        target_bytes=MAX_FACT_SET_BYTES,
    )
    oversized_graph_rows = raw_rows(exact_graph)
    adjustable_graph_row = [
        row for row in oversized_graph_rows if row["kind"] == "measurement"
    ][-1]
    adjustable_graph_row["payload"]["context_sources"][-1]["label"] += "x"
    oversized_graph_digest = recalculate(oversized_graph_rows)
    check_client_rejects(
        oversized_graph_rows,
        digest=oversized_graph_digest,
        kind_counts=exact_graph.kind_counts,
        label="client rejects a canonical graph one byte above 16 MiB",
    )
    raw_stage_fact_set(oversized_graph_rows, digest=oversized_graph_digest)
    oversized_graph_publication = FactPublicationRequest(
        workspace_id=WORKSPACE,
        agent_id=AGENT,
        project_id=boundary_project.project_id,
        publication_sequence=1,
        source_vector=boundary_vector,
        graphs=[
            manifest(
                exact_graph,
                source_id=boundary_source.source_id,
                observed_at=captured,
            )
        ],
    ).wire_payload()
    oversized_graph_publication["graphs"][0]["fact_set_digest"] = oversized_graph_digest
    graph_boundary_snapshot = rpc("ct_workspace_snapshot", {})["snapshot_sequence"]
    rpc(
        "ct_collector_publish_facts",
        oversized_graph_publication,
        status=413,
        key="graph-boundary-rejected:" + boundary_tag,
    )
    check(
        rpc("ct_workspace_snapshot", {})["snapshot_sequence"]
        == graph_boundary_snapshot,
        "oversized graph rejection leaves the snapshot unchanged",
    )
    raw_stage_fact_set(raw_rows(exact_graph), digest=exact_graph.fact_set_digest)
    exact_graph_receipt = remote.publish_facts(
        FactPublicationRequest(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            project_id=boundary_project.project_id,
            publication_sequence=1,
            source_vector=boundary_vector,
            graphs=[
                manifest(
                    exact_graph,
                    source_id=boundary_source.source_id,
                    observed_at=captured,
                )
            ],
        ),
        idempotency_key="graph-boundary-published:" + boundary_tag,
    )
    check(
        exact_graph_receipt.details["graphs_published"] == 1,
        "graph at the exact 16 MiB canonical boundary commits",
    )
    for fact_set in boundary_sets:
        stage_fact_set(remote, fact_set=fact_set)
    oversized_publication = FactPublicationRequest(
        workspace_id=WORKSPACE,
        agent_id=AGENT,
        project_id=boundary_project.project_id,
        publication_sequence=2,
        source_vector=boundary_vector,
        graphs=[
            manifest(
                fact_set,
                source_id=boundary_source.source_id,
                observed_at=captured,
            )
            for fact_set in boundary_sets
        ],
    )
    measured_receipt = remote.publish_facts(
        oversized_publication,
        idempotency_key="measured-publication:" + boundary_tag,
    )
    check(
        measured_receipt.details["graphs_published"] == len(boundary_sets),
        "publication above the old 16 MiB and measured 71 MB maximum commits",
    )

    exact_aggregate_sets: list[PublishedFactSet] = []
    exact_aggregate_bytes = 0
    index = 0
    while (
        FACT_PUBLICATION_MAX_BYTES - exact_aggregate_bytes > MAX_FACT_SET_BYTES - 1024
    ):
        fact_set = large_fact_set(
            seed=f"{boundary_tag}:aggregate:{index}", project=boundary_project_name
        )
        exact_aggregate_sets.append(fact_set)
        exact_aggregate_bytes += len(
            canonical_json(fact_set.model_dump(mode="json", exclude_none=True)).encode()
        )
        index += 1
    aggregate_tail = exact_graph_fact_set(
        seed=boundary_tag + ":aggregate-tail",
        project=boundary_project_name,
        target_bytes=FACT_PUBLICATION_MAX_BYTES - exact_aggregate_bytes,
    )
    exact_aggregate_sets.append(aggregate_tail)
    check(
        len(exact_aggregate_sets) <= FACT_PUBLICATION_MAX_GRAPHS,
        "exact aggregate fixture stays within the graph cardinality bound",
    )
    check(
        sum(
            len(
                canonical_json(
                    fact_set.model_dump(mode="json", exclude_none=True)
                ).encode()
            )
            for fact_set in exact_aggregate_sets
        )
        == FACT_PUBLICATION_MAX_BYTES,
        "synthetic publication reaches the exact aggregate byte bound",
    )
    for fact_set in exact_aggregate_sets:
        stage_fact_set(remote, fact_set=fact_set)
    exact_aggregate_publication = FactPublicationRequest(
        workspace_id=WORKSPACE,
        agent_id=AGENT,
        project_id=boundary_project.project_id,
        publication_sequence=3,
        source_vector=boundary_vector,
        graphs=[
            manifest(
                fact_set, source_id=boundary_source.source_id, observed_at=captured
            )
            for fact_set in exact_aggregate_sets
        ],
    )
    oversized_tail_rows = raw_rows(aggregate_tail)
    next(row for row in oversized_tail_rows if row["kind"] == "measurement")["payload"][
        "context_sources"
    ][-1]["label"] += "x"
    oversized_tail_digest = recalculate(oversized_tail_rows)
    raw_stage_fact_set(oversized_tail_rows, digest=oversized_tail_digest)
    oversized_aggregate = exact_aggregate_publication.wire_payload()
    oversized_aggregate["graphs"][-1]["fact_set_digest"] = oversized_tail_digest
    aggregate_boundary_snapshot = rpc("ct_workspace_snapshot", {})["snapshot_sequence"]
    rpc(
        "ct_collector_publish_facts",
        oversized_aggregate,
        status=413,
        key="aggregate-boundary-rejected:" + boundary_tag,
    )
    check(
        rpc("ct_workspace_snapshot", {})["snapshot_sequence"]
        == aggregate_boundary_snapshot,
        "aggregate byte bound plus one leaves the snapshot unchanged",
    )
    stage_fact_set(remote, fact_set=aggregate_tail)
    boundary_receipt = remote.publish_facts(
        exact_aggregate_publication,
        idempotency_key="aggregate-boundary-published:" + boundary_tag,
    )
    check(
        boundary_receipt.details["graphs_published"] == len(exact_aggregate_sets),
        "publication at the exact aggregate byte bound commits",
    )
    boundary_cursor = None
    boundary_pages = 0
    boundary_rows = 0
    boundary_tuples: set[tuple[str, str, str]] = set()
    while True:
        boundary_page = rpc(
            "ct_fact_read",
            {
                "project_name": boundary_project_name,
                "snapshot_sequence": boundary_receipt.committed_sequence,
                "limit": 2048,
                **({"cursor": boundary_cursor} if boundary_cursor else {}),
            },
            role="reader",
        )
        check(
            len(canonical_json(boundary_page["rows"]).encode())
            <= MAX_FACT_READ_PAGE_BYTES,
            "fact read page stays within its encoded byte budget",
        )
        boundary_pages += 1
        boundary_rows += len(boundary_page["rows"])
        for row in boundary_page["rows"]:
            fact_tuple = (row["graph_id"], row["kind"], row["fact_id"])
            check(
                fact_tuple not in boundary_tuples,
                "byte-bounded cursor pages do not duplicate rows",
            )
            boundary_tuples.add(fact_tuple)
        boundary_cursor = boundary_page["next_cursor"]
        if boundary_cursor is None:
            break
    check(boundary_pages > 1, "large fact rows require byte-bounded cursor pages")
    check(
        boundary_rows == sum(len(fact_set.rows) for fact_set in exact_aggregate_sets),
        "byte-bounded cursor pages return every committed fact row",
    )
    check(
        len(boundary_tuples) == boundary_rows,
        "byte-bounded cursor pages preserve unique continuation tuples",
    )

    selector_project_name = "Selector-boundary-" + tag
    selector_project = remote.register_project(
        ProjectRegistrationRequest(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            display_name=selector_project_name,
            aliases=[f"large-alias-{index}-" + "x" * 480 for index in range(64)],
        )
    )
    selector_sources = []
    selector_vector = []
    for index in range(64):
        selector_source = remote.register_source(
            SourceRegistrationRequest(
                workspace_id=WORKSPACE,
                agent_id=AGENT,
                project_id=selector_project.project_id,
                vendor=f"synthetic-vendor-{index}",
                native_session_id=f"{tag}:selector:{index}",
            ),
            idempotency_key=f"selector-source:{tag}:{index}",
        )
        selector_checkpoint_payload = {
            "kind": "ct.source_checkpoint.v1",
            "source_checkpoint": {"segments": [index + 1]},
            "session_digest": hashlib.sha256(
                f"{tag}:selector:{index}".encode()
            ).hexdigest(),
        }
        selector_checkpoint_digest = hashlib.sha256(
            canonical_json(selector_checkpoint_payload).encode()
        ).hexdigest()
        remote.publish_observation(
            ObservationRequest(
                workspace_id=WORKSPACE,
                agent_id=AGENT,
                source_id=selector_source.source_id,
                source_epoch=selector_source.source_epoch,
                source_sequence=0,
                event_id="checkpoint:" + selector_checkpoint_digest,
                parser_version="qualification.v1",
                content_sha256=selector_checkpoint_digest,
                observed_at=captured,
                payload=selector_checkpoint_payload,
            ),
            idempotency_key=f"selector-checkpoint:{tag}:{index}",
        )
        selector_sources.append(selector_source.source_id)
        selector_vector.append(
            SourceVectorEntry(
                source_id=selector_source.source_id,
                source_epoch=selector_source.source_epoch,
                source_sequence=0,
                content_sha256=selector_checkpoint_digest,
            )
        )
    selector_sets = [
        synthetic_fact_set(
            seed=f"{tag}:selector-graph:{index}",
            project=selector_project_name,
            include_unknown=False,
        )
        for index in range(512)
    ]
    for fact_set in selector_sets:
        stage_fact_set(remote, fact_set=fact_set)
    selector_receipt = remote.publish_facts(
        FactPublicationRequest(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            project_id=selector_project.project_id,
            publication_sequence=0,
            replacement_scope="upsert",
            source_vector=selector_vector,
            graphs=[
                manifest(
                    fact_set,
                    source_id=selector_sources[0],
                    observed_at=captured,
                ).model_copy(update={"source_ids": selector_sources})
                for fact_set in selector_sets
            ],
        ),
        idempotency_key="selector-publication:" + tag,
    )
    selector_page = rpc(
        "ct_fact_read",
        {
            "project_name": selector_project_name,
            "snapshot_sequence": selector_receipt.committed_sequence,
            "limit": 1,
        },
        role="reader",
    )
    check(
        len(selector_page["graph_digests"]) == 512,
        "fact selector accepts exactly 512 graph metadata rows",
    )
    overflow_source = remote.register_source(
        SourceRegistrationRequest(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            project_id=selector_project.project_id,
            vendor="synthetic-overflow-vendor",
            native_session_id=f"{tag}:selector:overflow",
        ),
        idempotency_key="selector-source-overflow:" + tag,
    )
    overflow_checkpoint_payload = {
        "kind": "ct.source_checkpoint.v1",
        "source_checkpoint": {"segments": [513]},
        "session_digest": hashlib.sha256(
            f"{tag}:selector:overflow".encode()
        ).hexdigest(),
    }
    overflow_checkpoint_digest = hashlib.sha256(
        canonical_json(overflow_checkpoint_payload).encode()
    ).hexdigest()
    remote.publish_observation(
        ObservationRequest(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            source_id=overflow_source.source_id,
            source_epoch=overflow_source.source_epoch,
            source_sequence=0,
            event_id="checkpoint:" + overflow_checkpoint_digest,
            parser_version="qualification.v1",
            content_sha256=overflow_checkpoint_digest,
            observed_at=captured,
            payload=overflow_checkpoint_payload,
        ),
        idempotency_key="selector-checkpoint-overflow:" + tag,
    )
    overflow_set = synthetic_fact_set(
        seed=f"{tag}:selector-graph:overflow",
        project=selector_project_name,
        include_unknown=False,
    )
    stage_fact_set(remote, fact_set=overflow_set)
    remote.publish_facts(
        FactPublicationRequest(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            project_id=selector_project.project_id,
            publication_sequence=1,
            replacement_scope="upsert",
            source_vector=[
                SourceVectorEntry(
                    source_id=overflow_source.source_id,
                    source_epoch=overflow_source.source_epoch,
                    source_sequence=0,
                    content_sha256=overflow_checkpoint_digest,
                )
            ],
            graphs=[
                manifest(
                    overflow_set,
                    source_id=overflow_source.source_id,
                    observed_at=captured,
                )
            ],
        ),
        idempotency_key="selector-publication-overflow:" + tag,
    )
    for index in range(32):
        remote.register_project(
            ProjectRegistrationRequest(
                workspace_id=WORKSPACE,
                agent_id=AGENT,
                display_name=f"Selector-clutter-{tag}-{index}",
                aliases=[f"clutter-{alias}-" + "y" * 480 for alias in range(64)],
            )
        )
    rpc(
        "ct_fact_read",
        {"project_name": selector_project_name, "limit": 1},
        role="reader",
        status=413,
    )
    selector_source = (
        Path(__file__).parents[1] / "cloudflare" / "control-plane" / "src" / "facts.ts"
    ).read_text()
    selector_body = selector_source.split("function selectGraphs", 1)[1].split(
        "/** Guard:", 1
    )[0]
    check(
        "state.all(" not in selector_body
        and "SELECT graph.key AS graph_id" in selector_body
        and "LIMIT ${FACT_PUBLICATION_MAX_GRAPHS + 1}" in selector_body,
        "fact selector projects at most 513 scalar rows without payload materialization",
    )
    publication_body = selector_source.split("async function validateStagedGraph", 1)[
        1
    ].split("async function cursorKey", 1)[0]
    check(
        "JSON.parse(batch.rows_json)" not in publication_body
        and "staged_fact_items" in publication_body
        and "INSERT INTO fact_rows" in publication_body
        and 'state.all("graph_publication")' not in publication_body,
        "publication validation and commit never materialize graph or aggregate payload rows in JS",
    )
    remote.close()
    print(
        json.dumps(
            {
                "status": "ok",
                "checks": checks,
                "initial_rows": len(first_a.rows) + len(first_b.rows),
                "revised_rows": len(second_a.rows),
                "rows_reused": details["rows_reused"],
                "rows_inserted": details["rows_inserted"],
                "rows_closed": details["rows_closed"],
                "graphs_tombstoned": details["omitted_graphs"],
                "boundary_publication_bytes": FACT_PUBLICATION_MAX_BYTES,
                "measured_workload_publication_bytes": sum(staged_sizes),
                "exact_graph_bytes": len(
                    canonical_json(
                        exact_graph.model_dump(mode="json", exclude_none=True)
                    ).encode()
                ),
                "boundary_read_pages": boundary_pages,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
