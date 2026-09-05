"""Reusable execution runtime for versioned CodingTrajectory service methods."""

from __future__ import annotations

import atexit
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol, Self

from pydantic import ValidationError

from coding_trajectory.control_plane import (
    METHOD_AUTHORITIES,
    ApplicationDispatcher,
    MethodAuthority,
)
from coding_trajectory.query import DocumentError, ResourceNotFoundError
from coding_trajectory.contracts import service_contract
from coding_trajectory.service import (
    IndexCache,
    dispatch,
)


def _error_item(request_id: Any, method: Any, message: str) -> dict[str, Any]:
    return {
        "id": request_id,
        "method": method,
        "ok": False,
        "error": {"message": message},
    }


class ServiceApiClient(Protocol):
    """In-process service API surface shared by plugin-facing adapters."""

    def call(self, method: str, params: Mapping[str, Any]) -> Any:
        """Validate and execute one service method, raising on failure."""

    def execute(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Execute one request, returning the ``ct api`` envelope shape."""


class HistoricalRepository(Protocol):
    """Supply graph stores from one local or remote historical authority."""

    def pin_snapshot(self) -> int: ...

    def store_for(self, method: str, params: dict[str, Any]) -> tuple[Any, str]: ...

    def metadata(self) -> dict[str, Any] | None: ...


class PluginApiError(RuntimeError):
    """Raised when an in-process service call fails."""


class PluginApiClient:
    """In-process equivalent of ``ct api call/batch --global-scope``.

    Pins a fresh database snapshot per top-level call, matching CLI/HTTP
    freshness. Use an explicit ServiceRuntime for a stable multi-call snapshot.
    A lock serializes plugin callers that fan out work over a thread pool.
    """

    def __init__(
        self, *, global_scope: bool = True, current_dir: Path | None = None
    ) -> None:
        self._global_scope = global_scope
        self._current_dir = current_dir or Path.cwd()
        self._lock = threading.Lock()
        self._runtime: ServiceRuntime | None = None

    def _get_runtime(self) -> ServiceRuntime:
        if self._runtime is not None:
            self._runtime.close()
        self._runtime = ServiceRuntime(
            global_scope=self._global_scope,
            current_dir=self._current_dir,
        )
        return self._runtime

    def call(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        """Call one service method, raising :class:`PluginApiError` on failure."""

        try:
            with self._lock:
                return self._get_runtime().call(method, dict(params or {}))
        except (
            KeyError,
            ValueError,
            ValidationError,
            ResourceNotFoundError,
            DocumentError,
        ) as exc:
            raise PluginApiError(str(exc)) from exc

    def execute(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Execute one request, returning the ``ct api`` envelope shape."""

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
    """Return the process-wide client for plugin entry-point scripts."""

    global _default_client
    with _default_client_lock:
        if _default_client is None:
            _default_client = PluginApiClient()
            atexit.register(_default_client.close)
        return _default_client


class ServiceRuntime:
    """Execute calls and batches while reusing compatible stores."""

    def __init__(
        self,
        *,
        global_scope: bool,
        current_dir: Path,
        historical_repository: HistoricalRepository | None = None,
        authority_handlers: Mapping[MethodAuthority, Callable[..., Any]] | None = None,
        transport_metadata: Callable[[], dict[str, Any] | None] | None = None,
        local_evidence: bool = True,
        before_read: Callable[..., dict[str, Any] | None] | None = None,
    ) -> None:
        if historical_repository is None:
            from coding_trajectory.control_plane.configuration import ApiConfiguration

            options = ApiConfiguration.from_environment().runtime_options(
                local_evidence=local_evidence, current_dir=current_dir
            )
            historical_repository = options["historical_repository"]
            authority_handlers = options["authority_handlers"]
            transport_metadata = options["transport_metadata"]
            before_read = options.get("before_read")
        self.global_scope = global_scope
        self.current_dir = current_dir
        self.historical_repository = historical_repository
        # API reads must not load or rewrite the local discovery index.
        self.cache = IndexCache()
        self._transport_metadata = transport_metadata
        self.before_read = before_read
        handlers = dict(authority_handlers or {})
        handlers[MethodAuthority.HISTORICAL] = self._call_historical
        self._dispatcher = ApplicationDispatcher(handlers)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        close = getattr(self.historical_repository, "close", None)
        if close is not None:
            close()

    def prepare_batch(self, requests: list[dict[str, Any]]) -> None:
        if any(
            request.get("method") in METHOD_AUTHORITIES
            and METHOD_AUTHORITIES[request["method"]] == MethodAuthority.HISTORICAL
            for request in requests
        ):
            self.historical_repository.pin_snapshot()

    def call(self, method: str, params: dict[str, Any]) -> Any:
        if (
            method == "project.sessions"
            and not self.global_scope
            and not params.get("project_name")
        ):
            params = {**params, "project_name": self.current_dir.name}
        params = service_contract(method).validate_request(params)
        self._prepare_read(method, params)
        return self._dispatcher.call(method, params)

    def _prepare_read(self, method: str, params: dict[str, Any]) -> None:
        if self.before_read is None:
            return
        options = self.before_read(method, params, self.historical_repository)
        if options is not None:
            self.close()
            self.historical_repository = options["historical_repository"]
            self._transport_metadata = options["transport_metadata"]
            handlers = dict(options["authority_handlers"])
            handlers[MethodAuthority.HISTORICAL] = self._call_historical
            self._dispatcher = ApplicationDispatcher(handlers)

    def _call_historical(self, method: str, params: dict[str, Any]) -> Any:
        store, discovery_note = self._store_for(method, params)
        return dispatch(
            method,
            params,
            store=store,
            global_scope=True,
            current_dir=self.current_dir,
            discovery_note=discovery_note,
            cache=self.cache,
        )

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        if not isinstance(method, str) or not method:
            return _error_item(request_id, method, "method is required")
        if not isinstance(params, dict):
            return _error_item(request_id, method, "params must be an object")
        try:
            result = self.call(method, params)
        except (
            KeyError,
            ValueError,
            ValidationError,
            ResourceNotFoundError,
            DocumentError,
        ) as exc:
            return _error_item(request_id, method, str(exc))
        response = {
            "id": request_id,
            "method": method,
            "ok": True,
            "result": result,
        }
        metadata = self.transport_metadata()
        if METHOD_AUTHORITIES.get(method) == MethodAuthority.HISTORICAL:
            metadata = {
                **(metadata or {}),
                **(self.historical_repository.metadata() or {}),
            }
        if metadata is not None:
            response["meta"] = metadata
        return response

    def batch(self, requests: list[dict[str, Any]]) -> dict[str, Any]:
        errors: dict[int, dict[str, Any]] = {}
        if self.before_read is not None:
            for index, request in enumerate(requests):
                try:
                    method = request.get("method")
                    if not isinstance(method, str) or not isinstance(
                        request.get("params") or {}, dict
                    ):
                        continue
                    params = service_contract(method).validate_request(
                        request.get("params") or {}
                    )
                    self._prepare_read(method, params)
                except (KeyError, ValueError, DocumentError) as exc:
                    errors[index] = _error_item(
                        request.get("id"), request.get("method"), str(exc)
                    )
        self.prepare_batch(requests)
        before_read = self.before_read
        self.before_read = None
        try:
            response = {
                "items": [
                    errors[index] if index in errors else self.execute(request)
                    for index, request in enumerate(requests)
                ]
            }
        finally:
            self.before_read = before_read
        metadata = self.transport_metadata()
        if metadata is not None:
            response["meta"] = metadata
        return response

    def transport_metadata(self) -> dict[str, Any] | None:
        if self._transport_metadata is not None:
            return self._transport_metadata()
        return self.historical_repository.metadata()

    def _store_for(self, method: str, params: dict[str, Any]) -> tuple[Any, str]:
        return self.historical_repository.store_for(method, params)
