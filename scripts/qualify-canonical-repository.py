"""Real SQLite and process-exit qualification for the canonical capture boundary."""

from __future__ import annotations

import hashlib
import os
import sqlite3
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
from coding_trajectory.control_plane.upload_capture import UploadCapturePage
from coding_trajectory.control_plane.upload_service import (
    CanonicalCapture,
    CaptureSource,
    UploadService,
)
from coding_trajectory.control_plane.upload_state import UploadStateError
from coding_trajectory.ingestion.common import canonical_json
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
        # Match UploadService's durable canonical/outbox pairing.
        path = (root / "upload.sqlite3").with_suffix(".canonical.sqlite3")
        commit(path, "")
        repo = CanonicalRepository(path, IDENTITY)
        assert repo.commit(captures=[capture()], snapshots={}) == 1
        page = repo.changes_after(None)
        assert (
            page and page.captures[0].artifact.digest() == capture().artifact.digest()
        )
        assert repo.changes_after(page.cursor) is None
        version = repo.db.execute(
            "SELECT body,root_digest FROM canonical_versions WHERE revision=1"
        ).fetchone()
        assert version[1] and len(version[0]) < 100
        assert (
            repo.db.execute(
                "SELECT max(length(CAST(body AS BLOB))) FROM canonical_blocks"
            ).fetchone()[0]
            <= 65536
        )
        service = UploadService(root / "upload.sqlite3", IDENTITY)
        assert service.prepare_capture(page)["prepared"] == 1
        stored = service.db.execute("SELECT body FROM sync_batches").fetchone()[0]
        assert "canonical_reference" in stored and "artifact" not in stored
        assert (
            repo.db.execute(
                "SELECT state FROM canonical_retentions WHERE token=?",
                (page.references[0].retention_token,),
            ).fetchone()[0]
            == "retained"
        )
        batch = service.db.execute("SELECT * FROM sync_batches").fetchone()
        assert service._batch_body(batch)["artifact"]["graph"][
            "root_session_id"
        ] == str(SESSION)
        try:
            service.prepare_capture(
                page.model_copy(update={"captures": (capture("2026-09-10T01:00:00Z"),)})
            )
        except UploadStateError as error:
            assert error.code == "canonical_reference_content_conflict"
        else:
            raise AssertionError("outbox accepted a reference to different content")
        checks += 1
        service.close()
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
        checks += 14
        read.close()
        repo.close()
        # A read-only client must still open an unmigrated inline repository.
        legacy = root / "legacy.sqlite3"
        with sqlite3.connect(legacy) as db:
            db.executescript(
                "CREATE TABLE canonical_versions(revision INTEGER PRIMARY KEY,artifact_id TEXT,digest TEXT,body TEXT); CREATE TABLE canonical_member_versions(session_id TEXT,revision INTEGER,artifact_id TEXT);"
            )
            db.execute(
                "INSERT INTO canonical_versions VALUES(1,?,?,?)",
                (
                    str(SESSION),
                    capture().artifact.digest(),
                    capture().model_dump_json(),
                ),
            )
            db.execute(
                "INSERT INTO canonical_member_versions VALUES(?,1,?)",
                (str(SESSION), str(SESSION)),
            )
        old_reader = CanonicalReadRepository(legacy)
        old_store, _ = old_reader.store_for(
            "session.tree", {"session_id": str(SESSION)}
        )
        assert old_store.sessions[SESSION].started_at.hour == 0
        old_reader.close()
        checks += 1
        with sqlite3.connect(path) as db:
            db.execute(
                "UPDATE canonical_blocks SET body='{}' WHERE digest=?", (version[1],)
            )
        corrupted = CanonicalReadRepository(path)
        corrupted.revision = 1
        try:
            corrupted.store_for("session.tree", {"session_id": str(SESSION)})
        except ValueError as error:
            assert "corrupt" in str(error)
        else:
            raise AssertionError("corrupt content-addressed block was accepted")
        finally:
            corrupted.close()
        checks += 1
        # The pre-reference wire shape defines the identity of inline retries.
        inline_wire = {
            "repository_id": "legacy-repository",
            "cursor": "legacy-repository:1",
            "captures": [capture().model_dump(mode="json")],
        }
        inline_page = UploadCapturePage.model_validate(inline_wire)
        inline = UploadService(root / "inline.sqlite3", IDENTITY)
        assert inline.prepare_capture(inline_page)["prepared"] == 1
        assert inline.db.execute("SELECT digest FROM sync_pages").fetchone()[0] == (
            hashlib.sha256(canonical_json(inline_wire).encode()).hexdigest()
        )
        inline.close()
        inline = UploadService(root / "inline.sqlite3", IDENTITY)
        assert inline.prepare_capture(inline_page)["prepared"] == 0
        assert inline.db.execute("SELECT count(*) FROM sync_batches").fetchone()[0] == 1
        inline.close()
        checks += 4
    print({"passed": checks, "process_crash_boundaries": 2})


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        commit(Path(sys.argv[2]), sys.argv[3])
    else:
        main()
