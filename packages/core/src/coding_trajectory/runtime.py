"""Reusable execution runtime for versioned CodingTrajectory service methods."""

from __future__ import annotations

import atexit
import json
import threading
from pathlib import Path
from typing import Any, Mapping, Protocol

from pydantic import ValidationError

from coding_trajectory.contracts import service_contract
from coding_trajectory.query import DocumentError, ResourceNotFoundError
from coding_trajectory.service import (
    IndexCache,
    dispatch,
    project_list_metadata,
    resolve_store,
)


def _discovery_params(params: dict[str, Any]) -> dict[str, Any]:
    """Expose nested living-event scope IDs to the ordinary discovery layer."""

    scope = params.get("scope")
    if not isinstance(scope, dict):
        return params
    result = dict(params)
    for key in ("session_id", "root_session_id", "turn_id"):
        value = scope.get(key)
        if value and key not in result:
            result[key] = value
    return result


def _entrypoint_ids_from_params(params: dict[str, Any]) -> list[str]:
    params = _discovery_params(params)
    ids: list[str] = []
    for key in ("session_id", "root_session_id", "turn_id"):
        value = params.get(key)
        if isinstance(value, str) and value:
            ids.append(value)
    session_ids = params.get("session_ids")
    if isinstance(session_ids, list):
        ids.extend(value for value in session_ids if isinstance(value, str) and value)
    return ids


def _entrypoint_ids(requests: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for request in requests:
        if request.get("method") in {"living.events", "project.list"}:
            continue
        params = request.get("params") or {}
        if not isinstance(params, dict):
            continue
        ids.extend(_entrypoint_ids_from_params(params))
    return list(dict.fromkeys(ids))


def _store_key(
    params: dict[str, Any],
    *,
    global_scope: bool,
    include_descendants: bool,
) -> tuple[Any, ...]:
    params = _discovery_params(params)
    discovery_params = {
        key: params.get(key)
        for key in (
            "project_name",
            "since_days",
            "modified_since",
            "agent_vendor",
            "session_id",
            "root_session_id",
            "turn_id",
            "session_ids",
        )
        if key in params
    }
    return (
        "discovery",
        global_scope,
        include_descendants,
        json.dumps(discovery_params, sort_keys=True, default=str),
    )


def _requires_session_component(method: str) -> bool:
    """Return whether a method needs descendant sessions in its source store."""

    return method.startswith("graph.") or method == "session.tree"


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


class PluginApiError(RuntimeError):
    """Raised when an in-process service call fails."""


class PluginApiClient:
    """In-process equivalent of ``ct api call/batch --global-scope``.

    Owns one lazily created :class:`ServiceRuntime` so stores and the index
    cache are reused across calls instead of paying subprocess and discovery
    cost per request.  A lock keeps the shared runtime safe for plugin
    callers that fan out work over a thread pool.
    """

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

    def __enter__(self) -> PluginApiClient:
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

    def __init__(self, *, global_scope: bool, current_dir: Path) -> None:
        self.global_scope = global_scope
        self.current_dir = current_dir
        self.cache = IndexCache.load()
        self._stores: dict[tuple[Any, ...], tuple[Any, str]] = {}
        self._batch_store: tuple[Any, str] | None = None

    def __enter__(self) -> ServiceRuntime:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.cache.save()

    def prepare_batch(self, requests: list[dict[str, Any]]) -> None:
        ids = _entrypoint_ids(requests)
        if not ids:
            return
        self._batch_store = resolve_store(
            {"session_ids": ids},
            global_scope=self.global_scope,
            current_dir=self.current_dir,
            cache=self.cache,
        )

    def call(self, method: str, params: dict[str, Any]) -> Any:
        contract = service_contract(method)
        validated_params = contract.validate_request(params)
        if method == "project.list":
            return contract.validate_response(
                project_list_metadata(
                    validated_params,
                    global_scope=True,
                    current_dir=self.current_dir,
                )
            )
        if method == "living.events":
            from coding_trajectory.living_events import serve_living_events

            return contract.validate_response(
                serve_living_events(
                    validated_params,
                    cache=self.cache,
                    current_dir=self.current_dir,
                    global_scope=self.global_scope,
                )
            )

        store, discovery_note = self._store_for(method, validated_params)
        return dispatch(
            method,
            validated_params,
            store=store,
            global_scope=self.global_scope,
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
        return {
            "id": request_id,
            "method": method,
            "ok": True,
            "result": result,
        }

    def batch(self, requests: list[dict[str, Any]]) -> dict[str, Any]:
        self.prepare_batch(requests)
        return {"items": [self.execute(request) for request in requests]}

    def _store_for(self, method: str, params: dict[str, Any]) -> tuple[Any, str]:
        if self._batch_store is not None and _entrypoint_ids_from_params(params):
            return self._batch_store
        include_descendants = _requires_session_component(method)
        key = _store_key(
            params,
            global_scope=self.global_scope,
            include_descendants=include_descendants,
        )
        if key not in self._stores:
            self._stores[key] = resolve_store(
                _discovery_params(params),
                global_scope=self.global_scope,
                current_dir=self.current_dir,
                cache=self.cache,
                include_descendants=include_descendants,
            )
        return self._stores[key]
