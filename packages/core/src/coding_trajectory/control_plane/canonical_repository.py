"""Durable canonical revisions shared by local readers and upload preparation.

Canonical payloads, complete source checkpoints and their change journal commit
in one SQLite transaction. The delivery cursor lives in the independent outbox;
readers can replay this journal after either process crashes.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from coding_trajectory.control_plane.chronicle import ChronicleGraphArtifact
from coding_trajectory.ingestion.common import canonical_json
from coding_trajectory.living_sources import LivingSourceSnapshot
from coding_trajectory.query import DocumentStore, ResourceNotFoundError

if TYPE_CHECKING:
    from coding_trajectory.control_plane.collector import CollectorIdentity
    from coding_trajectory.control_plane.upload_capture import (
        CanonicalCapture,
        UploadCapturePage,
    )


class CanonicalRepository:
    def __init__(
        self,
        path: Path,
        identity: CollectorIdentity,
        *,
        crash_hook: Callable[[str], None] | None = None,
        max_disk_bytes: int = 2 * 1024 * 1024 * 1024,
    ):
        if max_disk_bytes < 1024:
            raise ValueError("canonical disk budget must be positive")
        self.max_disk_bytes = max_disk_bytes
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path, timeout=30)
        os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.hook = crash_hook or (lambda _: None)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS canonical_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS canonical_sources(path TEXT PRIMARY KEY,snapshot TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS canonical_versions(
                revision INTEGER PRIMARY KEY AUTOINCREMENT, artifact_id TEXT NOT NULL,
                digest TEXT NOT NULL, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS canonical_artifact_revision ON canonical_versions(artifact_id,revision DESC);
            CREATE TABLE IF NOT EXISTS canonical_heads(artifact_id TEXT PRIMARY KEY,revision INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS canonical_members(session_id TEXT PRIMARY KEY,artifact_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS canonical_member_versions(session_id TEXT NOT NULL,revision INTEGER NOT NULL,artifact_id TEXT,PRIMARY KEY(session_id,revision));
            INSERT OR IGNORE INTO canonical_member_versions SELECT m.session_id,h.revision,m.artifact_id FROM canonical_members m JOIN canonical_heads h ON h.artifact_id=m.artifact_id;
        """)
        boundary = canonical_json(
            {
                "workspace_id": str(identity.workspace_id),
                "agent_id": str(identity.agent_id),
                "project_name": identity.project_name,
                "project_id": str(identity.project_id),
            }
        )
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO canonical_meta VALUES('identity',?)", (boundary,)
            )
            self.db.execute(
                "INSERT OR IGNORE INTO canonical_meta VALUES('repository_id',?)",
                (str(uuid4()),),
            )
        if self._meta("identity") != boundary:
            self.db.close()
            raise ValueError(
                "canonical repository belongs to another collector/project"
            )
        self.repository_id = self._meta("repository_id")

    def _meta(self, key: str) -> str:
        return self.db.execute(
            "SELECT value FROM canonical_meta WHERE key=?", (key,)
        ).fetchone()[0]

    def close(self):
        self.db.close()

    def source_snapshots(self) -> dict[str, LivingSourceSnapshot]:
        return {
            row[0]: LivingSourceSnapshot.model_validate_json(row[1])
            for row in self.db.execute("SELECT path,snapshot FROM canonical_sources")
        }

    def commit(
        self,
        *,
        captures: Iterable[CanonicalCapture],
        snapshots: dict[str, LivingSourceSnapshot],
    ) -> int:
        """Keep unchanged graph identities; commit the source fence with revisions."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for capture in captures:
                artifact = capture.artifact
                encoded = canonical_json(capture.model_dump(mode="json"))
                digest = hashlib.sha256(encoded.encode()).hexdigest()
                artifact_id = str(artifact.graph.root_session_id)
                prior = self.db.execute(
                    "SELECT v.digest FROM canonical_heads h JOIN canonical_versions v ON v.revision=h.revision WHERE h.artifact_id=?",
                    (artifact_id,),
                ).fetchone()
                if prior and prior[0] == digest:
                    continue
                revision = self.db.execute(
                    "INSERT INTO canonical_versions(artifact_id,digest,body) VALUES(?,?,?)",
                    (artifact_id, digest, encoded),
                ).lastrowid
                self.db.execute(
                    "INSERT INTO canonical_heads VALUES(?,?) ON CONFLICT(artifact_id) DO UPDATE SET revision=excluded.revision",
                    (artifact_id, revision),
                )
                previous_members = {
                    row[0]
                    for row in self.db.execute(
                        "SELECT session_id FROM canonical_members WHERE artifact_id=?",
                        (artifact_id,),
                    )
                }
                current_members = {
                    str(session.session_id) for session in artifact.sessions
                }
                for removed in previous_members - current_members:
                    self.db.execute(
                        "INSERT OR REPLACE INTO canonical_member_versions VALUES(?,?,NULL)",
                        (removed, revision),
                    )
                self.db.execute(
                    "DELETE FROM canonical_members WHERE artifact_id=?", (artifact_id,)
                )
                for session in artifact.sessions:
                    self.db.execute(
                        "INSERT OR REPLACE INTO canonical_member_versions VALUES(?,?,?)",
                        (str(session.session_id), revision, artifact_id),
                    )
                    self.db.execute(
                        "INSERT INTO canonical_members VALUES(?,?) ON CONFLICT(session_id) DO UPDATE SET artifact_id=excluded.artifact_id",
                        (str(session.session_id), artifact_id),
                    )
            for path, snapshot in snapshots.items():
                self.db.execute(
                    "INSERT INTO canonical_sources VALUES(?,?) ON CONFLICT(path) DO UPDATE SET snapshot=excluded.snapshot",
                    (path, snapshot.model_dump_json()),
                )
            allocated = (
                self.db.execute("PRAGMA page_count").fetchone()[0]
                * self.db.execute("PRAGMA page_size").fetchone()[0]
            )
            if allocated > self.max_disk_bytes:
                raise ValueError(
                    "canonical storage budget exhausted; source checkpoint retained"
                )
            self.hook("before_canonical_commit")
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        self.hook("after_canonical_commit")
        return self.pin_snapshot()

    def changes_after(self, cursor: str | None) -> UploadCapturePage | None:
        """One bounded compatibility graph per replayable page; never skip revisions."""
        from coding_trajectory.control_plane.upload_capture import (
            CanonicalCapture,
            UploadCapturePage,
        )

        after = 0
        if cursor:
            try:
                repository_id, revision = cursor.rsplit(":", 1)
                after = int(revision)
            except (ValueError, TypeError):
                raise ValueError("invalid canonical change cursor") from None
            if (
                repository_id != self.repository_id
                or after < 0
                or after > self.pin_snapshot()
            ):
                raise ValueError(
                    "canonical cursor requires explicit repository reconciliation"
                )
        row = self.db.execute(
            "SELECT revision,body FROM canonical_versions WHERE revision>? ORDER BY revision LIMIT 1",
            (after,),
        ).fetchone()
        if row is None:
            return None
        return UploadCapturePage(
            repository_id=self.repository_id,
            cursor=f"{self.repository_id}:{row[0]}",
            captures=(CanonicalCapture.model_validate_json(row[1]),),
        )

    def pin_snapshot(self) -> int:
        return self.db.execute(
            "SELECT COALESCE(max(revision),0) FROM canonical_versions"
        ).fetchone()[0]

    def graph(
        self, session_id: str, *, revision: int | None = None
    ) -> ChronicleGraphArtifact:
        # Historical membership can change with topology; scope candidate versions
        # by their indexed artifact identity, then inspect only those graph members.
        owner = self.db.execute(
            "SELECT artifact_id FROM canonical_member_versions WHERE session_id=? AND revision<=? ORDER BY revision DESC LIMIT 1",
            (session_id, self.pin_snapshot() if revision is None else revision),
        ).fetchone()
        if owner is None:
            raise ResourceNotFoundError(
                "session is not present in the canonical repository"
            )
        row = self.db.execute(
            "SELECT body FROM canonical_versions WHERE artifact_id=? AND revision<=? ORDER BY revision DESC LIMIT 1",
            (owner[0], self.pin_snapshot() if revision is None else revision),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError(
                "session is not present at this canonical revision"
            )
        return ChronicleGraphArtifact.model_validate(json.loads(row[0])["artifact"])

    def store_for(
        self, method: str, params: dict[str, Any]
    ) -> tuple[DocumentStore, str]:
        scope = params.get("scope") or params
        session_id = scope.get("session_id") or scope.get("root_session_id")
        if not session_id:
            raise ValueError("canonical repository reads require a session scope")
        graph = self.graph(str(session_id))
        return DocumentStore.from_session_graphs(
            [graph.to_session_graph()]
        ), "durable canonical revision"

    def metadata(self) -> dict[str, Any]:
        return {
            "source": "local",
            "freshness": "committed",
            "snapshot_sequence": self.pin_snapshot(),
        }


class CanonicalReadRepository:
    """Read-only attachment; opening a query never constructs or migrates state."""

    def __init__(self, path: Path):
        self.db = sqlite3.connect(
            path.expanduser().resolve().as_uri() + "?mode=ro", uri=True
        )
        self.revision = self.db.execute(
            "SELECT COALESCE(max(revision),0) FROM canonical_versions"
        ).fetchone()[0]

    def close(self):
        self.db.close()

    def store_for(
        self, method: str, params: dict[str, Any]
    ) -> tuple[DocumentStore, str]:
        scope = params.get("scope") or params
        session_id = scope.get("session_id") or scope.get("root_session_id")
        if not session_id:
            raise ResourceNotFoundError("canonical read requires a session scope")
        row = self.db.execute(
            "SELECT v.body FROM canonical_versions v WHERE v.artifact_id=(SELECT artifact_id FROM canonical_member_versions WHERE session_id=? AND revision<=? ORDER BY revision DESC LIMIT 1) AND v.revision<=? ORDER BY v.revision DESC LIMIT 1",
            (str(session_id), self.revision, self.revision),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError("session absent from this canonical revision")
        artifact = ChronicleGraphArtifact.model_validate(json.loads(row[0])["artifact"])
        if not any(
            str(session.session_id) == str(session_id) for session in artifact.sessions
        ):
            raise ResourceNotFoundError("session absent from this canonical revision")
        return DocumentStore.from_session_graphs(
            [artifact.to_session_graph()]
        ), "durable canonical revision"

    def metadata(self) -> dict[str, Any]:
        return {
            "source": "local",
            "freshness": "committed",
            "snapshot_sequence": self.revision,
        }


def configured_canonical_path(profile_name: str | None = None) -> Path | None:
    from coding_trajectory.control_plane.connections import load_profile, profile_path

    explicit = os.environ.get("CT_CANONICAL_REPOSITORY")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ValueError("configured canonical repository is unavailable")
        return path
    selected = profile_name or os.environ.get("CT_CREDENTIAL_PROFILE", "default")
    if not profile_path(selected).exists():
        return None
    profile = load_profile(selected)
    if profile.state_path:
        path = Path(profile.state_path).expanduser().with_suffix(".canonical.sqlite3")
    elif profile.workspace_id and profile.agent_id and profile.project_name:
        from coding_trajectory.control_plane.upload_state import (
            default_upload_state_path,
        )

        path = default_upload_state_path(
            workspace_id=profile.workspace_id,
            agent_id=profile.agent_id,
            project_name=profile.project_name,
        ).with_suffix(".canonical.sqlite3")
    else:
        return None
    return path if path.is_file() else None
