#!/usr/bin/env python3
"""Exercise the local workerd authority with synthetic, Pydantic-valid contracts.

Run wrangler dev on port 8794 with the documented qualification principals first.
This script sends synthetic data only and refuses a non-loopback target.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from coding_trajectory.contracts import service_contract
from coding_trajectory.control_plane.catalog_protocol import CatalogReadResponse
from coding_trajectory.control_plane.chronicle import ChronicleGraphArtifact
from coding_trajectory.control_plane.collector import (
    CloudflareCollectorRemote,
    _project_session_list_variants,
)
from coding_trajectory.control_plane.collector_protocol import (
    ArtifactPublicationRequest,
    LeaseHeartbeatRequest,
    ObservationRequest,
    ProjectRegistrationRequest,
    SourceRegistrationRequest,
)
from coding_trajectory.control_plane.http_service import RemoteRuntimeFactory
from coding_trajectory.control_plane.remote import (
    CloudflareHistoricalRepository,
    CloudflareRpcClient,
)
from coding_trajectory.control_plane.remote_estimation import RemoteEstimationAuthority

URL = "http://127.0.0.1:8794"
TOKENS = {
    "owner": "local-qualification-owner-token-0000000001",
    "reader": "local-qualification-reader-token-000000001",
    "worker": "local-qualification-worker-token-000000001",
    "other": "local-qualification-other-token-0000000001",
}
WORKSPACE = "00000000-0000-0000-0000-000000000001"
AGENT = "00000000-0000-0000-0000-000000000003"
checks = 0


def check(condition, label):
    global checks
    if not condition:
        raise AssertionError(label)
    checks += 1


def rpc(method, request, *, role="owner", status=200, key=None):
    body = {
        "protocol": "ct.core.v1",
        "id": None,
        "method": method,
        "params": {"workspace_id": WORKSPACE, **request},
    }
    if key:
        body["idempotency_key"] = key
    response = httpx.post(
        URL + "/v1/core",
        json=body,
        headers={"Authorization": "Bearer " + TOKENS[role]},
        timeout=30,
    )
    check(
        response.status_code == status,
        f"{method}: {response.status_code} {response.text[:200]}",
    )
    envelope = response.json()
    return envelope["data"] if envelope.get("ok") else envelope


def main():
    tag = uuid4().hex
    remote = CloudflareCollectorRemote(url=URL, access_token=TOKENS["owner"])
    project = remote.register_project(
        ProjectRegistrationRequest(
            workspace_id=WORKSPACE, agent_id=AGENT, display_name="Qualification-" + tag
        )
    )
    project_id = str(project.project_id)
    source_request = SourceRegistrationRequest(
        workspace_id=WORKSPACE,
        agent_id=AGENT,
        project_id=project_id,
        vendor="codex",
        native_session_id=tag,
    )
    source = remote.register_source(source_request, idempotency_key="source:" + tag)
    duplicate = remote.register_source(source_request, idempotency_key="source:" + tag)
    check(source == duplicate, "source retry")
    rpc(
        "ct_project_register",
        {"agent_id": AGENT, "display_name": "denied"},
        role="reader",
        status=403,
    )
    rpc("ct_workspace_snapshot", {}, role="other", status=403)
    check(
        httpx.post(
            URL + "/v1/core",
            json={
                "protocol": "ct.core.v1",
                "id": None,
                "method": "ct_workspace_snapshot",
                "params": {"workspace_id": WORKSPACE},
            },
        ).status_code
        == 401,
        "missing authentication",
    )
    session = str(uuid4())
    captured = datetime.now(UTC).isoformat()
    artifact = ChronicleGraphArtifact.model_validate(
        {
            "graph": {
                "root_session_id": session,
                "project": "Qualification-" + tag,
                "session_count": 1,
                "turn_count": 0,
                "item_count": 0,
            },
            "sessions": [
                {
                    "session_id": session,
                    "vendor": "codex_cli",
                    "started_at": captured,
                    "status": "not_living",
                }
            ],
        }
    )
    payload = {
        "kind": "ct.source_checkpoint.v1",
        "source_checkpoint": {"segments": [1]},
        "chronicle_digest": artifact.digest(),
    }
    content_hash = hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    observation = ObservationRequest(
        workspace_id=WORKSPACE,
        agent_id=AGENT,
        source_id=source.source_id,
        source_epoch=1,
        source_sequence=0,
        event_id="checkpoint:" + content_hash,
        parser_version="qualification",
        content_sha256=content_hash,
        observed_at=captured,
        payload=payload,
    )
    accepted = remote.publish_observation(
        observation, idempotency_key="checkpoint:" + tag
    )
    check(accepted.outcome == "accepted", "checkpoint accepted")
    check(
        remote.publish_observation(observation, idempotency_key="checkpoint:" + tag)
        == accepted,
        "checkpoint receipt retry",
    )
    corrupt = observation.model_dump(mode="json")
    corrupt["content_sha256"] = "f" * 64
    rpc("ct_collector_publish_observation", corrupt, status=400)
    publication = ArtifactPublicationRequest.model_validate(
        {
            "workspace_id": WORKSPACE,
            "agent_id": AGENT,
            "project_id": project_id,
            "publication_sequence": 0,
            "source_vector": [
                {
                    "source_id": str(source.source_id),
                    "source_epoch": 1,
                    "source_sequence": 0,
                    "content_sha256": content_hash,
                }
            ],
            "artifacts": [
                {
                    "artifact_id": session,
                    "observed_at": captured,
                    "schema_version": artifact.schema_version,
                    "content_sha256": artifact.digest(),
                    "serialized_bytes": len(artifact.canonical_bytes()),
                    "source_ids": [str(source.source_id)],
                    "payload": artifact.model_dump(mode="json"),
                }
            ],
        }
    )
    published = remote.publish_artifacts(
        publication, idempotency_key="publication:" + tag
    )
    check(
        published.details.get("publication_outcome") == "published",
        "artifact published",
    )
    check(
        remote.publish_artifacts(publication, idempotency_key="publication:" + tag)
        == published,
        "publication receipt retry",
    )
    client = CloudflareRpcClient(url=URL, access_token=TOKENS["reader"])
    historical = CloudflareHistoricalRepository(
        client=client,
        workspace_id=project.workspace_id
        if hasattr(project, "workspace_id")
        else __import__("uuid").UUID(WORKSPACE),
    )
    store, _ = historical.store_for("session.overview", {"session_id": session})
    check(__import__("uuid").UUID(session) in store.sessions, "gzip artifact replay")
    projected = rpc(
        "ct_project_sessions_projection", {"project_name": "Qualification-" + tag}
    )
    service_contract("project.sessions").validate_response(projected["result"])
    check(
        projected["complete"]
        and projected["result"] == _project_session_list_variants(artifact)["default"],
        "projection parity",
    )
    catalog_status = rpc("ct_catalog_read_v2", {"kind": "status"}, role="reader")
    CatalogReadResponse.model_validate(catalog_status)
    selection = catalog_status["selection"]["token"]
    for variant, include in (
        ("default", []),
        ("runtime", ["runtime"]),
        ("usage", ["usage"]),
        ("runtime_usage", ["runtime", "usage"]),
    ):
        catalog = rpc(
            "ct_catalog_read_v2",
            {
                "kind": "sessions",
                "project_name": "Qualification-" + tag,
                "selection": selection,
                "include": include,
            },
            role="reader",
        )
        CatalogReadResponse.model_validate(catalog)
        expected = _project_session_list_variants(artifact)[variant]
        actual = service_contract("project.sessions").validate_response(
            {"items": [row["projection"] for row in catalog["items"]]}
        )
        # Catalog names use the pinned registered identity, not host-local names.
        for item in expected["items"]:
            item["project"] = "Qualification-" + tag
        check(actual == expected, f"catalog v2 {variant} canonical parity")
        check(catalog["coverage"] == "complete", "catalog v2 complete coverage")
        runtime = RemoteRuntimeFactory(url=URL, workspace_id=UUID(WORKSPACE)).build(
            TOKENS["reader"], local_evidence=True
        )
        try:
            check(
                runtime.call(
                    "project.sessions",
                    {"project_name": "Qualification-" + tag, "include": include},
                )
                == expected,
                f"runtime catalog {variant} parity with local evidence enabled",
            )
            check(
                "Qualification-" + tag in runtime.call("project.list", {})["items"],
                "runtime registered inventory",
            )
        finally:
            runtime.close()
    rpc("ct_catalog_read_v2", {"kind": "status"}, role="other", status=403)
    rpc("ct_catalog_read_v2", {"kind": "status"}, role="worker", status=403)
    rpc(
        "ct_catalog_read_v2",
        {"kind": "status", "snapshot_sequence": 0},
        role="reader",
        status=400,
    )
    snapshot = rpc("ct_workspace_snapshot", {})["snapshot_sequence"]
    invalid = publication.reference_payload()
    invalid["publication_sequence"] = 1
    invalid["artifacts"][0]["content_sha256"] = "f" * 64
    rpc("ct_collector_publish_artifacts", invalid, status=400)
    check(
        rpc("ct_workspace_snapshot", {})["snapshot_sequence"] == snapshot,
        "failed publication rolls back",
    )
    inventory = rpc("ct_project_inventory_snapshot", {})
    check(
        any(p["project_id"] == project_id for p in inventory["projects"]),
        "portable project inventory",
    )
    instance = str(uuid4())
    heartbeat = LeaseHeartbeatRequest(
        workspace_id=WORKSPACE,
        agent_id=AGENT,
        agent_instance_id=instance,
        observation_sequence=1,
        observed_at=captured,
    )
    lease = remote.heartbeat(heartbeat)
    check(remote.heartbeat(heartbeat) == lease, "heartbeat retry does not extend lease")
    for index in (2, 3):
        change = {
            "cursor": "local",
            "revision": 0,
            "operation": "upsert",
            "resource_kind": "session",
            "path": {"root_session_id": session, "session_id": str(uuid4())},
            "resource": None,
        }
        rpc(
            "ct_collector_publish_living_observation",
            {
                "agent_id": AGENT,
                "agent_instance_id": instance,
                "observation_sequence": index,
                "observed_at": captured,
                "kind": "living.sessions",
                "payload": change,
            },
        )
    page = rpc(
        "ct_remote_living",
        {"calls": [{"method": "living.sessions", "params": {"limit": 1}}]},
    )["results"][0]["result"]
    service_contract("living.sessions").validate_response(page)
    check(page["has_more"] and len(page["changes"]) == 1, "living bounded page")
    next_page = rpc(
        "ct_remote_living",
        {
            "calls": [
                {
                    "method": "living.sessions",
                    "params": {
                        "limit": 1,
                        "after": page["next_cursor"],
                        "through": page["through"],
                    },
                }
            ]
        },
    )["results"][0]["result"]
    check(
        next_page["changes"][0]["cursor"] != page["changes"][0]["cursor"],
        "living continuation advances",
    )
    rpc(
        "ct_remote_living",
        {
            "calls": [
                {"method": "living.events", "params": {"through": page["through"]}}
            ]
        },
        status=400,
    )
    prediction = uuid4().hex
    plan = {
        "idempotency_key": hashlib.sha256(tag.encode()).hexdigest(),
        "record": {
            "prediction_id": prediction,
            "idempotency_key": hashlib.sha256(tag.encode()).hexdigest(),
            "forecast_kind": "prospective_unbound",
            "issued_at": captured,
            "turn_id": None,
            "estimator": {"provider": "qualification", "model": None, "effort": None},
        },
        "prompt": "Estimate this synthetic qualification task.",
        "comparison": None,
    }
    queued = rpc("ct_estimate_predict", {"plan": plan})
    check(queued["failure"]["reason"] == "forecast_pending", "predict enqueues")
    claim = rpc(
        "ct_estimator_claim", {"worker_id": "qualification:" + tag}, role="worker"
    )
    check(claim["job_id"] == queued["failure"]["detail"], "worker claim")
    completed = rpc(
        "ct_estimator_complete",
        {
            **{key: claim[key] for key in ("job_id", "worker_id", "attempt_number")},
            "p50_minutes": 2,
            "p80_minutes": 3,
        },
        role="worker",
    )
    check(completed["outcome"] == "succeeded", "worker completion")
    reused = rpc("ct_estimate_predict", {"plan": plan})
    check(
        reused["reused_existing"] and reused["forecast"]["p50_minutes"] == 2,
        "forecast retry reads result",
    )
    check(
        rpc(
            "ct_estimate_get",
            {"prediction_id": prediction, "snapshot_sequence": snapshot},
        )["forecast"]
        is None,
        "forecast historical pin",
    )
    backfill = rpc(
        "ct_estimate_backfill_start",
        {
            "spec": {"max_forecasts": 1, "concurrency": 1},
            "plans": [plan],
            "excluded": {},
        },
    )
    claim = rpc(
        "ct_estimator_claim", {"worker_id": "qualification:" + tag}, role="worker"
    )
    completion_request = {
        **{key: claim[key] for key in ("job_id", "worker_id", "attempt_number")},
        "p50_minutes": 2,
        "p80_minutes": 3,
    }
    check(
        rpc("ct_estimator_complete", completion_request, role="worker")["outcome"]
        == "skipped_existing",
        "backfill deduplication",
    )
    check(
        rpc("ct_estimator_complete", completion_request, role="worker")["outcome"]
        == "skipped_existing",
        "completion retry",
    )
    check(
        rpc("ct_estimate_backfill_status", {"job_id": backfill["job"]["job_id"]})[
            "job"
        ]["status"]
        == "completed",
        "backfill status",
    )
    check(rpc("ct_estimate_list", {})["items"], "forecast listing")
    check(rpc("ct_estimate_calibration", {})["records"], "calibration records")
    bound = rpc(
        "ct_estimate_bind",
        {
            "prediction_id": prediction,
            "binding": {
                "turn_id": str(uuid4()),
                "bound_at": captured,
                "target_execution_started_at": "2099-01-01T00:00:00Z",
            },
        },
    )
    check(bound["forecast"]["status"] == "uncompared", "forecast binding")
    compared = rpc(
        "ct_estimate_compare",
        {"prediction_id": prediction, "comparison": {"exclusion": "synthetic"}},
    )
    check(compared["forecast"]["status"] == "compared", "forecast comparison")
    # Observe actual HTTP requests: completed evidence reads need no artifacts.
    evidence_client = CloudflareRpcClient(url=URL, access_token=TOKENS["owner"])
    evidence_calls = []
    evidence_client._client.event_hooks["request"].append(
        lambda request: evidence_calls.append(json.loads(request.content)["method"])
    )
    authority = RemoteEstimationAuthority(
        client=evidence_client, workspace_id=UUID(WORKSPACE)
    )
    try:
        check(
            authority("estimate.get", {"prediction_id": prediction})["forecast"]
            == compared["forecast"],
            "completed evidence retained without reconstruction",
        )
        check(
            evidence_calls == ["ct_workspace_snapshot", "ct_estimate_get"],
            "completed forecast get uses only metadata fence and record",
        )
        evidence_calls.clear()
        check(authority("estimate.list", {})["items"], "authority forecast listing")
        check(
            evidence_calls == ["ct_workspace_snapshot", "ct_estimate_list"],
            "completed forecast list uses no historical hydration",
        )
    finally:
        evidence_client.close()
    recovery = rpc(
        "ct_collector_recover",
        {
            "agent_id": AGENT,
            "project_id": project_id,
            "vendor": "codex",
            "native_session_id": tag,
        },
    )
    check(
        recovery["next_publication_sequence"] == 1
        and recovery["source"]["next_source_sequence"] == 1,
        "collector recovery",
    )
    check(
        rpc("ct_historical_snapshot", {"metadata_only": True})["artifacts"] == [],
        "metadata-only pin",
    )
    failed_plan = {
        **plan,
        "idempotency_key": hashlib.sha256((tag + "retry").encode()).hexdigest(),
        "record": {**plan["record"], "prediction_id": uuid4().hex},
    }
    failed_plan["record"]["idempotency_key"] = failed_plan["idempotency_key"]
    rpc("ct_estimate_predict", {"plan": failed_plan})
    claim = rpc(
        "ct_estimator_claim", {"worker_id": "qualification:" + tag}, role="worker"
    )
    failed = rpc(
        "ct_estimator_fail",
        {
            **{key: claim[key] for key in ("job_id", "worker_id", "attempt_number")},
            "permanent": True,
            "retry_seconds": 1,
        },
        role="worker",
    )
    check(failed["status"] == "cancelled", "permanent worker failure")
    check(
        rpc("ct_estimate_predict", {"plan": failed_plan})["failure"]["state"]
        == "permanent_failed",
        "durable failed prediction",
    )
    print(json.dumps({"status": "passed", "checks": checks}))


if __name__ == "__main__":
    if sys.argv[1:] == ["--prepare-local"]:
        registry = {
            hashlib.sha256(token.encode()).hexdigest(): {
                "workspace_id": WORKSPACE
                if role != "other"
                else "00000000-0000-0000-0000-000000000002",
                "agent_id": AGENT,
                "roles": {
                    "owner": ["owner"],
                    "other": ["owner"],
                    "reader": ["read"],
                    "worker": ["estimate_worker"],
                }[role],
            }
            for role, token in TOKENS.items()
        }
        destination = (
            Path(__file__).resolve().parents[1] / "cloudflare/control-plane/.dev.vars"
        )
        if destination.exists():
            raise SystemExit(
                "Local variables already exist; preserve or remove them before preparing the harness."
            )
        destination.write_text("CT_PRINCIPALS=" + json.dumps(registry) + "\n")
        destination.chmod(0o600)
        print("Local-only qualification principals prepared.")
    elif sys.argv[1:]:
        raise SystemExit("Usage: qualify-cloudflare-control-plane.py [--prepare-local]")
    else:
        main()
