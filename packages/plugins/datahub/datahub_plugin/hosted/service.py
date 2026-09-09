"""Request-scoped hosted Datahub adapters over chronicle CT artifacts.

The hosted runtime deliberately has no local discovery, filesystem, cache, or
publication capability.  Every successful route pins the caller's remote
workspace sequence, loads only the artifacts required for that route, invokes
the existing Python CT handlers, and validates the final Datahub response.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import parse_qs, urlparse
from uuid import UUID

from coding_trajectory.contracts import service_contract
from coding_trajectory.control_plane.remote import (
    RemoteControlPlaneError,
    _historical_snapshot_request,
    _require_chronicle_historical_scope,
    _snapshot_artifact_graph,
)
from coding_trajectory.control_plane.remote_inventory import (
    RemoteProjectInventorySnapshot,
)
from coding_trajectory.query import DocumentStore
from coding_trajectory.service import IndexCache, dispatch
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from datahub_plugin.api_models import validate_api_response
from datahub_plugin.serving.routes import ROUTES

HOSTED_HORIZON_DAYS = 7
MAX_REMOTE_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_CURSOR_OFFSET = 100_000

HostedDecision = Literal["adapter", "metadata_only", "deferred", "omit", "prohibited"]

# Every local Datahub route must be classified here.  Keeping deferred and
# prohibited entries explicit makes route growth fail closed during validation.
HOSTED_ROUTE_DECISIONS: dict[tuple[str, str], HostedDecision] = {
    ("GET", "/api/datahub/events"): "deferred",
    ("GET", "/api/datahub/snapshot"): "adapter",
    ("GET", "/api/datahub/changes"): "adapter",
    ("GET", "/api/overview"): "deferred",
    ("GET", "/api/today"): "deferred",
    ("GET", "/api/projects"): "adapter",
    ("GET", "/api/projects/detail"): "deferred",
    ("GET", "/api/sessions"): "adapter",
    ("GET", "/api/sessions/timeline"): "omit",
    ("GET", "/api/sessions/context-window"): "prohibited",
    ("GET", "/api/sessions/graph"): "adapter",
    ("GET", "/api/sessions/tree"): "adapter",
    ("GET", "/api/sessions/evidence-timeline"): "prohibited",
    ("GET", "/api/sessions/events"): "prohibited",
    ("GET", "/api/sessions/items"): "metadata_only",
    ("GET", "/api/model-usage"): "deferred",
    ("GET", "/api/token-efficiency/project"): "deferred",
    ("GET", "/api/code-time/report"): "deferred",
    ("GET", "/api/code-time/forecasts"): "deferred",
    ("GET", "/api/code-time/calibration"): "deferred",
    ("POST", "/api/refresh"): "prohibited",
}


class HostedRequestError(ValueError):
    """A bounded client-facing hosted request failure."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class AsyncRpcClient(Protocol):
    async def call(self, name: str, request: dict[str, Any]) -> dict[str, Any]: ...


class _StrictQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _PageQuery(_StrictQuery):
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, max_length=4096)


class SnapshotQuery(_StrictQuery):
    pass


class ChangesQuery(_StrictQuery):
    after_revision: int = Field(ge=0)


class ProjectsQuery(_PageQuery):
    agent_vendor: str | None = Field(default=None, min_length=1, max_length=64)


class SessionsQuery(_PageQuery):
    since_days: int = Field(default=HOSTED_HORIZON_DAYS, ge=1, le=HOSTED_HORIZON_DAYS)
    project_name: str | None = Field(default=None, min_length=1, max_length=256)
    agent_vendor: str | None = Field(default=None, min_length=1, max_length=64)


class SessionQuery(_StrictQuery):
    session_id: UUID


class SessionItemsQuery(_StrictQuery):
    item_ids: list[UUID] = Field(min_length=1, max_length=200)
    include_content: bool = False
    turn_id: UUID | None = None

    @field_validator("item_ids", mode="before")
    @classmethod
    def _split_item_ids(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value


class _Cursor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    offset: int = Field(ge=0, le=MAX_CURSOR_OFFSET)


class HostedDatahubService:
    """Serve the hosted capability matrix through one async RPC client."""

    def __init__(self, *, rpc: AsyncRpcClient, workspace_id: UUID) -> None:
        self._rpc = rpc
        self._workspace_id = workspace_id

    async def handle_url(
        self, *, method: str, raw_url: str
    ) -> tuple[dict[str, Any] | list[Any], int]:
        parsed = urlparse(raw_url)
        query = _single_value_query(parsed.query)
        return await self.handle(method=method.upper(), path=parsed.path, query=query)

    async def handle(
        self, *, method: str, path: str, query: Mapping[str, Any]
    ) -> tuple[dict[str, Any] | list[Any], int]:
        decision = HOSTED_ROUTE_DECISIONS.get((method, path))
        if decision not in {"adapter", "metadata_only"}:
            # Prohibited, deferred, omitted, and unknown routes are deliberately
            # indistinguishable and perform no remote call.
            raise HostedRequestError(404, "not found")

        if path == "/api/datahub/snapshot":
            _validate_query(SnapshotQuery, query)
            payload = await self._snapshot()
            handler = "snapshot"
        elif path == "/api/datahub/changes":
            params = _validate_query(ChangesQuery, query)
            payload = await self._changes(params)
            handler = "changes"
        elif path == "/api/projects":
            params = _validate_query(ProjectsQuery, query)
            payload = await self._projects(params)
            handler = "projects"
        elif path == "/api/sessions":
            params = _validate_query(SessionsQuery, query)
            payload = await self._sessions(params)
            handler = "sessions"
        elif path == "/api/sessions/graph":
            params = _validate_query(SessionQuery, query)
            payload = await self._graph(params)
            handler = "graph_detail"
        elif path == "/api/sessions/tree":
            params = _validate_query(SessionQuery, query)
            payload = await self._tree(params)
            handler = "session_tree"
        elif path == "/api/sessions/items":
            params = _validate_query(SessionItemsQuery, query)
            if params.include_content:
                raise HostedRequestError(404, "not found")
            payload = await self._items(params)
            handler = "session_item_details"
        else:  # pragma: no cover - registry and dispatch are kept exhaustive
            raise HostedRequestError(404, "not found")

        try:
            validate_api_response(handler, payload)
        except ValidationError as exc:
            raise RuntimeError("hosted Datahub response contract mismatch") from exc
        return payload, 200

    async def _pin(self) -> int:
        raw = await self._rpc.call(
            "ct_workspace_snapshot", {"workspace_id": str(self._workspace_id)}
        )
        if str(raw.get("workspace_id")) != str(self._workspace_id):
            raise RemoteControlPlaneError("workspace snapshot identity mismatch")
        sequence = raw.get("snapshot_sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise RemoteControlPlaneError("workspace snapshot sequence is invalid")
        return sequence

    def _transport(self, sequence: int) -> dict[str, Any]:
        return {
            "workspace_id": str(self._workspace_id),
            "snapshot_sequence": sequence,
            "source": "remote",
            "freshness": "authoritative",
            "content_scope": "chronicle",
        }

    async def _snapshot(self) -> dict[str, Any]:
        sequence = await self._pin()
        return {
            "revision": sequence,
            "generated_at": datetime.now(UTC).isoformat(),
            "transport": self._transport(sequence),
            "freshness": {"last_refresh_at": None, "lag_seconds": None},
            "catching_up": False,
            "source_status": {
                "ready": 1,
                "ingesting": 0,
                "failed": 0,
                "incomplete": 0,
            },
            "minimum_available_revision": sequence,
            "bootstrap": {
                "ready": True,
                "scan_started_at": None,
                "scan_finished_at": None,
                "error": None,
                "last_result": None,
                "coverage": {
                    "mode": "hosted",
                    "content_scope": "chronicle",
                    "horizon_days": HOSTED_HORIZON_DAYS,
                },
            },
            "horizon_days": HOSTED_HORIZON_DAYS,
        }

    async def _changes(self, params: ChangesQuery) -> dict[str, Any]:
        sequence = await self._pin()
        changed = sequence != params.after_revision
        return {
            "from_revision": params.after_revision,
            "to_revision": sequence,
            "reset_required": changed,
            "upserts": [],
            "deletions": [],
            "invalidations": ["sessions", "projects", "session-tree", "session-graph"]
            if changed
            else [],
            "transport": self._transport(sequence),
            "freshness": {"last_refresh_at": None, "lag_seconds": None},
            "catching_up": False,
            "source_status": {
                "ready": 1,
                "ingesting": 0,
                "failed": 0,
                "incomplete": 0,
            },
        }

    async def _projects(self, params: ProjectsQuery) -> dict[str, Any]:
        sequence = await self._pin()
        offset = _cursor_offset(params.cursor, sequence)
        request: dict[str, Any] = {
            "workspace_id": str(self._workspace_id),
            "snapshot_sequence": sequence,
        }
        raw = await self._rpc.call("ct_project_inventory_snapshot", request)
        snapshot = RemoteProjectInventorySnapshot.model_validate(raw)
        if snapshot.workspace_id != self._workspace_id:
            raise RemoteControlPlaneError("project inventory workspace mismatch")
        if snapshot.snapshot_sequence != sequence:
            raise RemoteControlPlaneError("project inventory snapshot mismatch")
        items = [
            {"name": project.display_name, "path": None, "vendors": project.vendors}
            for project in snapshot.projects
            if params.agent_vendor is None or params.agent_vendor in project.vendors
        ]
        items.sort(key=lambda item: item["name"].casefold())
        page, next_cursor = _page(items, offset, params.limit, sequence)
        return {
            "items": page,
            "page": {
                "revision": sequence,
                "next_cursor": next_cursor,
                "has_more": next_cursor is not None,
            },
        }

    async def _sessions(self, params: SessionsQuery) -> dict[str, Any]:
        sequence = await self._pin()
        offset = _cursor_offset(params.cursor, sequence)
        method_params: dict[str, Any] = {
            "since_days": params.since_days,
            "include": ["runtime", "usage"],
        }
        if params.project_name is not None:
            method_params["project_name"] = params.project_name
        if params.agent_vendor is not None:
            method_params["agent_vendor"] = params.agent_vendor
        result = await self._project_sessions(method_params, sequence)
        rows = [_session_item(item) for item in result.get("items") or []]
        rows.sort(
            key=lambda item: (item.get("started_at") or "", item["root_session_id"]),
            reverse=True,
        )
        page, next_cursor = _page(rows, offset, params.limit, sequence)
        return {
            "items": page,
            "page": {
                "revision": sequence,
                "next_cursor": next_cursor,
                "has_more": next_cursor is not None,
            },
        }

    async def _project_sessions(
        self, params: dict[str, Any], sequence: int
    ) -> dict[str, Any]:
        """Prefer the small exact projection, retaining JSONB as rollback."""

        validated = service_contract("project.sessions").validate_request(params)
        request = {
            "workspace_id": str(self._workspace_id),
            "snapshot_sequence": sequence,
            **validated,
        }
        raw = await self._rpc.call("ct_project_sessions_projection", request)
        if str(raw.get("workspace_id")) != str(self._workspace_id):
            raise RemoteControlPlaneError("project sessions workspace mismatch")
        if raw.get("snapshot_sequence") != sequence:
            raise RemoteControlPlaneError("project sessions snapshot mismatch")
        if raw.get("complete") is True:
            result = raw.get("result")
            if not isinstance(result, dict):
                raise RemoteControlPlaneError("project sessions projection is invalid")
            return service_contract("project.sessions").validate_response(result)

        store = await self._store_for("project.sessions", params, sequence)
        return _dispatch("project.sessions", params, store, sequence)

    async def _graph(self, params: SessionQuery) -> dict[str, Any]:
        sequence = await self._pin()
        session_id = str(params.session_id)
        store = await self._store_for(
            "graph.overview", {"root_session_id": session_id}, sequence
        )
        overview = _dispatch(
            "graph.overview", {"root_session_id": session_id}, store, sequence
        )
        stats = _dispatch(
            "graph.stats",
            {"root_session_id": session_id, "include": ["session_composition"]},
            store,
            sequence,
        )
        usage = _dispatch(
            "graph.usage", {"root_session_id": session_id}, store, sequence
        )
        return {
            "root_session_id": str(overview.get("root_session_id") or session_id),
            "overview": overview,
            "stats": stats,
            "usage": usage,
        }

    async def _tree(self, params: SessionQuery) -> dict[str, Any]:
        sequence = await self._pin()
        session_id = str(params.session_id)
        method_params = {"session_id": session_id}
        store = await self._store_for("session.tree", method_params, sequence)
        return _dispatch("session.tree", method_params, store, sequence)

    async def _items(self, params: SessionItemsQuery) -> list[Any]:
        sequence = await self._pin()
        method_params: dict[str, Any] = {
            "item_ids": [str(value) for value in params.item_ids],
            "include_content": False,
        }
        if params.turn_id is not None:
            method_params["turn_id"] = str(params.turn_id)
        store = await self._store_for("session.items", method_params, sequence)
        result = _dispatch("session.items", method_params, store, sequence)
        if not isinstance(result, list):
            raise RemoteControlPlaneError("metadata item response is invalid")
        return result

    async def _store_for(
        self, method: str, params: dict[str, Any], sequence: int
    ) -> DocumentStore:
        _require_chronicle_historical_scope(method, params)
        validated = service_contract(method).validate_request(params)
        request = _historical_snapshot_request(
            workspace_id=self._workspace_id, method=method, params=validated
        )
        request["snapshot_sequence"] = sequence
        raw = await self._rpc.call("ct_historical_snapshot", request)
        if str(raw.get("workspace_id")) != str(self._workspace_id):
            raise RemoteControlPlaneError("historical snapshot workspace mismatch")
        if raw.get("snapshot_sequence") != sequence:
            raise RemoteControlPlaneError("historical snapshot sequence mismatch")
        artifacts = raw.get("artifacts")
        if not isinstance(artifacts, list):
            raise RemoteControlPlaneError("historical snapshot has no artifact list")
        return DocumentStore.from_session_graphs(
            [_snapshot_artifact_graph(item) for item in artifacts]
        )


def assert_hosted_route_inventory() -> None:
    """Fail if the local server adds or removes an unreviewed hosted route."""

    local = {(route.method, route.pattern) for route in ROUTES}
    classified = set(HOSTED_ROUTE_DECISIONS)
    if local != classified:
        missing = sorted(local - classified)
        stale = sorted(classified - local)
        raise RuntimeError(
            f"hosted route classification drift: missing={missing}, stale={stale}"
        )


def _single_value_query(raw_query: str) -> dict[str, str]:
    parsed = parse_qs(raw_query, keep_blank_values=True, strict_parsing=False)
    duplicates = sorted(key for key, values in parsed.items() if len(values) != 1)
    if duplicates:
        raise HostedRequestError(400, "query parameters must not be repeated")
    return {key: values[0] for key, values in parsed.items()}


def _validation_message(exc: ValidationError) -> str:
    errors = exc.errors(include_url=False, include_input=False)
    if not errors:
        return "invalid query parameters"
    location = ".".join(str(value) for value in errors[0].get("loc") or ())
    detail = str(errors[0].get("msg") or "invalid value")
    return f"invalid query parameter {location}: {detail}"[:200]


def _validate_query[T: BaseModel](model: type[T], query: Mapping[str, Any]) -> T:
    try:
        return model.model_validate(query)
    except ValidationError as exc:
        raise HostedRequestError(400, _validation_message(exc)) from exc


def _dispatch(
    method: str, params: dict[str, Any], store: DocumentStore, sequence: int
) -> Any:
    return dispatch(
        method,
        params,
        store=store,
        global_scope=True,
        current_dir=Path("/"),
        discovery_note=f"remote workspace snapshot {sequence}",
        cache=IndexCache(),
    )


def _session_item(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RemoteControlPlaneError("project session row is invalid")
    runtime = value.get("runtime") or {}
    usage = value.get("usage") or {}
    vendors = [str(item) for item in value.get("vendors") or []]
    root_id = str(value.get("root_session_id") or "")
    if not root_id:
        raise RemoteControlPlaneError("project session row has no root identity")
    return {
        "root_session_id": root_id,
        "lineage_root_session_id": value.get("lineage_root_session_id"),
        "graph_id": value.get("graph_id"),
        "vendors": vendors,
        "session_ids": [str(item) for item in value.get("session_ids") or []],
        "title": None,
        "preview": None,
        "project": value.get("project"),
        "started_at": runtime.get("started_at"),
        "ended_at": runtime.get("ended_at"),
        "status": runtime.get("status"),
        "turns": runtime.get("turns"),
        "execution_seconds": int(runtime.get("execution_seconds") or 0),
        "failed_tool_calls": runtime.get("failed_tool_calls"),
        "processed_tokens": usage.get("processed_tokens"),
        "cost_usd": None,
        "pricing_confidence": None,
    }


def _cursor_offset(raw: str | None, revision: int) -> int:
    if raw is None:
        return 0
    try:
        padding = "=" * (-len(raw) % 4)
        decoded = base64.urlsafe_b64decode(raw + padding)
        cursor = _Cursor.model_validate_json(decoded)
    except (ValueError, ValidationError) as exc:
        raise HostedRequestError(400, "invalid cursor") from exc
    if cursor.revision != revision:
        raise HostedRequestError(409, "cursor snapshot is no longer current")
    return cursor.offset


def _encode_cursor(revision: int, offset: int) -> str:
    raw = _Cursor(revision=revision, offset=offset).model_dump_json().encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _page(
    items: list[dict[str, Any]], offset: int, limit: int, revision: int
) -> tuple[list[dict[str, Any]], str | None]:
    selected = items[offset : offset + limit]
    next_offset = offset + len(selected)
    next_cursor = (
        _encode_cursor(revision, next_offset) if next_offset < len(items) else None
    )
    return selected, next_cursor


assert_hosted_route_inventory()

__all__ = [
    "HOSTED_HORIZON_DAYS",
    "HOSTED_ROUTE_DECISIONS",
    "HostedDatahubService",
    "HostedRequestError",
    "assert_hosted_route_inventory",
]
