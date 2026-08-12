"""Reusable execution runtime for versioned CodingTrajectory service methods."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from coding_trajectory.contracts import service_contract
from coding_trajectory.query import DocumentError, ResourceNotFoundError
from coding_trajectory.service import (
    IndexCache,
    dispatch,
    project_list_metadata,
    resolve_store,
)


def _entrypoint_ids_from_params(params: dict[str, Any]) -> list[str]:
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
        params = request.get("params") or {}
        if not isinstance(params, dict):
            continue
        ids.extend(_entrypoint_ids_from_params(params))
    return list(dict.fromkeys(ids))


def _store_key(params: dict[str, Any], *, global_scope: bool) -> tuple[Any, ...]:
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
        json.dumps(discovery_params, sort_keys=True, default=str),
    )


def _error_item(request_id: Any, method: Any, message: str) -> dict[str, Any]:
    return {
        "id": request_id,
        "method": method,
        "ok": False,
        "error": {"message": message},
    }


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

        store, discovery_note = self._store_for(validated_params)
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

    def _store_for(self, params: dict[str, Any]) -> tuple[Any, str]:
        if self._batch_store is not None and _entrypoint_ids_from_params(params):
            return self._batch_store
        key = _store_key(params, global_scope=self.global_scope)
        if key not in self._stores:
            self._stores[key] = resolve_store(
                params,
                global_scope=self.global_scope,
                current_dir=self.current_dir,
                cache=self.cache,
            )
        return self._stores[key]
