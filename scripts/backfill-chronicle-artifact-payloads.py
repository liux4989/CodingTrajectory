#!/usr/bin/env python3
"""Backfill current Chronicle artifacts into compressed payload projections.

Only aggregate counts and byte sizes are printed. Existing JSONB remains intact.
"""

from __future__ import annotations

import argparse
import json

from coding_trajectory.control_plane.chronicle import ChronicleGraphArtifact
from coding_trajectory.control_plane.collector import SupabaseCollectorRemote
from coding_trajectory.control_plane.remote import (
    SupabaseRpcClient,
    _snapshot_artifact_graph,
)
from coding_trajectory_cli.collector_credentials import refresh_profile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="default")
    args = parser.parse_args()

    credentials = refresh_profile(args.profile)
    profile = credentials.profile
    reader = SupabaseRpcClient(
        url=str(profile.supabase_url),
        api_key=profile.supabase_api_key,
        access_token=credentials.access_token,
        timeout=60,
    )
    writer = SupabaseCollectorRemote(
        url=str(profile.supabase_url),
        api_key=profile.supabase_api_key,
        access_token=credentials.access_token,
        timeout=60,
    )
    staged = 0
    canonical_bytes = 0
    try:
        raw = reader.call(
            "ct_historical_snapshot",
            {"workspace_id": str(profile.workspace_id)},
        )
        artifacts = raw.get("artifacts")
        if not isinstance(artifacts, list):
            raise TypeError("historical snapshot has no artifact list")
        for value in artifacts:
            _snapshot_artifact_graph(value)
            digest = value["content_sha256"]
            validated = ChronicleGraphArtifact.model_validate(value["payload"])
            writer.stage_artifact_payload(
                workspace_id=profile.workspace_id,
                agent_id=profile.agent_id,
                artifact=validated,
                content_sha256=digest,
            )
            staged += 1
            canonical_bytes += len(validated.canonical_bytes())
    finally:
        reader.close()
        writer.close()
    print(
        json.dumps(
            {
                "status": "passed",
                "artifacts_staged": staged,
                "canonical_bytes": canonical_bytes,
                "jsonb_retained": True,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
