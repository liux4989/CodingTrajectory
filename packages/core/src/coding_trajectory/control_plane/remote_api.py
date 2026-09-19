"""New-version direct remote API; never downloads or reconstructs graph facts."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import httpx

from coding_trajectory.contracts import service_contract
from coding_trajectory.contracts.prepared_api import (
    API_PROTOCOL,
    MAX_API_RESPONSE_BYTES,
    ViewIdentity,
)
from coding_trajectory.control_plane.prepared_api import encoded
from coding_trajectory.control_plane.remote import (
    RemoteControlPlaneError,
    cloudflare_endpoint,
)


class RemoteApiRepository:
    def __init__(self, *, url: str, access_token: str, workspace_id: UUID):
        self._url = cloudflare_endpoint(url).removesuffix("/v1/core") + "/v1/api"
        self._workspace_id = str(workspace_id)
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {access_token}"}, timeout=30
        )
        self._metadata: dict[str, Any] | None = None

    def close(self) -> None:
        self._client.close()

    def prepare_batch(self, requests: list[dict[str, Any]]) -> None:
        # Each response supplies its immutable view identity; clients carry it
        # explicitly to related calls rather than pinning unrelated workspace data.
        pass

    def metadata(self) -> dict[str, Any] | None:
        return self._metadata

    def response_for(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._metadata = None
        contract = service_contract(method)
        body = encoded(
            {
                "protocol": API_PROTOCOL,
                "id": None,
                "method": method,
                "method_version": contract.version,
                "params": params,
            }
        )
        if len(body) > 64 * 1024:
            raise RemoteControlPlaneError(
                "request_too_large", status=413, code="request_too_large"
            )
        try:
            with self._client.stream(
                "POST",
                self._url,
                content=body,
                headers={"Content-Type": "application/json"},
            ) as response:
                chunks = []
                length = 0
                for chunk in response.iter_bytes():
                    length += len(chunk)
                    if length > MAX_API_RESPONSE_BYTES:
                        raise RemoteControlPlaneError(
                            "remote response exceeds byte bound"
                        )
                    chunks.append(chunk)
                payload = json.loads(b"".join(chunks))
                if (
                    payload.get("protocol") != API_PROTOCOL
                    or payload.get("method") != method
                ):
                    raise RemoteControlPlaneError("unsupported remote API envelope")
                if response.status_code != 200 or not payload.get("ok"):
                    code = payload.get("error", {}).get("code", "method_failed")
                    raise RemoteControlPlaneError(
                        str(code), status=response.status_code, code=str(code)
                    )
        except (httpx.HTTPError, ValueError) as exc:
            raise RemoteControlPlaneError("remote API transport unavailable") from exc
        if payload.get("method_version") != contract.version:
            raise RemoteControlPlaneError(
                "unsupported_version", status=409, code="unsupported_version"
            )
        metadata = payload.get("meta") or {}
        if method != "living.sessions":
            identity = ViewIdentity.model_validate(metadata.get("identity"))
            if identity.workspace_id != self._workspace_id or params.get(
                "view_manifest_sha256"
            ) not in {None, identity.view_manifest_sha256}:
                raise RemoteControlPlaneError("remote view identity mismatch")
        self._metadata = metadata
        return contract.response_model.model_validate(payload["data"]).model_dump(
            mode="json"
        )

    __call__ = response_for
