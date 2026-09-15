"""One internal ``FactRepository`` contract for historical fact authorities.

Local execution derives an in-memory ``PublishedFactSet`` from host-local
provider logs through the Chronicle; remote execution fetches selected SQL fact
pages from the Cloudflare authority. Both reconstruct the identical bounded
representation before the shared Python historical handlers run, so summary,
overview, search, metrics, and display semantics are owned exactly once.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from coding_trajectory.contracts import service_contract
from coding_trajectory.control_plane.chronicle import build_chronicle_graph_artifact
from coding_trajectory.control_plane.fact_protocol import (
    FACT_READ_PAGE_MAX,
    FactReadResponse,
)
from coding_trajectory.control_plane.published_facts import (
    FactRow,
    PublishedFactSet,
    compute_fact_set_digest,
    derive_published_fact_set,
)
from coding_trajectory.control_plane.remote import (
    CloudflareRpcClient,
    RemoteControlPlaneError,
)
from coding_trajectory.ingestion.common import format_datetime
from coding_trajectory.query import DocumentStore


class FactRepository(Protocol):
    """Supply handler-ready stores from one local or remote fact authority."""

    def pin_snapshot(self) -> int: ...

    def store_for(self, method: str, params: dict[str, Any]) -> tuple[Any, str]: ...

    def metadata(self) -> dict[str, Any] | None: ...

    def close(self) -> None: ...


def published_fact_set_for_store(store: DocumentStore) -> list[PublishedFactSet]:
    """Derive every graph's fact set from one canonical local store."""

    return [
        derive_published_fact_set(build_chronicle_graph_artifact(graph))
        for graph in sorted(
            store.session_graphs.values(), key=lambda graph: str(graph.root_session_id)
        )
    ]


def document_store_from_fact_sets(fact_sets: list[PublishedFactSet]) -> DocumentStore:
    """Rebuild the handler store from validated fact sets."""

    graphs = [fact_set.to_session_graph() for fact_set in fact_sets]
    store = DocumentStore.from_session_graphs(graphs)
    return store


class LocalPublishedFactRepository:
    """Serve historical reads from an in-memory published fact set."""

    def __init__(
        self,
        *,
        global_scope: bool,
        current_dir: Path,
        cache: Any,
        resolve: Callable[..., tuple[DocumentStore, str]] | None = None,
    ) -> None:
        self.global_scope = global_scope
        self.current_dir = current_dir
        self.cache = cache
        self._resolve = resolve
        self._fact_sets: dict[tuple[Any, ...], list[PublishedFactSet]] = {}
        self._stores: dict[tuple[Any, ...], tuple[DocumentStore, str]] = {}

    def pin_snapshot(self) -> int:
        """Local sources are read live and therefore have no snapshot number."""

        return 0

    def prepare_batch(self, requests: list[dict[str, Any]]) -> None:
        ids = entrypoint_ids(requests)
        if ids:
            self.store_for("session.tree", {"session_ids": ids})

    def store_for(
        self, method: str, params: dict[str, Any]
    ) -> tuple[DocumentStore, str]:
        key = fact_store_key(
            params,
            global_scope=self.global_scope,
            include_descendants=requires_graph_scope(method),
        )
        if key not in self._stores:
            store, note = self._resolve_store(method, params, key)
            fact_sets = self._fact_sets.setdefault(
                key, published_fact_set_for_store(store)
            )
            self._stores[key] = (document_store_from_fact_sets(fact_sets), note)
        return self._stores[key]

    def _resolve_store(
        self, method: str, params: dict[str, Any], key: tuple[Any, ...]
    ) -> tuple[DocumentStore, str]:
        from coding_trajectory.service import resolve_store

        include_descendants = requires_graph_scope(method)
        resolve = self._resolve or resolve_store
        return resolve(
            discovery_params(params),
            global_scope=self.global_scope,
            current_dir=self.current_dir,
            cache=self.cache,
            include_descendants=include_descendants,
        )

    def metadata(self) -> dict[str, Any]:
        return {"source": "local", "freshness": "live", "content_scope": "facts"}

    def close(self) -> None:
        save = getattr(self.cache, "save", None)
        if save is not None:
            save()


class CloudflareFactRepository:
    """Fetch selected SQL fact pages from the remote workspace authority."""

    def __init__(
        self,
        *,
        client: CloudflareRpcClient,
        workspace_id: UUID,
        snapshot_sequence: int | None = None,
    ) -> None:
        if snapshot_sequence is not None and snapshot_sequence < 0:
            raise ValueError("snapshot_sequence must not be negative")
        self._client = client
        self.workspace_id = workspace_id
        self.snapshot_sequence = snapshot_sequence
        self._stores: dict[str, tuple[DocumentStore, str]] = {}

    def close(self) -> None:
        self._client.close()

    def pin_snapshot(self) -> int:
        if self.snapshot_sequence is None:
            raw = self._client.call(
                "ct_workspace_snapshot",
                {"workspace_id": str(self.workspace_id)},
            )
            self.snapshot_sequence = _validated_sequence(
                raw.get("snapshot_sequence"), self.snapshot_sequence
            )
        return self.snapshot_sequence

    def store_for(
        self, method: str, params: dict[str, Any]
    ) -> tuple[DocumentStore, str]:
        validated = service_contract(method).validate_request(params)
        scope = fact_read_scope(validated)
        key = json.dumps(scope, sort_keys=True, default=str)
        if key not in self._stores:
            fact_sets = self._read_fact_sets(scope)
            self._stores[key] = (
                document_store_from_fact_sets(fact_sets),
                f"remote workspace snapshot {self.snapshot_sequence}",
            )
        return self._stores[key]

    def _read_fact_sets(self, scope: dict[str, Any]) -> list[PublishedFactSet]:
        rows: list[FactRow] = []
        digests: dict[str, str] = {}
        counts: dict[str, int] = {}
        cursor: str | None = None
        pages = 0
        while True:
            request: dict[str, Any] = {
                "workspace_id": str(self.workspace_id),
                "limit": FACT_READ_PAGE_MAX,
                **scope,
            }
            if self.snapshot_sequence is not None:
                request["snapshot_sequence"] = self.snapshot_sequence
            if cursor is not None:
                request["cursor"] = cursor
            response = FactReadResponse.model_validate(
                self._client.call("ct_fact_read", request)
            )
            if response.workspace_id != self.workspace_id:
                raise RemoteControlPlaneError("fact read workspace mismatch")
            self.snapshot_sequence = _validated_sequence(
                response.snapshot_sequence, self.snapshot_sequence
            )
            rows.extend(response.rows)
            digests.update(response.graph_digests)
            counts.update(response.graph_fact_counts)
            cursor = response.next_cursor
            pages += 1
            if cursor is None:
                break
            if pages > 4096:
                raise RemoteControlPlaneError("fact read exceeded the page bound")
        return _fact_sets_from_rows(rows, digests=digests, counts=counts)

    def metadata(self) -> dict[str, Any] | None:
        if self.snapshot_sequence is None:
            return None
        return {
            "workspace_id": str(self.workspace_id),
            "snapshot_sequence": self.snapshot_sequence,
            "source": "remote",
            "freshness": "authoritative",
            "content_scope": "facts",
        }


def _validated_sequence(value: Any, pinned: int | None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RemoteControlPlaneError("fact read returned an invalid snapshot")
    if pinned is not None and value != pinned:
        raise RemoteControlPlaneError("fact read snapshot mismatch")
    return value


def _fact_sets_from_rows(
    rows: list[FactRow], *, digests: dict[str, str], counts: dict[str, int]
) -> list[PublishedFactSet]:
    grouped: dict[str, list[FactRow]] = {}
    for row in rows:
        grouped.setdefault(str(row.graph_id), []).append(row)
    if set(grouped) != set(digests):
        raise RemoteControlPlaneError("fact read omitted a selected graph")
    fact_sets: list[PublishedFactSet] = []
    for graph_id in sorted(grouped):
        graph_rows = sorted(
            grouped[graph_id], key=lambda row: (row.kind, str(row.fact_id))
        )
        expected = digests[graph_id]
        if compute_fact_set_digest(UUID(graph_id), graph_rows) != expected:
            raise RemoteControlPlaneError("fact read graph digest mismatch")
        kind_counts: dict[str, int] = {}
        for row in graph_rows:
            kind_counts[row.kind] = kind_counts.get(row.kind, 0) + 1
        if (
            counts.get(graph_id) is not None
            and sum(kind_counts.values()) != counts[graph_id]
        ):
            raise RemoteControlPlaneError("fact read graph count mismatch")
        try:
            fact_sets.append(
                PublishedFactSet(
                    graph_id=UUID(graph_id),
                    fact_set_digest=expected,
                    kind_counts=kind_counts,
                    rows=graph_rows,
                )
            )
        except ValueError as exc:
            raise RemoteControlPlaneError(
                f"fact read graph failed integrity validation: {exc}"
            ) from exc
    return fact_sets


def fact_read_scope(params: dict[str, Any]) -> dict[str, Any]:
    scope: dict[str, Any] = {}
    root_session_id = params.get("root_session_id")
    if isinstance(root_session_id, str) and root_session_id:
        # The graph is identified by its root session id on the wire.
        scope["graph_id"] = root_session_id
    for key in ("session_id", "project_name", "agent_vendor"):
        value = params.get(key)
        if isinstance(value, str) and value:
            scope[key] = value
    if "modified_since" in params and params["modified_since"] is not None:
        scope["modified_since"] = format_datetime(params["modified_since"])
    return scope


def requires_graph_scope(method: str) -> bool:
    return method.startswith("graph.") or method == "session.tree"


def discovery_params(params: dict[str, Any]) -> dict[str, Any]:
    scope = params.get("scope")
    if not isinstance(scope, dict):
        return params
    result = dict(params)
    for key in ("session_id", "root_session_id", "turn_id"):
        value = scope.get(key)
        if value and key not in result:
            result[key] = value
    return result


def entrypoint_ids_from_params(params: dict[str, Any]) -> list[str]:
    params = discovery_params(params)
    ids = [
        value
        for key in ("session_id", "root_session_id", "turn_id")
        if isinstance((value := params.get(key)), str) and value
    ]
    session_ids = params.get("session_ids")
    if isinstance(session_ids, list):
        ids.extend(value for value in session_ids if isinstance(value, str) and value)
    return ids


def entrypoint_ids(requests: list[dict[str, Any]]) -> list[str]:
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
            ids.extend(entrypoint_ids_from_params(params))
    return list(dict.fromkeys(ids))


def fact_store_key(
    params: dict[str, Any], *, global_scope: bool, include_descendants: bool
) -> tuple[Any, ...]:
    params = discovery_params(params)
    selected = {
        key: params.get(key)
        for key in (
            "project_name",
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
        "facts",
        global_scope,
        include_descendants,
        json.dumps(selected, sort_keys=True, default=str),
    )


__all__ = [
    "CloudflareFactRepository",
    "FactRepository",
    "LocalPublishedFactRepository",
    "discovery_params",
    "document_store_from_fact_sets",
    "entrypoint_ids",
    "entrypoint_ids_from_params",
    "fact_store_key",
    "published_fact_set_for_store",
    "requires_graph_scope",
]
