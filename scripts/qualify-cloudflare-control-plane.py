#!/usr/bin/env python3
"""Qualify the local Cloudflare fact authority with synthetic Chronicle facts.

Start ``wrangler dev --local`` on port 8794 with the synthetic principals
documented in ``cloudflare/control-plane/README.md``. This script refuses a
non-loopback target and never reads provider logs or user data.
"""

from __future__ import annotations

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
from coding_trajectory.contracts import SERVICE_CONTRACTS, service_contract
from coding_trajectory.control_plane.chronicle import (
    ChronicleEvent,
    ChronicleGraphArtifact,
    ChronicleGraphSummary,
    ChronicleItem,
    ChronicleItemMeasurements,
    ChronicleSession,
    ChronicleToolOutputEvidence,
    ChronicleTurn,
    ChronicleUserRequest,
)
from coding_trajectory.control_plane.collector import CloudflareCollectorRemote
from coding_trajectory.control_plane.collector_protocol import (
    LeaseHeartbeatRequest,
    LivingObservationRequest,
    ObservationRequest,
    ProjectRegistrationRequest,
    SourceRegistrationRequest,
    SourceVectorEntry,
)
from coding_trajectory.control_plane.fact_protocol import (
    FactGraphPublication,
    FactPublicationRequest,
    StageFactRowsRequest,
)
from coding_trajectory.control_plane.fact_repository import (
    CloudflareFactRepository,
    document_store_from_fact_sets,
)
from coding_trajectory.control_plane.published_facts import (
    PublishedFactSet,
    compute_row_hash,
    derive_published_fact_set,
)
from coding_trajectory.control_plane.remote import CloudflareRpcClient
from coding_trajectory.ingestion.common import canonical_json
from coding_trajectory.service.handlers import dispatch
from coding_trajectory.service.store import IndexCache

URL = os.environ.get("CT_QUALIFY_URL", "http://127.0.0.1:8794")
TOKENS = {
    "owner": "local-qualification-owner-token-0000000001",
    "reader": "local-qualification-reader-token-000000001",
    "worker": "local-qualification-worker-token-000000001",
    "other": "local-qualification-other-token-0000000001",
}
WORKSPACE = "00000000-0000-0000-0000-000000000001"
AGENT = "00000000-0000-0000-0000-000000000003"
checks = 0


def check(condition: object, label: str) -> None:
    global checks
    if not condition:
        raise AssertionError(label)
    checks += 1


def rpc(
    method: str,
    request: dict[str, Any],
    *,
    role: str = "owner",
    status: int = 200,
    key: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "protocol": "ct.core.v1",
        "id": None,
        "method": method,
        "params": {"workspace_id": WORKSPACE, **request},
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


def synthetic_artifact(
    *,
    seed: str,
    project: str,
    include_unknown: bool = True,
    revised: bool = False,
) -> ChronicleGraphArtifact:
    root = uuid5(UUID(WORKSPACE), seed + ":session")
    turn_id = uuid5(root, "turn")
    command_id = uuid5(root, "command")
    unknown_id = uuid5(root, "unknown")
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
        operation="command",
        exit_code=23,
        measurements=ChronicleItemMeasurements(
            output_chars=14,
            output_tokens=4,
            output_original_tokens=4,
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
            event_id=command_events[0],
            timestamp=started + timedelta(seconds=1),
            type="tool.call.requested",
            sequence=0,
            turn_id=turn_id,
            item_id=command_id,
            status="running",
        ),
        ChronicleEvent(
            event_id=command_events[1],
            timestamp=started + timedelta(seconds=2),
            type="tool.call.failed",
            sequence=1,
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
                measurements=ChronicleItemMeasurements(
                    output_chars=9,
                    output_tokens=3,
                    output_original_tokens=3,
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
                    sequence=2,
                    turn_id=turn_id,
                    item_id=unknown_id,
                    status="running",
                ),
                ChronicleEvent(
                    event_id=unknown_events[1],
                    timestamp=started + timedelta(seconds=4),
                    type="tool.call.succeeded",
                    sequence=3,
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
            request_id=uuid5(turn_id, "request"),
            content="Synthetic qualification request",
            chars=31,
            tokens=4,
        ),
        items=items,
    )
    session = ChronicleSession(
        session_id=root,
        vendor="amp",
        started_at=started,
        ended_at=started + timedelta(seconds=5),
        status="not_living",
        title="Synthetic revised session" if revised else "Synthetic session",
        events=events,
        turns=[turn],
    )
    return ChronicleGraphArtifact(
        graph=ChronicleGraphSummary(
            root_session_id=root,
            project=project,
            started_at=started,
            ended_at=started + timedelta(seconds=5),
            status="completed",
            session_count=1,
            turn_count=1,
            item_count=len(items),
        ),
        sessions=[session],
    )


def stage(remote: CloudflareCollectorRemote, request: StageFactRowsRequest) -> None:
    receipt = remote.stage_fact_rows(request)
    check(not receipt.missing_batches, "staged all fact rows")


def stage_fact_set(
    remote: CloudflareCollectorRemote,
    *,
    fact_set: PublishedFactSet,
) -> None:
    rows = list(fact_set.rows)
    batch_count = max(1, (len(rows) + 511) // 512)
    for batch_index in range(batch_count):
        chunk = rows[batch_index * 512 : (batch_index + 1) * 512]
        stage(
            remote,
            StageFactRowsRequest(
                workspace_id=WORKSPACE,
                agent_id=AGENT,
                graph_id=fact_set.graph_id,
                fact_set_digest=fact_set.fact_set_digest,
                batch_index=batch_index,
                batch_count=batch_count,
                rows=chunk,
            ),
        )


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
    basis = {
        "schema_version": "ct.published_facts.v1",
        "graph_id": rows[0]["graph_id"],
        "rows": [
            [row["kind"], row["fact_id"], row["row_hash"]]
            for row in sorted(rows, key=lambda row: (row["kind"], row["fact_id"]))
        ],
    }
    return hashlib.sha256(canonical_json(basis).encode()).hexdigest()


def raw_stage(
    rows: list[dict[str, Any]],
    *,
    digest: str,
    status: int,
) -> None:
    rpc(
        "ct_collector_stage_fact_rows",
        {
            "agent_id": AGENT,
            "graph_id": rows[0]["graph_id"],
            "fact_set_digest": digest,
            "batch_index": 0,
            "batch_count": 1,
            "rows": rows,
        },
        status=status,
    )


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
    local_store = document_store_from_fact_sets(fact_sets)
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
    first_a = derive_published_fact_set(
        synthetic_artifact(seed=tag + ":a", project=project_name),
    )
    first_b = derive_published_fact_set(
        synthetic_artifact(seed=tag + ":b", project=project_name),
    )
    checkpoint_payload_0 = {
        "kind": "ct.source_checkpoint.v1",
        "source_checkpoint": {"segments": [100]},
        "chronicle_digest": first_a.fact_set_digest,
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
    publication_0 = remote.publish_facts(
        FactPublicationRequest(
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
        ),
        idempotency_key="publication:" + tag + ":0",
    )
    check(publication_0.details["graphs_published"] == 2, "initial fact publication")
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
    rpc("ct_collector_publish_facts", invalid_parent, status=400)
    check(
        rpc("ct_workspace_snapshot", {})["snapshot_sequence"] == pinned_sequence,
        "invalid parent publication rolls back",
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

    second_a = derive_published_fact_set(
        synthetic_artifact(
            seed=tag + ":a",
            project=project_name,
            include_unknown=False,
            revised=True,
        ),
    )
    checkpoint_payload_1 = {
        "kind": "ct.source_checkpoint.v1",
        "source_checkpoint": {"segments": [80]},
        "chronicle_digest": second_a.fact_set_digest,
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
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
