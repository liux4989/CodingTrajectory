"""Reusable execution runtime for versioned CodingTrajectory service methods."""

from __future__ import annotations

import atexit
import json
import os
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol, Self

from pydantic import ValidationError

from coding_trajectory.contracts import service_contract
from coding_trajectory.control_plane import (
    METHOD_AUTHORITIES,
    ApplicationDispatcher,
    MethodAuthority,
)
from coding_trajectory.query import DocumentError, DocumentStore, ResourceNotFoundError
from coding_trajectory.service import (
    IndexCache,
    dispatch,
    project_list_metadata,
    resolve_store,
)


class LocalSourceUnavailableError(DocumentError):
    """The host has no local coding-agent source that can answer a request."""


class SourceFallbackError(DocumentError):
    """A local miss could not be satisfied by the configured remote source."""


def _environment_remote_fallback(
    *, current_dir: Path
) -> Callable[[], ServiceRuntime] | None:
    """Return a lazy remote builder only for complete embedded configuration."""

    names = (
        "CT_CLOUDFLARE_URL",
        "CT_ACCESS_TOKEN",
        "CT_REMOTE_WORKSPACE_ID",
    )
    if not all(os.environ.get(name) for name in names):
        return None

    def build() -> ServiceRuntime:
        from coding_trajectory.control_plane.configuration import ApiConfiguration

        options = ApiConfiguration.from_environment().runtime_options(
            local_evidence=False, current_dir=current_dir
        )
        return ServiceRuntime(**options)

    return build


def _discovery_params(params: dict[str, Any]) -> dict[str, Any]:
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
    ids = [
        value
        for key in ("session_id", "root_session_id", "turn_id")
        if isinstance((value := params.get(key)), str) and value
    ]
    session_ids = params.get("session_ids")
    if isinstance(session_ids, list):
        ids.extend(value for value in session_ids if isinstance(value, str) and value)
    return ids


def _entrypoint_ids(requests: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for request in requests:
        if request.get("method") in {
            "living.events",
            "living.sessions",
            "project.list",
        }:
            continue
        params = request.get("params") or {}
        if isinstance(params, dict):
            ids.extend(_entrypoint_ids_from_params(params))
    return list(dict.fromkeys(ids))


def _store_key(
    params: dict[str, Any], *, global_scope: bool, include_descendants: bool
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
    return method.startswith("graph.") or method == "session.tree"


def _requires_local_evidence(method: str, params: dict[str, Any]) -> bool:
    return (
        method in {"session.events", "session.search"}
        or method == "session.items"
        and bool(params.get("include_content"))
        or method == "graph.overview"
        and "narrative" in params.get("include", [])
    )


def _chronicle_store(store: DocumentStore) -> DocumentStore:
    from coding_trajectory.control_plane.chronicle import chronicle_session_graph

    return DocumentStore.from_session_graphs(
        [
            chronicle_session_graph(graph)
            for graph in sorted(
                store.session_graphs.values(),
                key=lambda graph: str(graph.root_session_id),
            )
        ]
    )


def _local_sources_available(*, current_dir: Path, global_scope: bool) -> bool:
    """Check source availability independently of query filters.

    A filtered query may validly return an empty collection. It is unavailable
    only when the host exposes no supported local source at all.
    """

    from coding_trajectory.discovery import discover_source_candidates

    try:
        return bool(
            discover_source_candidates(
                current_dir=current_dir, global_scope=global_scope
            )
        )
    except OSError as exc:
        raise LocalSourceUnavailableError(
            f"local source discovery is unavailable: {exc}"
        ) from exc


def _empty_items(result: Any) -> bool:
    return isinstance(result, dict) and "items" in result and not result["items"]


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


class LocalHistoricalRepository:
    """Resolve historical stores from host-local sources without remote I/O."""

    def __init__(
        self, *, global_scope: bool, current_dir: Path, cache: IndexCache
    ) -> None:
        self.global_scope = global_scope
        self.current_dir = current_dir
        self.cache = cache
        self._stores: dict[tuple[Any, ...], tuple[DocumentStore, str]] = {}
        self._batch_store: tuple[DocumentStore, str] | None = None
        self._batch_chronicle_store: tuple[DocumentStore, str] | None = None

    def pin_snapshot(self) -> int:
        """Local sources are read live and therefore have no snapshot number."""

        return 0

    def prepare_batch(self, requests: list[dict[str, Any]]) -> None:
        ids = _entrypoint_ids(requests)
        if not ids:
            return
        self._batch_chronicle_store = None
        self._batch_store = resolve_store(
            {"session_ids": ids},
            global_scope=self.global_scope,
            current_dir=self.current_dir,
            cache=self.cache,
        )
        self._require_available(self._batch_store[0])

    def store_for(
        self, method: str, params: dict[str, Any]
    ) -> tuple[DocumentStore, str]:
        if self._batch_store is not None and _entrypoint_ids_from_params(params):
            self._require_available(self._batch_store[0])
            if _requires_local_evidence(method, params):
                return self._batch_store
            if self._batch_chronicle_store is None:
                store, note = self._batch_store
                self._batch_chronicle_store = (_chronicle_store(store), note)
            return self._batch_chronicle_store

        include_descendants = _requires_session_component(method)
        key = (
            "local_evidence"
            if _requires_local_evidence(method, params)
            else "chronicle",
            *_store_key(
                params,
                global_scope=self.global_scope,
                include_descendants=include_descendants,
            ),
        )
        if key not in self._stores:
            store, note = resolve_store(
                _discovery_params(params),
                global_scope=self.global_scope,
                current_dir=self.current_dir,
                cache=self.cache,
                include_descendants=include_descendants,
            )
            self._require_available(store)
            self._stores[key] = (
                store
                if _requires_local_evidence(method, params)
                else _chronicle_store(store),
                note,
            )
        return self._stores[key]

    def _require_available(self, store: DocumentStore) -> None:
        if store.session_graphs or _local_sources_available(
            current_dir=self.current_dir, global_scope=self.global_scope
        ):
            return
        raise LocalSourceUnavailableError(
            "no supported coding-agent source is available on this host"
        )

    def metadata(self) -> dict[str, Any]:
        return {"source": "local", "freshness": "live"}

    def close(self) -> None:
        self.cache.save()


class PluginApiError(RuntimeError):
    """Raised when an in-process service call fails."""


class PluginApiClient:
    """In-process equivalent of ``ct api call/batch --global-scope``.

    Reuses a local-first runtime across calls. Local source caches avoid repeated
    discovery, while any remote fallback runtime pins its snapshot only after a
    local miss. A lock serializes callers that fan out over a thread pool.
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
                fallback_factory=_environment_remote_fallback(
                    current_dir=self._current_dir
                ),
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
        fallback_factory: Callable[[], ServiceRuntime] | None = None,
    ) -> None:
        self.global_scope = global_scope
        self.current_dir = current_dir
        self.cache = (
            IndexCache.load() if historical_repository is None else IndexCache()
        )
        self.historical_repository = historical_repository or LocalHistoricalRepository(
            global_scope=global_scope,
            current_dir=current_dir,
            cache=self.cache,
        )
        self._transport_metadata = transport_metadata
        self._last_call_metadata: dict[str, Any] | None = None
        self.before_read = before_read
        self._fallback_factory = fallback_factory
        self._fallback_runtime: ServiceRuntime | None = None
        handlers = dict(authority_handlers or {})
        self._dispatcher = ApplicationDispatcher(
            {
                MethodAuthority.HISTORICAL: self._call_historical,
                MethodAuthority.PROJECT_INVENTORY: handlers.get(
                    MethodAuthority.PROJECT_INVENTORY, self._call_project_inventory
                ),
                MethodAuthority.LIVING: handlers.get(
                    MethodAuthority.LIVING, self._call_living
                ),
                MethodAuthority.ESTIMATION: handlers.get(
                    MethodAuthority.ESTIMATION, self._call_estimation
                ),
            }
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        close = getattr(self.historical_repository, "close", None)
        if close is not None:
            close()
        if self._fallback_runtime is not None:
            self._fallback_runtime.close()
            self._fallback_runtime = None

    def prepare_batch(self, requests: list[dict[str, Any]]) -> None:
        prepare = getattr(self.historical_repository, "prepare_batch", None)
        if prepare is not None:
            try:
                prepare(requests)
            except LocalSourceUnavailableError:
                # Per-item execution applies the same lazy remote fallback and
                # preserves independent provenance/error envelopes.
                pass
            return
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
        self._last_call_metadata = None
        try:
            self._prepare_read(method, params)
            result = self._dispatcher.call(method, params)
        except (LocalSourceUnavailableError, ResourceNotFoundError) as local_error:
            if self._fallback_factory is None:
                raise
            try:
                fallback = self._get_fallback_runtime()
                result = fallback.call(method, params)
            except Exception as remote_error:
                raise SourceFallbackError(
                    "local source could not satisfy the request "
                    f"({local_error}); remote Chronicles fallback failed "
                    f"({remote_error})"
                ) from remote_error
            self._last_call_metadata = fallback.transport_metadata()
            return result
        self._last_call_metadata = self._primary_metadata()
        return result

    def _get_fallback_runtime(self) -> ServiceRuntime:
        if self._fallback_runtime is None:
            if self._fallback_factory is None:
                raise ValueError("remote Chronicles fallback is not configured")
            self._fallback_runtime = self._fallback_factory()
        return self._fallback_runtime

    def _primary_metadata(self) -> dict[str, Any] | None:
        if self._transport_metadata is not None:
            return self._transport_metadata()
        return self.historical_repository.metadata()

    def _call_project_inventory(self, method: str, params: dict[str, Any]) -> Any:
        if method != "project.list":
            raise KeyError(
                f"no local project inventory handler registered for {method}"
            )
        result = project_list_metadata(
            params, global_scope=True, current_dir=self.current_dir
        )
        self._require_local_source_for_empty(result)
        return result

    def _call_living(self, method: str, params: dict[str, Any]) -> Any:
        if method == "living.events":
            from coding_trajectory.living_events import serve_living_events

            result = serve_living_events(
                params,
                cache=self.cache,
                current_dir=self.current_dir,
                global_scope=self.global_scope,
            )
        elif method == "living.sessions":
            from coding_trajectory.living_sessions import serve_living_sessions

            result = serve_living_sessions(
                params,
                current_dir=self.current_dir,
                global_scope=self.global_scope,
            )
        else:
            raise KeyError(f"no local living handler registered for {method}")
        self._require_local_source_for_empty(result)
        return result

    def _require_local_source_for_empty(self, result: Any) -> None:
        if _empty_items(result) and not _local_sources_available(
            current_dir=self.current_dir, global_scope=self.global_scope
        ):
            raise LocalSourceUnavailableError(
                "no supported coding-agent source is available on this host"
            )

    def _call_estimation(self, method: str, params: dict[str, Any]) -> Any:
        from coding_trajectory.estimation import serve_estimate

        return serve_estimate(
            method,
            params,
            global_scope=self.global_scope,
            current_dir=self.current_dir,
            cache=self.cache,
        )

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
        response_for = getattr(self.historical_repository, "response_for", None)
        if response_for is not None:
            projected = response_for(method, params)
            if projected is not None:
                return projected
        store, discovery_note = self._store_for(method, params)
        return dispatch(
            method,
            params,
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
        response = {
            "id": request_id,
            "method": method,
            "ok": True,
            "result": result,
        }
        metadata = self.transport_metadata()
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
        return self._last_call_metadata or self._primary_metadata()

    def _store_for(self, method: str, params: dict[str, Any]) -> tuple[Any, str]:
        return self.historical_repository.store_for(method, params)
