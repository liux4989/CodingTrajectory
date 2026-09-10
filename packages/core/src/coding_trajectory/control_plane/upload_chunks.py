"""Content addressed canonical JSON nodes; wire fragments preserve numeric spelling.

Session/turn/item arrays are split at resource boundaries. Inserting an earlier
item rewrites index nodes, never the bodies of later items. This is transport
chunking, not a claim that vendor adapters incrementally construct Chronicle.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from coding_trajectory.ingestion.common import canonical_json

MAX_NODE_BYTES = 64 * 1024
MAX_BATCH_BYTES = 256 * 1024
MAX_BATCH_NODES = 128
MAX_GRAPH_BYTES = 8 * 1024 * 1024
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class UploadModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ChunkNode(UploadModel):
    kind: Literal["json", "object", "array", "concat"]
    fragment: str | None = Field(default=None, max_length=MAX_NODE_BYTES)
    entries: list[str] | dict[str, str] | None = None


class Chunk(UploadModel):
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    node: ChunkNode


class ChunkBatchRequest(UploadModel):
    workspace_id: UUID
    agent_id: UUID
    chunks: list[Chunk] = Field(min_length=1, max_length=MAX_BATCH_NODES)


class ChunkMissingRequest(UploadModel):
    workspace_id: UUID
    agent_id: UUID
    digests: list[Digest] = Field(min_length=1, max_length=MAX_BATCH_NODES)


class ChunkManifestRequest(UploadModel):
    workspace_id: UUID
    agent_id: UUID
    schema_version: Literal["ct.chronicle_graph.v2"] = "ct.chronicle_graph.v2"
    root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    uncompressed_bytes: int = Field(ge=1, le=MAX_GRAPH_BYTES)
    projections: dict[str, dict[str, Any]]


def build_chunks(payload: dict[str, Any]) -> tuple[str, dict[str, dict[str, Any]]]:
    """Return a deterministic DAG, bounded even for one oversized turn."""
    chunks: dict[str, dict[str, Any]] = {}

    def put(node: dict[str, Any]) -> str:
        encoded = canonical_json(node).encode()
        if len(encoded) > MAX_NODE_BYTES:
            raise ValueError("canonical chunk exceeds 64 KiB")
        digest = hashlib.sha256(encoded).hexdigest()
        chunks[digest] = node
        return digest

    def array(values: list[str]) -> str:
        if len(values) <= 128:
            return put({"kind": "array", "entries": values})
        groups = [array(values[i : i + 128]) for i in range(0, len(values), 128)]
        while len(groups) > 128:
            groups = [
                put({"kind": "concat", "entries": groups[i : i + 128]})
                for i in range(0, len(groups), 128)
            ]
        return put({"kind": "concat", "entries": groups})

    def visit(value: Any) -> str:
        # Resource-array children remain independent even when their parent is
        # small. An insertion changes index nodes, not unrelated resource bodies.
        if isinstance(value, list) and any(
            isinstance(v, dict)
            and any(k in v for k in ("session_id", "turn_id", "item_id"))
            for v in value
        ):
            return array([visit(v) for v in value])
        fragment = canonical_json(value)
        structural = isinstance(value, dict) and any(
            key in value for key in ("sessions", "turns", "items")
        )
        if not structural and len(fragment.encode()) < 24 * 1024:
            return put({"kind": "json", "fragment": fragment})
        if isinstance(value, dict):
            return put(
                {
                    "kind": "object",
                    "entries": {k: visit(v) for k, v in sorted(value.items())},
                }
            )
        if isinstance(value, list):
            return array([visit(v) for v in value])
        raise ValueError("oversized canonical scalar")

    return visit(payload), chunks


def chunk_batches(chunks: dict[str, dict[str, Any]]):
    batch: list[dict[str, Any]] = []
    size = 0
    for digest, node in chunks.items():
        entry = {"content_sha256": digest, "node": node}
        count = len(canonical_json(entry).encode())
        if batch and (
            size + count > MAX_BATCH_BYTES - 1024 or len(batch) == MAX_BATCH_NODES
        ):
            yield batch
            batch, size = [], 0
        batch.append(entry)
        size += count
    if batch:
        yield batch
