#!/usr/bin/env python3
"""Generate the Cloudflare ingress schemas from canonical Pydantic contracts."""

from __future__ import annotations

import json
from pathlib import Path

from coding_trajectory.contracts import (
    LivingChange,
    LivingSessionsChange,
    service_contract,
)
from coding_trajectory.control_plane import artifact_protocol
from coding_trajectory.control_plane import collector_protocol as protocol

ROOT = Path(__file__).resolve().parents[1]


def main():
    models = {
        "ct_project_register": protocol.ProjectRegistrationRequest,
        "ct_collector_register_source": protocol.SourceRegistrationRequest,
        "ct_collector_recover": protocol.CollectorRecoveryRequest,
        "ct_collector_publish_observation": protocol.ObservationRequest,
        "ct_collector_publish_artifacts": artifact_protocol.ArtifactPublicationRequest,
        "ct_collector_heartbeat": protocol.LeaseHeartbeatRequest,
        "ct_collector_publish_living_observation": protocol.LivingObservationRequest,
        "ct_artifact_manifest": artifact_protocol.ArtifactManifestRequest,
        "ct_artifact_read": artifact_protocol.ArtifactReadRequest,
        "checkpoint": protocol.SourceCheckpointPayload,
        "living_events_change": LivingChange,
        "living_sessions_change": LivingSessionsChange,
    }
    for name, method in (
        ("living_events", "living.events"),
        ("living_sessions", "living.sessions"),
    ):
        models[f"{name}_request"] = service_contract(method).request_model
        models[f"{name}_response"] = service_contract(method).response_model
    schemas = {name: model.model_json_schema() for name, model in models.items()}
    destination = ROOT / "cloudflare/control-plane/src/contracts.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(schemas, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
