"""Real SQLite and process-exit qualification for the canonical capture boundary."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import UUID

from coding_trajectory.control_plane.canonical_repository import (
    CanonicalReadRepository,
    CanonicalRepository,
)
from coding_trajectory.control_plane.chronicle import ChronicleGraphArtifact
from coding_trajectory.control_plane.collector import CollectorIdentity
from coding_trajectory.control_plane.read_projections import build_read_projections
from coding_trajectory.control_plane.upload_service import (
    CanonicalCapture,
    CaptureSource,
)
from coding_trajectory.living_sources import LivingSourceSnapshot

SESSION = UUID("11111111-1111-4111-8111-111111111111")
IDENTITY = CollectorIdentity(
    workspace_id=SESSION,
    agent_id=SESSION,
    agent_instance_id=SESSION,
    project_name="Synthetic",
)


def capture(start="2026-09-10T00:00:00Z"):
    artifact = ChronicleGraphArtifact.model_validate(
        {
            "graph": {
                "root_session_id": SESSION,
                "project": "Synthetic",
                "session_count": 1,
                "turn_count": 0,
                "item_count": 0,
            },
            "sessions": [
                {
                    "session_id": SESSION,
                    "vendor": "codex_cli",
                    "started_at": start,
                    "status": "not_living",
                }
            ],
        }
    )
    return CanonicalCapture(
        artifact=artifact,
        sources=(
            CaptureSource(
                vendor="codex_cli",
                native_session_id=str(SESSION),
                segments=(1,),
                chronicle_digest=artifact.digest(),
                observed_at=start,
                source_generation="fixture-v1",
            ),
        ),
    )


def commit(path: Path, boundary: str):
    def crash(point: str):
        if point == boundary:
            os._exit(91)

    repo = CanonicalRepository(path, IDENTITY, crash_hook=crash)
    snapshot = LivingSourceSnapshot(
        path="synthetic",
        vendor="codex_cli",
        file_identity="fixture",
        size=1,
        mtime_ns=1,
        ctime_ns=1,
        committed_offset=1,
        status="ready",
    )
    repo.commit(captures=[capture()], snapshots={"synthetic": snapshot})
    repo.close()


def main():
    checks = 0
    with tempfile.TemporaryDirectory(prefix="ct-canonical-") as directory:
        root = Path(directory)
        for boundary, revision in (
            ("before_canonical_commit", 0),
            ("after_canonical_commit", 1),
        ):
            path = root / f"{boundary}.sqlite3"
            result = subprocess.run(
                [sys.executable, __file__, "--child", str(path), boundary], check=False
            )
            assert result.returncode == 91
            repo = CanonicalRepository(path, IDENTITY)
            assert repo.pin_snapshot() == revision
            assert bool(repo.source_snapshots()) == bool(revision)
            checks += 3
            repo.close()
        path = root / "read.sqlite3"
        commit(path, "")
        repo = CanonicalRepository(path, IDENTITY)
        assert repo.commit(captures=[capture()], snapshots={}) == 1
        page = repo.changes_after(None)
        assert (
            page and page.captures[0].artifact.digest() == capture().artifact.digest()
        )
        assert repo.changes_after(page.cursor) is None
        read = CanonicalReadRepository(path)
        assert (
            repo.commit(captures=[capture("2026-09-10T01:00:00Z")], snapshots={}) == 2
        )
        store, _ = read.store_for("session.tree", {"session_id": str(SESSION)})
        assert store.sessions[SESSION].started_at.hour == 0
        assert repo.graph(str(SESSION)).sessions[0].started_at.hour == 1
        assert (
            repo.changes_after(page.cursor).captures[0].artifact.digest()
            != page.captures[0].artifact.digest()
        )
        projections = build_read_projections(capture().artifact)
        assert projections["content_sha256"] == capture().artifact.digest()
        assert projections["graph"]["root_session_id"] == str(SESSION)
        checks += 9
        read.close()
        repo.close()
    print({"passed": checks, "process_crash_boundaries": 2})


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        commit(Path(sys.argv[2]), sys.argv[3])
    else:
        main()
