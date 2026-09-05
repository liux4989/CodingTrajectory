"""Authenticated HTTP transport for the shared CT application runtime."""

from __future__ import annotations

import json
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import UUID

from coding_trajectory.contracts import command_schema
from coding_trajectory.control_plane.authority import MethodAuthority
from coding_trajectory.control_plane.remote import (
    SupabaseHistoricalRepository,
    SupabaseRpcClient,
)
from coding_trajectory.control_plane.remote_estimation import RemoteEstimationAuthority
from coding_trajectory.control_plane.remote_inventory import (
    SupabaseProjectInventoryRepository,
)
from coding_trajectory.control_plane.remote_living import SupabaseLivingAuthority
from coding_trajectory.runtime import HistoricalRepository, ServiceRuntime


class RemoteRuntimeFactory:
    """Build a request-scoped runtime pinned to one remote workspace sequence."""

    def __init__(self, *, url: str, api_key: str, workspace_id: UUID) -> None:
        self._url = url
        self._api_key = api_key
        self.workspace_id = workspace_id

    def build(
        self,
        access_token: str,
        *,
        snapshot_sequence: int | None = None,
        local_evidence: bool = False,
        current_dir: Path | None = None,
    ) -> ServiceRuntime:
        return ServiceRuntime(
            **self.runtime_options(
                access_token,
                snapshot_sequence=snapshot_sequence,
                local_evidence=local_evidence,
                current_dir=current_dir,
            )
        )

    def runtime_options(
        self,
        access_token: str,
        *,
        snapshot_sequence: int | None = None,
        local_evidence: bool = False,
        current_dir: Path | None = None,
    ) -> dict[str, Any]:
        """Resolve the same database authorities for every client surface."""
        if not access_token:
            raise ValueError("access token must not be empty")
        if snapshot_sequence is not None and (
            isinstance(snapshot_sequence, bool)
            or not isinstance(snapshot_sequence, int)
            or snapshot_sequence < 0
        ):
            raise ValueError("snapshot_sequence must be a non-negative integer")
        client = SupabaseRpcClient(
            url=self._url, api_key=self._api_key, access_token=access_token
        )
        request: dict[str, Any] = {"workspace_id": str(self.workspace_id)}
        if snapshot_sequence is not None:
            request["snapshot_sequence"] = snapshot_sequence
        pinned = client.call("ct_workspace_snapshot", request)
        sequence = pinned.get("snapshot_sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("remote workspace returned an invalid snapshot sequence")
        historical: HistoricalRepository = SupabaseHistoricalRepository(
            client=client,
            workspace_id=self.workspace_id,
            snapshot_sequence=sequence,
        )
        if local_evidence:
            from coding_trajectory.control_plane.local_evidence import (
                LocalEvidenceRepository,
            )

            historical = LocalEvidenceRepository(
                historical, current_dir=current_dir or Path.cwd()
            )
        inventory = SupabaseProjectInventoryRepository(
            client=client,
            workspace_id=self.workspace_id,
            snapshot_sequence=sequence,
        )
        living = SupabaseLivingAuthority(
            client=client,
            workspace_id=self.workspace_id,
            snapshot_sequence=sequence,
        )
        handlers: dict[MethodAuthority, Callable[..., Any]] = {
            MethodAuthority.PROJECT_INVENTORY: inventory,
            MethodAuthority.LIVING: living,
            MethodAuthority.ESTIMATION: RemoteEstimationAuthority(
                client=client,
                workspace_id=self.workspace_id,
                snapshot_sequence=sequence,
            ),
        }
        metadata = {
            "workspace_id": str(self.workspace_id),
            "snapshot_sequence": sequence,
            "source": "remote",
            "freshness": "authoritative",
            "content_scope": "shareable",
        }
        return {
            "global_scope": True,
            "current_dir": current_dir or Path.cwd(),
            "historical_repository": historical,
            "authority_handlers": handlers,
            "transport_metadata": lambda: metadata,
        }


def serve_http(
    *, factory: RemoteRuntimeFactory, host: str = "127.0.0.1", port: int = 8765
) -> None:
    """Serve authenticated call, batch, and schema endpoints until interrupted."""

    build_http_server(factory=factory, host=host, port=port).serve_forever()


def build_http_server(
    *, factory: RemoteRuntimeFactory, host: str = "127.0.0.1", port: int = 8765
) -> ThreadingHTTPServer:
    """Build the HTTP server, allowing an owning process to manage its lifecycle."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "CodingTrajectory/1"

        def do_POST(self) -> None:
            token = self._bearer_token()
            if token is None:
                self._write(HTTPStatus.UNAUTHORIZED, {"error": "bearer token required"})
                return
            try:
                body = self._body()
                snapshot = body.pop("snapshot_sequence", None)
                with factory.build(token, snapshot_sequence=snapshot) as runtime:
                    if self.path == "/v1/call":
                        result = runtime.execute(body)
                    elif self.path == "/v1/batch":
                        requests = body.get("requests")
                        if not isinstance(requests, list):
                            raise ValueError("requests must be an array")
                        result = runtime.batch(requests)
                    elif self.path == "/v1/schema":
                        method = body.get("method")
                        if not isinstance(method, str):
                            raise ValueError("method is required")
                        result = command_schema(method, command=f"ct api call {method}")
                    else:
                        self._write(HTTPStatus.NOT_FOUND, {"error": "not found"})
                        return
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                self._write(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001 - HTTP process boundary
                self._write(HTTPStatus.BAD_GATEWAY, {"error": str(exc)[:500]})
                return
            self._write(HTTPStatus.OK, result)

        def _bearer_token(self) -> str | None:
            value = self.headers.get("Authorization", "")
            prefix = "Bearer "
            token = value[len(prefix) :].strip() if value.startswith(prefix) else ""
            return token or None

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 1_000_000:
                raise ValueError("request body must be between 1 byte and 1 MB")
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise TypeError("request body must be an object")
            return value

        def _write(self, status: HTTPStatus, payload: Any) -> None:
            encoded = json.dumps(payload, separators=(",", ":"), default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)


__all__ = ["RemoteRuntimeFactory", "build_http_server", "serve_http"]
