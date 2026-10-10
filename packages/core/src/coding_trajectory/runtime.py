"""Local execution of versioned queries over lazily retained canonical runs."""

from __future__ import annotations

import atexit
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, Self

from pydantic import ValidationError

from coding_trajectory.contracts import SERVICE_CONTRACTS, service_contract
from coding_trajectory.contracts.envelope import (
    API_PROTOCOL,
    ApiTransportMetadata,
)
from coding_trajectory.query import DocumentError, DocumentStore, ResourceNotFoundError
from coding_trajectory.service import IndexCache, dispatch, resolve_store
from coding_trajectory.service.pagination import LocalQueryError

# One capability declaration, shared by explicit source selection and callers.
# Remote adapters are intentionally unavailable for this contract revision.
METHOD_SOURCES = {method: frozenset({"local"}) for method in SERVICE_CONTRACTS}


class LocalSourceUnavailableError(DocumentError):
    """No supported local source exists on this host."""


class ServiceApiClient(Protocol):
    def call(self, method: str, params: Mapping[str, Any]) -> Any: ...

    def execute(self, request: Mapping[str, Any]) -> dict[str, Any]: ...


class PluginApiError(RuntimeError):
    """An in-process plugin query failed."""


class PluginApiClient:
    """Serialize plugin calls over one local runtime, without remote fallback."""

    def __init__(
        self, *, global_scope: bool = True, current_dir: Path | None = None
    ) -> None:
        self._global_scope = global_scope
        self._current_dir = current_dir or Path.cwd()
        self._lock = threading.Lock()
        self._runtime: ServiceRuntime | None = None

    def _get_runtime(self) -> ServiceRuntime:
        if self._runtime is None:
            self._runtime = ServiceRuntime(
                global_scope=self._global_scope, current_dir=self._current_dir
            )
        return self._runtime

    def call(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        try:
            with self._lock:
                return self._get_runtime().call(method, dict(params or {}))
        except (KeyError, ValueError, DocumentError, ResourceNotFoundError) as exc:
            raise PluginApiError(str(exc)) from exc

    def execute(self, request: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            return self._get_runtime().execute(dict(request))

    def close(self) -> None:
        with self._lock:
            if self._runtime is not None:
                self._runtime.close()
                self._runtime = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


_default_client: PluginApiClient | None = None
_default_client_lock = threading.Lock()


def default_plugin_client() -> PluginApiClient:
    global _default_client
    with _default_client_lock:
        if _default_client is None:
            _default_client = PluginApiClient()
            atexit.register(_default_client.close)
        return _default_client


class ServiceRuntime:
    """Refresh requested dependencies, then compute only requested methods."""

    def __init__(
        self, *, global_scope: bool, current_dir: Path, source: str = "local"
    ) -> None:
        self.global_scope = global_scope
        self.current_dir = current_dir
        self.source = "local" if source == "auto" else source
        self.cache = IndexCache()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.cache._reset()

    def _require_sources(self) -> None:
        from coding_trajectory.discovery import discover_source_candidates

        if not discover_source_candidates(
            current_dir=self.current_dir, global_scope=True
        ):
            raise LocalSourceUnavailableError(
                "no supported coding-agent source is available on this host"
            )

    def _validate(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        contract = service_contract(method)
        if self.source not in METHOD_SOURCES[method]:
            raise LocalQueryError("method_unavailable", 404)
        try:
            return contract.validate_request(params)
        except ValidationError as exc:
            if any(error["loc"] == ("cursor",) for error in exc.errors()):
                raise LocalQueryError("invalid_cursor") from exc
            raise

    def call(self, method: str, params: dict[str, Any]) -> Any:
        if self.cache._batch_mode:
            return self._call(method, params)
        self.cache._reset()
        try:
            return self._call(method, params)
        finally:
            self.cache._reset()

    def _call(self, method: str, params: dict[str, Any]) -> Any:
        params = self._validate(method, params)
        if method.startswith("living."):
            if method == "living.events":
                from coding_trajectory.living_events import serve_living_events

                result = serve_living_events(
                    params,
                    cache=self.cache,
                    current_dir=self.current_dir,
                    global_scope=self.global_scope,
                )
            else:
                from coding_trajectory.living_sessions import serve_living_sessions

                result = serve_living_sessions(
                    params, current_dir=self.current_dir, global_scope=self.global_scope
                )
            self._require_sources()
            return service_contract(method).validate_response(result)
        if method.startswith("project."):
            store, note = DocumentStore.from_session_graphs([]), "(source metadata)"
        else:
            store, note = resolve_store(
                params,
                global_scope=self.global_scope,
                current_dir=self.current_dir,
                cache=self.cache,
                selector="lineage" if method == "session.tree" else "run",
            )
        self._require_sources()
        return dispatch(
            method,
            params,
            store=store,
            global_scope=self.global_scope,
            current_dir=self.current_dir,
            discovery_note=note,
            cache=self.cache,
        )

    def transport_metadata(self) -> dict[str, Any]:
        return ApiTransportMetadata().model_dump()

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        method = request.get("method")
        response: dict[str, Any] = {
            "protocol": API_PROTOCOL,
            "id": request.get("id"),
            "method": method,
            "method_version": SERVICE_CONTRACTS[method].version
            if isinstance(method, str) and method in SERVICE_CONTRACTS
            else None,
            "ok": False,
            "result": None,
            "availability": {"state": "unavailable", "missing": []},
            "error": None,
        }
        try:
            if not isinstance(method, str) or not method:
                raise ValueError("method is required")
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise ValueError("params must be an object")  # noqa: TRY004 - public invalid_request
            if request.get("protocol", API_PROTOCOL) != API_PROTOCOL:
                raise LocalQueryError("unsupported_protocol")
            if (
                request.get("method_version", response["method_version"])
                != response["method_version"]
            ):
                raise LocalQueryError("unsupported_version", 409)
            result = self.call(method, params)
        except (KeyError, ValueError, DocumentError, ResourceNotFoundError) as exc:
            code = getattr(exc, "code", None) or (
                "resource_not_found"
                if isinstance(exc, ResourceNotFoundError)
                else "local_source_unavailable"
                if isinstance(exc, LocalSourceUnavailableError)
                else "unknown_method"
                if isinstance(exc, KeyError)
                else "invalid_request"
            )
            response["error"] = {"code": code, "message": str(exc)}
            if code in {"unknown_method", "method_unavailable"}:
                response["availability"]["state"] = "unsupported"
            return response
        response.update(
            ok=True,
            result=result,
            availability={"state": "complete", "missing": []},
            meta=self.transport_metadata(),
        )
        return response

    def prepare_batch(self, requests: list[dict[str, Any]]) -> None:
        """Load valid detail dependencies once; invalid items remain independent."""
        ids: set[str] = set()
        lineage_ids: set[str] = set()
        inventory = False
        for request in requests:
            method, params = request.get("method"), request.get("params", {})
            if not isinstance(method, str) or not isinstance(params, dict):
                continue
            if request.get("protocol", API_PROTOCOL) != API_PROTOCOL:
                continue
            contract = SERVICE_CONTRACTS.get(method)
            if (
                contract is None
                or request.get("method_version", contract.version) != contract.version
            ):
                continue
            try:
                params = self._validate(method, params)
            except (KeyError, ValueError):
                continue
            if method.startswith("project."):
                inventory = True
            if method.startswith(("session.", "graph.")):
                entrypoint = params.get("session_id") or params["root_session_id"]
                ids.add(entrypoint)
                if method == "session.tree":
                    lineage_ids.add(entrypoint)
        if inventory:
            from coding_trajectory.service.store import _refresh_topology

            _refresh_topology(self.cache, self.current_dir)
        if ids:
            resolve_store(
                {
                    "session_ids": sorted(ids),
                    "lineage_session_ids": sorted(lineage_ids),
                },
                global_scope=self.global_scope,
                current_dir=self.current_dir,
                cache=self.cache,
                selector="lineage" if lineage_ids else "run",
            )

    def batch(self, requests: list[dict[str, Any]]) -> dict[str, Any]:
        self.cache._reset()
        self.cache._batch_mode = True
        try:
            self.prepare_batch(requests)
            return {
                "items": [self.execute(request) for request in requests],
                "meta": self.transport_metadata(),
            }
        finally:
            self.cache._batch_mode = False
            self.cache._reset()
