"""Owner-only SQLite persistence for Monitor product state.

Stores watch configuration, evaluations, and findings as JSON records plus
indexed identity columns. Records are reference-only: canonical IDs, measured
values, thresholds, fingerprints, and lifecycle state — never transcript text
or event bodies. This is rebuildable product state, not canonical history.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import UUID

from loop_plugin.monitor.models import Evaluation, Finding, Watch

SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    watch_id TEXT PRIMARY KEY,
    record TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evaluations (
    evaluation_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    watch_id TEXT NOT NULL,
    config_revision INTEGER NOT NULL,
    trigger_kind TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    state TEXT NOT NULL,
    result TEXT,
    fingerprint TEXT NOT NULL,
    superseded_by TEXT,
    finding_id TEXT,
    completed_at TEXT NOT NULL,
    record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS evaluations_watch
    ON evaluations (watch_id, trigger_kind, state, result);
CREATE INDEX IF NOT EXISTS evaluations_turn_latest
    ON evaluations (watch_id, config_revision, trigger_kind, session_id, turn_id, superseded_by);
CREATE TABLE IF NOT EXISTS findings (
    finding_id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    watch_id TEXT NOT NULL,
    evaluation_id TEXT NOT NULL,
    status TEXT NOT NULL,
    severity TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS findings_status ON findings (status, severity);
"""


class MonitorStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript(SCHEMA)
        path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    # --- watches ---------------------------------------------------------

    def save_watch(self, watch: Watch) -> None:
        record = watch.model_dump_json()
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO watches VALUES (?, ?, ?) "
                "ON CONFLICT(watch_id) DO UPDATE SET record=excluded.record, "
                "updated_at=excluded.updated_at",
                (str(watch.watch_id), record, watch.updated_at.isoformat()),
            )

    def get_watch(self, watch_id: UUID) -> Watch | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT record FROM watches WHERE watch_id = ?", (str(watch_id),)
            ).fetchone()
        return Watch.model_validate_json(row[0]) if row else None

    def list_watches(self) -> list[Watch]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT record FROM watches ORDER BY updated_at DESC"
            ).fetchall()
        return [Watch.model_validate_json(row[0]) for row in rows]

    # --- evaluations -----------------------------------------------------

    def latest_evaluation(
        self,
        *,
        watch_id: UUID,
        config_revision: int,
        trigger: str,
        session_id: str,
        turn_id: str,
    ) -> Evaluation | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT record FROM evaluations WHERE watch_id = ? "
                "AND config_revision = ? AND trigger_kind = ? AND session_id = ? "
                "AND turn_id = ? AND superseded_by IS NULL",
                (str(watch_id), config_revision, trigger, session_id, turn_id),
            ).fetchone()
        return Evaluation.model_validate_json(row[0]) if row else None

    def insert_evaluation(self, evaluation: Evaluation) -> bool:
        """Insert idempotently. Returns False when the key already exists."""
        with closing(self._connect()) as db, db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO evaluations VALUES (?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?)",
                (
                    str(evaluation.evaluation_id),
                    evaluation.idempotency_key,
                    str(evaluation.watch_id),
                    evaluation.config_revision,
                    evaluation.trigger,
                    evaluation.reference.session_id,
                    evaluation.reference.turn_id,
                    evaluation.state,
                    evaluation.result,
                    evaluation.evidence_fingerprint,
                    None,
                    None,
                    evaluation.completed_at.isoformat(),
                    evaluation.model_dump_json(),
                ),
            )
            return cursor.rowcount == 1

    def mark_superseded(self, evaluation_id: UUID, superseded_by: UUID) -> None:
        with closing(self._connect()) as db, db:
            row = db.execute(
                "SELECT record FROM evaluations WHERE evaluation_id = ?",
                (str(evaluation_id),),
            ).fetchone()
            if not row:
                return
            record = Evaluation.model_validate_json(row[0])
            updated = record.model_copy(update={"superseded_by": superseded_by})
            db.execute(
                "UPDATE evaluations SET superseded_by = ?, record = ? "
                "WHERE evaluation_id = ?",
                (str(superseded_by), updated.model_dump_json(), str(evaluation_id)),
            )

    def attach_finding(self, evaluation_id: UUID, finding_id: UUID) -> None:
        with closing(self._connect()) as db, db:
            row = db.execute(
                "SELECT record FROM evaluations WHERE evaluation_id = ?",
                (str(evaluation_id),),
            ).fetchone()
            if not row:
                return
            record = Evaluation.model_validate_json(row[0])
            updated = record.model_copy(update={"finding_id": finding_id})
            db.execute(
                "UPDATE evaluations SET finding_id = ?, record = ? "
                "WHERE evaluation_id = ?",
                (str(finding_id), updated.model_dump_json(), str(evaluation_id)),
            )

    def list_evaluations(
        self,
        *,
        watch_id: UUID | None = None,
        trigger: str | None = None,
        state: str | None = None,
        result: str | None = None,
        include_superseded: bool = False,
        limit: int = 100,
    ) -> list[Evaluation]:
        conditions = [] if include_superseded else ["superseded_by IS NULL"]
        bindings: list[object] = []
        if watch_id is not None:
            conditions.append("watch_id = ?")
            bindings.append(str(watch_id))
        if trigger is not None:
            conditions.append("trigger_kind = ?")
            bindings.append(trigger)
        if state is not None:
            conditions.append("state = ?")
            bindings.append(state)
        if result is not None:
            conditions.append("result = ?")
            bindings.append(result)
        bindings.append(limit)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT record FROM evaluations {where} "
                "ORDER BY completed_at DESC, evaluation_id DESC LIMIT ?",
                bindings,
            ).fetchall()
        return [Evaluation.model_validate_json(row[0]) for row in rows]

    # --- findings ----------------------------------------------------------

    def finding_for_dedupe(self, dedupe_key: str) -> Finding | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT record FROM findings WHERE dedupe_key = ?", (dedupe_key,)
            ).fetchone()
        return Finding.model_validate_json(row[0]) if row else None

    def insert_finding(self, finding: Finding) -> bool:
        with closing(self._connect()) as db, db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO findings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(finding.finding_id),
                    finding.dedupe_key,
                    str(finding.watch_id),
                    str(finding.evaluation_id),
                    finding.status,
                    finding.severity,
                    finding.reference.session_id,
                    finding.reference.turn_id,
                    finding.updated_at.isoformat(),
                    finding.model_dump_json(),
                ),
            )
            return cursor.rowcount == 1

    def get_finding(self, finding_id: UUID) -> Finding | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT record FROM findings WHERE finding_id = ?", (str(finding_id),)
            ).fetchone()
        return Finding.model_validate_json(row[0]) if row else None

    def save_finding(self, finding: Finding) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                "UPDATE findings SET status = ?, updated_at = ?, record = ? "
                "WHERE finding_id = ?",
                (
                    finding.status,
                    finding.updated_at.isoformat(),
                    finding.model_dump_json(),
                    str(finding.finding_id),
                ),
            )

    def list_findings(
        self,
        *,
        status: str | None = None,
        watch_id: UUID | None = None,
        limit: int = 200,
    ) -> list[Finding]:
        conditions: list[str] = []
        bindings: list[object] = []
        if status is not None:
            conditions.append("status = ?")
            bindings.append(status)
        if watch_id is not None:
            conditions.append("watch_id = ?")
            bindings.append(str(watch_id))
        bindings.append(limit)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT record FROM findings {where} "
                "ORDER BY updated_at DESC, finding_id DESC LIMIT ?",
                bindings,
            ).fetchall()
        return [Finding.model_validate_json(row[0]) for row in rows]


def export_records(path: Path) -> dict[str, list[dict]]:
    """Read all stored records for qualification assertions."""
    with closing(sqlite3.connect(path)) as db:
        return {
            table: [
                json.loads(row[0]) for row in db.execute(f"SELECT record FROM {table}")
            ]
            for table in ("watches", "evaluations", "findings")
        }
