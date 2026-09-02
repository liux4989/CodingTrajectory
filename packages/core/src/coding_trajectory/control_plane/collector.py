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
from uuid import UUID

from coding_trajectory.control_plane.collector_protocol import (
    LeaseHeartbeatRequest,
    LeaseHeartbeatResponse,
    ObservationReceipt,
    ObservationRequest,
    SourceRegistrationRequest,
    SourceRegistrationResponse,
)
from coding_trajectory.control_plane.compact import build_remote_compact_session
from coding_trajectory.discovery import (
    DiscoveryCandidate,
    discover_source_candidates,
)
from coding_trajectory.ingestion.common import canonical_json, last_complete_line_offset

_PARSER_VERSION = "ct-local-collector-v2"
_SNAPSHOT_SCHEMA_VERSION = "canonical_session_snapshot.v2"


class CollectorRemote(Protocol):
    """The narrow remote authority used by a collector."""

    def register_source(
        self, request: SourceRegistrationRequest, *, idempotency_key: str
    ) -> SourceRegistrationResponse: ...

    def publish_observation(
        self, request: ObservationRequest, *, idempotency_key: str
    ) -> ObservationReceipt: ...

    def heartbeat(self, request: LeaseHeartbeatRequest) -> LeaseHeartbeatResponse: ...


class CollectorRemoteError(RuntimeError):
    """A remote response was unavailable or did not match its contract."""


class SupabaseCollectorRemote:
    """Call the committed Supabase RPC ingress contract over HTTPS."""

    def __init__(self, *, url: str, api_key: str, access_token: str, timeout: float = 20) -> None:
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
            with urllib.request.urlopen(http_request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            raise CollectorRemoteError(f"collector remote {name} failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise CollectorRemoteError(f"collector remote {name} returned a non-object response")
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
        queued = 0
        for candidate in candidates:
            try:
                queued += self._collect_candidate(candidate, remote=remote)
            except (CollectorRemoteError, OSError, ValueError, json.JSONDecodeError):
                # A changing or malformed local source is retried on the next pass.
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
            "select count(*) as count from observation_outbox where state = 'pending'"
        ).fetchone()
        return int(row["count"])

    def _collect_candidate(
        self, candidate: DiscoveryCandidate, *, remote: CollectorRemote | None
    ) -> int:
        source = candidate.path
        header = candidate.adapter_cls().scan_identity(source)
        if header is None:
            return 0
        stat = source.stat()
        complete_offset, complete_bytes = _complete_prefix(source, stat.st_size)
        if complete_offset == 0:
            return 0
        file_identity = f"{stat.st_dev}:{stat.st_ino}"
        state = self._source_state(source)
        schema_changed = state is not None and (
            state["snapshot_schema_version"] != _SNAPSHOT_SCHEMA_VERSION
        )
        rollover = state is not None and (
            state["file_identity"] != file_identity
            or complete_offset < state["committed_offset"]
            or schema_changed
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
                    vendor=candidate.vendor.value,
                    native_session_id=str(header.session_id),
                    project_id=self.identity.project_id,
                    source_epoch=local_epoch,
                    rollover=rollover,
                ),
                idempotency_key=_source_key(
                    self.identity, candidate.vendor.value, str(header.session_id), local_epoch
                ),
            )
            source_id = registration.source_id
            local_epoch = registration.source_epoch
        self._upsert_source(
            source=source,
            vendor=candidate.vendor.value,
            native_session_id=str(header.session_id),
            source_id=source_id,
            source_epoch=local_epoch,
            file_identity=file_identity,
            committed_offset=complete_offset,
            snapshot_schema_version=_SNAPSHOT_SCHEMA_VERSION,
        )
        digest = _sha256(complete_bytes)
        if source_id is None or (
            state is not None and not rollover and state["last_digest"] == digest
        ):
            self._connection.commit()
            return 0
        sequence = 0 if rollover else int(state["next_source_sequence"]) if state else 0
        normalized = _normalized_session(candidate, source_id, complete_bytes)
        payload = {
            "kind": _SNAPSHOT_SCHEMA_VERSION,
            "source_checkpoint": {"committed_offset": complete_offset},
            "session": normalized,
        }
        content_sha256 = _sha256(canonical_json(payload).encode())
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
            observed_at=datetime.fromtimestamp(stat.st_mtime_ns / 1_000_000_000, tz=UTC),
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
            "update registered_sources set last_digest = ?, next_source_sequence = ?, snapshot_schema_version = ? where path = ?",
            (digest, sequence + 1, _SNAPSHOT_SCHEMA_VERSION, str(source)),
        )
        self._connection.commit()
        return 1

    def _heartbeat(self, remote: CollectorRemote) -> int | None:
        source_watermarks = {
            row["source_id"]: int(row["next_source_sequence"]) - 1
            for row in self._connection.execute(
                "select source_id, next_source_sequence from registered_sources where source_id is not null"
            )
        }
        sequence = int(self._get_meta("next_heartbeat_sequence", "1"))
        response = remote.heartbeat(
            LeaseHeartbeatRequest(
                workspace_id=self.identity.workspace_id,
                agent_id=self.identity.agent_id,
                agent_instance_id=self.identity.agent_instance_id,
                observation_sequence=sequence,
                observed_at=datetime.now(UTC),
                source_watermarks=source_watermarks,
                runtime_state="living" if source_watermarks else "unknown",
            )
        )
        self._set_meta("next_heartbeat_sequence", str(sequence + 1))
        self._connection.commit()
        return response.committed_sequence

    def _source_state(self, source: Path) -> sqlite3.Row | None:
        return self._connection.execute(
            "select * from registered_sources where path = ?", (str(source),)
        ).fetchone()

    def _upsert_source(self, **values: Any) -> None:
        self._connection.execute(
            "insert into registered_sources (path, vendor, native_session_id, source_id, source_epoch, file_identity, committed_offset, next_source_sequence, snapshot_schema_version) values (:path, :vendor, :native_session_id, :source_id, :source_epoch, :file_identity, :committed_offset, 0, :snapshot_schema_version) on conflict(path) do update set vendor = excluded.vendor, native_session_id = excluded.native_session_id, source_id = excluded.source_id, source_epoch = excluded.source_epoch, file_identity = excluded.file_identity, committed_offset = excluded.committed_offset, next_source_sequence = case when registered_sources.source_epoch <> excluded.source_epoch then 0 else registered_sources.next_source_sequence end, last_digest = case when registered_sources.source_epoch <> excluded.source_epoch then null else registered_sources.last_digest end, snapshot_schema_version = excluded.snapshot_schema_version",
            {**values, "path": str(values["source"]), "source_id": str(values["source_id"]) if values["source_id"] else None},
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
              last_digest text, snapshot_schema_version text
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


def _normalized_session(candidate: DiscoveryCandidate, source_id: UUID, raw: bytes) -> dict[str, Any]:
    records = [json.loads(line) for line in raw.splitlines() if line.strip()]
    # The opaque URI creates host-independent stable event/item IDs.  It is not
    # opened and it deliberately cannot reveal the source path to the server.
    portable_source = Path(f"ct-source-{source_id}")
    session = build_remote_compact_session(
        candidate, source=portable_source, records=records
    )
    return session.model_dump(mode="json")


def _source_key(identity: CollectorIdentity, vendor: str, native_session_id: str, epoch: int) -> str:
    return _sha256(f"{identity.agent_id}:{vendor}:{native_session_id}:{epoch}".encode())


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
