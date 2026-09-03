"""Supabase-backed projection worker and historical snapshot repository."""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from coding_trajectory.control_plane.compact import validate_remote_compact_session
from coding_trajectory.ingestion.common import canonical_json
from coding_trajectory.ingestion.graph import assemble_project_session_graphs
from coding_trajectory.ingestion.models import Session, SessionGraph
from coding_trajectory.query import DocumentError, DocumentStore


class RemoteControlPlaneError(DocumentError):
    """A remote control-plane operation failed or violated its contract."""


class ProjectionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: UUID
    source_epoch: int = Field(gt=0)
    source_sequence: int = Field(ge=0)
    observed_at: datetime
    payload: dict[str, Any]

    def compact_session(self) -> Session:
        """Validate the small v2 wrapper and return its canonical session."""

        if self.payload.get("kind") != "canonical_session_snapshot.v2":
            raise ValueError("projection observation is not a compact v2 snapshot")
        checkpoint = self.payload.get("source_checkpoint")
        if not isinstance(checkpoint, dict) or set(checkpoint) != {"segments"}:
            raise TypeError("compact snapshot has no valid segmented checkpoint")
        segments = checkpoint.get("segments")
        if not (
            isinstance(segments, list)
            and bool(segments)
            and all(
                isinstance(offset, int)
                and not isinstance(offset, bool)
                and offset >= 0
                for offset in segments
            )
        ):
            raise TypeError("compact snapshot has no valid segmented checkpoint")
        session = Session.model_validate(self.payload.get("session"))
        validate_remote_compact_session(session)
        return session


class ProjectionClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    outbox_id: int
    project_id: UUID
    workspace_sequence: int = Field(gt=0)
    attempts: int = Field(gt=0)
    observations: list[ProjectionObservation]


class ProjectedArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: UUID
    schema_version: Literal["canonical_session_graph.measurements.v1"] = (
        "canonical_session_graph.measurements.v1"
    )
    payload: dict[str, Any]
    content_sha256: str
    source_vector: dict[str, dict[str, int]]
    observed_at: datetime


class ProjectionPublication(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    outbox_id: int
    worker_id: str
    project_id: UUID
    artifacts: list[ProjectedArtifact]


class SupabaseRpcClient:
    """Small PostgREST RPC transport shared by remote CT workers and readers."""

    def __init__(
        self, *, url: str, api_key: str, access_token: str, timeout: float = 20
    ) -> None:
        self._url = url.rstrip("/") + "/rest/v1/rpc/"
        self._headers = {
            "apikey": api_key,
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        self._timeout = timeout

    def call(self, name: str, request: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps({"request": request}, separators=(",", ":")).encode()
        http_request = urllib.request.Request(
            self._url + name,
            data=encoded,
            headers=self._headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                http_request, timeout=self._timeout
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RemoteControlPlaneError(
                f"remote control-plane {name} failed ({exc.code}): {detail}"
            ) from exc
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise RemoteControlPlaneError(
                f"remote control-plane {name} failed: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise RemoteControlPlaneError(
                f"remote control-plane {name} returned a non-object response"
            )
        return payload


def project_claim(claim: ProjectionClaim, *, worker_id: str) -> ProjectionPublication:
    """Deterministically assemble all source snapshots in a claimed project."""

    sessions: dict[UUID, tuple[Session, list[ProjectionObservation]]] = {}
    for observation in claim.observations:
        session = observation.compact_session()
        existing = sessions.get(session.session_id)
        if existing is not None:
            if existing[0] != session:
                raise RemoteControlPlaneError(
                    f"conflicting source snapshots for session {session.session_id}"
                )
            existing[1].append(observation)
            continue
        sessions[session.session_id] = (session, [observation])

    graphs = assemble_project_session_graphs(
        str(claim.project_id), [entry[0] for entry in sessions.values()]
    )
    artifacts = [
        _projected_artifact(graph, sessions=sessions)
        for graph in sorted(graphs, key=lambda item: str(item.root_session_id))
    ]
    return ProjectionPublication(
        workspace_id=claim.workspace_id,
        outbox_id=claim.outbox_id,
        worker_id=worker_id,
        project_id=claim.project_id,
        artifacts=artifacts,
    )


def _projected_artifact(
    graph: SessionGraph,
    *,
    sessions: dict[UUID, tuple[Session, list[ProjectionObservation]]],
) -> ProjectedArtifact:
    for edge in graph.edges:
        if edge.metadata is not None and (
            set(edge.metadata) != {"tool_name"}
            or not isinstance(edge.metadata["tool_name"], str)
            or not edge.metadata["tool_name"]
        ):
            raise RemoteControlPlaneError(
                "remote graph edge retained metadata other than canonical tool_name"
            )
    payload = graph.model_dump(mode="json")
    encoded = canonical_json(payload).encode()
    observations = [
        observation
        for session in graph.sessions
        for observation in sessions[session.session_id][1]
    ]
    return ProjectedArtifact(
        artifact_id=graph.root_session_id,
        payload=payload,
        content_sha256=hashlib.sha256(encoded).hexdigest(),
        source_vector={
            str(item.source_id): {
                "source_epoch": item.source_epoch,
                "source_sequence": item.source_sequence,
            }
            for item in sorted(observations, key=lambda value: str(value.source_id))
        },
        observed_at=max(item.observed_at for item in observations),
    )


class ProjectionWorker:
    """Lease and project one remote outbox job at a time."""

    def __init__(self, *, client: SupabaseRpcClient, worker_id: str) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be empty")
        self._client = client
        self.worker_id = worker_id

    def run_once(self, *, lease_seconds: int = 120) -> bool:
        raw = self._client.call(
            "ct_projector_claim",
            {"worker_id": self.worker_id, "lease_seconds": lease_seconds},
        )
        if not raw:
            return False
        claim = ProjectionClaim.model_validate(raw)
        try:
            publication = project_claim(claim, worker_id=self.worker_id)
            self._client.call(
                "ct_projector_publish", publication.model_dump(mode="json")
            )
        except Exception as exc:
            self._client.call(
                "ct_projector_fail",
                {
                    "workspace_id": str(claim.workspace_id),
                    "outbox_id": claim.outbox_id,
                    "worker_id": self.worker_id,
                    "error": str(exc)[:1000],
                    "retry_seconds": min(3600, 2 ** min(10, claim.attempts)),
                },
            )
            raise
        return True


class SupabaseHistoricalRepository:
    """Load one immutable workspace snapshot for existing historical handlers."""

    def __init__(
        self,
        *,
        client: SupabaseRpcClient,
        workspace_id: UUID,
        snapshot_sequence: int | None = None,
    ) -> None:
        self._client = client
        self.workspace_id = workspace_id
        self.snapshot_sequence = snapshot_sequence
        self._store: DocumentStore | None = None

    def store_for(
        self, method: str, params: dict[str, Any]
    ) -> tuple[DocumentStore, str]:
        _require_compact_historical_scope(method, params)
        if self._store is None:
            request: dict[str, Any] = {"workspace_id": str(self.workspace_id)}
            if self.snapshot_sequence is not None:
                request["snapshot_sequence"] = self.snapshot_sequence
            raw = self._client.call("ct_historical_snapshot", request)
            if str(raw.get("workspace_id")) != str(self.workspace_id):
                raise RemoteControlPlaneError("historical snapshot workspace mismatch")
            sequence = raw.get("snapshot_sequence")
            artifacts = raw.get("artifacts")
            if not isinstance(sequence, int) or sequence < 0:
                raise RemoteControlPlaneError(
                    "historical snapshot has invalid sequence"
                )
            if not isinstance(artifacts, list):
                raise RemoteControlPlaneError(
                    "historical snapshot has no artifact list"
                )
            graphs = [
                SessionGraph.model_validate(item["payload"])
                for item in artifacts
                if isinstance(item, dict) and "payload" in item
            ]
            if len(graphs) != len(artifacts):
                raise RemoteControlPlaneError(
                    "historical snapshot contains invalid artifacts"
                )
            self.snapshot_sequence = sequence
            self._store = DocumentStore.from_session_graphs(graphs)
        return self._store, f"remote workspace snapshot {self.snapshot_sequence}"

    def metadata(self) -> dict[str, Any] | None:
        if self.snapshot_sequence is None:
            return None
        return {
            "workspace_id": str(self.workspace_id),
            "snapshot_sequence": self.snapshot_sequence,
            "source": "remote",
            "freshness": "authoritative",
            "content_scope": "compact",
        }


def _require_compact_historical_scope(method: str, params: dict[str, Any]) -> None:
    """Reject historical requests whose contract requires omitted private content."""

    if method == "session.search":
        raise RemoteControlPlaneError(
            "session.search is unavailable for compact remote snapshots because searchable content is not retained"
        )
    if method == "session.events":
        raise RemoteControlPlaneError(
            "session.events is unavailable for compact remote snapshots because full event content is not retained"
        )
    if method == "session.items" and params.get("include_content"):
        raise RemoteControlPlaneError(
            "session.items include_content=true is unavailable for compact remote snapshots"
        )
