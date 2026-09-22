"""Benchmark exact frozen artifact bytes without discovery, reset, or publication.

Without --execute this only reports transport grouping. Remote benchmarks require
all objects already retained and compare warm single/batch transport fairly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from coding_trajectory.control_plane.artifact_protocol import (
    ArtifactPublicationRequest,
    ArtifactReadinessReference,
    ArtifactReadinessRequest,
)
from coding_trajectory.control_plane.artifact_transport import upload_groups
from coding_trajectory.control_plane.collector import CloudflareCollectorRemote
from coding_trajectory.control_plane.connections import load_profile_credentials


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--transport", choices=["single", "batch"], default="batch")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--profile")
    parser.add_argument("--worker-version")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    if args.output.exists():
        raise ValueError("benchmark output already exists")
    with sqlite3.connect(
        args.database.expanduser().resolve().as_uri() + "?mode=ro", uri=True
    ) as db:
        row = db.execute(
            "select request_json from publication_outbox where state='accepted' order by publication_sequence desc limit 1"
        ).fetchone()
        if not row:
            raise ValueError("benchmark requires an accepted frozen publication")
        request = ArtifactPublicationRequest.model_validate(
            json.loads(row[0])["artifact_publication"]
        )
        refs = {}
        for graph in request.graphs:
            for ref in (graph.facts, graph.summary, *graph.api_objects):
                refs[(ref.kind, ref.sha256)] = ref
        objects = list(refs.values())
        if args.limit is not None:
            if args.limit <= 0:
                raise ValueError("limit must be positive")
            objects = objects[: args.limit]
        groups = [
            group
            for offset in range(0, len(objects), 512)
            for group in upload_groups(
                objects[offset : offset + 512], enabled=args.transport == "batch"
            )
        ]
        report = {
            "transport": args.transport,
            "objects": len(objects),
            "object_bytes": sum(o.bytes for o in objects),
            "requests": len(groups),
            "condition": "retained objects; no discovery or publication",
            "executed": False,
        }
        if args.execute:
            if not args.profile or not args.worker_version:
                raise ValueError(
                    "execution requires an explicit profile and Worker version"
                )
            credentials = load_profile_credentials(args.profile)
            profile = credentials.profile
            remote = CloudflareCollectorRemote(
                url=str(profile.cloudflare_url),
                access_token=credentials.access_token,
                timeout=120,
            )

            def pinned(response):
                if response.headers.get("X-CT-Worker-Version") != args.worker_version:
                    raise ValueError("benchmark Worker version changed")

            remote._client.event_hooks["response"].append(pinned)
            for offset in range(0, len(objects), 512):
                ready = remote.artifact_readiness(
                    ArtifactReadinessRequest(
                        workspace_id=profile.workspace_id,
                        agent_id=profile.agent_id,
                        objects=[
                            ArtifactReadinessReference(**o.model_dump())
                            for o in objects[offset : offset + 512]
                        ],
                    )
                )
                if not all(ready.ready):
                    remote.close()
                    raise ValueError(
                        "warm benchmark requires all selected objects retained"
                    )
            measurements = []

            def upload(payload):
                start = time.monotonic()
                if len(payload) > 1:
                    remote.upload_artifact_batch(payload)
                else:
                    kind, sha256, body = payload[0]
                    remote.upload_artifact(kind=kind, sha256=sha256, body=body)
                return {
                    "objects": len(payload),
                    "bytes": sum(len(b) for _, _, b in payload),
                    "seconds": time.monotonic() - start,
                }

            start = time.monotonic()
            try:
                with ThreadPoolExecutor(max_workers=4) as executor:
                    pending = set()

                    def submit(group):
                        payload = []
                        for ref in group:
                            row = db.execute(
                                "select body from artifact_objects where sha256=? and kind=?",
                                (ref.sha256, ref.kind),
                            ).fetchone()
                            if (
                                not row
                                or hashlib.sha256(row[0]).hexdigest() != ref.sha256
                                or len(row[0]) != ref.bytes
                            ):
                                raise ValueError("frozen object integrity failure")
                            payload.append((ref.kind, ref.sha256, bytes(row[0])))
                        return executor.submit(upload, payload)

                    queue = iter(groups)
                    for _ in range(4):
                        group = next(queue, None)
                        if group:
                            pending.add(submit(group))
                    while pending:
                        done, pending = wait(pending, return_when=FIRST_COMPLETED)
                        for future in done:
                            measurements.append(future.result())
                        for _ in done:
                            group = next(queue, None)
                            if group:
                                pending.add(submit(group))
                report["executed"] = True
            finally:
                remote.close()
                report.update(
                    seconds=time.monotonic() - start,
                    completed_requests=len(measurements),
                    measurements=measurements,
                    worker_version=args.worker_version,
                )
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, indent=2))
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2))
        print(
            json.dumps(
                {k: v for k, v in report.items() if k != "measurements"}, indent=2
            )
        )


if __name__ == "__main__":
    main()
