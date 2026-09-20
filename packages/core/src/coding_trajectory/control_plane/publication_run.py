"""Private, pinned publication runs; recovery reads precede explicit write resumption."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import sqlite3
import subprocess
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from coding_trajectory.control_plane.artifact_protocol import (
    ArtifactManifest,
    ArtifactManifestGraph,
    ArtifactPublicationRequest,
    ArtifactReadinessResponse,
    compact_graph,
    compact_publication,
)
from coding_trajectory.control_plane.collector import (
    CloudflareCollectorRemote,
    CollectorIdentity,
    CollectorRemoteError,
    FrozenSource,
    LocalCollector,
    _collector_rpc_body,
    freeze_inventory,
)
from coding_trajectory.control_plane.collector_protocol import (
    CollectorRecoveryRequest,
    CollectorRecoveryResponse,
    ObservationReceipt,
    SourceRegistrationResponse,
)
from coding_trajectory.control_plane.connections import load_profile_credentials
from coding_trajectory.ingestion.common import canonical_json


class PublicationStopped(RuntimeError):
    """Not swallowed by the collector's best-effort delivery exception handling."""


class PublicationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    source_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_tree: str
    python_version: str
    worker_version: UUID
    credential_profile: str
    url: str
    workspace_id: UUID
    agent_id: UUID
    agent_instance_id: UUID
    project_id: UUID
    project_name: str = Field(min_length=1, max_length=256)
    project_root: Path
    next_publication_sequence: int = Field(ge=0)
    inventory: list[FrozenSource]


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def encode(value: Any) -> bytes:
    return canonical_json(value).encode()


def source_pin(expected: str) -> str:
    root = Path(__file__).resolve().parents[5]

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(root), *args], text=True
        ).strip()

    if Path(git("rev-parse", "--show-toplevel")).resolve() != root:
        raise PublicationStopped("collector must be loaded from its source checkout")
    if git("rev-parse", "HEAD") != expected or git(
        "status", "--porcelain", "--untracked-files=no"
    ):
        raise PublicationStopped(
            "collector checkout must be clean and match source SHA"
        )
    return git("rev-parse", "HEAD^{tree}")


def events(directory: Path) -> list[dict[str, Any]]:
    path = directory / "audit.jsonl"
    return (
        [json.loads(line) for line in path.read_bytes().splitlines()]
        if path.exists()
        else []
    )


def record(directory: Path, event: str, **values: Any) -> None:
    created = not (directory / "audit.jsonl").exists()
    with (directory / "audit.jsonl").open("ab") as stream:
        stream.write(
            encode({"event": event, "at": datetime.now(UTC).isoformat(), **values})
            + b"\n"
        )
        stream.flush()
        os.fsync(stream.fileno())
    if created:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def save(directory: Path, name: str, body: bytes) -> None:
    """Immutable receipt, including fsync of the directory entry."""
    with (directory / name).open("xb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class PinnedRemote(CloudflareCollectorRemote):
    def __init__(self, plan: PublicationPlan, *, audit: Path | None = None):
        credentials = load_profile_credentials(plan.credential_profile)
        profile = credentials.profile
        if (
            profile.role != "collector"
            or profile.workspace_id != plan.workspace_id
            or profile.agent_id != plan.agent_id
            or str(profile.cloudflare_url) != plan.url
            or profile.project_id not in (None, plan.project_id)
        ):
            raise PublicationStopped(
                "credential profile differs from pinned publication target"
            )
        super().__init__(
            url=plan.url, access_token=credentials.access_token, timeout=120
        )
        self.plan, self.audit = plan, audit
        self._audit_lock = Lock()
        self._client.event_hooks["response"].append(self._check_version)

    def _check_version(self, response):
        if response.headers.get("X-CT-Worker-Version") != str(self.plan.worker_version):
            raise PublicationStopped(
                "deployed Worker version changed or is unavailable"
            )

    def _event(self, event, **values):
        if self.audit:
            with self._audit_lock:
                record(self.audit, event, **values)

    def _rpc(self, name, request, *, idempotency_key=None):
        body = _collector_rpc_body(name, request, idempotency_key=idempotency_key)
        meta = {
            "method": name,
            "request_sha256": digest(body),
            "bytes": len(body),
            "idempotency_key": idempotency_key,
        }
        self._event("rpc_started", **meta)
        try:
            result = super()._rpc(name, request, idempotency_key=idempotency_key)
            if name == "ct_collector_register_source":
                SourceRegistrationResponse.model_validate(result)
            elif name in {
                "ct_collector_publish_observation",
                "ct_collector_publish_artifacts",
            }:
                receipt = ObservationReceipt.model_validate(result)
                if receipt.outcome not in {
                    "accepted",
                    "duplicate",
                } or receipt.details.get("publication_outcome") in {
                    "rejected",
                    "superseded",
                }:
                    raise PublicationStopped("remote rejected publication work")
            elif name == "ct_collector_recover":
                CollectorRecoveryResponse.model_validate(result)
            elif name == "ct_collector_artifact_readiness":
                readiness = ArtifactReadinessResponse.model_validate(result)
                if len(readiness.ready) != len(request["objects"]):
                    raise PublicationStopped(
                        "artifact readiness response length mismatch"
                    )
        except (CollectorRemoteError, PublicationStopped, ValueError, OSError) as exc:
            self._event(
                "rpc_stopped",
                **meta,
                error_type=type(exc).__name__,
                cause_type=type(exc.__cause__).__name__ if exc.__cause__ else None,
            )
            raise PublicationStopped(
                "RPC failed or outcome unknown; reconcile before resuming"
            ) from None
        self._event(
            "rpc_completed",
            **meta,
            outcome=result.get("outcome"),
            committed_sequence=result.get("committed_sequence"),
        )
        return result

    def upload_artifact(self, *, kind, sha256, body):
        if digest(body) != sha256:
            raise PublicationStopped(
                "local artifact hash differs from its immutable reference"
            )
        meta = {"kind": kind, "sha256": sha256, "bytes": len(body)}
        self._event("upload_started", **meta)
        try:
            super().upload_artifact(kind=kind, sha256=sha256, body=body)
        except (CollectorRemoteError, PublicationStopped, ValueError, OSError) as exc:
            self._event(
                "upload_stopped",
                **meta,
                error_type=type(exc).__name__,
                cause_type=type(exc.__cause__).__name__ if exc.__cause__ else None,
            )
            raise PublicationStopped(
                "upload outcome unknown; reconcile before resuming"
            ) from None
        self._event("upload_completed", **meta)

    def check_target(self):
        result = self._rpc(
            "ct_connection_status", {"workspace_id": str(self.plan.workspace_id)}
        )
        roles = set(result.get("roles", []))
        if (
            result.get("workspace_id") != str(self.plan.workspace_id)
            or result.get("agent_id") != str(self.plan.agent_id)
            or not ({"read", "collect"} <= roles or "owner" in roles)
        ):
            raise PublicationStopped(
                "publication requires matching read+collect workspace and agent"
            )


class PublicationRun:
    def __init__(self, directory: Path, *, create: bool = False):
        self.directory = directory.expanduser().resolve()
        if create:
            self.directory.mkdir(parents=True, mode=0o700, exist_ok=False)
        if not self.directory.is_dir() or self.directory.stat().st_mode & 0o077:
            raise PublicationStopped("publication directory must exist with mode 0700")
        self.database = self.directory / "collector.sqlite"

    def __enter__(self):
        self._umask = os.umask(0o077)
        self._lock = (self.directory / ".lock").open("ab")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            os.umask(self._umask)
            raise PublicationStopped("publication run is already active") from None
        return self

    def __exit__(self, *_args):
        self._lock.close()
        os.umask(self._umask)

    def plan(
        self,
        *,
        source_sha,
        worker_version,
        credential_profile,
        workspace_id,
        project_id,
        project_name,
        project_root,
    ):
        tree = source_pin(source_sha)
        if not project_root.expanduser().is_dir():
            raise PublicationStopped(
                "project discovery root must be an existing directory"
            )
        profile = load_profile_credentials(credential_profile).profile
        plan = PublicationPlan(
            source_sha=source_sha,
            source_tree=tree,
            python_version=platform.python_version(),
            worker_version=worker_version,
            credential_profile=credential_profile,
            url=str(profile.cloudflare_url),
            workspace_id=workspace_id,
            agent_id=profile.agent_id,
            agent_instance_id=uuid4(),
            project_id=project_id,
            project_name=project_name,
            project_root=project_root.expanduser().resolve(),
            next_publication_sequence=0,
            inventory=[],
        )
        with closing(PinnedRemote(plan)) as remote:
            remote.check_target()
            plan.next_publication_sequence = remote.recover(
                self.recovery_request(plan)
            ).next_publication_sequence
        plan.inventory = freeze_inventory(plan.project_root)
        if not plan.inventory:
            raise PublicationStopped("refusing an empty project inventory")
        body = plan.model_dump_json().encode()
        save(self.directory, "plan.json", body)
        record(
            self.directory,
            "planned",
            plan_sha256=digest(body),
            files=len(plan.inventory),
            bytes=sum(s.bytes for s in plan.inventory),
        )
        return {
            "state": "planned",
            "files": len(plan.inventory),
            "source_bytes": sum(s.bytes for s in plan.inventory),
            "plan_sha256": digest(body),
        }

    def load(self):
        body = (self.directory / "plan.json").read_bytes()
        if not events(self.directory) or events(self.directory)[0].get(
            "plan_sha256"
        ) != digest(body):
            raise PublicationStopped("publication plan digest mismatch")
        plan = PublicationPlan.model_validate_json(body)
        if (
            source_pin(plan.source_sha) != plan.source_tree
            or platform.python_version() != plan.python_version
        ):
            raise PublicationStopped("publication toolchain differs from plan")
        return plan

    @staticmethod
    def recovery_request(plan, key=None):
        return CollectorRecoveryRequest(
            workspace_id=plan.workspace_id,
            agent_id=plan.agent_id,
            project_id=plan.project_id,
            publication_idempotency_key=key,
        )

    def _local_state(self):
        if not self.database.exists():
            return [], []
        with closing(
            sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)
        ) as db:
            db.row_factory = sqlite3.Row
            publications = [
                dict(row) for row in db.execute("SELECT * FROM publication_outbox")
            ]
            observations = [
                dict(row)
                for row in db.execute("""SELECT o.*, l.vendor, l.native_session_id
                FROM observation_outbox o JOIN logical_sources l ON l.source_id=o.source_id""")
            ]
        if len(publications) > 1:
            raise PublicationStopped(
                "run contains multiple publications; manual reconciliation required"
            )
        return publications, observations

    def _fingerprint(self):
        fingerprints = {}
        for name in (
            "plan.json",
            "audit.jsonl",
            "collector.sqlite",
            "collector.sqlite-wal",
        ):
            path = self.directory / name
            if path.exists():
                with path.open("rb") as stream:
                    fingerprints[name] = hashlib.file_digest(
                        stream, "sha256"
                    ).hexdigest()
        return fingerprints

    def preflight(self, plan, request, key):
        if (
            request.workspace_id != plan.workspace_id
            or request.agent_id != plan.agent_id
            or request.project_id != plan.project_id
            or request.inventory_state != "complete"
            or request.publication_sequence != plan.next_publication_sequence
            or any(
                method.error for graph in request.graphs for method in graph.api_methods
            )
        ):
            raise PublicationStopped(
                "publication identity, completeness or prepared-method preflight failed"
            )
        wire = _collector_rpc_body(
            "ct_collector_publish_artifacts",
            compact_publication(request),
            idempotency_key=key,
        )
        stored = {
            "schema_version": "ct.artifact-manifest.v3",
            "preparation_version": request.preparation_version,
            "workspace_id": str(request.workspace_id),
            "project_id": str(request.project_id),
            "publisher_agent_id": str(request.agent_id),
            "publication_sequence": request.publication_sequence,
            "snapshot_sequence": 2**63 - 1,
            "published_at": "9999-12-31T23:59:59.999Z",
            "inventory_state": "complete",
            "graphs": [
                compact_graph(
                    ArtifactManifestGraph.model_validate(
                        {
                            k: v
                            for k, v in graph.model_dump(mode="json").items()
                            if k in ArtifactManifestGraph.model_fields
                        }
                    ).model_dump(mode="json")
                )
                for graph in request.graphs
            ],
        }
        storage_bytes = len(encode(stored))
        if storage_bytes > 2 * 1024 * 1024 - 4096:
            raise PublicationStopped(
                "projected stored manifest exceeds SQLite row guard"
            )
        record(
            self.directory,
            "preflight",
            idempotency_key=key,
            rpc_bytes=len(wire),
            rpc_sha256=digest(wire),
            stored_bytes_upper_bound=storage_bytes,
            graphs=len(request.graphs),
            methods=sum(len(g.api_methods) for g in request.graphs),
            object_references=sum(2 + len(g.api_objects) for g in request.graphs),
        )

    def _reconcile(self, plan, remote):
        publications, observations = self._local_state()
        row = publications[0] if publications else None
        if row:
            request = ArtifactPublicationRequest.model_validate(
                json.loads(row["request_json"])["artifact_publication"]
            )
            wire_sha = digest(
                _collector_rpc_body(
                    "ct_collector_publish_artifacts",
                    compact_publication(request),
                    idempotency_key=row["idempotency_key"],
                )
            )
            if any(
                event["event"] == "preflight"
                and event["idempotency_key"] == row["idempotency_key"]
                and event["rpc_sha256"] != wire_sha
                for event in events(self.directory)
            ):
                raise PublicationStopped(
                    "staged request differs from its original preflight"
                )
        recovery = remote.recover(
            self.recovery_request(plan, row["idempotency_key"] if row else None)
        )
        if row and recovery.publication_receipt:
            receipt = ObservationReceipt.model_validate(recovery.publication_receipt)
            if (
                receipt.outcome not in {"accepted", "duplicate"}
                or receipt.committed_sequence is None
            ):
                raise PublicationStopped("publication receipt is not committed")
            result = remote._rpc(
                "ct_artifact_manifest",
                {
                    "workspace_id": str(plan.workspace_id),
                    "project_id": str(plan.project_id),
                    "snapshot_sequence": receipt.committed_sequence,
                },
            )
            manifests = [
                ArtifactManifest.model_validate(m) for m in result.get("manifests", [])
            ]
            if (
                result.get("workspace_id") != str(plan.workspace_id)
                or result.get("snapshot_sequence") != receipt.committed_sequence
            ):
                raise PublicationStopped(
                    "manifest response snapshot differs from committed receipt"
                )
            expected = [
                ArtifactManifestGraph.model_validate(
                    {
                        k: v
                        for k, v in g.model_dump(mode="json").items()
                        if k in ArtifactManifestGraph.model_fields
                    }
                )
                for g in request.graphs
            ]
            if not any(
                m.workspace_id == plan.workspace_id
                and m.project_id == plan.project_id
                and m.publisher_agent_id == plan.agent_id
                and m.publication_sequence == request.publication_sequence
                and m.snapshot_sequence == receipt.committed_sequence
                and m.graphs == expected
                for m in manifests
            ):
                raise PublicationStopped(
                    "committed manifest is unavailable or differs from frozen publication"
                )
            return {
                "state": "committed",
                "committed_sequence": receipt.committed_sequence,
            }
        if recovery.next_publication_sequence != plan.next_publication_sequence:
            raise PublicationStopped(
                "publication sequence changed; do not replay this run"
            )
        settled = []
        for observation in observations:
            recovered = remote.recover(
                CollectorRecoveryRequest(
                    workspace_id=plan.workspace_id,
                    agent_id=plan.agent_id,
                    project_id=plan.project_id,
                    vendor=observation["vendor"],
                    native_session_id=observation["native_session_id"],
                )
            ).source
            if (
                not recovered
                or str(recovered.source_id) != observation["source_id"]
                or recovered.source_epoch != observation["source_epoch"]
            ):
                raise PublicationStopped(
                    "source authority changed; do not replay checkpoints"
                )
            if (
                recovered.next_source_sequence == observation["source_sequence"] + 1
                and recovered.content_sha256 == observation["content_sha256"]
            ):
                settled.append(observation["idempotency_key"])
            elif (
                observation["state"] == "accepted"
                or recovered.next_source_sequence != observation["source_sequence"]
            ):
                raise PublicationStopped(
                    "source watermark differs from pending checkpoint"
                )
        return {
            "state": "resumable",
            "settled_observations": settled,
            "staged_publication": bool(row),
            "uploads": "batch-check authority readiness, then upload missing objects; local PUT receipts are not retention proof",
        }

    def reconcile(self):
        plan = self.load()
        with closing(PinnedRemote(plan)) as remote:
            remote.check_target()
            result = self._reconcile(plan, remote)
        report = {**result, "local_state": self._fingerprint()}
        body = encode(report)
        sha = digest(body)
        path = self.directory / f"reconciliation-{sha}.json"
        if not path.exists():
            save(self.directory, path.name, body)
        return {
            "state": result["state"],
            "reconciliation_sha256": sha,
            "remote_writes": 0,
        }

    def execute(self, reconciliation_sha: str | None = None):
        plan = self.load()
        started = any(e["event"] == "execution_started" for e in events(self.directory))
        if reconciliation_sha is None and (started or self.database.exists()):
            raise PublicationStopped(
                "existing run requires reconcile, then explicit resume"
            )
        if reconciliation_sha is not None:
            if len(reconciliation_sha) != 64 or any(
                c not in "0123456789abcdef" for c in reconciliation_sha
            ):
                raise PublicationStopped("invalid reconciliation digest")
            body = (
                self.directory / f"reconciliation-{reconciliation_sha}.json"
            ).read_bytes()
            report = json.loads(body)
            if (
                digest(body) != reconciliation_sha
                or report["local_state"] != self._fingerprint()
            ):
                raise PublicationStopped("reconciliation is stale; reconcile again")
        # Remote reads are repeated under the exclusive run lock immediately
        # before writes. No saved report alone authorizes replay.
        with closing(PinnedRemote(plan)) as remote:
            remote.check_target()
            decision = self._reconcile(plan, remote)
        if decision["state"] == "committed":
            record(
                self.directory,
                "committed",
                **{k: v for k, v in decision.items() if k != "state"},
            )
            return decision
        record(
            self.directory,
            "execution_started",
            reconciliation_sha256=reconciliation_sha,
        )
        identity = CollectorIdentity(
            workspace_id=plan.workspace_id,
            agent_id=plan.agent_id,
            agent_instance_id=plan.agent_instance_id,
            project_id=plan.project_id,
            project_name=plan.project_name,
        )
        try:
            with (
                closing(PinnedRemote(plan, audit=self.directory)) as remote,
                LocalCollector(
                    database_path=self.database,
                    identity=identity,
                    preparation_cache_path=self.directory / "prepared.sqlite",
                    publication_preflight=lambda request, key: self.preflight(
                        plan, request, key
                    ),
                ) as collector,
            ):
                for key in decision["settled_observations"]:
                    collector._connection.execute(
                        "UPDATE observation_outbox SET state='accepted', last_error=NULL WHERE idempotency_key=?",
                        (key,),
                    )
                collector._connection.commit()
                if decision["staged_publication"]:
                    collector._flush_facts(remote)
                else:
                    result = collector.collect(
                        current_dir=plan.project_root,
                        remote=remote,
                        heartbeat=False,
                        frozen_inventory=plan.inventory,
                    )
                    if (
                        result.failed
                        or result.facts_rejected
                        or result.facts_accepted != 1
                    ):
                        raise PublicationStopped(
                            "collection incomplete; reconcile before continuing"
                        )
                outcome = self._reconcile(plan, remote)
                if outcome["state"] != "committed":
                    raise PublicationStopped("publication has no committed receipt")
            record(
                self.directory,
                "committed",
                committed_sequence=outcome["committed_sequence"],
            )
            return outcome
        except (
            CollectorRemoteError,
            PublicationStopped,
            ValueError,
            OSError,
            sqlite3.Error,
        ):
            record(self.directory, "stopped")
            raise PublicationStopped(
                "publication stopped; use status and read-only reconcile before resume"
            ) from None

    @staticmethod
    def status(directory: Path):
        directory = directory.expanduser().resolve()
        rows = events(directory)
        return {
            "state": rows[-1]["event"] if rows else "unplanned",
            "upload_requests_started": sum(
                r["event"] == "upload_started" for r in rows
            ),
            "upload_requests_completed": sum(
                r["event"] == "upload_completed" for r in rows
            ),
            "preflight": next(
                (r for r in reversed(rows) if r["event"] == "preflight"), None
            ),
            "note": "Local receipts only, not proof of live process or retained remote claims.",
        }
