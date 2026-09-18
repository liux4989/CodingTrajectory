#!/usr/bin/env python3
"""Qualify retired SQL routes and provide artifact benchmark fixtures."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4, uuid5

import httpx
from coding_trajectory.analysis.tool_summary_shared import RUN_COMMAND
from coding_trajectory.control_plane.collector import CloudflareCollectorRemote
from coding_trajectory.control_plane.collector_protocol import (
    CollectorRecoveryRequest,
    LeaseHeartbeatRequest,
    LivingObservationRequest,
    ObservationRequest,
    ProjectRegistrationRequest,
    SourceRegistrationRequest,
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
)
from coding_trajectory.control_plane.published_facts import (
    PublishedFactSet,
    _assemble_published_fact_set,
)
from coding_trajectory.ingestion.common import canonical_json

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


def synthetic_fact_set(
    *,
    seed: str,
    project: str,
    include_unknown: bool = True,
    revised: bool = False,
    command_description: str | None = None,
    large_measurement: bool = False,
) -> PublishedFactSet:
    """Build the rich deterministic fixture shared by artifact qualifiers."""
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
                    detail=_tool_detail(RUN_COMMAND, command_description, cwd=None),
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


def rpc(
    method: str,
    request: dict[str, object],
    *,
    role: str = "owner",
    status: int = 200,
    workspace_id: str = WORKSPACE,
):
    response = httpx.post(
        URL + "/v1/core",
        json={
            "protocol": "ct.core.v1",
            "id": None,
            "method": method,
            "params": {**request, "workspace_id": workspace_id},
        },
        headers={"Authorization": "Bearer " + TOKENS[role]},
        timeout=30,
    )
    check(response.status_code == status, f"{method} returned {response.status_code}")
    envelope = response.json()
    return envelope["data"] if envelope.get("ok") else envelope


def main() -> None:
    if urlparse(URL).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("qualification refuses a non-loopback CT_QUALIFY_URL")
    tag = uuid4().hex
    captured = datetime.now(UTC).replace(microsecond=0)
    remote = CloudflareCollectorRemote(url=URL, access_token=TOKENS["owner"])
    try:
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
        rpc(
            "ct_workspace_snapshot",
            {},
            role="owner_b",
            workspace_id=WORKSPACE,
            status=403,
        )
        unauthenticated = httpx.post(
            URL + "/v1/core",
            json={
                "protocol": "ct.core.v1",
                "id": None,
                "method": "ct_workspace_snapshot",
                "params": {"workspace_id": WORKSPACE},
            },
            timeout=30,
        )
        check(unauthenticated.status_code == 401, "missing authentication denied")

        instance = uuid4()
        heartbeat = LeaseHeartbeatRequest(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            agent_instance_id=instance,
            observation_sequence=1,
            observed_at=captured,
        )
        lease = remote.heartbeat(heartbeat)
        check(remote.heartbeat(heartbeat) == lease, "heartbeat is idempotent")

        fixture = synthetic_fact_set(seed=tag, project="Qualification-" + tag)
        check(
            any(row.kind == "session" for row in fixture.rows),
            "synthetic fixture contains sessions",
        )
        checkpoint_payload = {
            "kind": "ct.source_checkpoint.v1",
            "source_checkpoint": {"segments": [100]},
            "session_digest": fixture.fact_set_digest,
        }
        checkpoint_digest = hashlib.sha256(
            canonical_json(checkpoint_payload).encode()
        ).hexdigest()
        observation = ObservationRequest(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            source_id=source.source_id,
            source_epoch=source.source_epoch,
            source_sequence=0,
            event_id="checkpoint:" + checkpoint_digest,
            parser_version="qualification.v1",
            content_sha256=checkpoint_digest,
            observed_at=captured,
            payload=checkpoint_payload,
        )
        receipt = remote.publish_observation(
            observation, idempotency_key="checkpoint:" + checkpoint_digest
        )
        check(receipt.outcome == "accepted", "checkpoint metadata accepted")
        check(
            remote.publish_observation(
                observation, idempotency_key="checkpoint:" + checkpoint_digest
            )
            == receipt,
            "checkpoint publication is idempotent",
        )
        recovery = remote.recover(
            CollectorRecoveryRequest(
                workspace_id=WORKSPACE,
                agent_id=AGENT,
                project_id=project.project_id,
                agent_instance_id=instance,
                vendor="amp",
                native_session_id=tag,
            )
        )
        check(
            recovery.source is not None
            and recovery.source.next_source_sequence == 1
            and recovery.source.content_sha256 == checkpoint_digest,
            "recovery returns the retained checkpoint metadata",
        )
        inventory = rpc("ct_project_inventory_snapshot", {}, role="reader")
        check(
            any(
                item["project_id"] == str(project.project_id)
                and item["display_name"] == "Qualification-" + tag
                for item in inventory["projects"]
            ),
            "project metadata is visible at the workspace snapshot",
        )

        remote.publish_living_observation(
            LivingObservationRequest(
                workspace_id=WORKSPACE,
                agent_id=AGENT,
                agent_instance_id=instance,
                observation_sequence=2,
                observed_at=captured,
                kind="living.sessions",
                payload={
                    "cursor": "local-only",
                    "revision": 0,
                    "operation": "upsert",
                    "resource_kind": "session",
                    "path": {
                        "root_session_id": str(fixture.graph_id),
                        "session_id": str(fixture.graph_id),
                    },
                    "resource": None,
                },
            )
        )
        living = rpc(
            "ct_remote_living",
            {"calls": [{"method": "living.sessions", "params": {"limit": 10}}]},
            role="reader",
        )["results"][0]["result"]
        check(living["changes"], "living observation is independently queryable")

        for method in (
            "ct_collector_stage_fact_rows",
            "ct_collector_missing_fact_rows",
            "ct_collector_publish_facts",
            "ct_fact_read",
        ):
            rejected = rpc(method, {"agent_id": AGENT}, status=404)
            check(rejected["error"]["code"] == "not_found", f"{method} is retired")
        source_root = Path(__file__).parents[1] / "cloudflare/control-plane/src"
        check(not (source_root / "facts.ts").exists(), "legacy facts module is absent")
    finally:
        remote.close()
    print(
        json.dumps(
            {"status": "ok", "checks": checks, "synthetic_sessions": 1},
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
