"""Authenticated direct-API proxy for the shared CT application runtime."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import UUID

from coding_trajectory.contracts import service_contract
from coding_trajectory.contracts.prepared_api import (
    API_PROTOCOL,
    MAX_API_REQUEST_BYTES,
    MAX_API_RESPONSE_BYTES,
    ApiRequest,
)
from coding_trajectory.control_plane.authority import MethodAuthority
from coding_trajectory.control_plane.remote import RemoteControlPlaneError
from coding_trajectory.control_plane.remote_api import RemoteApiRepository
from coding_trajectory.runtime import ServiceRuntime


class RemoteRuntimeFactory:
    """Build request-scoped direct API clients without reconstructing facts."""

    def __init__(self, *, url: str, workspace_id: UUID) -> None:
        self._url = url
        self.workspace_id = workspace_id

    def repository(self, access_token: str) -> RemoteApiRepository:
        if not access_token:
            raise ValueError("access token must not be empty")
        return RemoteApiRepository(
            url=self._url, access_token=access_token, workspace_id=self.workspace_id
        )

    def build(
        self, access_token: str, *, current_dir: Path | None = None
    ) -> ServiceRuntime:
        return ServiceRuntime(
            **self.runtime_options(access_token, current_dir=current_dir)
        )

    def runtime_options(
        self, access_token: str, *, current_dir: Path | None = None
    ) -> dict[str, Any]:
        historical = self.repository(access_token)
        return {
            "global_scope": True,
            "current_dir": current_dir or Path.cwd(),
            "historical_repository": historical,
            "authority_handlers": {
                MethodAuthority.PROJECT_INVENTORY: historical,
                MethodAuthority.LIVING: historical,
            },
            "transport_metadata": historical.metadata,
        }


def serve_http(
    *, factory: RemoteRuntimeFactory, host: str = "127.0.0.1", port: int = 8765
) -> None:
    build_http_server(factory=factory, host=host, port=port).serve_forever()


def build_http_server(
    *, factory: RemoteRuntimeFactory, host: str = "127.0.0.1", port: int = 8765
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "CodingTrajectory/1"

        def do_POST(self) -> None:
            request_id = method = version = None
            try:
                authorization = self.headers.get("Authorization", "")
                token = (
                    authorization.removeprefix("Bearer ").strip()
                    if authorization.startswith("Bearer ")
                    else ""
                )
                if not token:
                    raise RemoteControlPlaneError(
                        "bearer token required",
                        status=401,
                        code="authentication_required",
                    )
                if self.path != "/v1/api":
                    raise RemoteControlPlaneError(
                        "not found", status=404, code="not_found"
                    )
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_API_REQUEST_BYTES:
                    raise RemoteControlPlaneError(
                        "request byte bound exceeded",
                        status=413,
                        code="request_too_large",
                    )
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise TypeError("request must be an object")
                request_id, method, version = (
                    body.get("id"),
                    body.get("method"),
                    body.get("method_version"),
                )
                if body.get("protocol") != API_PROTOCOL:
                    raise RemoteControlPlaneError(
                        "unsupported protocol", status=400, code="unsupported_version"
                    )
                request = ApiRequest.model_validate(body)
                contract = service_contract(request.method)
                if request.method_version != contract.version:
                    raise RemoteControlPlaneError(
                        "unsupported method version",
                        status=400,
                        code="unsupported_version",
                    )
                params = contract.request_model.model_validate(
                    request.params
                ).model_dump(mode="json")
                repository = factory.repository(token)
                try:
                    data = repository.response_for(request.method, params)
                    metadata = repository.metadata()
                finally:
                    repository.close()
                payload = {
                    "protocol": API_PROTOCOL,
                    "id": request_id,
                    "method": method,
                    "method_version": version,
                    "ok": True,
                    "data": data,
                    "availability": {"state": "complete", "missing": []},
                    "error": None,
                    "meta": metadata,
                }
                if (
                    len(json.dumps(payload, separators=(",", ":")).encode())
                    > MAX_API_RESPONSE_BYTES
                ):
                    raise RemoteControlPlaneError(
                        "response byte bound exceeded",
                        status=413,
                        code="remote_result_too_large",
                    )
                self._write(200, payload)
                return
            except RemoteControlPlaneError as exc:
                status, code, message = (
                    exc.status or 502,
                    exc.code or "authority_unavailable",
                    str(exc),
                )
            except (KeyError, TypeError, ValueError) as exc:
                status, code, message = 400, "invalid_request", str(exc)
            except Exception:  # noqa: BLE001 - HTTP boundary
                status, code, message = (
                    502,
                    "authority_unavailable",
                    "authority unavailable",
                )
            self._write(
                status,
                {
                    "protocol": API_PROTOCOL,
                    "id": request_id,
                    "method": method,
                    "method_version": version,
                    "ok": False,
                    "data": None,
                    "availability": {
                        "state": "unsupported"
                        if code == "unsupported_version"
                        else "unavailable",
                        "missing": [{"field": "$", "reason": code}],
                    },
                    "error": {"code": code, "message": message[:500]},
                    "meta": None,
                },
            )

        def _write(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, separators=(",", ":"), default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)


__all__ = ["RemoteRuntimeFactory", "build_http_server", "serve_http"]
