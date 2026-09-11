"""Bounded shareable read projections computed by canonical Python handlers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from coding_trajectory.contracts import service_contract
from coding_trajectory.control_plane.catalog_protocol import ResourceProjections
from coding_trajectory.control_plane.chronicle import ChronicleGraphArtifact
from coding_trajectory.ingestion.common import canonical_json
from coding_trajectory.query import DocumentStore
from coding_trajectory.service import IndexCache, dispatch

MAX_READ_PROJECTION_BYTES = 128 * 1024


def build_read_projections(
    artifact: ChronicleGraphArtifact,
    *,
    store: DocumentStore | None = None,
    cache: IndexCache | None = None,
) -> dict[str, Any]:
    if store is None:
        store = DocumentStore.from_session_graphs([artifact.to_session_graph()])
    if cache is None:
        cache = IndexCache()

    def call(method: str, params: dict[str, Any]):
        result = dispatch(
            method,
            params,
            store=store,
            global_scope=True,
            current_dir=Path("/"),
            discovery_note="canonical projection",
            cache=cache,
        )
        return service_contract(method).validate_response(result)

    root = str(artifact.graph.root_session_id)
    result: dict[str, Any] = {
        "schema_version": "ct.canonical_read.v1",
        "content_sha256": artifact.digest(),
        "root_session_id": root,
        "graph": None,
        "trees": {},
        "items": [],
    }

    def fits() -> bool:
        return len(canonical_json(result).encode()) <= MAX_READ_PROJECTION_BYTES

    result["graph"] = {
        "root_session_id": root,
        "overview": call("graph.overview", {"root_session_id": root}),
        "stats": call(
            "graph.stats", {"root_session_id": root, "include": ["session_composition"]}
        ),
        "usage": call("graph.usage", {"root_session_id": root}),
    }
    if not fits():
        result["graph"] = None
    for session in artifact.sessions:
        session_id = str(session.session_id)
        result["trees"][session_id] = call("session.tree", {"session_id": session_id})
        if not fits():
            del result["trees"][session_id]
    ids = [str(item) for item in store.items]
    for offset in range(0, len(ids), 100):
        page = call(
            "session.items",
            {"item_ids": ids[offset : offset + 100], "include_content": False},
        )
        result["items"].extend(page)
        if not fits():
            del result["items"][-len(page) :]
            break
    return result


def build_resource_projections(
    artifact: ChronicleGraphArtifact, *, store: DocumentStore, cache: IndexCache
) -> dict[str, Any]:
    """Independent projections; one oversized tree never hides unrelated items.

    Preparation still uses a whole local graph. These are bounded read resources,
    not incremental parsing or the eventual paged topology contract.
    """
    rows: list[dict[str, Any]] = []

    def add(kind: str, resource_id: str, value: dict[str, Any]) -> None:
        available = len(canonical_json(value).encode()) <= MAX_READ_PROJECTION_BYTES
        rows.append(
            {
                "resource_kind": kind,
                "resource_id": resource_id,
                "page_index": 0,
                "page_count": 1,
                "coverage": "complete" if available else "budget_exceeded",
                "payload": value if available else None,
            }
        )

    def call(method: str, params: dict[str, Any]) -> Any:
        return service_contract(method).validate_response(
            dispatch(
                method,
                params,
                store=store,
                global_scope=True,
                current_dir=Path("/"),
                discovery_note="canonical resource projection",
                cache=cache,
            )
        )

    root = str(artifact.graph.root_session_id)
    add(
        "graph",
        root,
        {
            "root_session_id": root,
            "overview": call("graph.overview", {"root_session_id": root}),
            "stats": call(
                "graph.stats",
                {"root_session_id": root, "include": ["session_composition"]},
            ),
            "usage": call("graph.usage", {"root_session_id": root}),
        },
    )
    for session in artifact.sessions:
        session_id = str(session.session_id)
        tree = call("session.tree", {"session_id": session_id})
        if len(canonical_json(tree).encode()) <= MAX_READ_PROJECTION_BYTES:
            add("tree", session_id, tree)
            continue
        # Page at semantic branch boundaries.  Never expose fragments of a JSON
        # document; consumers concatenate branches in page order.
        pages: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for branch in tree["branches"]:
            candidate = {
                "root_session_id": tree["root_session_id"],
                "branches": [*current, branch],
            }
            if (
                current
                and len(canonical_json(candidate).encode()) > MAX_READ_PROJECTION_BYTES
            ):
                pages.append(current)
                current = []
            single = {"root_session_id": tree["root_session_id"], "branches": [branch]}
            if len(canonical_json(single).encode()) > MAX_READ_PROJECTION_BYTES:
                raise ValueError(
                    "single conversation-tree branch exceeds resource budget"
                )
            current.append(branch)
        pages.append(current)
        count = len(pages)
        for index, branches in enumerate(pages):
            rows.append(
                {
                    "resource_kind": "tree",
                    "resource_id": session_id,
                    "page_index": index,
                    "page_count": count,
                    "coverage": "complete",
                    "payload": {
                        "root_session_id": tree["root_session_id"],
                        "branches": branches,
                    },
                }
            )
    ids = [str(item) for item in store.items]
    for offset in range(0, len(ids), 100):
        for item in call(
            "session.items",
            {"item_ids": ids[offset : offset + 100], "include_content": False},
        ):
            add("item", item["item_id"], item)
    result = {"schema_version": "ct.resource_projections.v2", "rows": rows}
    ResourceProjections.model_validate(result)
    if len(canonical_json(result).encode()) > 4 * 1024 * 1024:
        raise ValueError("resource projection staging budget exceeded")
    return result
