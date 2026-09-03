"""Host-local vendor-log collector for the remote CT control plane.

The collector is deliberately a delivery client: its SQLite database holds
paths, offsets and unacknowledged canonical observations, but is never a query
authority.  Vendor JSONL is parsed locally through the existing adapters and
only the normalized canonical session snapshot is queued for publishing.
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
    LeaseHeartbeatRequest,
    LeaseHeartbeatResponse,
    LivingObservationReceipt,
    LivingObservationRequest,
    ObservationReceipt,
    ObservationRequest,
    ProjectRegistrationRequest,
    ProjectRegistrationResponse,
    SourceRegistrationRequest,
    SourceRegistrationResponse,
)
from coding_trajectory.control_plane.compact import build_remote_compact_segments
from coding_trajectory.discovery import (
    DiscoveryCandidate,
    discover_source_candidates,
)
from coding_trajectory.ingestion.adapters.base import SessionHeader
from coding_trajectory.ingestion.common import canonical_json, last_complete_line_offset

_PARSER_VERSION = "ct-local-collector-v2"
_SNAPSHOT_SCHEMA_VERSION = "canonical_session_snapshot.v2"


class CollectorRemote(Protocol):
    """The narrow remote authority used by a collector."""

    def register_project(
        self, request: ProjectRegistrationRequest
    ) -> ProjectRegistrationResponse: ...

    def register_source(
        self, request: SourceRegistrationRequest, *, idempotency_key: str
    ) -> SourceRegistrationResponse: ...

    def publish_observation(
        self, request: ObservationRequest, *, idempotency_key: str
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
            with urllib.request.urlopen(
                http_request, timeout=self._timeout
            ) as response:
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


@dataclass(frozen=True, slots=True)
class CollectorRunResult:
    discovered: int
    queued: int
    accepted: int
    rejected: int
    pending: int
    heartbeat_sequence: int | None


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


class LocalCollector:
    """Collect complete JSONL prefixes into a durable, retry-safe local outbox."""

    def __init__(self, *, database_path: Path, identity: CollectorIdentity) -> None:
        self.database_path = database_path.expanduser()
        self.identity = identity
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.database_path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("pragma journal_mode = wal")
        self._create_schema()
        self._connection.execute(
            "update observation_outbox set state = 'pending' where state = 'in_flight'"
        )
        self._retire_legacy_outbox()
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
        """Discover, normalize, queue, publish and optionally heartbeat once."""

        candidates = discover_source_candidates(
            current_dir=current_dir,
            global_scope=global_scope,
            agent_vendor=agent_vendor,
            since_days=since_days,
        )
        fenced: list[_FencedCandidate] = []
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
                continue
        parent_turn_ids = _parent_started_turn_ids(fenced)
        grouped: dict[tuple[str, UUID], list[_FencedCandidate]] = {}
        for source in fenced:
            grouped.setdefault(
                (source.candidate.vendor.value, source.header.session_id), []
            ).append(source)
        queued = 0
        for group in grouped.values():
            try:
                queued += self._collect_segments(
                    group, parent_turn_ids=parent_turn_ids, remote=remote
                )
            except (CollectorRemoteError, OSError, ValueError, json.JSONDecodeError):
                continue
        accepted, rejected = self.flush(remote) if remote is not None else (0, 0)
        heartbeat_sequence: int | None = None
        if remote is not None and heartbeat:
            try:
                heartbeat_sequence = self._heartbeat(remote)
            except CollectorRemoteError:
                # Delivery remains pending; lease freshness must never make a
                # local source appear terminal when the network is unavailable.
                heartbeat_sequence = None
        return CollectorRunResult(
            discovered=len(candidates),
            queued=queued,
            accepted=accepted,
            rejected=rejected,
            pending=self.pending_count(),
            heartbeat_sequence=heartbeat_sequence,
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

    def pending_count(self) -> int:
        row = self._connection.execute(
            "select (select count(*) from observation_outbox where state = 'pending') + (select count(*) from living_outbox where state = 'pending') as count"
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
    ) -> int:
        segments = sorted(segments, key=lambda segment: str(segment.segment_id))
        first = segments[0]
        vendor = first.candidate.vendor.value
        native_session_id = str(first.header.session_id)
        state = self._logical_source_state(vendor, native_session_id)
        rollover = any(segment.rollover for segment in segments) or (
            state is not None
            and state["snapshot_schema_version"] != _SNAPSHOT_SCHEMA_VERSION
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
                snapshot_schema_version=_SNAPSHOT_SCHEMA_VERSION,
            )
        self._upsert_logical_source(
            vendor=vendor,
            native_session_id=native_session_id,
            source_id=source_id,
            source_epoch=local_epoch,
            snapshot_schema_version=_SNAPSHOT_SCHEMA_VERSION,
        )
        if source_id is None:
            self._connection.commit()
            return 0
        sequence = 0 if rollover else int(state["next_source_sequence"]) if state else 0
        normalized = _normalized_segments(segments, source_id, parent_turn_ids)
        payload = {
            "kind": _SNAPSHOT_SCHEMA_VERSION,
            "source_checkpoint": {
                "segments": [segment.complete_offset for segment in segments]
            },
            "session": normalized,
        }
        content_sha256 = _sha256(canonical_json(payload).encode())
        if (
            state is not None
            and not rollover
            and state["last_digest"] == content_sha256
        ):
            self._connection.commit()
            return 0
        event_id = f"snapshot:{content_sha256}"
        request = ObservationRequest(
            workspace_id=self.identity.workspace_id,
            agent_id=self.identity.agent_id,
            source_id=source_id,
            source_epoch=local_epoch,
            source_sequence=sequence,
            event_id=event_id,
            schema_version=_SNAPSHOT_SCHEMA_VERSION,
            parser_version=_PARSER_VERSION,
            content_sha256=content_sha256,
            observed_at=max(segment.modified_at for segment in segments),
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
                _SNAPSHOT_SCHEMA_VERSION,
                vendor,
                native_session_id,
            ),
        )
        self._connection.execute(
            "update registered_sources set last_digest = ?, next_source_sequence = ? where vendor = ? and native_session_id = ?",
            (content_sha256, sequence + 1, vendor, native_session_id),
        )
        self._connection.commit()
        return 1

    def _heartbeat(self, remote: CollectorRemote) -> int | None:
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

    def _retire_legacy_outbox(self) -> None:
        rows = self._connection.execute(
            "select idempotency_key, request_json from observation_outbox where state in ('pending', 'in_flight')"
        ).fetchall()
        for row in rows:
            try:
                request = ObservationRequest.model_validate_json(row["request_json"])
            except ValueError:
                continue
            if request.schema_version == "canonical_session_snapshot.v1":
                self._connection.execute(
                    "update observation_outbox set state = 'rejected', last_error = 'legacy_v1_not_published' where idempotency_key = ?",
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
    source_id: UUID,
    parent_turn_ids: dict[UUID, set[str]],
) -> dict[str, Any]:
    ordered = sorted(segments, key=lambda segment: str(segment.segment_id))
    # Opaque, durable segment URIs create collision-free canonical IDs without
    # disclosing host paths. Resumed files are then coalesced as one session.
    session = build_remote_compact_segments(
        [
            (
                segment.candidate,
                Path(f"ct-source-{source_id}-segment-{segment.segment_id}"),
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
    return session.model_dump(mode="json")


def _source_key(
    identity: CollectorIdentity, vendor: str, native_session_id: str, epoch: int
) -> str:
    return _sha256(f"{identity.agent_id}:{vendor}:{native_session_id}:{epoch}".encode())


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
