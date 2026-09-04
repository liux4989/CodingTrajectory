"""Host-local vendor-log collector for the remote CT control plane.

The collector is deliberately a delivery client: its SQLite database holds
paths, offsets, and unacknowledged checkpoint/artifact requests, but is never a
query authority. Vendor JSONL is parsed locally through the existing adapters;
only metadata checkpoints and bounded shareable graphs are queued remotely.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, Self
from uuid import UUID, uuid4

from coding_trajectory.contracts import LivingChange, LivingSessionsChange
from coding_trajectory.control_plane.collector_protocol import (
    ArtifactPublicationRequest,
    CollectorRecoveryRequest,
    CollectorRecoveryResponse,
    LeaseHeartbeatRequest,
    LeaseHeartbeatResponse,
    LivingObservationReceipt,
    LivingObservationRequest,
    ObservationReceipt,
    ObservationRequest,
    ProjectRegistrationRequest,
    ProjectRegistrationResponse,
    RecoveredSource,
    ShareableArtifactPublication,
    SourceVectorEntry,
    SourceRegistrationRequest,
    SourceRegistrationResponse,
)
from coding_trajectory.control_plane.shareable import (
    ShareableGraphArtifact,
    build_shareable_graph_artifact,
    build_shareable_segments,
)
from coding_trajectory.discovery import (
    DiscoveryCandidate,
    discover_source_candidates,
)
from coding_trajectory.ingestion.adapters.base import SessionHeader
from coding_trajectory.ingestion.common import canonical_json, last_complete_line_offset
from coding_trajectory.ingestion.graph import assemble_project_session_graphs
from coding_trajectory.ingestion.models import Session

_PARSER_VERSION = "ct-local-collector-v4"
_SOURCE_SCHEMA_VERSION = "ct.source_checkpoint.v1"


class CollectorRemote(Protocol):
    """The narrow remote authority used by a collector."""

    def recover(
        self, request: CollectorRecoveryRequest
    ) -> CollectorRecoveryResponse: ...

    def register_project(
        self, request: ProjectRegistrationRequest
    ) -> ProjectRegistrationResponse: ...

    def register_source(
        self, request: SourceRegistrationRequest, *, idempotency_key: str
    ) -> SourceRegistrationResponse: ...

    def publish_observation(
        self, request: ObservationRequest, *, idempotency_key: str
    ) -> ObservationReceipt: ...

    def publish_artifacts(
        self, request: ArtifactPublicationRequest, *, idempotency_key: str
    ) -> ObservationReceipt: ...

    def heartbeat(self, request: LeaseHeartbeatRequest) -> LeaseHeartbeatResponse: ...

    def publish_living_observation(
        self, request: LivingObservationRequest
    ) -> LivingObservationReceipt: ...


class CollectorRemoteError(RuntimeError):
    """A remote response was unavailable or did not match its contract."""


class SupabaseCollectorRemote:
    """Call the committed Supabase RPC ingress contract over HTTPS."""

    def __init__(
        self, *, url: str, api_key: str, access_token: str, timeout: float = 20
    ) -> None:
        self._url = url.rstrip("/") + "/rest/v1/rpc/"
        self._api_key = api_key
        self._access_token = access_token
        self._timeout = timeout

    def recover(self, request: CollectorRecoveryRequest) -> CollectorRecoveryResponse:
        return CollectorRecoveryResponse.model_validate(
            self._rpc(
                "ct_collector_recover",
                request.model_dump(mode="json", exclude_none=True),
            )
        )

    def register_source(
        self, request: SourceRegistrationRequest, *, idempotency_key: str
    ) -> SourceRegistrationResponse:
        return SourceRegistrationResponse.model_validate(
            self._rpc(
                "ct_collector_register_source",
                request.model_dump(mode="json"),
                idempotency_key=idempotency_key,
            )
        )

    def register_project(
        self, request: ProjectRegistrationRequest
    ) -> ProjectRegistrationResponse:
        return ProjectRegistrationResponse.model_validate(
            self._rpc("ct_project_register", request.model_dump(mode="json"))
        )

    def publish_observation(
        self, request: ObservationRequest, *, idempotency_key: str
    ) -> ObservationReceipt:
        return ObservationReceipt.model_validate(
            self._rpc(
                "ct_collector_publish_observation",
                request.model_dump(mode="json"),
                idempotency_key=idempotency_key,
            )
        )

    def publish_artifacts(
        self, request: ArtifactPublicationRequest, *, idempotency_key: str
    ) -> ObservationReceipt:
        return ObservationReceipt.model_validate(
            self._rpc(
                "ct_collector_publish_artifacts",
                request.model_dump(mode="json", exclude_none=True),
                idempotency_key=idempotency_key,
            )
        )

    def heartbeat(self, request: LeaseHeartbeatRequest) -> LeaseHeartbeatResponse:
        return LeaseHeartbeatResponse.model_validate(
            self._rpc("ct_collector_heartbeat", request.model_dump(mode="json"))
        )

    def publish_living_observation(
        self, request: LivingObservationRequest
    ) -> LivingObservationReceipt:
        return LivingObservationReceipt.model_validate(
            self._rpc(
                "ct_collector_publish_living_observation",
                request.model_dump(mode="json"),
            )
        )

    def _rpc(
        self, name: str, request: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"request": request}
        if idempotency_key is not None:
            body["idempotency_key"] = idempotency_key
            body["request_sha256"] = _sha256(canonical_json(request).encode())
        encoded = json.dumps(body, separators=(",", ":")).encode()
        http_request = urllib.request.Request(
            self._url + name,
            data=encoded,
            headers={
                "apikey": self._api_key,
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            # The publication RPC has a bounded 60s database budget. Allow
            # transport overhead so a valid commit can return its receipt.
            timeout = (
                max(self._timeout, 90)
                if name == "ct_collector_publish_artifacts"
                else self._timeout
            )
            with urllib.request.urlopen(http_request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            json.JSONDecodeError,
        ) as exc:
            raise CollectorRemoteError(
                f"collector remote {name} failed: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise CollectorRemoteError(
                f"collector remote {name} returned a non-object response"
            )
        return payload


@dataclass(frozen=True, slots=True)
class CollectorIdentity:
    workspace_id: UUID
    agent_id: UUID
    agent_instance_id: UUID
    project_id: UUID | None = None
    project_name: str | None = None


@dataclass(frozen=True, slots=True)
class CollectorRunResult:
    discovered: int
    queued: int
    accepted: int
    rejected: int
    pending: int
    heartbeat_sequence: int | None
    failed: int = 0
    artifacts_queued: int = 0
    artifacts_accepted: int = 0
    artifacts_rejected: int = 0
    artifact_scope_incomplete: bool = False


@dataclass(frozen=True, slots=True)
class _FencedCandidate:
    candidate: DiscoveryCandidate
    header: SessionHeader
    records: list[dict[str, Any]]
    complete_offset: int
    file_identity: str
    modified_at: datetime
    segment_id: UUID
    rollover: bool


@dataclass(frozen=True, slots=True)
class _CollectedSource:
    artifact: ShareableGraphArtifact
    source_id: UUID | None
    source_epoch: int
    source_sequence: int | None
    content_sha256: str | None
    observed_at: datetime
    queued: int


class LocalCollector:
    """Collect complete JSONL prefixes into a durable, retry-safe local outbox."""

    def __init__(self, *, database_path: Path, identity: CollectorIdentity) -> None:
        self.database_path = database_path.expanduser()
        self.identity = identity
        self._recovered_sources: dict[UUID, RecoveredSource] = {}
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.database_path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("pragma journal_mode = wal")
        self._create_schema()
        self._connection.execute(
            "update observation_outbox set state = 'pending' where state = 'in_flight'"
        )
        self._connection.execute(
            "update artifact_outbox set state = 'pending' where state = 'in_flight'"
        )
        self._reject_invalid_observations()
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def collect(
        self,
        *,
        current_dir: Path,
        global_scope: bool = False,
        agent_vendor: str | None = None,
        since_days: int | None = None,
        remote: CollectorRemote | None = None,
        heartbeat: bool = True,
    ) -> CollectorRunResult:
        """Discover, fence, publish checkpoints, and publish local graph artifacts."""

        if remote is not None and global_scope:
            raise ValueError(
                "remote shareable publication requires project-scoped collection"
            )
        if remote is not None and self.identity.project_id is None:
            raise ValueError("remote shareable publication requires a project_id")
        if remote is not None and not (self.identity.project_name or "").strip():
            raise ValueError("remote shareable publication requires a project_name")

        candidates = discover_source_candidates(
            current_dir=current_dir,
            global_scope=global_scope,
            agent_vendor=agent_vendor,
            since_days=since_days,
        )
        fenced: list[_FencedCandidate] = []
        failed = 0
        for candidate in candidates:
            try:
                source = candidate.path
                stat = source.stat()
                complete_offset, complete_bytes = _complete_prefix(source, stat.st_size)
                if complete_offset == 0:
                    continue
                records = [
                    json.loads(line)
                    for line in complete_bytes.splitlines()
                    if line.strip()
                ]
                header = candidate.adapter_cls().scan_identity_records(source, records)
                if header is None:
                    continue
                state = self._source_state(source)
                file_identity = f"{stat.st_dev}:{stat.st_ino}"
                rollover = state is not None and (
                    state["file_identity"] != file_identity
                    or complete_offset < state["committed_offset"]
                )
                fenced.append(
                    _FencedCandidate(
                        candidate=candidate,
                        header=header,
                        records=records,
                        complete_offset=complete_offset,
                        file_identity=file_identity,
                        modified_at=datetime.fromtimestamp(
                            stat.st_mtime_ns / 1_000_000_000, tz=UTC
                        ),
                        segment_id=(
                            UUID(state["segment_id"])
                            if state is not None and state["segment_id"]
                            else uuid4()
                        ),
                        rollover=rollover,
                    )
                )
            except (CollectorRemoteError, OSError, ValueError, json.JSONDecodeError):
                # A changing or malformed local source is retried on the next pass.
                failed += 1
                continue
        parent_turn_ids = _parent_started_turn_ids(fenced)
        grouped: dict[tuple[str, UUID], list[_FencedCandidate]] = {}
        for source in fenced:
            grouped.setdefault(
                (source.candidate.vendor.value, source.header.session_id), []
            ).append(source)
        queued = 0
        collected: list[_CollectedSource] = []
        for group in grouped.values():
            try:
                source = self._collect_segments(
                    group, parent_turn_ids=parent_turn_ids, remote=remote
                )
                queued += source.queued
                collected.append(source)
            except (CollectorRemoteError, OSError, ValueError, json.JSONDecodeError):
                failed += 1
                continue
        accepted, rejected = self.flush(remote) if remote is not None else (0, 0)
        artifacts_accepted = 0
        artifacts_rejected = 0
        artifacts_queued = 0
        if remote is not None:
            # Drain an older exact request before assigning the next monotonic
            # project publication sequence.
            prior_accepted, prior_rejected = self._flush_artifacts(remote)
            artifacts_accepted += prior_accepted
            artifacts_rejected += prior_rejected
            if (
                prior_rejected == 0
                and not self._artifact_publication_blocked()
                and failed == 0
                and collected
                and all(self._source_delivery_accepted(source) for source in collected)
            ):
                recovery = remote.recover(
                    CollectorRecoveryRequest(
                        workspace_id=self.identity.workspace_id,
                        agent_id=self.identity.agent_id,
                        project_id=self.identity.project_id,
                    )
                )
                self._set_meta(
                    f"artifact_publication:{self.identity.project_id}:next_sequence",
                    str(recovery.next_publication_sequence),
                )
                self._connection.commit()
                artifacts_queued = self._queue_artifact_publication(collected)
                current_accepted, current_rejected = self._flush_artifacts(remote)
                artifacts_accepted += current_accepted
                artifacts_rejected += current_rejected
        heartbeat_sequence: int | None = None
        if remote is not None and heartbeat:
            try:
                heartbeat_sequence = self._heartbeat(remote)
            except CollectorRemoteError:
                # Delivery remains pending; lease freshness must never make a
                # local source appear terminal when the network is unavailable.
                heartbeat_sequence = None
        latest_publication = self._connection.execute(
            "select state from artifact_outbox where project_id = ? order by publication_sequence desc limit 1",
            (str(self.identity.project_id),),
        ).fetchone()
        return CollectorRunResult(
            discovered=len(candidates),
            queued=queued,
            accepted=accepted,
            rejected=rejected,
            pending=self.pending_count(),
            heartbeat_sequence=heartbeat_sequence,
            failed=failed,
            artifacts_queued=artifacts_queued,
            artifacts_accepted=artifacts_accepted,
            artifacts_rejected=artifacts_rejected,
            artifact_scope_incomplete=bool(
                latest_publication and latest_publication["state"] == "rejected_scope"
            ),
        )

    def flush(self, remote: CollectorRemote) -> tuple[int, int]:
        """Publish pending work with the exact key and payload originally queued."""

        accepted = 0
        rejected = 0
        rows = self._connection.execute(
            "select * from observation_outbox where state = 'pending' order by created_at, idempotency_key"
        ).fetchall()
        for row in rows:
            self._connection.execute(
                "update observation_outbox set state = 'in_flight', attempts = attempts + 1 where idempotency_key = ?",
                (row["idempotency_key"],),
            )
            self._connection.commit()
            try:
                receipt = remote.publish_observation(
                    ObservationRequest.model_validate_json(row["request_json"]),
                    idempotency_key=row["idempotency_key"],
                )
            except (CollectorRemoteError, OSError, ValueError):
                self._connection.execute(
                    "update observation_outbox set state = 'pending', last_error = ? where idempotency_key = ?",
                    ("remote delivery failed", row["idempotency_key"]),
                )
                self._connection.commit()
                continue
            if receipt.outcome in {"accepted", "duplicate"}:
                state = "accepted"
                accepted += 1
            else:
                state = "rejected"
                rejected += 1
            self._connection.execute(
                "update observation_outbox set state = ?, last_error = ? where idempotency_key = ?",
                (
                    state,
                    None if state == "accepted" else receipt.outcome,
                    row["idempotency_key"],
                ),
            )
            self._connection.execute(
                "insert or replace into remote_receipts (idempotency_key, receipt_id, outcome, committed_sequence, received_at) values (?, ?, ?, ?, ?)",
                (
                    row["idempotency_key"],
                    str(receipt.receipt_id),
                    receipt.outcome,
                    receipt.committed_sequence,
                    datetime.now(UTC).isoformat(),
                ),
            )
            self._connection.commit()
        return accepted, rejected

    def _artifact_publication_blocked(self) -> bool:
        return (
            self._connection.execute(
                "select 1 from artifact_outbox where state in ('pending', 'rejected') limit 1"
            ).fetchone()
            is not None
        )

    def _source_delivery_accepted(self, source: _CollectedSource) -> bool:
        if (
            source.source_id is None
            or source.source_sequence is None
            or source.content_sha256 is None
        ):
            return False
        row = self._connection.execute(
            "select state from observation_outbox where source_id = ? and source_epoch = ? and source_sequence = ? and content_sha256 = ?",
            (
                str(source.source_id),
                source.source_epoch,
                source.source_sequence,
                source.content_sha256,
            ),
        ).fetchone()
        if row is not None:
            return row["state"] == "accepted"
        recovered = self._recovered_sources.get(source.source_id)
        return recovered is not None and (
            recovered.source_epoch == source.source_epoch
            and recovered.next_source_sequence == source.source_sequence + 1
            and recovered.content_sha256 == source.content_sha256
        )

    def _queue_artifact_publication(self, sources: list[_CollectedSource]) -> int:
        if self.identity.project_id is None:
            raise ValueError("shareable publication requires a project_id")
        session_sources: dict[UUID, tuple[Session, list[_CollectedSource]]] = {}
        for source in sources:
            graph = source.artifact.to_session_graph()
            if len(graph.sessions) != 1:
                raise ValueError(
                    "one collected source must contain exactly one session"
                )
            session = graph.sessions[0]
            existing = session_sources.get(session.session_id)
            if existing is not None:
                if existing[0] != session:
                    raise ValueError("collected sources disagree on canonical session")
                existing[1].append(source)
            else:
                session_sources[session.session_id] = (session, [source])

        graphs = assemble_project_session_graphs(
            self.identity.project_name or "project",
            [entry[0] for entry in session_sources.values()],
        )
        source_vector = [
            SourceVectorEntry(
                source_id=source.source_id,
                source_epoch=source.source_epoch,
                source_sequence=source.source_sequence,
                content_sha256=source.content_sha256,
            )
            for source in sorted(sources, key=lambda entry: str(entry.source_id))
            if source.source_id is not None
            and source.source_sequence is not None
            and source.content_sha256 is not None
        ]
        if len(source_vector) != len(sources):
            raise ValueError("shareable publication has an incomplete source vector")

        artifacts: list[ShareableArtifactPublication] = []
        for graph in sorted(graphs, key=lambda entry: str(entry.root_session_id)):
            graph_sources = [
                source
                for session in graph.sessions
                for source in session_sources[session.session_id][1]
            ]
            artifact = build_shareable_graph_artifact(graph)
            artifacts.append(
                ShareableArtifactPublication(
                    artifact_id=artifact.graph.root_session_id,
                    payload=artifact,
                    content_sha256=artifact.digest(),
                    serialized_bytes=len(artifact.canonical_bytes()),
                    source_ids=sorted(
                        (
                            source.source_id
                            for source in graph_sources
                            if source.source_id is not None
                        ),
                        key=str,
                    ),
                    observed_at=max(source.observed_at for source in graph_sources),
                )
            )

        basis = {
            "source_vector": [
                entry.model_dump(mode="json", exclude_none=True)
                for entry in source_vector
            ],
            "artifacts": [
                {
                    "artifact_id": str(artifact.artifact_id),
                    "content_sha256": artifact.content_sha256,
                    "serialized_bytes": artifact.serialized_bytes,
                    "source_ids": [str(value) for value in artifact.source_ids],
                    "observed_at": artifact.observed_at.isoformat(),
                }
                for artifact in artifacts
            ],
        }
        publication_digest = _sha256(canonical_json(basis).encode())
        meta_prefix = f"artifact_publication:{self.identity.project_id}"
        if self._get_meta(f"{meta_prefix}:last_digest", "") == publication_digest:
            return 0
        sequence = int(self._get_meta(f"{meta_prefix}:next_sequence", "0"))
        request = ArtifactPublicationRequest(
            workspace_id=self.identity.workspace_id,
            agent_id=self.identity.agent_id,
            project_id=self.identity.project_id,
            publication_sequence=sequence,
            source_vector=source_vector,
            artifacts=artifacts,
        )
        idempotency_key = _sha256(
            (
                f"{self.identity.agent_id}:{self.identity.project_id}:"
                f"{sequence}:{publication_digest}"
            ).encode()
        )
        self._connection.execute(
            "insert or ignore into artifact_outbox (idempotency_key, project_id, publication_sequence, content_sha256, request_json, state, attempts, created_at) values (?, ?, ?, ?, ?, 'pending', 0, ?)",
            (
                idempotency_key,
                str(self.identity.project_id),
                sequence,
                publication_digest,
                request.model_dump_json(exclude_none=True),
                datetime.now(UTC).isoformat(),
            ),
        )
        self._set_meta(f"{meta_prefix}:last_digest", publication_digest)
        self._set_meta(f"{meta_prefix}:next_sequence", str(sequence + 1))
        self._connection.commit()
        return 1

    def _flush_artifacts(self, remote: CollectorRemote) -> tuple[int, int]:
        accepted = 0
        rejected = 0
        rows = self._connection.execute(
            "select * from artifact_outbox where state = 'pending' or (state = 'rejected' and last_error = 'conflict') order by publication_sequence, created_at"
        ).fetchall()
        for row in rows:
            self._connection.execute(
                "update artifact_outbox set state = 'in_flight', attempts = attempts + 1 where idempotency_key = ?",
                (row["idempotency_key"],),
            )
            self._connection.commit()
            try:
                receipt = remote.publish_artifacts(
                    ArtifactPublicationRequest.model_validate_json(row["request_json"]),
                    idempotency_key=row["idempotency_key"],
                )
            except (CollectorRemoteError, OSError, ValueError):
                self._connection.execute(
                    "update artifact_outbox set state = 'pending', last_error = ? where idempotency_key = ?",
                    ("remote delivery failed", row["idempotency_key"]),
                )
                self._connection.commit()
                break
            if receipt.outcome in {"accepted", "duplicate"}:
                state = "accepted"
                accepted += 1
            elif (
                receipt.outcome == "rejected"
                and receipt.details.get("reason") == "incomplete_graph_scope"
            ):
                # The server consumed this sequence without changing history.
                # Keep the exact request as evidence; an expanded scope can
                # publish next time without a permanently poisoned outbox.
                state = "rejected_scope"
                rejected += 1
            elif (
                receipt.outcome == "conflict"
                and receipt.details.get("reason") == "stale_publication_sequence"
            ):
                state = "superseded"
                self._set_meta(
                    f"artifact_publication:{self.identity.project_id}:last_digest", ""
                )
            else:
                state = "rejected"
                rejected += 1
            self._connection.execute(
                "update artifact_outbox set state = ?, last_error = ? where idempotency_key = ?",
                (
                    state,
                    None if state == "accepted" else receipt.outcome,
                    row["idempotency_key"],
                ),
            )
            self._connection.execute(
                "insert or replace into remote_receipts (idempotency_key, receipt_id, outcome, committed_sequence, received_at) values (?, ?, ?, ?, ?)",
                (
                    row["idempotency_key"],
                    str(receipt.receipt_id),
                    receipt.outcome,
                    receipt.committed_sequence,
                    datetime.now(UTC).isoformat(),
                ),
            )
            self._connection.commit()
            if state == "rejected":
                break
        return accepted, rejected

    def pending_count(self) -> int:
        row = self._connection.execute(
            "select (select count(*) from observation_outbox where state = 'pending') + (select count(*) from artifact_outbox where state = 'pending') + (select count(*) from living_outbox where state = 'pending') as count"
        ).fetchone()
        return int(row["count"])

    def publish_living_changes(
        self,
        *,
        remote: CollectorRemote,
        kind: str,
        changes: list[dict[str, Any]],
        observed_at: datetime | None = None,
    ) -> int:
        """Durably publish local canonical changes using the shared lease sequence."""

        model = (
            LivingChange
            if kind == "living.events"
            else LivingSessionsChange
            if kind == "living.sessions"
            else None
        )
        if model is None:
            raise ValueError(f"unsupported living observation kind: {kind}")
        if (
            not self._connection.execute(
                "select 1 from living_outbox where kind = 'heartbeat' and state = 'accepted' limit 1"
            ).fetchone()
            and self._heartbeat(remote) is None
        ):
            raise CollectorRemoteError(
                "collector heartbeat must be accepted before living changes"
            )
        timestamp = observed_at or datetime.now(UTC)
        queued_sequences: set[int] = set()
        for change in changes:
            validated = model.model_validate(change).model_dump(
                mode="json", exclude_none=True
            )
            source_cursor = validated.pop("cursor")
            validated.pop("revision")
            sequence = self._queue_living_request(
                kind=kind,
                source_cursor=source_cursor,
                observed_at=timestamp,
                payload=validated,
            )
            if sequence is not None:
                queued_sequences.add(sequence)
        committed = self._flush_living(remote)
        return len(queued_sequences & committed.keys())

    def _collect_segments(
        self,
        segments: list[_FencedCandidate],
        *,
        parent_turn_ids: dict[UUID, set[str]],
        remote: CollectorRemote | None,
    ) -> _CollectedSource:
        segments = sorted(segments, key=lambda segment: str(segment.segment_id))
        first = segments[0]
        vendor = first.candidate.vendor.value
        native_session_id = str(first.header.session_id)
        artifact = _normalized_segments(segments, parent_turn_ids)
        observed_at = max(segment.modified_at for segment in segments)
        state = self._logical_source_state(vendor, native_session_id)
        if remote is not None and (
            state is None
            or not self._connection.execute(
                "select 1 from observation_outbox where source_id = ? limit 1",
                (state["source_id"],),
            ).fetchone()
        ):
            recovery = remote.recover(
                CollectorRecoveryRequest(
                    workspace_id=self.identity.workspace_id,
                    agent_id=self.identity.agent_id,
                    project_id=self.identity.project_id,
                    vendor=vendor,
                    native_session_id=native_session_id,
                )
            )
            if recovery.source is not None:
                recovered = recovery.source
                self._recovered_sources[recovered.source_id] = recovered
                self._upsert_logical_source(
                    vendor=vendor,
                    native_session_id=native_session_id,
                    source_id=recovered.source_id,
                    source_epoch=recovered.source_epoch,
                    snapshot_schema_version=_SOURCE_SCHEMA_VERSION,
                )
                self._connection.execute(
                    "update logical_sources set next_source_sequence = ?, last_digest = ? where vendor = ? and native_session_id = ?",
                    (
                        recovered.next_source_sequence,
                        recovered.content_sha256,
                        vendor,
                        native_session_id,
                    ),
                )
                self._connection.commit()
                state = self._logical_source_state(vendor, native_session_id)
        rollover = any(segment.rollover for segment in segments) or (
            state is not None
            and state["snapshot_schema_version"] != _SOURCE_SCHEMA_VERSION
        )
        local_epoch = (
            int(state["source_epoch"]) + 1
            if rollover
            else int(state["source_epoch"])
            if state
            else 1
        )
        source_id = UUID(state["source_id"]) if state and state["source_id"] else None
        if remote is not None and (source_id is None or rollover):
            registration = remote.register_source(
                SourceRegistrationRequest(
                    workspace_id=self.identity.workspace_id,
                    agent_id=self.identity.agent_id,
                    vendor=vendor,
                    native_session_id=native_session_id,
                    project_id=self.identity.project_id,
                    source_epoch=local_epoch,
                    rollover=rollover,
                ),
                idempotency_key=_source_key(
                    self.identity,
                    vendor,
                    native_session_id,
                    local_epoch,
                ),
            )
            source_id = registration.source_id
            local_epoch = registration.source_epoch
        for segment in segments:
            self._upsert_source(
                source=segment.candidate.path,
                vendor=vendor,
                native_session_id=native_session_id,
                source_id=source_id,
                source_epoch=local_epoch,
                file_identity=segment.file_identity,
                committed_offset=segment.complete_offset,
                segment_id=segment.segment_id,
                snapshot_schema_version=_SOURCE_SCHEMA_VERSION,
            )
        self._upsert_logical_source(
            vendor=vendor,
            native_session_id=native_session_id,
            source_id=source_id,
            source_epoch=local_epoch,
            snapshot_schema_version=_SOURCE_SCHEMA_VERSION,
        )
        if source_id is None:
            self._connection.commit()
            return _CollectedSource(
                artifact=artifact,
                source_id=None,
                source_epoch=local_epoch,
                source_sequence=None,
                content_sha256=None,
                observed_at=observed_at,
                queued=0,
            )
        sequence = 0 if rollover else int(state["next_source_sequence"]) if state else 0
        payload = {
            "kind": _SOURCE_SCHEMA_VERSION,
            "source_checkpoint": {
                "segments": [segment.complete_offset for segment in segments]
            },
            "shareable_digest": artifact.digest(),
        }
        content_sha256 = _sha256(canonical_json(payload).encode())
        if (
            state is not None
            and not rollover
            and state["last_digest"] == content_sha256
        ):
            self._connection.commit()
            return _CollectedSource(
                artifact=artifact,
                source_id=source_id,
                source_epoch=local_epoch,
                source_sequence=max(int(state["next_source_sequence"]) - 1, 0),
                content_sha256=content_sha256,
                observed_at=observed_at,
                queued=0,
            )
        event_id = f"checkpoint:{content_sha256}"
        request = ObservationRequest(
            workspace_id=self.identity.workspace_id,
            agent_id=self.identity.agent_id,
            source_id=source_id,
            source_epoch=local_epoch,
            source_sequence=sequence,
            event_id=event_id,
            schema_version=_SOURCE_SCHEMA_VERSION,
            parser_version=_PARSER_VERSION,
            content_sha256=content_sha256,
            observed_at=observed_at,
            payload=payload,
        )
        idempotency_key = _sha256(
            (
                f"{self.identity.agent_id}:{source_id}:{local_epoch}:"
                f"{sequence}:{content_sha256}"
            ).encode()
        )
        self._connection.execute(
            "insert or ignore into observation_outbox (idempotency_key, source_id, source_epoch, source_sequence, event_id, content_sha256, request_json, state, attempts, created_at) values (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?)",
            (
                idempotency_key,
                str(source_id),
                local_epoch,
                sequence,
                event_id,
                content_sha256,
                request.model_dump_json(),
                datetime.now(UTC).isoformat(),
            ),
        )
        self._connection.execute(
            "update logical_sources set last_digest = ?, next_source_sequence = ?, snapshot_schema_version = ? where vendor = ? and native_session_id = ?",
            (
                content_sha256,
                sequence + 1,
                _SOURCE_SCHEMA_VERSION,
                vendor,
                native_session_id,
            ),
        )
        self._connection.execute(
            "update registered_sources set last_digest = ?, next_source_sequence = ? where vendor = ? and native_session_id = ?",
            (content_sha256, sequence + 1, vendor, native_session_id),
        )
        self._connection.commit()
        return _CollectedSource(
            artifact=artifact,
            source_id=source_id,
            source_epoch=local_epoch,
            source_sequence=sequence,
            content_sha256=content_sha256,
            observed_at=observed_at,
            queued=1,
        )

    def _heartbeat(self, remote: CollectorRemote) -> int | None:
        if (
            self.identity.project_id is not None
            and not self._connection.execute(
                "select 1 from living_outbox limit 1"
            ).fetchone()
        ):
            recovery = remote.recover(
                CollectorRecoveryRequest(
                    workspace_id=self.identity.workspace_id,
                    agent_id=self.identity.agent_id,
                    project_id=self.identity.project_id,
                    agent_instance_id=self.identity.agent_instance_id,
                )
            )
            if recovery.next_living_sequence is None:
                raise CollectorRemoteError("recovery omitted living watermark")
            self._set_meta(
                "next_living_observation_sequence", str(recovery.next_living_sequence)
            )
            self._connection.commit()
        source_watermarks = {
            row["source_id"]: int(row["next_source_sequence"]) - 1
            for row in self._connection.execute(
                "select source_id, next_source_sequence from logical_sources where source_id is not null"
            )
        }
        pending = self._connection.execute(
            "select observation_sequence from living_outbox where kind = 'heartbeat' and state = 'pending' order by observation_sequence limit 1"
        ).fetchone()
        if pending is None:
            sequence = self._next_living_sequence()
            request = LeaseHeartbeatRequest(
                workspace_id=self.identity.workspace_id,
                agent_id=self.identity.agent_id,
                agent_instance_id=self.identity.agent_instance_id,
                observation_sequence=sequence,
                observed_at=datetime.now(UTC),
                source_watermarks=source_watermarks,
                runtime_state="living" if source_watermarks else "unknown",
            )
            self._connection.execute(
                "insert into living_outbox (observation_sequence, kind, source_cursor, request_json, state, created_at) values (?, 'heartbeat', null, ?, 'pending', ?)",
                (sequence, request.model_dump_json(), datetime.now(UTC).isoformat()),
            )
            self._connection.commit()
        else:
            sequence = int(pending["observation_sequence"])
        committed = self._flush_living(remote)
        return committed.get(sequence)

    def _queue_living_request(
        self,
        *,
        kind: str,
        source_cursor: str,
        observed_at: datetime,
        payload: dict[str, Any],
    ) -> int | None:
        existing = self._connection.execute(
            "select observation_sequence, state from living_outbox where kind = ? and source_cursor = ?",
            (kind, source_cursor),
        ).fetchone()
        if existing is not None:
            return (
                int(existing["observation_sequence"])
                if existing["state"] == "pending"
                else None
            )
        sequence = self._next_living_sequence()
        request = LivingObservationRequest(
            workspace_id=self.identity.workspace_id,
            agent_id=self.identity.agent_id,
            agent_instance_id=self.identity.agent_instance_id,
            observation_sequence=sequence,
            observed_at=observed_at,
            kind=kind,
            payload=payload,
        )
        self._connection.execute(
            "insert into living_outbox (observation_sequence, kind, source_cursor, request_json, state, created_at) values (?, ?, ?, ?, 'pending', ?)",
            (
                sequence,
                kind,
                source_cursor,
                request.model_dump_json(),
                datetime.now(UTC).isoformat(),
            ),
        )
        self._connection.commit()
        return sequence

    def _next_living_sequence(self) -> int:
        legacy = int(self._get_meta("next_heartbeat_sequence", "1"))
        sequence = int(self._get_meta("next_living_observation_sequence", str(legacy)))
        self._set_meta(
            "next_living_observation_sequence", str(max(sequence, legacy) + 1)
        )
        return max(sequence, legacy)

    def _flush_living(self, remote: CollectorRemote) -> dict[int, int]:
        committed: dict[int, int] = {}
        rows = self._connection.execute(
            "select * from living_outbox where state = 'pending' order by observation_sequence"
        ).fetchall()
        for row in rows:
            try:
                if row["kind"] == "heartbeat":
                    receipt = remote.heartbeat(
                        LeaseHeartbeatRequest.model_validate_json(row["request_json"])
                    )
                else:
                    receipt = remote.publish_living_observation(
                        LivingObservationRequest.model_validate_json(
                            row["request_json"]
                        )
                    )
            except (CollectorRemoteError, OSError, ValueError):
                break
            self._connection.execute(
                "update living_outbox set state = 'accepted', committed_sequence = ? where observation_sequence = ?",
                (receipt.committed_sequence, row["observation_sequence"]),
            )
            sequence = int(row["observation_sequence"])
            committed[sequence] = receipt.committed_sequence
            self._connection.commit()
        return committed

    def _source_state(self, source: Path) -> sqlite3.Row | None:
        return self._connection.execute(
            "select * from registered_sources where path = ?", (str(source),)
        ).fetchone()

    def _logical_source_state(
        self, vendor: str, native_session_id: str
    ) -> sqlite3.Row | None:
        return self._connection.execute(
            "select * from logical_sources where vendor = ? and native_session_id = ?",
            (vendor, native_session_id),
        ).fetchone()

    def _upsert_source(self, **values: Any) -> None:
        self._connection.execute(
            "insert into registered_sources (path, vendor, native_session_id, source_id, source_epoch, file_identity, committed_offset, next_source_sequence, segment_id, snapshot_schema_version) values (:path, :vendor, :native_session_id, :source_id, :source_epoch, :file_identity, :committed_offset, 0, :segment_id, :snapshot_schema_version) on conflict(path) do update set vendor = excluded.vendor, native_session_id = excluded.native_session_id, source_id = excluded.source_id, source_epoch = excluded.source_epoch, file_identity = excluded.file_identity, committed_offset = excluded.committed_offset, segment_id = coalesce(registered_sources.segment_id, excluded.segment_id), snapshot_schema_version = excluded.snapshot_schema_version",
            {
                **values,
                "path": str(values["source"]),
                "source_id": str(values["source_id"]) if values["source_id"] else None,
                "segment_id": str(values["segment_id"]),
            },
        )

    def _upsert_logical_source(self, **values: Any) -> None:
        self._connection.execute(
            "insert into logical_sources (vendor, native_session_id, source_id, source_epoch, next_source_sequence, snapshot_schema_version) values (:vendor, :native_session_id, :source_id, :source_epoch, 0, :snapshot_schema_version) on conflict(vendor, native_session_id) do update set source_id = excluded.source_id, source_epoch = excluded.source_epoch, next_source_sequence = case when logical_sources.source_epoch <> excluded.source_epoch then 0 else logical_sources.next_source_sequence end, last_digest = case when logical_sources.source_epoch <> excluded.source_epoch then null else logical_sources.last_digest end, snapshot_schema_version = excluded.snapshot_schema_version",
            {
                **values,
                "source_id": str(values["source_id"]) if values["source_id"] else None,
            },
        )

    def _get_meta(self, key: str, default: str) -> str:
        row = self._connection.execute(
            "select value from collector_meta where key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row is not None else default

    def _set_meta(self, key: str, value: str) -> None:
        self._connection.execute(
            "insert into collector_meta (key, value) values (?, ?) on conflict(key) do update set value = excluded.value",
            (key, value),
        )

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            create table if not exists collector_meta (key text primary key, value text not null);
            create table if not exists registered_sources (
              path text primary key, vendor text not null, native_session_id text not null,
              source_id text, source_epoch integer not null, file_identity text not null,
              committed_offset integer not null, next_source_sequence integer not null,
              last_digest text, segment_id text, snapshot_schema_version text
            );
            create table if not exists logical_sources (
              vendor text not null, native_session_id text not null, source_id text,
              source_epoch integer not null, next_source_sequence integer not null,
              last_digest text, snapshot_schema_version text,
              primary key (vendor, native_session_id)
            );
            create table if not exists observation_outbox (
              idempotency_key text primary key, source_id text not null, source_epoch integer not null,
              source_sequence integer not null, event_id text not null, content_sha256 text not null,
              request_json text not null, state text not null, attempts integer not null,
              last_error text, created_at text not null
            );
            create table if not exists artifact_outbox (
              idempotency_key text primary key, project_id text not null,
              publication_sequence integer not null, content_sha256 text not null,
              request_json text not null, state text not null, attempts integer not null,
              last_error text, created_at text not null,
              unique(project_id, publication_sequence)
            );
            create table if not exists remote_receipts (
              idempotency_key text primary key, receipt_id text not null, outcome text not null,
              committed_sequence integer, received_at text not null
            );
            create table if not exists living_outbox (
              observation_sequence integer primary key, kind text not null,
              source_cursor text, request_json text not null, state text not null,
              committed_sequence integer, created_at text not null,
              unique(kind, source_cursor)
            );
            """
        )
        columns = {
            row["name"]
            for row in self._connection.execute("pragma table_info(registered_sources)")
        }
        if "snapshot_schema_version" not in columns:
            self._connection.execute(
                "alter table registered_sources add column snapshot_schema_version text"
            )
        if "segment_id" not in columns:
            self._connection.execute(
                "alter table registered_sources add column segment_id text"
            )
        self._connection.execute(
            "update registered_sources set segment_id = lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' || substr(lower(hex(randomblob(2))), 2) || '-' || substr('89ab', abs(random()) % 4 + 1, 1) || substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6))) where segment_id is null"
        )
        self._connection.execute(
            "insert or ignore into logical_sources (vendor, native_session_id, source_id, source_epoch, next_source_sequence, last_digest, snapshot_schema_version) select vendor, native_session_id, max(source_id), max(source_epoch), max(next_source_sequence), null, max(snapshot_schema_version) from registered_sources group by vendor, native_session_id"
        )

    def _reject_invalid_observations(self) -> None:
        rows = self._connection.execute(
            "select idempotency_key, request_json from observation_outbox where state in ('pending', 'in_flight')"
        ).fetchall()
        for row in rows:
            try:
                ObservationRequest.model_validate_json(row["request_json"])
            except ValueError:
                self._connection.execute(
                    "update observation_outbox set state = 'rejected', last_error = 'invalid_checkpoint_not_published' where idempotency_key = ?",
                    (row["idempotency_key"],),
                )


def _complete_prefix(source: Path, size: int) -> tuple[int, bytes]:
    with source.open("rb") as handle:
        offset = last_complete_line_offset(handle, size)
        handle.seek(0)
        return offset, handle.read(offset)


def _parent_started_turn_ids(
    sources: list[_FencedCandidate],
) -> dict[UUID, set[str]]:
    """Derive fork-cut inputs exclusively from the same fenced source bytes."""

    referenced = {
        source.header.parent_session_id
        for source in sources
        if source.header.parent_session_id is not None
    }
    started: dict[UUID, set[str]] = {}
    for source in sources:
        if source.header.session_id not in referenced:
            continue
        values = source.candidate.adapter_cls().scan_started_turn_ids_records(
            source.records
        )
        if values is not None:
            started.setdefault(source.header.session_id, set()).update(values)
    return started


def _normalized_segments(
    segments: list[_FencedCandidate],
    parent_turn_ids: dict[UUID, set[str]],
) -> ShareableGraphArtifact:
    ordered = sorted(segments, key=lambda segment: str(segment.segment_id))
    # The source path participates only in deterministic canonical IDs and is
    # never serialized into the shareable artifact. Using the same identity
    # input as local discovery keeps local and remote turn/item IDs identical.
    artifact = build_shareable_segments(
        [
            (
                segment.candidate,
                segment.candidate.path,
                segment.records,
                (
                    parent_turn_ids.get(segment.header.parent_session_id)
                    if segment.header.parent_session_id is not None
                    else None
                ),
            )
            for segment in ordered
        ]
    )
    return artifact


def _source_key(
    identity: CollectorIdentity, vendor: str, native_session_id: str, epoch: int
) -> str:
    return _sha256(f"{identity.agent_id}:{vendor}:{native_session_id}:{epoch}".encode())


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
