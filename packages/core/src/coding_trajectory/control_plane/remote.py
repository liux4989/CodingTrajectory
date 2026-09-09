"""Supabase-backed historical snapshot repository and RPC transport."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any
from uuid import UUID

import httpx
from pydantic import ValidationError

from coding_trajectory.contracts import service_contract
from coding_trajectory.control_plane.chronicle import ChronicleGraphArtifact
from coding_trajectory.ingestion.common import canonical_json, format_datetime
from coding_trajectory.ingestion.models import SessionGraph
from coding_trajectory.query import DocumentError, DocumentStore


class RemoteControlPlaneError(DocumentError):
    """A remote control-plane operation failed or violated its contract."""


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
        self._client = httpx.Client(headers=self._headers, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def call(self, name: str, request: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.post(
                self._url + name,
                json={"request": request},
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500]
            raise RemoteControlPlaneError(
                f"remote control-plane {name} failed "
                f"({exc.response.status_code}): {detail}"
            ) from exc
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise RemoteControlPlaneError(
                f"remote control-plane {name} failed: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise RemoteControlPlaneError(
                f"remote control-plane {name} returned a non-object response"
            )
        return payload


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
        self._stores: dict[str, DocumentStore] = {}

    def close(self) -> None:
        self._client.close()

    def response_for(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Serve complete versioned projections without loading graph artifacts."""

        if method != "project.sessions":
            return None
        validated = service_contract(method).validate_request(params)
        request: dict[str, Any] = {
            "workspace_id": str(self.workspace_id),
            **validated,
        }
        if self.snapshot_sequence is not None:
            request["snapshot_sequence"] = self.snapshot_sequence
        if "modified_since" in request:
            request["modified_since"] = format_datetime(request["modified_since"])
        raw = self._client.call("ct_project_sessions_projection", request)
        sequence = raw.get("snapshot_sequence")
        if str(raw.get("workspace_id")) != str(self.workspace_id):
            raise RemoteControlPlaneError("project sessions workspace mismatch")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise RemoteControlPlaneError("project sessions snapshot is invalid")
        if self.snapshot_sequence is not None and sequence != self.snapshot_sequence:
            raise RemoteControlPlaneError("project sessions snapshot mismatch")
        self.snapshot_sequence = sequence
        if not raw.get("complete"):
            return None
        result = raw.get("result")
        if not isinstance(result, dict):
            raise RemoteControlPlaneError("project sessions projection is invalid")
        return service_contract(method).validate_response(result)

    def pin_snapshot(self) -> int:
        """Pin the workspace sequence without loading historical artifacts."""

        if self.snapshot_sequence is None:
            raw = self._client.call(
                "ct_historical_snapshot",
                {
                    "workspace_id": str(self.workspace_id),
                    "metadata_only": True,
                },
            )
            sequence = raw.get("snapshot_sequence")
            artifacts = raw.get("artifacts")
            if (
                isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or sequence < 0
                or artifacts != []
            ):
                raise RemoteControlPlaneError(
                    "historical snapshot metadata response is invalid"
                )
            self.snapshot_sequence = sequence
        return self.snapshot_sequence

    def store_for(
        self, method: str, params: dict[str, Any]
    ) -> tuple[DocumentStore, str]:
        _require_chronicle_historical_scope(method, params)
        validated = service_contract(method).validate_request(params)
        request = _historical_snapshot_request(
            workspace_id=self.workspace_id,
            method=method,
            params=validated,
        )
        key = canonical_json(request)
        if key not in self._stores:
            if self.snapshot_sequence is not None:
                request["snapshot_sequence"] = self.snapshot_sequence
            raw = self._client.call("ct_historical_artifacts", request)
            if str(raw.get("workspace_id")) != str(self.workspace_id):
                raise RemoteControlPlaneError("historical snapshot workspace mismatch")
            sequence = raw.get("snapshot_sequence")
            artifacts = raw.get("artifacts")
            if (
                isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or sequence < 0
            ):
                raise RemoteControlPlaneError(
                    "historical snapshot has invalid sequence"
                )
            if not isinstance(artifacts, list):
                raise RemoteControlPlaneError(
                    "historical snapshot has no artifact list"
                )
            graphs = [
                _snapshot_artifact_graph(_decode_artifact(item)) for item in artifacts
            ]
            if (
                self.snapshot_sequence is not None
                and sequence != self.snapshot_sequence
            ):
                raise RemoteControlPlaneError("historical snapshot sequence mismatch")
            self.snapshot_sequence = sequence
            self._stores[key] = DocumentStore.from_session_graphs(graphs)
        return self._stores[key], f"remote workspace snapshot {self.snapshot_sequence}"

    def metadata(self) -> dict[str, Any] | None:
        if self.snapshot_sequence is None:
            return None
        return {
            "workspace_id": str(self.workspace_id),
            "snapshot_sequence": self.snapshot_sequence,
            "source": "remote",
            "freshness": "authoritative",
            "content_scope": "chronicle",
        }


def _historical_snapshot_request(
    *, workspace_id: UUID, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "workspace_id": str(workspace_id),
        "method": method,
    }
    resource_ids = [
        value
        for key in ("session_id", "root_session_id", "turn_id")
        if isinstance((value := params.get(key)), str) and value
    ]
    item_ids = params.get("item_ids")
    if isinstance(item_ids, list):
        resource_ids.extend(value for value in item_ids if isinstance(value, str))
    if resource_ids:
        request["resource_ids"] = list(dict.fromkeys(resource_ids))
    for key in ("project_name", "agent_vendor", "since_days"):
        if key in params:
            request[key] = params[key]
    if "modified_since" in params:
        request["modified_since"] = format_datetime(params["modified_since"])
    return request


def _snapshot_artifact_graph(value: Any) -> SessionGraph:
    if not isinstance(value, dict) or "payload" not in value:
        raise RemoteControlPlaneError("historical snapshot contains invalid artifacts")
    try:
        artifact = ChronicleGraphArtifact.model_validate(value["payload"])
    except ValidationError as exc:
        raise RemoteControlPlaneError(
            "historical snapshot artifact schema is invalid"
        ) from exc
    if str(value.get("artifact_id")) != str(artifact.graph.root_session_id):
        raise RemoteControlPlaneError("historical snapshot artifact identity mismatch")
    if value.get("content_sha256") != artifact.digest():
        raise RemoteControlPlaneError("historical snapshot artifact digest mismatch")
    return artifact.to_session_graph()


def _decode_artifact(value: Any) -> Any:
    """Decode a bounded content-addressed artifact, retaining legacy fallback."""

    if not isinstance(value, dict) or value.get("payload_encoding") != "zstd":
        return value
    # The Cloudflare Python facade imports this module but serves collection
    # projections and legacy detail fallback; native Zstandard is intentionally
    # absent from its Emscripten package set.
    import zstandard

    encoded = value.get("payload_base64")
    expected_digest = value.get("content_sha256")
    expected_bytes = value.get("uncompressed_bytes")
    if (
        not isinstance(encoded, str)
        or not isinstance(expected_digest, str)
        or isinstance(expected_bytes, bool)
        or not isinstance(expected_bytes, int)
        or expected_bytes < 1
        or expected_bytes > 8 * 1024 * 1024
    ):
        raise RemoteControlPlaneError("compressed historical artifact is invalid")
    try:
        compressed = base64.b64decode(encoded, validate=True)
        canonical = zstandard.ZstdDecompressor().decompress(
            compressed,
            max_output_size=expected_bytes,
        )
        payload = json.loads(canonical)
    except (ValueError, zstandard.ZstdError, json.JSONDecodeError) as exc:
        raise RemoteControlPlaneError(
            "compressed historical artifact could not be decoded"
        ) from exc
    if len(canonical) != expected_bytes:
        raise RemoteControlPlaneError("compressed historical artifact size mismatch")
    if hashlib.sha256(canonical).hexdigest() != expected_digest:
        raise RemoteControlPlaneError("compressed historical artifact digest mismatch")
    result = dict(value)
    result.pop("payload_base64", None)
    result["payload"] = payload
    return result


def _require_chronicle_historical_scope(method: str, params: dict[str, Any]) -> None:
    """Reject requests whose contract requires host-local evidence bodies."""

    if method == "graph.overview" and "narrative" in params.get("include", []):
        raise RemoteControlPlaneError("graph.overview narrative is local-only")

    if method == "session.search":
        raise RemoteControlPlaneError(
            "session.search is local-only because searchable evidence is not retained remotely"
        )
    if method == "session.events":
        raise RemoteControlPlaneError(
            "session.events is local-only because event evidence is not retained remotely"
        )
    if method == "session.items" and params.get("include_content"):
        raise RemoteControlPlaneError(
            "session.items include_content=true is local-only"
        )
