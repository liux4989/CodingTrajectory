"""Cloudflare RPC transport shared by remote CT collectors and readers."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from coding_trajectory.query import DocumentError


class RemoteControlPlaneError(DocumentError):
    """A remote control-plane operation failed or violated its contract."""

    def __init__(
        self, message: str, *, status: int | None = None, code: str | None = None
    ):
        super().__init__(message)
        self.status = status
        self.code = code


def _remote_error_code(response: httpx.Response) -> str | None:
    """Keep only a bounded machine code from a failed Core envelope."""
    if len(response.content) > 16 * 1024:
        return None
    try:
        payload = response.json()
    except (ValueError, UnicodeError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("protocol") != "ct.core.v1"
        or payload.get("ok") is not False
        or not isinstance(payload.get("error"), dict)
    ):
        return None
    code = payload["error"].get("code")
    if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code):
        return code
    return None


def cloudflare_endpoint(url: str) -> str:
    """Require TLS except for an explicit loopback qualification endpoint."""
    parsed = urlsplit(url)
    local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (
        (parsed.scheme != "https" and not (local and parsed.scheme == "http"))
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("Cloudflare endpoint must be an HTTPS origin")
    return url.rstrip("/") + "/v1/core"


class CloudflareRpcClient:
    """Small Cloudflare RPC transport shared by remote CT workers and readers."""

    def __init__(self, *, url: str, access_token: str, timeout: float = 20) -> None:
        self._url = cloudflare_endpoint(url)
        self._headers = {
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
                self._url,
                json={
                    "protocol": "ct.core.v1",
                    "id": None,
                    "method": name,
                    "params": request,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            code = _remote_error_code(exc.response)
            detail = f"; {code}" if code else ""
            raise RemoteControlPlaneError(
                f"remote control-plane {name} failed ({exc.response.status_code}{detail})",
                status=exc.response.status_code,
                code=code,
            ) from None
        except (httpx.HTTPError, json.JSONDecodeError):
            raise RemoteControlPlaneError(
                f"remote control-plane {name} transport unavailable"
            ) from None
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise RemoteControlPlaneError(
                f"remote control-plane {name} returned an invalid envelope"
            )
        result = payload.get("data")
        if not isinstance(result, dict):
            raise RemoteControlPlaneError(
                f"remote control-plane {name} returned non-object data"
            )
        return result


__all__ = [
    "CloudflareRpcClient",
    "RemoteControlPlaneError",
    "cloudflare_endpoint",
]
