"""Durable canonical capture and asynchronous, duplicate-safe publication.

Canonical revisions and their source fences commit in CanonicalRepository first.
The outbox consumes its replayable pages in a separate atomic transaction. No
network call occurs during preparation, and no background process starts here.
"""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from coding_trajectory.control_plane.canonical_repository import CanonicalRepository
from coding_trajectory.control_plane.chronicle import (
    ChronicleGraphArtifact,
    build_chronicle_graph_artifact,
)
from coding_trajectory.control_plane.collector import (
    _SNAPSHOT_STATE_VERSION,
    CloudflareCollectorRemote,
    CollectorRemoteError,
    LocalCollector,
    _body_free_artifact,
    _expand_scoped_graph_candidates,
)
from coding_trajectory.control_plane.collector_protocol import (
    ArtifactPublicationRequest,
    ChronicleArtifactPublication,
    CollectorRecoveryRequest,
    ObservationReceipt,
    ObservationRequest,
    ProjectRegistrationRequest,
    SourceRegistrationRequest,
    SourceVectorEntry,
)
from coding_trajectory.control_plane.publication_lock import publication_lock
from coding_trajectory.control_plane.upload_capture import (
    CanonicalCapture,
    CaptureSource,
    UploadCapturePage,
)
from coding_trajectory.control_plane.upload_state import (
    UploadState,
    UploadStateError,
    completed_resources,
    resource_fingerprints,
)
from coding_trajectory.discovery import discover_source_candidates
from coding_trajectory.ingestion.common import canonical_json
from coding_trajectory.ingestion.graph import assemble_project_session_graphs
from coding_trajectory.living_sources import inventory_source_changes

__all__ = ["CanonicalCapture", "CaptureSource", "UploadCapturePage", "UploadService"]


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _source_identity(source: dict[str, Any]) -> str:
    return canonical_json([source["vendor"], source["native_session_id"]])


class UploadService(UploadState):
    def _batch_body(self, row) -> dict[str, Any]:
        stored = json.loads(row["body"])
        reference = stored.get("canonical_reference")
        if reference is None:
            return stored
        repository = CanonicalRepository(
            self.path.with_suffix(".canonical.sqlite3"), self.identity
        )
        try:
            if repository.repository_id != reference["repository_id"]:
                raise UploadStateError("canonical_reconciliation_required")
            capture = repository.capture_for_reference(
                revision=reference["revision"],
                root_digest=reference["root_sha256"],
            )
        finally:
            repository.close()
        return {
            "artifact": _body_free_artifact(capture.artifact).wire_payload(),
            "sources": [source.model_dump(mode="json") for source in capture.sources],
        }

    def prepare_capture(self, page: UploadCapturePage) -> dict[str, Any]:
        """Persist one canonical page and its cursor atomically, without network."""
        with self.owner_lock("prepare"):
            return self._capture_page(page)

    def _capture_page(self, page: UploadCapturePage) -> dict[str, Any]:
        current_repository = self.meta("repository_id")
        if current_repository and current_repository != page.repository_id:
            raise UploadStateError("canonical_reconciliation_required")
        if page.references:
            path = self.path.with_suffix(".canonical.sqlite3")
            if not path.exists():
                raise UploadStateError("canonical_reference_unavailable")
            repository = CanonicalRepository(path, self.identity)
            try:
                if repository.repository_id != page.repository_id:
                    raise UploadStateError("canonical_reconciliation_required")
                for reference, capture in zip(
                    page.references, page.captures, strict=True
                ):
                    offer = repository.db.execute(
                        "SELECT revision,root_digest FROM canonical_retentions WHERE token=?",
                        (reference.retention_token,),
                    ).fetchone()
                    if offer is None or tuple(offer) != (
                        reference.revision,
                        reference.root_sha256,
                    ):
                        raise UploadStateError("canonical_reference_unavailable")
                    if (
                        repository.capture_for_reference(
                            revision=reference.revision,
                            root_digest=reference.root_sha256,
                        )
                        != capture
                    ):
                        raise UploadStateError("canonical_reference_content_conflict")
            finally:
                repository.close()
        # Empty additive fields must not change the identity of legacy retries.
        page_payload = page.model_dump(mode="json")
        if not page.references:
            page_payload.pop("references")
        page_digest = _digest(page_payload)
        prior_page = self.db.execute(
            "SELECT digest,ordinal FROM sync_pages WHERE cursor=?", (page.cursor,)
        ).fetchone()
        if prior_page:
            if prior_page[0] != page_digest:
                raise UploadStateError("canonical_cursor_content_conflict")
            consumed = self.db.execute(
                "SELECT ordinal FROM sync_pages WHERE cursor=?",
                (self.meta("consumed_cursor"),),
            ).fetchone()
            if consumed is None or prior_page[1] > consumed[0]:
                with self.db:
                    self.set_meta("repository_id", page.repository_id)
                    self.set_meta("consumed_cursor", page.cursor)
            self._confirm_retentions(page)
            return {**self.status(), "prepared": 0}
        bodies = []
        for capture in page.captures:
            # The canonical repository can retain bounded local previews; the
            # existing remote publication policy still strips their bodies.
            bodies.append(
                {
                    "artifact": _body_free_artifact(capture.artifact).wire_payload(),
                    "sources": [
                        source.model_dump(mode="json") for source in capture.sources
                    ],
                }
            )
        self.check_backpressure(
            sum(len(canonical_json(body).encode()) for body in bodies)
        )
        self.hook("before_prepare_commit")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            ordinal = self.db.execute(
                "INSERT INTO sync_pages(repository_id,cursor,digest) VALUES(?,?,?)",
                (page.repository_id, page.cursor, page_digest),
            ).lastrowid
            retained: list[str] = []
            for index, body in enumerate(bodies):
                artifact_id = body["artifact"]["graph"]["root_session_id"]
                prior = self.db.execute(
                    "SELECT * FROM sync_batches WHERE artifact_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                    (artifact_id,),
                ).fetchone()
                if prior and _digest(self._batch_body(prior)) == _digest(body):
                    self.db.execute(
                        "INSERT INTO sync_page_batches VALUES(?,?)",
                        (ordinal, prior["id"]),
                    )
                    continue
                prior_body = self._batch_body(prior) if prior else None
                fingerprints = resource_fingerprints(body)
                old = resource_fingerprints(prior_body) if prior_body else {}
                changed = sum(
                    old.get(key) != fingerprints.get(key)
                    for key in old.keys() | fingerprints.keys()
                )
                completion = bool(
                    completed_resources(body)
                    - (completed_resources(prior_body) if prior_body else set())
                )
                batch_id = str(uuid4())
                stored_body = (
                    canonical_json(
                        {
                            "canonical_reference": page.references[index].model_dump(
                                mode="json"
                            )
                        }
                    )
                    if page.references
                    else canonical_json(body)
                )
                self.db.execute(
                    "INSERT INTO sync_batches(id,artifact_id,cursor,body,state,created_at,phase,changed_resources,completion) VALUES(?,?,?,?, 'prepared',?,'ready',?,?)",
                    (
                        batch_id,
                        artifact_id,
                        page.cursor,
                        stored_body,
                        datetime.now(UTC).isoformat(),
                        changed,
                        int(completion),
                    ),
                )
                if page.references:
                    reference = page.references[index]
                    self.db.execute(
                        "INSERT INTO sync_canonical_retentions VALUES(?,?,?,?)",
                        (
                            batch_id,
                            reference.repository_id,
                            reference.retention_token,
                            reference.root_sha256,
                        ),
                    )
                    retained.append(reference.retention_token)
                self.db.execute(
                    "INSERT INTO sync_page_batches VALUES(?,?)", (ordinal, batch_id)
                )
                for source in body["sources"]:
                    self.db.execute(
                        "INSERT INTO sync_batch_sources VALUES(?,?)",
                        (batch_id, _source_identity(source)),
                    )
                # Only unattempted complete states with all earlier resource IDs
                # and the same source generations may coalesce. Corrected or
                # removed histories remain distinct revisions when scope shrinks.
                if (
                    prior
                    and prior["state"] == "prepared"
                    and set(old) <= set(fingerprints)
                ):
                    generations = lambda value: {
                        _source_identity(s): s.get("source_generation", "legacy-v1")
                        for s in value["sources"]
                    }
                    if generations(prior_body) == generations(body):
                        self._redirect_pages(prior["id"], batch_id)
                        self.db.execute(
                            "UPDATE sync_batches SET state='superseded',error_code='coalesced_before_attempt' WHERE id=?",
                            (prior["id"],),
                        )
            self.set_meta("repository_id", page.repository_id)
            self.set_meta("consumed_cursor", page.cursor)
            self.set_meta("preparation_error", None)
            self.advance_acknowledged_cursor()
        self.hook("after_prepare_commit")
        # The durable offer exists before the outbox transaction. Confirming it
        # afterwards never creates a window where a referenced DAG is collectible.
        if retained:
            self._confirm_retentions(page, tokens=retained)
        return {**self.status(), "prepared": len(bodies)}

    def _confirm_retentions(
        self, page: UploadCapturePage, *, tokens: list[str] | None = None
    ):
        selected = tokens or [
            reference.retention_token for reference in page.references
        ]
        if selected:
            repository = CanonicalRepository(
                self.path.with_suffix(".canonical.sqlite3"), self.identity
            )
            try:
                for token in selected:
                    repository.retain(token)
            finally:
                repository.close()

    def _redirect_pages(self, original: str, successor: str):
        self.db.execute(
            "INSERT OR IGNORE INTO sync_page_batches SELECT page,? FROM sync_page_batches WHERE batch_id=?",
            (successor, original),
        )
        self.db.execute("DELETE FROM sync_page_batches WHERE batch_id=?", (original,))

    def prepare(
        self,
        *,
        current_dir: Path,
        agent_vendor: str | None = None,
        since_days: int | None = None,
    ) -> dict[str, Any]:
        """Compatibility ingestion: cheap inventory; fenced full-prefix parsing.

        This adapter does not claim append-only canonical parsing. Repository
        producers can bypass it with prepare_capture without changing delivery.
        """
        with self.owner_lock("prepare"):
            repository = CanonicalRepository(
                self.path.with_suffix(".canonical.sqlite3"),
                self.identity,
                crash_hook=self.hook,
                max_disk_bytes=self.policy.max_disk_bytes,
            )
            try:
                # Explicit v1-candidate migration. Existing batches/plans/receipts
                # survive; the new canonical journal begins at its own identity.
                if self.meta("repository_id") is None and self.meta("consumed_cursor"):
                    with self.db:
                        self.set_meta(
                            "legacy_consumed_cursor", self.meta("consumed_cursor")
                        )
                        self.set_meta("consumed_cursor", None)
                        self.set_meta(
                            "migration", "candidate_v1_to_canonical_journal_v2"
                        )
                prepared = self._drain_repository(repository)
                self.check_backpressure()
                candidates = discover_source_candidates(
                    current_dir=current_dir,
                    global_scope=False,
                    agent_vendor=agent_vendor,
                    since_days=since_days,
                )
                if since_days is not None:
                    candidates = _expand_scoped_graph_candidates(
                        candidates, current_dir=current_dir, agent_vendor=agent_vendor
                    )
                snapshots = repository.source_snapshots()
                inventory = inventory_source_changes(candidates, snapshots)
                if any(
                    change.current.status == "error" for change in inventory.changes
                ):
                    raise UploadStateError("source_inventory_incomplete")
                if not inventory.changes:
                    with self.db:
                        self.set_meta("preparation_error", None)
                    return {
                        **self.status(),
                        "prepared": prepared,
                        "unchanged_sources": inventory.unchanged_count,
                    }
                changed = any(
                    change.needs_rebuild and change.kind != "delete"
                    for change in inventory.changes
                )
                captures: list[CanonicalCapture] = []
                if changed:
                    with LocalCollector(
                        database_path=self.path.with_suffix(".capture.sqlite3"),
                        identity=self.identity,
                    ) as collector:
                        result = collector.collect(
                            current_dir=current_dir,
                            agent_vendor=agent_vendor,
                            since_days=None,
                            candidate_paths={
                                candidate.path.resolve() for candidate in candidates
                            },
                            remote=None,
                            heartbeat=False,
                        )
                        if result.failed:
                            raise UploadStateError("canonical_capture_incomplete")
                        sources = collector.prepared_sources
                        metadata: dict[tuple[str, str], list[int]] = {}
                        eligible_paths = {
                            candidate.path.resolve() for candidate in candidates
                        }
                        for row in collector._connection.execute(
                            "SELECT vendor,native_session_id,committed_offset,path FROM registered_sources ORDER BY segment_id"
                        ):
                            if Path(row[3]).resolve() in eligible_paths:
                                metadata.setdefault((row[0], row[1]), []).append(row[2])
                        sessions = [
                            source.artifact.to_session_graph().sessions[0]
                            for source in sources
                        ]
                        by_session = {
                            source.artifact.sessions[0].session_id: source
                            for source in sources
                        }
                        for graph in assemble_project_session_graphs(
                            self.identity.project_name or "project", sessions
                        ):
                            artifact = _body_free_artifact(
                                build_chronicle_graph_artifact(graph)
                            )
                            descriptions = []
                            for session in graph.sessions:
                                source = by_session[session.session_id]
                                vendor, native = (
                                    session.vendor.value,
                                    str(session.session_id),
                                )
                                descriptions.append(
                                    CaptureSource(
                                        vendor=vendor,
                                        native_session_id=native,
                                        segments=metadata[(vendor, native)],
                                        chronicle_digest=source.artifact.digest(),
                                        observed_at=source.observed_at,
                                        source_generation=f"{_SNAPSHOT_STATE_VERSION}:{source.source_epoch}",
                                    )
                                )
                            captures.append(
                                CanonicalCapture(
                                    artifact=artifact, sources=tuple(descriptions)
                                )
                            )
                snapshots.update(
                    {
                        change.current.path: change.current
                        for change in inventory.changes
                    }
                )
                if inventory_source_changes(candidates, snapshots).changes:
                    raise UploadStateError("source_changed_during_capture")
                self.hook("before_canonical_repository_commit")
                repository.commit(captures=captures, snapshots=snapshots)
                self.hook("after_canonical_repository_commit")
                prepared += self._drain_repository(repository)
                with self.db:
                    self.set_meta("preparation_error", None)
                return {
                    **self.status(),
                    "prepared": prepared,
                    "unchanged_sources": inventory.unchanged_count,
                }
            except (UploadStateError, ValueError, OSError, sqlite3.Error) as error:
                with self.db:
                    self.set_meta(
                        "preparation_error",
                        error.code
                        if isinstance(error, UploadStateError)
                        else "canonical_preparation_failed",
                    )
                raise
            finally:
                repository.close()

    def _drain_repository(self, repository: CanonicalRepository) -> int:
        prepared = 0
        if self.meta("repository_id") not in (None, repository.repository_id):
            raise UploadStateError("canonical_reconciliation_required")
        while True:
            try:
                page = repository.changes_after(self.meta("consumed_cursor"))
            except ValueError:
                raise UploadStateError("canonical_reconciliation_required") from None
            if page is None:
                return prepared
            prepared += self._capture_page(page)["prepared"]

    def reconcile_local(self) -> dict[str, Any]:
        """Explicitly replay a reset repository; pending attempts are preserved."""
        with self.owner_lock("prepare"):
            repository = CanonicalRepository(
                self.path.with_suffix(".canonical.sqlite3"), self.identity
            )
            try:
                with self.db:
                    self.set_meta("repository_id", repository.repository_id)
                    self.set_meta("consumed_cursor", None)
                    self.set_meta("preparation_error", None)
                prepared = self._drain_repository(repository)
                return {**self.status(), "prepared": prepared}
            finally:
                repository.close()

    def resume(self) -> dict[str, Any]:
        """Retry exact blocked work after an operator fixes its reported cause."""
        with self.db:
            self.db.execute(
                "UPDATE sync_batches SET state=phase,error_code=NULL,next_retry_at=NULL WHERE state IN ('blocked','retry_wait')"
            )
        return self.configure(paused=False)

    def _recovery(self, remote, project_id: UUID, **extra):
        return remote.recover(
            CollectorRecoveryRequest(
                workspace_id=self.identity.workspace_id,
                agent_id=self.identity.agent_id,
                project_id=project_id,
                include_upload_state=True,
                **extra,
            )
        )

    def _verify_authority(self, response):
        if (
            response.authority_incarnation is None
            or response.authority_sequence is None
        ):
            raise UploadStateError("upload_protocol_upgrade_required")
        incarnation = str(response.authority_incarnation)
        previous = self.meta("authority_incarnation")
        if previous and (
            previous != incarnation
            or response.authority_sequence < int(self.meta("authority_sequence") or "0")
        ):
            with self.db:
                self.set_meta("authority_error", "remote_state_reset")
            raise UploadStateError("remote_state_reset")
        if self.meta("authority_error") == "remote_state_reset":
            raise UploadStateError("remote_state_reset")
        with self.db:
            self.set_meta("authority_incarnation", incarnation)
            self.set_meta("authority_sequence", str(response.authority_sequence))
            self.set_meta("authority_error", None)
            # A rotated token can safely retry the same frozen request. Ownership
            # and schema conflicts require explicit resume/reconciliation.
            self.db.execute(
                "UPDATE sync_batches SET state=phase,error_code=NULL WHERE state='blocked' AND error_code IN ('authentication_required','capability_required','agent_denied')"
            )

    def publish(
        self,
        remote: CloudflareCollectorRemote,
        *,
        max_batches: int = 16,
        force: bool = True,
    ) -> dict[str, Any]:
        """Drain bounded eligible work. Retry identities and per-source order persist."""
        if not 1 <= max_batches <= 128:
            raise ValueError("max_batches must be between 1 and 128")
        if self.policy.paused:
            return {**self.status(), "published": 0}
        with (
            self.owner_lock("delivery"),
            publication_lock(
                self.identity.workspace_id, self.identity.agent_id, timeout=5
            ),
        ):
            return self._publish_locked(remote, max_batches=max_batches, force=force)

    def _publish_locked(self, remote, *, max_batches: int, force: bool):
        published = 0
        try:
            project_id = (
                UUID(self.meta("project_id"))
                if self.meta("project_id")
                else self.identity.project_id
            )
            if project_id:
                self._verify_authority(self._recovery(remote, project_id))
            else:
                project = remote.register_project(
                    ProjectRegistrationRequest(
                        workspace_id=self.identity.workspace_id,
                        agent_id=self.identity.agent_id,
                        display_name=self.identity.project_name or "project",
                    )
                )
                project_id = project.project_id
                self._verify_authority(self._recovery(remote, project_id))
                with self.db:
                    self.set_meta("project_id", str(project_id))
        except (CollectorRemoteError, UploadStateError) as error:
            with self.db:
                self.set_meta("authority_error", error.code)
            return {**self.status(), "published": 0}
        attempted: list[str] = []
        while len(attempted) < max_batches:
            exclusions = (
                "b.id NOT IN (" + ",".join("?" for _ in attempted) + ")"
                if attempted
                else "1"
            )
            row = self.db.execute(
                f"""SELECT b.* FROM sync_batches b
              WHERE b.state IN ('prepared','ready','uploading','awaiting_commit','retry_wait')
                AND (? OR b.next_retry_at IS NULL OR b.next_retry_at<=?) AND {exclusions}
                AND NOT EXISTS (SELECT 1 FROM sync_batch_sources s JOIN sync_batch_sources other ON other.identity=s.identity
                  JOIN sync_batches earlier ON earlier.id=other.batch_id WHERE s.batch_id=b.id
                  AND earlier.state NOT IN ('acknowledged','superseded')
                  AND (earlier.created_at<b.created_at OR (earlier.created_at=b.created_at AND earlier.rowid<b.rowid)))
              ORDER BY b.created_at,b.rowid LIMIT 1""",
                (int(force), time.time(), *attempted),
            ).fetchone()
            if row is None:
                break
            attempted.append(row["id"])
            with self.db:
                self.db.execute(
                    "UPDATE sync_batches SET state=phase,attempts=attempts+1,next_retry_at=NULL WHERE id=?",
                    (row["id"],),
                )
            try:
                receipt = self._deliver(row, remote, project_id)
                self.acknowledge(row, receipt)
                if receipt.committed_sequence:
                    with self.db:
                        self.set_meta(
                            "authority_sequence",
                            str(
                                max(
                                    receipt.committed_sequence,
                                    int(self.meta("authority_sequence") or "0"),
                                )
                            ),
                        )
                published += 1
            except (
                CollectorRemoteError,
                UploadStateError,
                ValueError,
                OSError,
                sqlite3.Error,
            ) as error:
                self._record_failure(row, error)
                # An ambiguous commit holds the project sequence. Resolve that
                # identity before assigning its sequence to independent work.
                current = self.db.execute(
                    "SELECT phase,state FROM sync_batches WHERE id=?", (row["id"],)
                ).fetchone()
                if (
                    current["phase"] == "awaiting_commit"
                    and current["state"] == "retry_wait"
                ):
                    break
        return {**self.status(), "published": published}

    def _record_failure(self, row, error):
        retryable = isinstance(error, (OSError, sqlite3.OperationalError)) or (
            isinstance(error, CollectorRemoteError)
            and (
                error.status_code is None
                or error.status_code in {408, 429}
                or error.status_code >= 500
            )
        )
        code = (
            error.code
            if isinstance(error, (CollectorRemoteError, UploadStateError))
            else "local_delivery_error"
        )
        attempts = self.db.execute(
            "SELECT attempts FROM sync_batches WHERE id=?", (row["id"],)
        ).fetchone()[0]
        delay = min(300, 2 ** min(attempts, 8)) * random.uniform(0.8, 1.2)
        with self.db:
            self.db.execute(
                "UPDATE sync_batches SET state=?,error_code=?,next_retry_at=? WHERE id=?",
                (
                    "retry_wait" if retryable else "blocked",
                    code,
                    time.time() + delay if retryable else None,
                    row["id"],
                ),
            )

    def _save_plan(
        self, batch_id: str, plan: dict[str, Any], *, phase: str | None = None
    ):
        with self.db:
            self.db.execute(
                "UPDATE sync_batches SET plan=? WHERE id=?",
                (canonical_json(plan), batch_id),
            )
            if phase:
                self.db.execute(
                    "UPDATE sync_batches SET phase=?,state=? WHERE id=?",
                    (phase, phase, batch_id),
                )
        self.hook("after_plan_commit")

    def _deliver(self, row, remote, project_id: UUID) -> ObservationReceipt:
        body = self._batch_body(row)
        artifact = ChronicleGraphArtifact.model_validate(body["artifact"])
        plan = (
            json.loads(row["plan"])
            if row["plan"]
            else {"checkpoints": [], "registrations": {}, "publication": None}
        )
        plan.setdefault("registrations", {})
        if plan["publication"]:
            # Candidate v1 saved full artifact bodies in its plan; preserve the
            # exact wire identity while migrating to reference-only commit plans.
            if any("payload" in value for value in plan["publication"]["artifacts"]):
                plan["publication"] = ArtifactPublicationRequest.model_validate(
                    plan["publication"]
                ).reference_payload()
                self._save_plan(row["id"], plan, phase="awaiting_commit")
        else:
            if not plan["checkpoints"]:
                self._plan_checkpoints(row, body, plan, remote, project_id)
            for checkpoint in plan["checkpoints"]:
                self.hook("before_checkpoint_send")
                receipt = remote.publish_observation(
                    ObservationRequest.model_validate(checkpoint),
                    idempotency_key=f"sync-checkpoint:{row['id']}:{checkpoint['source_id']}",
                )
                self.hook("after_checkpoint_send")
                if receipt.outcome not in {"accepted", "duplicate"}:
                    raise UploadStateError("checkpoint_conflict")
            self._save_plan(row["id"], plan, phase="uploading")
            remote.stage_artifact_payload(
                workspace_id=self.identity.workspace_id,
                agent_id=self.identity.agent_id,
                artifact=artifact,
                content_sha256=artifact.digest(),
            )
            self.hook("after_manifest_stage")
            recovery = self._recovery(remote, project_id)
            self._verify_authority(recovery)
            vector = [
                SourceVectorEntry(
                    **{
                        key: checkpoint[key]
                        for key in (
                            "source_id",
                            "source_epoch",
                            "source_sequence",
                            "content_sha256",
                        )
                    }
                )
                for checkpoint in plan["checkpoints"]
            ]
            request = ArtifactPublicationRequest(
                workspace_id=self.identity.workspace_id,
                agent_id=self.identity.agent_id,
                project_id=project_id,
                publication_sequence=recovery.next_publication_sequence,
                replacement_scope="complete_sources",
                source_vector=vector,
                artifacts=[
                    ChronicleArtifactPublication(
                        artifact_id=artifact.graph.root_session_id,
                        payload=artifact,
                        content_sha256=artifact.digest(),
                        serialized_bytes=len(artifact.canonical_bytes()),
                        source_ids=[value.source_id for value in vector],
                        observed_at=max(
                            source["observed_at"] for source in body["sources"]
                        ),
                    )
                ],
            )
            plan["publication"] = request.reference_payload()
            self._save_plan(row["id"], plan, phase="awaiting_commit")
        self.hook("before_publication_send")
        receipt = remote.commit_artifact_manifest(
            plan["publication"], idempotency_key=f"sync-publication:{row['id']}"
        )
        self.hook("after_publication_send")
        if (
            receipt.outcome not in {"accepted", "duplicate"}
            or receipt.details.get("publication_outcome") != "published"
        ):
            with self.db:
                self.db.execute(
                    "UPDATE sync_batches SET receipt=? WHERE id=?",
                    (receipt.model_dump_json(), row["id"]),
                )
            reason = (
                receipt.details.get("reason")
                or f"publication_{receipt.details.get('publication_outcome', receipt.outcome)}"
            )
            raise UploadStateError(
                reason
                if isinstance(reason, str) and len(reason) < 100
                else "publication_conflict"
            )
        return receipt

    def _plan_checkpoints(self, row, body, plan, remote, project_id):
        checkpoints = []
        for source in body["sources"]:
            key = _source_identity(source)
            generation = source.get("source_generation", "legacy-v1")
            registration = plan["registrations"].get(key)
            if registration is None:
                recovered = remote.recover(
                    CollectorRecoveryRequest(
                        workspace_id=self.identity.workspace_id,
                        agent_id=self.identity.agent_id,
                        project_id=project_id,
                        vendor=source["vendor"],
                        native_session_id=source["native_session_id"],
                    )
                )
                binding = self.db.execute(
                    "SELECT * FROM sync_source_bindings WHERE vendor=? AND native_session_id=?",
                    (source["vendor"], source["native_session_id"]),
                ).fetchone()
                if (
                    binding
                    and recovered.source
                    and binding["generation"] == generation
                    and binding["source_epoch"] != recovered.source.source_epoch
                ):
                    raise UploadStateError("source_epoch_conflict")
                rollover = bool(
                    binding and binding["generation"] != generation and recovered.source
                )
                epoch = (
                    (recovered.source.source_epoch + int(rollover))
                    if recovered.source
                    else 1
                )
                sequence = (
                    0
                    if rollover or not recovered.source
                    else recovered.source.next_source_sequence
                )
                payload = {
                    "kind": "ct.source_checkpoint.v1",
                    "source_checkpoint": {"segments": source["segments"]},
                    "chronicle_digest": source["chronicle_digest"],
                }
                digest = _digest(payload)
                if (
                    recovered.source
                    and not rollover
                    and recovered.source.content_sha256 == digest
                ):
                    sequence -= 1
                request = SourceRegistrationRequest(
                    workspace_id=self.identity.workspace_id,
                    agent_id=self.identity.agent_id,
                    project_id=project_id,
                    vendor=source["vendor"],
                    native_session_id=source["native_session_id"],
                    source_epoch=epoch,
                    rollover=rollover,
                )
                registration = {
                    "request": request.model_dump(mode="json"),
                    "sequence": sequence,
                    "payload": payload,
                    "digest": digest,
                }
                plan["registrations"][key] = registration
                self._save_plan(row["id"], plan, phase="ready")
            registered = remote.register_source(
                SourceRegistrationRequest.model_validate(registration["request"]),
                idempotency_key=f"sync-source:{row['id']}:{source['native_session_id']}",
            )
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO sync_source_bindings VALUES(?,?,?,?,?)",
                    (
                        source["vendor"],
                        source["native_session_id"],
                        generation,
                        str(registered.source_id),
                        registered.source_epoch,
                    ),
                )
            checkpoint = ObservationRequest(
                workspace_id=self.identity.workspace_id,
                agent_id=self.identity.agent_id,
                source_id=registered.source_id,
                source_epoch=registered.source_epoch,
                source_sequence=registration["sequence"],
                event_id=f"checkpoint:{registration['digest']}",
                parser_version="ct-upload-service-v2",
                content_sha256=registration["digest"],
                observed_at=source["observed_at"],
                payload=registration["payload"],
            )
            checkpoints.append(checkpoint.model_dump(mode="json"))
        plan["checkpoints"] = checkpoints
        self._save_plan(row["id"], plan, phase="ready")

    def reconcile_remote(self, remote: CloudflareCollectorRemote) -> dict[str, Any]:
        """Resolve exact receipts, then retain successor work for rejected attempts.

        This is explicit and never publishes. A remote reset requeues current
        local heads; it does not claim to restore pruned historical revisions.
        """
        with (
            self.owner_lock("delivery"),
            publication_lock(
                self.identity.workspace_id, self.identity.agent_id, timeout=5
            ),
        ):
            project = remote.register_project(
                ProjectRegistrationRequest(
                    workspace_id=self.identity.workspace_id,
                    agent_id=self.identity.agent_id,
                    display_name=self.identity.project_name or "project",
                )
            )
            if (
                self.identity.project_id
                and project.project_id != self.identity.project_id
            ):
                raise UploadStateError("project_rebind_required")
            recovery = self._recovery(remote, project.project_id)
            if recovery.authority_incarnation is None:
                raise UploadStateError("upload_protocol_upgrade_required")
            reset = (
                self.meta("authority_error") == "remote_state_reset"
                or self.meta("authority_incarnation")
                not in (None, str(recovery.authority_incarnation))
                or (recovery.authority_sequence or 0)
                < int(self.meta("authority_sequence") or "0")
            )
            rows = self.db.execute(
                "SELECT * FROM sync_batches WHERE state IN ('blocked','retry_wait','ready','uploading','awaiting_commit') OR (? AND rowid IN (SELECT max(rowid) FROM sync_batches GROUP BY artifact_id)) ORDER BY created_at,rowid",
                (int(reset),),
            ).fetchall()
            requeued = 0
            for row in rows:
                response = self._recovery(
                    remote,
                    project.project_id,
                    publication_idempotency_key=f"sync-publication:{row['id']}",
                )
                receipt = (
                    ObservationReceipt.model_validate(response.publication_receipt)
                    if response.publication_receipt
                    else None
                )
                if (
                    receipt
                    and receipt.outcome in {"accepted", "duplicate"}
                    and receipt.details.get("publication_outcome") == "published"
                ):
                    self.acknowledge(row, receipt)
                    continue
                plan = json.loads(row["plan"] or "{}")
                frozen = plan.get("publication")
                # No definitive sequence rejection and no remote reset: keep the
                # exact identity. A missing ACK is not permission to fork it.
                if (
                    not reset
                    and not receipt
                    and (
                        not frozen
                        or response.next_publication_sequence
                        <= frozen["publication_sequence"]
                    )
                ):
                    with self.db:
                        self.db.execute(
                            "UPDATE sync_batches SET state=phase,error_code=NULL,next_retry_at=NULL WHERE id=?",
                            (row["id"],),
                        )
                    continue
                successor = str(uuid4())
                with self.db:
                    self.db.execute(
                        "INSERT INTO sync_batches(id,artifact_id,cursor,body,state,created_at,phase,changed_resources,completion) VALUES(?,?,?,?, 'prepared',?,'ready',?,?)",
                        (
                            successor,
                            row["artifact_id"],
                            row["cursor"],
                            row["body"],
                            row["created_at"],
                            row["changed_resources"],
                            row["completion"],
                        ),
                    )
                    for source in self._batch_body(row)["sources"]:
                        self.db.execute(
                            "INSERT INTO sync_batch_sources VALUES(?,?)",
                            (successor, _source_identity(source)),
                        )
                    self.db.execute(
                        "UPDATE sync_canonical_retentions SET batch_id=? WHERE batch_id=?",
                        (successor, row["id"]),
                    )
                    self._redirect_pages(row["id"], successor)
                    self.db.execute(
                        "UPDATE sync_batches SET state='superseded',error_code='reconciled_to_successor' WHERE id=?",
                        (row["id"],),
                    )
                requeued += 1
            with self.db:
                if reset:
                    self.db.execute("DELETE FROM sync_source_bindings")
                    self.set_meta("acknowledged_cursor", None)
                self.set_meta(
                    "authority_incarnation", str(recovery.authority_incarnation)
                )
                self.set_meta(
                    "authority_sequence", str(recovery.authority_sequence or 0)
                )
                self.set_meta("authority_error", None)
                self.set_meta("project_id", str(project.project_id))
            return {**self.status(), "requeued": requeued}
