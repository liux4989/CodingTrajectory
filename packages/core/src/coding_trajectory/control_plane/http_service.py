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
from coding_trajectory.contracts.envelope import CORE_PROTOCOL
from coding_trajectory.control_plane.authority import MethodAuthority
from coding_trajectory.control_plane.remote import (
    CloudflareHistoricalRepository,
    CloudflareRpcClient,
)
from coding_trajectory.control_plane.remote_catalog import CloudflareCatalogRepository
from coding_trajectory.control_plane.remote_estimation import RemoteEstimationAuthority
from coding_trajectory.control_plane.remote_inventory import (
    CloudflareProjectInventoryRepository,
)
from coding_trajectory.control_plane.remote_living import CloudflareLivingAuthority
from coding_trajectory.runtime import HistoricalRepository, ServiceRuntime


class RemoteRuntimeFactory:
    """Build a request-scoped runtime pinned to one remote workspace sequence."""

    def __init__(self, *, url: str, workspace_id: UUID) -> None:
        self._url = url
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
        client = CloudflareRpcClient(url=self._url, access_token=access_token)
        catalog = None
        if snapshot_sequence is None:
            catalog = CloudflareCatalogRepository(
                client=client, workspace_id=self.workspace_id
            )
            # Select the catalog heads and the separate compatibility/estimation
            # fence in one authority transaction, not two racing metadata reads.
            selected = catalog.page(kind="status").selection
            sequence = selected.workspace_sequence
        else:
            pinned = client.call(
                "ct_workspace_snapshot",
                {
                    "workspace_id": str(self.workspace_id),
                    "snapshot_sequence": snapshot_sequence,
                },
            )
            sequence = pinned.get("snapshot_sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("remote workspace returned an invalid snapshot sequence")
        historical: HistoricalRepository = CloudflareHistoricalRepository(
            client=client,
            workspace_id=self.workspace_id,
            snapshot_sequence=sequence,
            catalog=catalog,
        )
        if local_evidence:
            from coding_trajectory.control_plane.local_evidence import (
                LocalEvidenceRepository,
            )

            historical = LocalEvidenceRepository(
                historical, current_dir=current_dir or Path.cwd()
            )
        inventory = CloudflareProjectInventoryRepository(
            client=client,
            workspace_id=self.workspace_id,
            snapshot_sequence=sequence,
        )
        living = CloudflareLivingAuthority(
            client=client,
            workspace_id=self.workspace_id,
            # Living pagination carries its own snapshot in `through`. Only an
            # explicitly pinned caller should force the workspace sequence;
            # otherwise a publication between pages would invalidate the
            # previous page's cursor.
            snapshot_sequence=snapshot_sequence,
        )
        handlers: dict[MethodAuthority, Callable[..., Any]] = {
            MethodAuthority.PROJECT_INVENTORY: catalog.call if catalog else inventory,
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
            "content_scope": "chronicle",
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
                self._write(
                    HTTPStatus.UNAUTHORIZED,
                    self._error(
                        None, None, "authentication_required", "bearer token required"
                    ),
                )
                return
            request_id: Any = None
            method: Any = None
            try:
                body = self._body()
                if self.path != "/v1/core":
                    self._write(
                        HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}}
                    )
                    return
                if set(body) - {
                    "protocol",
                    "id",
                    "method",
                    "params",
                    "snapshot_sequence",
                }:
                    raise ValueError("request contains unknown fields")
                if body.get("protocol") != CORE_PROTOCOL:
                    raise ValueError(f"protocol must be {CORE_PROTOCOL}")
                request_id = body.get("id")
                method = body.get("method")
                params = body.get("params")
                if not isinstance(method, str) or not method:
                    raise ValueError("method is required")
                if not isinstance(params, dict):
                    raise TypeError("params must be an object")
                snapshot = body.get("snapshot_sequence")
                with factory.build(token, snapshot_sequence=snapshot) as runtime:
                    if method == "core.batch":
                        if set(params) != {"requests"}:
                            raise ValueError(
                                "core.batch params must contain only requests"
                            )
                        requests = params.get("requests")
                        if not isinstance(requests, list):
                            raise ValueError("requests must be an array")
                        batch = runtime.batch(requests)
                        data = {
                            "items": [
                                self._runtime_item(item)
                                for item in batch.get("items", [])
                            ]
                        }
                    elif method == "core.schema":
                        if set(params) != {"method"} or not isinstance(
                            params.get("method"), str
                        ):
                            raise ValueError("core.schema params require method")
                        target = params["method"]
                        data = command_schema(target, command=f"ct api call {target}")
                    else:
                        executed = runtime.execute(
                            {"id": request_id, "method": method, "params": params}
                        )
                        if not executed.get("ok"):
                            message = str(
                                executed.get("error", {}).get("message")
                                or "core method failed"
                            )
                            self._write(
                                HTTPStatus.BAD_REQUEST,
                                self._error(
                                    request_id, method, "method_failed", message
                                ),
                            )
                            return
                        data = executed.get("result")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                self._write(
                    HTTPStatus.BAD_REQUEST,
                    self._error(request_id, method, "invalid_request", str(exc)),
                )
                return
            except Exception as exc:  # noqa: BLE001 - HTTP process boundary
                self._write(
                    HTTPStatus.BAD_GATEWAY,
                    self._error(
                        request_id, method, "authority_unavailable", str(exc)[:500]
                    ),
                )
                return
            self._write(
                HTTPStatus.OK,
                {
                    "protocol": CORE_PROTOCOL,
                    "id": request_id,
                    "method": method,
                    "ok": True,
                    "data": data,
                    "availability": {"state": "complete", "missing": []},
                    "error": None,
                    "meta": runtime.transport_metadata(),
                },
            )

        def _error(
            self, request_id: Any, method: Any, code: str, message: str
        ) -> dict[str, Any]:
            return {
                "protocol": CORE_PROTOCOL,
                "id": request_id,
                "method": method,
                "ok": False,
                "data": None,
                "availability": {
                    "state": "unavailable",
                    "missing": [{"field": "$", "reason": code}],
                },
                "error": {"code": code, "message": message},
                "meta": None,
            }

        def _runtime_item(self, item: dict[str, Any]) -> dict[str, Any]:
            if item.get("ok"):
                return {
                    "protocol": CORE_PROTOCOL,
                    "id": item.get("id"),
                    "method": item.get("method"),
                    "ok": True,
                    "data": item.get("result"),
                    "availability": {"state": "complete", "missing": []},
                    "error": None,
                    "meta": item.get("meta"),
                }
            message = str(item.get("error", {}).get("message") or "core method failed")
            return self._error(
                item.get("id"), item.get("method"), "method_failed", message
            )

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
