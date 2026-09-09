"""Bounded asynchronous transport for the hosted Datahub facade."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import httpx
from coding_trajectory.control_plane.remote import RemoteControlPlaneError

from datahub_plugin.hosted.service import MAX_REMOTE_RESPONSE_BYTES

_MAX_AUTH_RESPONSE_BYTES = 32 * 1024
_MAX_ACCESS_TOKEN_BYTES = 8192
_TOKEN_EXPIRY_BUFFER_SECONDS = 60
_cached_access_token: str | None = None
_cached_access_token_until = 0.0
_cached_reader_identity: bytes | None = None


class SupabaseAsyncRpcClient:
    """Call PostgREST RPCs as the configured bounded reader principal."""

    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        reader_email: str,
        reader_password: str,
        timeout_seconds: float = 20,
    ) -> None:
        self._origin = url.rstrip("/")
        self._base_url = self._origin + "/rest/v1/rpc/"
        self._api_key = api_key
        self._reader_email = reader_email
        self._reader_password = reader_password
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    async def close(self) -> None:
        await self._client.aclose()

    async def call(self, name: str, request: dict[str, Any]) -> dict[str, Any]:
        access_token = await self._access_token()
        response = await self._rpc_response(name, request, access_token)
        if response.status_code == 401:
            _invalidate_access_token(access_token)
            access_token = await self._access_token()
            response = await self._rpc_response(name, request, access_token)
        if response.status_code != 200:
            raise RemoteControlPlaneError(
                f"remote control-plane {name} failed with status {response.status_code}"
            )
        payload = _decode_object(response.content, operation=name)
        return payload

    async def _access_token(self) -> str:
        identity = _reader_identity(
            self._origin,
            self._api_key,
            self._reader_email,
            self._reader_password,
        )
        access_token = _valid_cached_access_token(identity)
        if access_token is not None:
            return access_token
        # Do not await process-global locks or tasks here: workerd forbids one
        # request from waiting on async state created by another request. Two
        # concurrent cache misses may authenticate twice; both remain bounded,
        # and later requests reuse the last equivalent token.
        return await self._authenticate(identity)

    async def _authenticate(self, identity: bytes) -> str:
        global _cached_access_token
        global _cached_access_token_until
        global _cached_reader_identity

        now = time.monotonic()
        url = self._origin + "/auth/v1/token?grant_type=password"
        try:
            response = await self._bounded_request(
                url,
                headers={
                    "apikey": self._api_key,
                    "Content-Type": "application/json",
                },
                body={
                    "email": self._reader_email,
                    "password": self._reader_password,
                },
                limit=_MAX_AUTH_RESPONSE_BYTES,
                operation="reader authentication",
            )
        except RemoteControlPlaneError:
            raise
        except Exception as exc:
            raise RemoteControlPlaneError(
                "reader authentication transport failed"
            ) from exc
        if response.status_code != 200:
            raise RemoteControlPlaneError(
                f"reader authentication failed with status {response.status_code}"
            )
        payload = _decode_object(response.content, operation="reader authentication")
        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if (
            not isinstance(access_token, str)
            or not access_token
            or len(access_token) > _MAX_ACCESS_TOKEN_BYTES
            or not isinstance(expires_in, (int, float))
            or isinstance(expires_in, bool)
            or expires_in <= 0
        ):
            raise RemoteControlPlaneError(
                "reader authentication returned an invalid session"
            )
        _cached_reader_identity = identity
        _cached_access_token = access_token
        _cached_access_token_until = now + max(
            0, float(expires_in) - _TOKEN_EXPIRY_BUFFER_SECONDS
        )
        return access_token

    async def _rpc_response(
        self, name: str, request: dict[str, Any], access_token: str
    ) -> httpx.Response:
        try:
            return await self._bounded_request(
                self._base_url + name,
                headers={
                    "apikey": self._api_key,
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                body={"request": request},
                limit=MAX_REMOTE_RESPONSE_BYTES,
                operation=name,
            )
        except RemoteControlPlaneError:
            raise
        except Exception as exc:
            raise RemoteControlPlaneError(
                f"remote control-plane {name} transport failed"
            ) from exc

    async def _bounded_request(
        self,
        url: str,
        *,
        headers: dict[str, str],
        body: dict[str, Any],
        limit: int,
        operation: str,
    ) -> httpx.Response:
        async with self._client.stream(
            "POST", url, headers=headers, json=body
        ) as response:
            declared = response.headers.get("content-length")
            if declared is not None and int(declared) > limit:
                raise RemoteControlPlaneError(
                    f"{operation} response exceeds hosted bound"
                )
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > limit:
                    raise RemoteControlPlaneError(
                        f"{operation} response exceeds hosted bound"
                    )
                chunks.append(chunk)
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=b"".join(chunks),
            )


def _invalidate_access_token(access_token: str) -> None:
    global _cached_access_token
    global _cached_access_token_until
    if _cached_access_token == access_token:
        _cached_access_token = None
        _cached_access_token_until = 0.0


def _reader_identity(
    origin: str, api_key: str, reader_email: str, reader_password: str
) -> bytes:
    digest = hashlib.sha256()
    for value in (origin, api_key, reader_email, reader_password):
        encoded = value.encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.digest()


def _valid_cached_access_token(identity: bytes) -> str | None:
    if (
        _cached_reader_identity == identity
        and _cached_access_token is not None
        and time.monotonic() < _cached_access_token_until
    ):
        return _cached_access_token
    return None


def _decode_object(content: bytes, *, operation: str) -> dict[str, Any]:
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RemoteControlPlaneError(f"{operation} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RemoteControlPlaneError(f"{operation} returned a non-object response")
    return payload


__all__ = ["SupabaseAsyncRpcClient"]
