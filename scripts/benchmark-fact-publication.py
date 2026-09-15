#!/usr/bin/env python3
"""Benchmark synthetic fact staging, publication, and reads on local workerd."""

from __future__ import annotations

import argparse
import hashlib
import json
import runpy
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

from coding_trajectory.control_plane.collector import CloudflareCollectorRemote
from coding_trajectory.control_plane.collector_protocol import (
    ObservationRequest,
    ProjectRegistrationRequest,
    SourceRegistrationRequest,
    SourceVectorEntry,
)
from coding_trajectory.control_plane.fact_protocol import FactPublicationRequest
from coding_trajectory.control_plane.remote import CloudflareRpcClient
from coding_trajectory.ingestion.common import canonical_json

WORKSPACE = UUID("00000000-0000-0000-0000-000000000001")
AGENT = UUID("00000000-0000-0000-0000-000000000003")
TOKEN = "local-qualification-owner-token-0000000001"


def benchmark_scale(
    *,
    url: str,
    graph_count: int,
    qualification: dict[str, Any],
    boundary_rows: bool = False,
) -> dict[str, Any]:
    tag = uuid4().hex
    observed_at = datetime.now(UTC).replace(microsecond=0)
    project_name = f"Fact-benchmark-{graph_count}-{tag}"
    remote = CloudflareCollectorRemote(url=url, access_token=TOKEN, timeout=60)
    try:
        project = remote.register_project(
            ProjectRegistrationRequest(
                workspace_id=WORKSPACE,
                agent_id=AGENT,
                display_name=project_name,
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
            idempotency_key="benchmark-source:" + tag,
        )
        fact_sets = [
            (
                qualification["large_fact_set"](
                    seed=f"{tag}:{index}", project=project_name
                )
                if boundary_rows
                else qualification["derive_published_fact_set"](
                    qualification["synthetic_artifact"](
                        seed=f"{tag}:{index}", project=project_name
                    )
                )
            )
            for index in range(graph_count)
        ]
        encoded_bytes = sum(
            len(fact_set.model_dump_json(exclude_none=True).encode())
            for fact_set in fact_sets
        )
        checkpoint_payload = {
            "kind": "ct.source_checkpoint.v1",
            "source_checkpoint": {"segments": [encoded_bytes]},
            "chronicle_digest": hashlib.sha256(
                "".join(fact_set.fact_set_digest for fact_set in fact_sets).encode()
            ).hexdigest(),
        }
        checkpoint_digest = hashlib.sha256(
            canonical_json(checkpoint_payload).encode()
        ).hexdigest()
        remote.publish_observation(
            ObservationRequest(
                workspace_id=WORKSPACE,
                agent_id=AGENT,
                source_id=source.source_id,
                source_epoch=source.source_epoch,
                source_sequence=0,
                event_id="checkpoint:" + checkpoint_digest,
                parser_version="fact-benchmark.v1",
                content_sha256=checkpoint_digest,
                observed_at=observed_at,
                payload=checkpoint_payload,
            ),
            idempotency_key="benchmark-checkpoint:" + checkpoint_digest,
        )

        started = time.perf_counter()
        for fact_set in fact_sets:
            qualification["stage_fact_set"](remote, fact_set=fact_set)
        stage_ms = round((time.perf_counter() - started) * 1000, 3)

        publication = FactPublicationRequest(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            project_id=project.project_id,
            publication_sequence=0,
            source_vector=[
                SourceVectorEntry(
                    source_id=source.source_id,
                    source_epoch=source.source_epoch,
                    source_sequence=0,
                    content_sha256=checkpoint_digest,
                )
            ],
            graphs=[
                qualification["manifest"](
                    fact_set,
                    source_id=source.source_id,
                    observed_at=observed_at,
                )
                for fact_set in fact_sets
            ],
        )
        started = time.perf_counter()
        receipt = remote.publish_facts(
            publication, idempotency_key="benchmark-publication:" + tag
        )
        publish_ms = round((time.perf_counter() - started) * 1000, 3)

        client = CloudflareRpcClient(url=url, access_token=TOKEN, timeout=60)
        try:
            started = time.perf_counter()
            cursor = None
            pages = 0
            read_rows = 0
            max_read_page_bytes = 0
            while True:
                page = client.call(
                    "ct_fact_read",
                    {
                        "workspace_id": str(WORKSPACE),
                        "project_name": project_name,
                        "snapshot_sequence": receipt.committed_sequence,
                        "limit": 2048,
                        **({"cursor": cursor} if cursor else {}),
                    },
                )
                pages += 1
                read_rows += len(page["rows"])
                max_read_page_bytes = max(
                    max_read_page_bytes,
                    len(canonical_json(page["rows"]).encode()),
                )
                cursor = page.get("next_cursor")
                if not cursor:
                    break
            read_ms = round((time.perf_counter() - started) * 1000, 3)
        finally:
            client.close()
        expected_rows = sum(len(fact_set.rows) for fact_set in fact_sets)
        if read_rows != expected_rows:
            raise AssertionError(
                f"fact benchmark read {read_rows} rows, expected {expected_rows}"
            )
        return {
            "graphs": graph_count,
            "rows": expected_rows,
            "boundary_sized_rows": boundary_rows,
            "encoded_fact_set_bytes": encoded_bytes,
            "largest_row_bytes": max(
                len(
                    canonical_json(
                        row.model_dump(mode="json", exclude_none=True)
                    ).encode()
                )
                for fact_set in fact_sets
                for row in fact_set.rows
            ),
            "stage_ms": stage_ms,
            "publish_ms": publish_ms,
            "read_ms": read_ms,
            "read_pages": pages,
            "max_read_page_bytes": max_read_page_bytes,
            "rows_inserted": receipt.details["rows_inserted"],
        }
    finally:
        remote.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url", default="http://127.0.0.1:8794", help="local Wrangler URL"
    )
    parser.add_argument("--small-graphs", type=int, default=25)
    parser.add_argument("--large-graphs", type=int, default=250)
    parser.add_argument("--boundary-graphs", type=int, default=32)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if urlparse(args.url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("benchmark refuses a non-loopback URL")
    if not 1 <= args.small_graphs < args.large_graphs <= 512:
        raise SystemExit("require 1 <= small-graphs < large-graphs <= 512")
    if not 1 <= args.boundary_graphs <= 512:
        raise SystemExit("require 1 <= boundary-graphs <= 512")

    qualification = runpy.run_path(
        str(Path(__file__).with_name("qualify-cloudflare-control-plane.py"))
    )
    result = {
        "schema_version": "ct.fact_publication_benchmark.v1",
        "environment": "wrangler dev --local; synthetic facts only",
        "recorded_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "results": [
            benchmark_scale(
                url=args.url,
                graph_count=count,
                qualification=qualification,
            )
            for count in (args.small_graphs, args.large_graphs)
        ]
        + [
            benchmark_scale(
                url=args.url,
                graph_count=args.boundary_graphs,
                qualification=qualification,
                boundary_rows=True,
            )
        ],
    }
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
