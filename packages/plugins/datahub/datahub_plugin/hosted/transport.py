"""Bounded asynchronous transport for the hosted Datahub facade."""

from __future__ import annotations

import json
from typing import Any

import httpx
from coding_trajectory.control_plane.remote import RemoteControlPlaneError

from datahub_plugin.hosted.service import MAX_REMOTE_RESPONSE_BYTES


class SupabaseAsyncRpcClient:
    """Call PostgREST RPCs without persisting or logging caller credentials."""

    def __init__(
        self, *, url: str, api_key: str, access_token: str, timeout_seconds: float = 20
    ) -> None:
        self._base_url = url.rstrip("/") + "/rest/v1/rpc/"
        self._headers = {
            "apikey": api_key,
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    async def close(self) -> None:
        await self._client.aclose()

    async def call(self, name: str, request: dict[str, Any]) -> dict[str, Any]:
        try:
            async with self._client.stream(
                "POST",
                self._base_url + name,
                headers=self._headers,
                json={"request": request},
            ) as response:
                if response.status_code != 200:
                    raise RemoteControlPlaneError(
                        f"remote control-plane {name} failed with status {response.status_code}"
                    )
                declared = response.headers.get("content-length")
                if declared is not None and int(declared) > MAX_REMOTE_RESPONSE_BYTES:
                    raise RemoteControlPlaneError("remote response exceeds hosted bound")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_REMOTE_RESPONSE_BYTES:
                        raise RemoteControlPlaneError("remote response exceeds hosted bound")
                    chunks.append(chunk)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            if isinstance(exc, RemoteControlPlaneError):
                raise
            raise RemoteControlPlaneError(
                f"remote control-plane {name} transport failed"
            ) from exc
        try:
            payload = json.loads(b"".join(chunks))
        except json.JSONDecodeError as exc:
            raise RemoteControlPlaneError(
                f"remote control-plane {name} returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise RemoteControlPlaneError(
                f"remote control-plane {name} returned a non-object response"
            )
        return payload


__all__ = ["SupabaseAsyncRpcClient"]
