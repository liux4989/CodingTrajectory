#!/usr/bin/env python3
"""Export a private seven-day Datahub snapshot from local sources only.

Run after the hosted web build. Only sanitized Chronicle replay is used to
produce API responses; no remote credentials, database or network calls occur.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import tempfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from coding_trajectory.control_plane.chronicle import build_chronicle_graph_artifact
from coding_trajectory.discovery import discover_store
from coding_trajectory.query import DocumentStore
from datahub_plugin.api_models import validate_api_response
from datahub_plugin.hosted.service import (
    HostedDatahubService,
    ProjectsQuery,
    _dispatch,
    _page,
)
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ASSETS = ROOT / "packages/plugins/datahub/web/dist"
MAX_ASSET_BYTES = 20 * 1024 * 1024


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    revision: int = Field(gt=0)
    generated_at: str
    horizon_days: int = 7
    source: str = "local_snapshot"
    graph_count: int
    session_count: int
    item_count: int
    files: dict[str, str]


class NoRemote:
    async def call(self, name: str, request: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("snapshot export cannot call remote services")


class LocalSnapshotService(HostedDatahubService):
    def __init__(self, store: DocumentStore, revision: int, workspace_id: UUID):
        super().__init__(rpc=NoRemote(), workspace_id=workspace_id)
        self.store = store
        self.revision = revision

    async def _pin(self) -> int:
        return self.revision

    async def _store_for(self, method, params, sequence):
        return self.store

    async def _project_sessions(self, params, sequence):
        return _dispatch("project.sessions", params, self.store, sequence)

    async def _projects(self, params: ProjectsQuery):
        projects: dict[str, set[str]] = defaultdict(set)
        for graph in self.store.session_graphs.values():
            for session in graph.sessions:
                projects[graph.project_identifier].add(session.vendor.value)
        rows = [
            {"name": name, "path": None, "vendors": sorted(vendors)}
            for name, vendors in sorted(projects.items(), key=lambda x: x[0].casefold())
            if params.agent_vendor is None or params.agent_vendor in vendors
        ]
        from datahub_plugin.hosted.service import _cursor_offset

        rows, cursor = _page(
            rows,
            _cursor_offset(params.cursor, self.revision),
            params.limit,
            self.revision,
        )
        return {
            "items": rows,
            "page": {
                "revision": self.revision,
                "next_cursor": cursor,
                "has_more": cursor is not None,
            },
        }


async def export(project: Path, assets: Path) -> Manifest:
    if not (assets / "index.html").is_file():
        raise ValueError("build the hosted web app before exporting")
    captured = datetime.now(UTC)
    revision = int(captured.timestamp() * 1000)
    discovered = discover_store(current_dir=project, since_days=7, global_scope=False)
    # Sanitize before invoking any output handler. Never serialize the raw store.
    graphs = []
    for graph in discovered.store.session_graphs.values():
        artifact = build_chronicle_graph_artifact(graph)
        replay = artifact.to_session_graph()
        if (
            build_chronicle_graph_artifact(replay).canonical_bytes()
            != artifact.canonical_bytes()
        ):
            raise ValueError("Chronicle replay mismatch")
        graphs.append(replay)
    store = DocumentStore.from_session_graphs(graphs)
    service = LocalSnapshotService(
        store, revision, uuid5(NAMESPACE_URL, "ct:local-snapshot")
    )
    files: dict[str, str] = {}
    # Sibling staging prevents failed exports from leaving a partially published bundle.
    with tempfile.TemporaryDirectory(
        prefix="datahub-snapshot-", dir=assets.parent
    ) as tmp:
        staging = Path(tmp)

        def write(name: str, value: Any):
            data = json.dumps(
                value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode()
            if len(data) > MAX_ASSET_BYTES:
                raise ValueError("snapshot response exceeds asset byte limit")
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            files[name] = hashlib.sha256(data).hexdigest()

        async def response(path: str, **query):
            payload, status = await service.handle(method="GET", path=path, query=query)
            assert status == 200
            return payload

        async def collection(path: str, **query):
            rows = []
            while True:
                payload = await response(path, limit=200, **query)
                rows.extend(payload["items"])
                cursor = payload["page"]["next_cursor"]
                if cursor is None:
                    return rows
                query["cursor"] = cursor

        snapshot = await response("/api/datahub/snapshot")
        snapshot["generated_at"] = captured.isoformat()
        snapshot["freshness"]["last_refresh_at"] = captured.isoformat()
        snapshot["bootstrap"]["coverage"]["mode"] = "snapshot"
        validate_api_response("snapshot", snapshot)
        write("snapshot.json", snapshot)
        write(
            "changes.json",
            await response("/api/datahub/changes", after_revision=revision),
        )
        write("projects.json", await collection("/api/projects"))
        for days in range(1, 8):
            write(
                f"sessions-{days}.json",
                await collection("/api/sessions", since_days=days),
            )
        for session_id in sorted(store.sessions, key=str):
            write(
                f"trees/{session_id}.json",
                await response("/api/sessions/tree", session_id=str(session_id)),
            )
            write(
                f"graphs/{session_id}.json",
                await response("/api/sessions/graph", session_id=str(session_id)),
            )
        buckets: dict[str, dict[str, Any]] = defaultdict(dict)
        ids = sorted(store.items, key=str)
        for offset in range(0, len(ids), 200):
            details = await response(
                "/api/sessions/items",
                item_ids=[str(x) for x in ids[offset : offset + 200]],
            )
            for detail in details:
                item_id = detail["item_id"]
                buckets[item_id[:1]][item_id] = detail
        for prefix, details in buckets.items():
            write(f"items/{prefix}.json", details)
        manifest = Manifest(
            revision=revision,
            generated_at=captured.isoformat(),
            graph_count=len(graphs),
            session_count=len(store.sessions),
            item_count=len(ids),
            files=dict(files),
        )
        write("manifest.json", manifest.model_dump())
        destination = assets / "_snapshot"
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(staging, destination)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=ROOT)
    parser.add_argument("--assets", type=Path, default=DEFAULT_ASSETS)
    args = parser.parse_args()
    manifest = asyncio.run(export(args.project.resolve(), args.assets.resolve()))
    print(manifest.model_dump_json(exclude={"files"}))


if __name__ == "__main__":
    main()
