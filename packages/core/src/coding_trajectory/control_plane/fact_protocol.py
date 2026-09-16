"""Versioned contracts for typed publication fact staging, commit, and reads.

The Cloudflare authority stores fact rows in Durable Object SQLite and commits
one workspace publication sequence atomically. Collectors stage bounded fact
row batches, then publish references validated against the staged rows. Readers
fetch selected fact pages pinned to one workspace sequence; the shared Python
handlers reconstruct the same ``PublishedFactSet`` representation locally and
remotely, so semantics never diverge.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from coding_trajectory.control_plane.collector_protocol import (
    CollectorModel,
    SourceVectorEntry,
)
from coding_trajectory.control_plane.published_facts import (
    DERIVED_FACT_KINDS,
    FACT_SET_SCHEMA_VERSION,
    MAX_FACT_READ_PAGE_BYTES,
    MAX_FACT_ROWS_PER_GRAPH,
    FactRow,
    compute_fact_set_digest,
    compute_row_hash,
)
from coding_trajectory.ingestion.common import canonical_json

FACT_ROW_BATCH_MAX = 512
FACT_READ_PAGE_MAX = 2048
FACT_PUBLICATION_MAX_GRAPHS = 512
FACT_PUBLICATION_MAX_BYTES = 16 * 1024 * 1024

FactKind = Literal[
    "graph",
    "session",
    "turn",
    "item",
    "event",
    "edge",
    "request",
    "model",
    "runtime",
    "measurement",
    "output_evidence",
]
assert set(DERIVED_FACT_KINDS) == set(
    FactKind.__args__  # type: ignore[attr-defined]
), "fact kind literal diverged from published fact kinds"


class FactModelBase(CollectorModel):
    model_config = ConfigDict(extra="forbid")


class StageFactRowsRequest(FactModelBase):
    """One bounded batch of typed fact rows for one graph publication."""

    version: Literal[1] = 1
    workspace_id: UUID
    agent_id: UUID
    graph_id: UUID
    fact_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    batch_index: int = Field(ge=0)
    batch_count: int = Field(ge=1, le=MAX_FACT_ROWS_PER_GRAPH // FACT_ROW_BATCH_MAX + 1)
    rows: list[FactRow] = Field(min_length=1, max_length=FACT_ROW_BATCH_MAX)

    @model_validator(mode="after")
    def validate_batch(self) -> StageFactRowsRequest:
        if self.batch_index >= self.batch_count:
            raise ValueError("fact row batch index exceeds the declared batch count")
        keys = [(row.kind, row.fact_id) for row in self.rows]
        if len(set(keys)) != len(keys):
            raise ValueError("fact row batch contains duplicate facts")
        if any(row.graph_id != self.graph_id for row in self.rows):
            raise ValueError("fact row batch contains a foreign graph row")
        for row in self.rows:
            if compute_row_hash(row.hashable_view()) != row.row_hash:
                raise ValueError("fact row hash mismatch")
        return self


class StageFactRowsResponse(FactModelBase):
    graph_id: UUID
    fact_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    staged_batches: int = Field(ge=0)
    missing_batches: list[int] = Field(default_factory=list)


class MissingFactRowsRequest(FactModelBase):
    version: Literal[1] = 1
    workspace_id: UUID
    agent_id: UUID
    graph_id: UUID
    fact_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    batch_count: int = Field(ge=1, le=MAX_FACT_ROWS_PER_GRAPH // FACT_ROW_BATCH_MAX + 1)


class MissingFactRowsResponse(FactModelBase):
    graph_id: UUID
    fact_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    missing_batches: list[int] = Field(default_factory=list)


class FactGraphPublication(FactModelBase):
    """Validated reference to one fully staged graph fact set."""

    graph_id: UUID
    schema_version: Literal["ct.published_facts.v1"] = FACT_SET_SCHEMA_VERSION
    fact_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    fact_count: int = Field(ge=1, le=MAX_FACT_ROWS_PER_GRAPH)
    kind_counts: dict[str, int]
    source_ids: list[UUID] = Field(min_length=1)
    observed_at: datetime

    @model_validator(mode="after")
    def validate_graph(self) -> FactGraphPublication:
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("graph publication source_ids must be unique")
        if set(self.kind_counts) - set(DERIVED_FACT_KINDS):
            raise ValueError("graph publication declares an unknown fact kind")
        if self.kind_counts.get("graph") != 1:
            raise ValueError("graph publication requires exactly one graph row")
        if sum(self.kind_counts.values()) != self.fact_count:
            raise ValueError("graph publication fact count mismatch")
        return self


class FactPublicationRequest(FactModelBase):
    """One atomic workspace publication of complete graph fact sets.

    With ``replacement_scope="complete_sources"``, graphs previously published
    from a source in ``source_vector`` that are absent from ``graphs`` are
    tombstoned in the same sequence.
    """

    version: Literal[1] = 1
    workspace_id: UUID
    agent_id: UUID
    project_id: UUID
    publication_sequence: int = Field(ge=0)
    replacement_scope: Literal["upsert", "complete_sources"] | None = None
    source_vector: list[SourceVectorEntry] = Field(min_length=1)
    graphs: list[FactGraphPublication] = Field(
        min_length=1, max_length=FACT_PUBLICATION_MAX_GRAPHS
    )

    @model_validator(mode="after")
    def validate_publication(self) -> FactPublicationRequest:
        source_ids = [entry.source_id for entry in self.source_vector]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("publication source_vector must contain unique sources")
        known = set(source_ids)
        graph_ids = [graph.graph_id for graph in self.graphs]
        if len(set(graph_ids)) != len(graph_ids):
            raise ValueError("publication graph_id values must be unique")
        digests = [graph.fact_set_digest for graph in self.graphs]
        if len(set(digests)) != len(digests):
            raise ValueError("publication fact-set digests must be unique")
        if any(not set(graph.source_ids) <= known for graph in self.graphs):
            raise ValueError("graph source_ids must be present in source_vector")
        represented = {
            source_id for graph in self.graphs for source_id in graph.source_ids
        }
        if represented != known:
            raise ValueError("every source_vector entry must belong to a graph")
        return self

    def wire_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class FactReadRequest(FactModelBase):
    """Select one page of fact rows pinned to one workspace sequence.

    The server resolves the matching graph set first (by ``graph_id``,
    ``session_id``, ``project_name``, ``agent_vendor``, and ``modified_since``),
    then streams rows in deterministic ``(graph_id, kind, fact_id)`` order.
    Cursors preserve a pinned selector while row and encoded-byte limits bound
    each page.
    """

    version: Literal[1] = 1
    workspace_id: UUID
    graph_id: UUID | None = None
    session_id: UUID | None = None
    kinds: list[FactKind] | None = Field(
        default=None, max_length=len(DERIVED_FACT_KINDS)
    )
    project_name: str | None = Field(default=None, max_length=512)
    agent_vendor: str | None = Field(default=None, max_length=512)
    modified_since: datetime | None = None
    snapshot_sequence: int | None = Field(default=None, ge=0)
    limit: int = Field(default=FACT_READ_PAGE_MAX, ge=1, le=FACT_READ_PAGE_MAX)
    cursor: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def validate_scope(self) -> FactReadRequest:
        if self.graph_id is not None and self.session_id is not None:
            raise ValueError("fact reads select one graph or one session, not both")
        return self


class FactReadResponse(FactModelBase):
    workspace_id: UUID
    snapshot_sequence: int = Field(ge=0)
    rows: list[FactRow] = Field(max_length=FACT_READ_PAGE_MAX)
    graph_digests: dict[str, str] = Field(max_length=FACT_PUBLICATION_MAX_GRAPHS)
    graph_fact_counts: dict[str, int] = Field(max_length=FACT_PUBLICATION_MAX_GRAPHS)
    next_cursor: str | None = None

    @model_validator(mode="after")
    def validate_page(self) -> FactReadResponse:
        if set(self.graph_digests) != set(self.graph_fact_counts):
            raise ValueError("fact read graph digests and counts diverge")
        for digest in self.graph_digests.values():
            if not digest or len(digest) != 64:
                raise ValueError("fact read returned an invalid graph digest")
        if (
            len(
                canonical_json(
                    [
                        row.model_dump(mode="json", exclude_none=True)
                        for row in self.rows
                    ]
                ).encode()
            )
            > MAX_FACT_READ_PAGE_BYTES
        ):
            raise ValueError("fact read page exceeds the 1 MiB row budget")
        return self


__all__ = [
    "FACT_PUBLICATION_MAX_BYTES",
    "FACT_PUBLICATION_MAX_GRAPHS",
    "FACT_READ_PAGE_MAX",
    "FACT_ROW_BATCH_MAX",
    "FactGraphPublication",
    "FactPublicationRequest",
    "FactReadRequest",
    "FactReadResponse",
    "MissingFactRowsRequest",
    "MissingFactRowsResponse",
    "StageFactRowsRequest",
    "StageFactRowsResponse",
    "compute_fact_set_digest",
]
