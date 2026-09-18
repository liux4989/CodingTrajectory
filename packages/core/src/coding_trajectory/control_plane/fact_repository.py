"""One internal ``FactRepository`` contract for historical fact authorities.

Local execution derives typed fact rows from canonical session graphs without
publication size budgets; remote execution fetches the same rows from the Cloudflare
authority. Both expose one indexed read view to the shared Python historical
handlers, so summary, overview, search, metrics, and display semantics remain
owned exactly once.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Any, Protocol
from uuid import UUID

from coding_trajectory.contracts import service_contract
from coding_trajectory.control_plane.artifact_protocol import (
    ARTIFACT_PREPARATION_VERSION,
    ArtifactManifest,
    PreparedGraphSummary,
)
from coding_trajectory.control_plane.fact_projection import (
    build_fact_rows,
    build_published_fact_set,
)
from coding_trajectory.control_plane.graph_preparation import (
    PreparedGraph,
    filter_session_cards,
    graph_input_digest,
    prepare_graph,
)
from coding_trajectory.control_plane.published_facts import (
    FactIndex,
    PublishedFactSet,
)
from coding_trajectory.control_plane.remote import (
    CloudflareRpcClient,
    RemoteControlPlaneError,
)
from coding_trajectory.control_plane.remote_inventory import (
    CloudflareProjectInventoryRepository,
)
from coding_trajectory.ingestion.common import canonical_json
from coding_trajectory.project_identity import graph_project_id
from coding_trajectory.query import DocumentStore


class FactRepository(Protocol):
    """Supply indexed rows from one local or remote fact authority."""

    def pin_snapshot(self) -> int: ...

    def store_for(self, method: str, params: dict[str, Any]) -> tuple[Any, str]: ...

    def metadata(self) -> dict[str, Any] | None: ...

    def close(self) -> None: ...


def published_fact_set_for_store(store: DocumentStore) -> list[PublishedFactSet]:
    """Derive every graph's fact set from one canonical local store."""

    return [
        build_published_fact_set(graph)
        for graph in sorted(
            store.session_graphs.values(), key=lambda graph: str(graph.root_session_id)
        )
    ]


def fact_index_for_store(store: DocumentStore) -> FactIndex:
    """Index every local fact independently of remote publication budgets."""

    return FactIndex.from_rows(
        row
        for graph in sorted(
            store.session_graphs.values(), key=lambda graph: str(graph.root_session_id)
        )
        for row in build_fact_rows(graph)
    )


class LocalPublishedFactRepository:
    """Serve historical reads from all locally projected fact rows."""

    def __init__(
        self,
        *,
        global_scope: bool,
        current_dir: Path,
        cache: Any,
        resolve: Callable[..., tuple[DocumentStore, str]] | None = None,
        require_available: Callable[[bool], None] | None = None,
    ) -> None:
        self.global_scope = global_scope
        self.current_dir = current_dir
        self.cache = cache
        self._resolve = resolve
        self._require_available = require_available
        self._indexes: dict[tuple[Any, ...], tuple[FactIndex, str]] = {}
        self._prepared_cache = ArtifactReadCache()
        self._batch_index: tuple[FactIndex, str] | None = None

    def pin_snapshot(self) -> int:
        """Local sources are read live and therefore have no snapshot number."""

        return 0

    def prepare_batch(self, requests: list[dict[str, Any]]) -> None:
        self._batch_index = None
        ids = entrypoint_ids(requests)
        if not ids:
            return
        store, note = self._resolve_store(
            "session.tree", {"session_ids": ids}, ("batch",)
        )
        self._check_available(bool(store.session_graphs))
        self._batch_index = (
            FactIndex.from_rows(
                row for prepared in self._prepare_store(store) for row in prepared.rows
            ),
            note,
        )

    def end_batch(self) -> None:
        self._batch_index = None

    def _prepare_store(self, store: DocumentStore) -> list[PreparedGraph]:
        values = []
        for graph in store.session_graphs.values():
            key = ("local", ARTIFACT_PREPARATION_VERSION, graph_input_digest(graph))
            prepared = self._prepared_cache.get(key)
            if prepared is None:
                prepared = prepare_graph(graph)
                self._prepared_cache.put(
                    key, prepared, len(prepared.model_dump_json().encode())
                )
            values.append(prepared)
        return values

    def response_for(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        if method != "project.sessions":
            return None
        params = service_contract(method).validate_request(params)
        store, _ = self._resolve_store(method, params, ())
        self._check_available(bool(store.session_graphs))
        graphs = list(store.session_graphs.values())
        if params.get("project_id"):
            graphs = [
                graph
                for graph in graphs
                if graph_project_id(graph) == params["project_id"]
            ]
        elif (
            params.get("project_name")
            and len({graph_project_id(graph) for graph in graphs}) > 1
        ):
            raise ValueError("ambiguous project name; use project_id")
        selected = DocumentStore.from_session_graphs(graphs)
        project_ids = {
            graph.root_session_id: graph_project_id(graph) for graph in graphs
        }
        return filter_session_cards(
            [
                {**item, "project_id": project_ids[prepared.summary.graph_id]}
                for prepared in self._prepare_store(selected)
                for item in prepared.summary.project_sessions
            ],
            params,
        )

    def store_for(self, method: str, params: dict[str, Any]) -> tuple[FactIndex, str]:
        if self._batch_index is not None and entrypoint_ids_from_params(params):
            return self._batch_index
        store, note = self._resolve_store(method, params, ())
        self._check_available(bool(store.session_graphs))
        prepared = self._prepare_store(store)
        key = tuple(sorted(value.summary.fact_set_digest for value in prepared))
        if key not in self._indexes:
            self._indexes = {
                key: (
                    FactIndex.from_rows(
                        row for value in prepared for row in value.rows
                    ),
                    note,
                )
            }
        return self._indexes[key]

    def _check_available(self, has_graphs: bool) -> None:
        if self._require_available is not None:
            self._require_available(has_graphs)

    def _resolve_store(
        self, method: str, params: dict[str, Any], key: tuple[Any, ...]
    ) -> tuple[DocumentStore, str]:
        from coding_trajectory.service import resolve_store

        include_descendants = requires_graph_scope(method)
        resolve = self._resolve or resolve_store
        return resolve(
            discovery_params(params),
            global_scope=self.global_scope or bool(params.get("project_id")),
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


class ArtifactReadCache:
    """Bound immutable indexes/summaries by encoded bytes and entry count."""

    def __init__(self, *, max_bytes: int = 32 * 1024 * 1024, max_entries: int = 16):
        self._max_bytes = max_bytes
        self._max_entries = max_entries
        self._values: OrderedDict[tuple[str, str, str], tuple[Any, int]] = OrderedDict()
        self._bytes = 0
        self._lock = Lock()

    def get(self, key: tuple[str, str, str]) -> Any | None:
        with self._lock:
            entry = self._values.get(key)
            if entry is None:
                return None
            self._values.move_to_end(key)
            return entry[0]

    def put(self, key: tuple[str, str, str], value: Any, encoded_bytes: int) -> None:
        if encoded_bytes > self._max_bytes or self._max_entries < 1:
            return
        with self._lock:
            previous = self._values.pop(key, None)
            if previous is not None:
                self._bytes -= previous[1]
            while self._values and (
                self._bytes + encoded_bytes > self._max_bytes
                or len(self._values) >= self._max_entries
            ):
                self._bytes -= self._values.popitem(last=False)[1][1]
            self._values[key] = (value, encoded_bytes)
            self._bytes += encoded_bytes


class CloudflareArtifactRepository:
    """Read prepared lists and lazily load only a selected immutable graph."""

    def __init__(
        self,
        *,
        client: CloudflareRpcClient,
        workspace_id: UUID,
        snapshot_sequence: int,
        cache: ArtifactReadCache,
        inventory: CloudflareProjectInventoryRepository | None = None,
    ) -> None:
        self._client = client
        self.workspace_id = workspace_id
        self.snapshot_sequence = snapshot_sequence
        self._cache = cache
        self._inventory = inventory or CloudflareProjectInventoryRepository(
            client=client,
            workspace_id=workspace_id,
            snapshot_sequence=snapshot_sequence,
        )
        self._manifests_value: list[ArtifactManifest] | None = None
        self._summaries_value: dict[UUID, PreparedGraphSummary] | None = None

    def pin_snapshot(self) -> int:
        return self.snapshot_sequence

    def close(self) -> None:
        self._client.close()

    def _manifests(self) -> list[ArtifactManifest]:
        if self._manifests_value is None:
            raw = self._client.call(
                "ct_artifact_manifest",
                {
                    "workspace_id": str(self.workspace_id),
                    "snapshot_sequence": self.snapshot_sequence,
                },
            )
            if raw.get("workspace_id") != str(self.workspace_id) or (
                raw.get("snapshot_sequence") != self.snapshot_sequence
            ):
                raise RemoteControlPlaneError("artifact manifest snapshot mismatch")
            self._manifests_value = [
                ArtifactManifest.model_validate(value)
                for value in raw.get("manifests", [])
            ]
            if any(
                manifest.preparation_version != ARTIFACT_PREPARATION_VERSION
                for manifest in self._manifests_value
            ):
                raise RemoteControlPlaneError("artifact preparation version mismatch")
        return self._manifests_value

    def _read_object(self, *, kind: str, sha256: str) -> dict[str, Any]:
        return self._client.call(
            "ct_artifact_read",
            {
                "workspace_id": str(self.workspace_id),
                "snapshot_sequence": self.snapshot_sequence,
                "kind": kind,
                "sha256": sha256,
            },
        )

    def _summaries(
        self, project_id: str | None = None
    ) -> dict[UUID, PreparedGraphSummary]:
        if self._summaries_value is None:
            self._summaries_value = {}
        summaries = self._summaries_value
        for manifest in self._manifests():
            if project_id is not None and str(manifest.project_id) != project_id:
                continue
            for graph in manifest.graphs:
                if graph.graph_id not in summaries:
                    key = (str(self.workspace_id), "summary", graph.summary.sha256)
                    summary = self._cache.get(key)
                    if summary is None:
                        raw = self._read_object(
                            kind="summary", sha256=graph.summary.sha256
                        )
                        encoded = canonical_json(raw).encode()
                        if hashlib.sha256(encoded).hexdigest() != graph.summary.sha256:
                            raise RemoteControlPlaneError(
                                "prepared summary digest mismatch"
                            )
                        summary = PreparedGraphSummary.model_validate(raw)
                        self._cache.put(key, summary, len(encoded))
                    if (
                        summary.graph_id != graph.graph_id
                        or summary.fact_set_digest != graph.fact_set_digest
                    ):
                        raise RemoteControlPlaneError(
                            "prepared summary identity mismatch"
                        )
                    summaries[graph.graph_id] = summary
        return summaries

    def response_for(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        if method != "project.sessions":
            return None
        validated = service_contract(method).validate_request(params)
        project_id = validated.get("project_id")
        if validated.get("project_name"):
            project_id = self._inventory.resolve_name(validated["project_name"])
            if project_id is None:
                return {"items": []}
        summaries = self._summaries(project_id)
        items = [
            {**item, "project_id": str(manifest.project_id)}
            for manifest in self._manifests()
            if project_id is None or str(manifest.project_id) == project_id
            for graph in manifest.graphs
            for item in self._summary_for(summaries, graph.graph_id).project_sessions
        ]
        return filter_session_cards(items, {**validated, "project_name": None})

    @staticmethod
    def _summary_for(
        summaries: dict[UUID, PreparedGraphSummary], graph_id: UUID
    ) -> PreparedGraphSummary:
        try:
            return summaries[graph_id]
        except KeyError as exc:
            raise RemoteControlPlaneError(
                f"artifact manifest is missing detail for graph {graph_id}"
            ) from exc

    def store_for(self, method: str, params: dict[str, Any]) -> tuple[FactIndex, str]:
        manifests = self._manifests()
        if not manifests:
            raise RemoteControlPlaneError("artifact snapshot contains no graphs")
        aliases = entrypoint_ids_from_params(params)
        if not aliases:
            raise ValueError(f"{method} requires a graph or child entrypoint")
        selected: UUID | None = None
        summary_values = self._summaries()
        for raw in aliases:
            try:
                alias = UUID(raw)
            except ValueError:
                continue
            matches = [
                graph_id
                for graph_id, summary in summary_values.items()
                if alias in summary.aliases
            ]
            if len(matches) > 1:
                raise RemoteControlPlaneError("artifact alias maps to multiple graphs")
            if matches:
                if selected is not None and selected != matches[0]:
                    raise ValueError("request entrypoints select different graphs")
                selected = matches[0]
        if selected is None:
            raise RemoteControlPlaneError("artifact entrypoint was not found")
        graph = next(
            graph
            for manifest in manifests
            for graph in manifest.graphs
            if graph.graph_id == selected
        )
        key = (str(self.workspace_id), "facts", graph.facts.sha256)
        index = self._cache.get(key)
        if index is None:
            raw = self._read_object(kind="facts", sha256=graph.facts.sha256)
            encoded = canonical_json(raw).encode()
            if hashlib.sha256(encoded).hexdigest() != graph.facts.sha256:
                raise RemoteControlPlaneError("graph artifact digest mismatch")
            facts = PublishedFactSet.model_validate(raw)
            if (
                facts.graph_id != selected
                or facts.fact_set_digest != graph.fact_set_digest
            ):
                raise RemoteControlPlaneError("graph artifact identity mismatch")
            index = FactIndex.from_fact_sets([facts])
            self._cache.put(key, index, len(encoded))
        return index, f"remote artifact snapshot {self.snapshot_sequence}"

    def metadata(self) -> dict[str, Any]:
        return {
            "workspace_id": str(self.workspace_id),
            "snapshot_sequence": self.snapshot_sequence,
            "source": "remote",
            "freshness": "authoritative",
            "content_scope": "facts",
        }


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
        for key in ("session_id", "root_session_id", "turn_id", "item_id")
        if isinstance((value := params.get(key)), str) and value
    ]
    for key in ("session_ids", "item_ids", "event_ids"):
        values = params.get(key)
        if isinstance(values, list):
            ids.extend(value for value in values if isinstance(value, str) and value)
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
            "project_id",
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
    "ArtifactReadCache",
    "CloudflareArtifactRepository",
    "FactRepository",
    "LocalPublishedFactRepository",
    "discovery_params",
    "entrypoint_ids",
    "entrypoint_ids_from_params",
    "fact_index_for_store",
    "fact_store_key",
    "published_fact_set_for_store",
    "requires_graph_scope",
]
