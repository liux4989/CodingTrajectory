"""Host-local upload state, policy, locks, and atomic capture/ACK bookkeeping."""

from __future__ import annotations

import fcntl
import hashlib
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from coding_trajectory.ingestion.common import canonical_json

ACTIVE_STATES = (
    "prepared",
    "ready",
    "uploading",
    "awaiting_commit",
    "retry_wait",
    "blocked",
)


class UploadStateError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code.replace("_", " "))
        self.code = code


class UploadPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["manual", "automatic"] = "manual"
    paused: bool = False
    batch_seconds: int = Field(default=60, ge=1, le=86400)
    batch_bytes: int = Field(default=256 * 1024, ge=1024, le=8 * 1024 * 1024)
    batch_resources: int = Field(default=200, ge=1, le=100000)
    max_pending_bytes: int = Field(default=256 * 1024 * 1024, ge=1024)
    max_disk_bytes: int = Field(default=2 * 1024 * 1024 * 1024, ge=1024)


def default_upload_state_path(
    *, workspace_id: object, agent_id: object, project_name: str
) -> Path:
    """A default delivery database cannot silently mix two projects/collectors."""
    key = hashlib.sha256(
        canonical_json([str(workspace_id), str(agent_id), project_name]).encode()
    ).hexdigest()[:24]
    return Path.home() / ".coding-trajectory/control-plane" / f"upload-{key}.sqlite3"


def resource_fingerprints(body: dict[str, Any]) -> dict[str, str]:
    graph = body["artifact"]
    resources: dict[str, Any] = {"graph": graph["graph"]}
    for session in graph["sessions"]:
        resources[session["session_id"]] = {
            key: value for key, value in session.items() if key != "turns"
        }
        for turn in session.get("turns", []):
            resources[turn["turn_id"]] = {
                key: value for key, value in turn.items() if key != "items"
            }
            resources.update({item["item_id"]: item for item in turn.get("items", [])})
    for edge in graph.get("edges", []):
        key = canonical_json(
            [
                edge.get("kind"),
                edge.get("source_session_id"),
                edge.get("target_session_id"),
                edge.get("origin"),
            ]
        )
        resources[f"edge:{key}"] = edge
    return {
        key: hashlib.sha256(canonical_json(value).encode()).hexdigest()
        for key, value in resources.items()
    }


def completed_resources(body: dict[str, Any]) -> set[str]:
    # A quiet/not_living session is not completion evidence. Require the adapter's
    # explicit terminal turn status AND completion time from a complete fence.
    return {
        turn["turn_id"]
        for session in body["artifact"]["sessions"]
        for turn in session.get("turns", [])
        if turn.get("completed_at") is not None
        and turn.get("status") in {"completed", "interrupted"}
    }


class UploadState:
    def __init__(
        self, path: Path, identity, *, crash_hook: Callable[[str], None] | None = None
    ):
        self.path = path.expanduser().resolve()
        self.identity = identity
        self.hook = crash_hook or (lambda _: None)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path, timeout=30)
        os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          PRAGMA synchronous=FULL;
          PRAGMA foreign_keys=ON;
          CREATE TABLE IF NOT EXISTS sync_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS sync_sources (path TEXT PRIMARY KEY, snapshot TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS sync_batches (
            id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, cursor TEXT NOT NULL,
            body TEXT NOT NULL, state TEXT NOT NULL, plan TEXT, receipt TEXT,
            created_at TEXT NOT NULL, acknowledged_at TEXT);
          CREATE TABLE IF NOT EXISTS sync_pages (
            ordinal INTEGER PRIMARY KEY AUTOINCREMENT, repository_id TEXT NOT NULL,
            cursor TEXT NOT NULL UNIQUE, digest TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS sync_page_batches (
            page INTEGER NOT NULL REFERENCES sync_pages(ordinal), batch_id TEXT NOT NULL,
            PRIMARY KEY(page,batch_id));
          CREATE TABLE IF NOT EXISTS sync_canonical_retentions (
            batch_id TEXT PRIMARY KEY, repository_id TEXT NOT NULL,
            token TEXT NOT NULL, root_digest TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS sync_source_bindings (
            vendor TEXT NOT NULL, native_session_id TEXT NOT NULL, generation TEXT NOT NULL,
            source_id TEXT NOT NULL, source_epoch INTEGER NOT NULL,
            PRIMARY KEY(vendor,native_session_id));
          CREATE TABLE IF NOT EXISTS sync_batch_sources (
            batch_id TEXT NOT NULL, identity TEXT NOT NULL, PRIMARY KEY(batch_id,identity));
          CREATE INDEX IF NOT EXISTS sync_lineages ON sync_batch_sources(identity,batch_id);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(sync_batches)")}
        with self.db:
            for name, declaration in (
                ("phase", "TEXT NOT NULL DEFAULT 'ready'"),
                ("attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("error_code", "TEXT"),
                ("next_retry_at", "REAL"),
                ("changed_resources", "INTEGER NOT NULL DEFAULT 0"),
                ("completion", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    self.db.execute(
                        f"ALTER TABLE sync_batches ADD COLUMN {name} {declaration}"
                    )
            # Candidate migration retains the exact attempted request. No ACK or
            # automatic mode can be manufactured by opening an older database.
            self.db.execute(
                "UPDATE sync_batches SET state='awaiting_commit',phase='awaiting_commit' WHERE state='sending' AND json_extract(plan,'$.publication') IS NOT NULL"
            )
            self.db.execute(
                "UPDATE sync_batches SET state='uploading',phase='uploading' WHERE state='sending'"
            )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS sync_delivery_queue ON sync_batches(state,next_retry_at,created_at)"
            )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS sync_artifact_batches ON sync_batches(artifact_id,created_at)"
            )
        boundary = canonical_json(
            {
                "workspace_id": str(identity.workspace_id),
                "agent_id": str(identity.agent_id),
                "project_name": identity.project_name,
            }
        )
        prior = self.meta("identity")
        if prior and prior != boundary:
            self.db.close()
            raise UploadStateError("upload_identity_conflict")
        with self.db:
            self.set_meta("identity", boundary)
            if self.meta("policy") is None:
                self.set_meta("policy", UploadPolicy().model_dump_json())
            self.set_meta("schema_version", "2")

    def close(self):
        self.db.close()

    def meta(self, key: str) -> str | None:
        row = self.db.execute(
            "SELECT value FROM sync_meta WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str | None):
        if value is None:
            self.db.execute("DELETE FROM sync_meta WHERE key=?", (key,))
        else:
            self.db.execute(
                "INSERT OR REPLACE INTO sync_meta VALUES(?,?)", (key, value)
            )

    @property
    def policy(self) -> UploadPolicy:
        return UploadPolicy.model_validate_json(self.meta("policy") or "{}")

    def configure(self, **updates: Any) -> dict[str, Any]:
        policy = UploadPolicy.model_validate({**self.policy.model_dump(), **updates})
        with self.db:
            self.set_meta("policy", policy.model_dump_json())
        return self.status()

    @contextmanager
    def owner_lock(self, purpose: str) -> Iterator[None]:
        lock = self.path.with_suffix(f".{purpose}.lock")
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise UploadStateError(f"{purpose}_owner_busy") from None
            yield
        finally:
            os.close(descriptor)

    def status(self) -> dict[str, Any]:
        counts = {
            row[0]: row[1]
            for row in self.db.execute(
                "SELECT state,count(*) FROM sync_batches GROUP BY state"
            )
        }
        pending = self.db.execute(
            "SELECT COALESCE(sum(length(CAST(body AS BLOB))+coalesce(length(CAST(plan AS BLOB)),0)),0),min(created_at),COALESCE(sum(changed_resources),0),COALESCE(max(completion),0) FROM sync_batches WHERE state NOT IN ('acknowledged','superseded')"
        ).fetchone()
        blocked = [
            dict(row)
            for row in self.db.execute(
                "SELECT id AS batch_id,error_code,phase,attempts FROM sync_batches WHERE state='blocked' ORDER BY created_at LIMIT 20"
            )
        ]
        retry = self.db.execute(
            "SELECT min(next_retry_at) FROM sync_batches WHERE state='retry_wait'"
        ).fetchone()[0]
        return {
            "batches": counts,
            "pending_bytes": pending[0],
            "oldest_pending_at": pending[1],
            "oldest_pending_age_seconds": (
                max(
                    0.0,
                    (
                        datetime.now(UTC) - datetime.fromisoformat(pending[1])
                    ).total_seconds(),
                )
                if pending[1]
                else None
            ),
            "pending_resources": pending[2],
            "completion_pending": bool(pending[3]),
            "consumed_cursor": self.meta("consumed_cursor"),
            "acknowledged_cursor": self.meta("acknowledged_cursor"),
            "repository_id": self.meta("repository_id"),
            "policy": self.policy.model_dump(),
            "last_successful_publish_at": self.meta("last_successful_publish_at"),
            "preparation_error": self.meta("preparation_error"),
            "blocked": blocked,
            "next_retry_at": retry,
            "authority_error": self.meta("authority_error"),
        }

    def should_flush(self, *, now: datetime | None = None) -> bool:
        status = self.status()
        if (
            self.policy.paused
            or self.policy.mode != "automatic"
            or not status["oldest_pending_at"]
        ):
            return False
        age = (
            (now or datetime.now(UTC))
            - datetime.fromisoformat(status["oldest_pending_at"])
        ).total_seconds()
        return bool(
            status["completion_pending"]
            or status["pending_resources"] >= self.policy.batch_resources
            or status["pending_bytes"] >= self.policy.batch_bytes
            or age >= self.policy.batch_seconds
        )

    def check_backpressure(self, incoming: int = 0):
        pending = self.status()["pending_bytes"]
        disk = sum(
            path.stat().st_size
            for path in (self.path, Path(f"{self.path}-wal"))
            if path.exists()
        )
        if (
            pending + incoming > self.policy.max_pending_bytes
            or disk + incoming > self.policy.max_disk_bytes
        ):
            raise UploadStateError("upload_backpressure")

    def advance_acknowledged_cursor(self):
        first = self.db.execute(
            "SELECT min(p.ordinal) FROM sync_pages p JOIN sync_page_batches m ON m.page=p.ordinal JOIN sync_batches b ON b.id=m.batch_id WHERE b.state!='acknowledged'"
        ).fetchone()[0]
        row = self.db.execute(
            "SELECT cursor FROM sync_pages WHERE (? IS NULL OR ordinal<?) ORDER BY ordinal DESC LIMIT 1",
            (first, first),
        ).fetchone()
        if row:
            self.set_meta("acknowledged_cursor", row[0])

    def acknowledge(self, row, receipt):
        with self.db:
            stamp = datetime.now(UTC).isoformat()
            self.db.execute(
                "UPDATE sync_batches SET state='acknowledged',phase='acknowledged',receipt=?,acknowledged_at=?,error_code=NULL,next_retry_at=NULL WHERE id=?",
                (receipt.model_dump_json(), stamp, row["id"]),
            )
            self.set_meta("last_successful_publish_at", stamp)
            self.advance_acknowledged_cursor()
            # Databases from the paused candidate have no page table yet.
            if (
                not self.db.execute("SELECT 1 FROM sync_pages LIMIT 1").fetchone()
                and not self.db.execute(
                    "SELECT 1 FROM sync_batches WHERE state NOT IN ('acknowledged','superseded') AND created_at<=? LIMIT 1",
                    (row["created_at"],),
                ).fetchone()
            ):
                self.set_meta("acknowledged_cursor", row["cursor"])
        self.hook("after_ack_commit")
        # Acknowledged bodies still participate in change comparison and replay.
        # Release their pins only when the corresponding outbox rows are retired.
