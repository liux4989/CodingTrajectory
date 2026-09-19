"""Immutable graph-artifact and atomic snapshot-manifest contracts.

Collectors remain the compute authority: they parse the complete available
inventory, validate canonical facts, and prepare list summaries.  Cloudflare
stores those immutable values and only publishes a manifest after every object
is present.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from coding_trajectory.control_plane.collector_protocol import (
    CollectorModel,
    SourceVectorEntry,
)
from coding_trajectory.control_plane.published_facts import (
    FACT_SET_SCHEMA_VERSION,
    MAX_FACT_ROWS_PER_GRAPH,
)

ARTIFACT_PREPARATION_VERSION = "ct.graph-preparation.v2"
ARTIFACT_SUMMARY_SCHEMA_VERSION = "ct.prepared-summary.v1"
ARTIFACT_MANIFEST_SCHEMA_VERSION = "ct.artifact-manifest.v1"
ARTIFACT_RETENTION = 3
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_SUMMARY_BYTES = 4 * 1024 * 1024
MAX_ARTIFACT_GRAPHS = 512
MAX_ARTIFACT_ALIASES = 262_144


class PreparedGraphSummary(CollectorModel):
    """Small, validated read projection stored separately from graph facts."""

    schema_version: Literal["ct.prepared-summary.v1"] = ARTIFACT_SUMMARY_SCHEMA_VERSION
    preparation_version: Literal[
        "ct.graph-preparation.v1", "ct.graph-preparation.v2"
    ] = ARTIFACT_PREPARATION_VERSION
    graph_id: UUID
    fact_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    aliases: list[UUID] = Field(max_length=MAX_ARTIFACT_ALIASES)
    project_sessions: list[dict[str, Any]]

    @model_validator(mode="after")
    def validate_aliases(self) -> PreparedGraphSummary:
        if self.graph_id not in self.aliases:
            raise ValueError("prepared summary omits its graph root alias")
        if len(set(self.aliases)) != len(self.aliases):
            raise ValueError("prepared summary aliases must be unique")
        return self


class ArtifactObjectReference(CollectorModel):
    kind: Literal["facts", "summary"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(ge=1, le=MAX_ARTIFACT_BYTES)

    @model_validator(mode="after")
    def validate_kind_bound(self) -> ArtifactObjectReference:
        if self.kind == "summary" and self.bytes > MAX_SUMMARY_BYTES:
            raise ValueError("prepared summary exceeds its byte bound")
        return self


class ArtifactGraphPublication(CollectorModel):
    graph_id: UUID
    graph_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fact_schema_version: Literal["ct.published_facts.v1"] = FACT_SET_SCHEMA_VERSION
    fact_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    fact_count: int = Field(ge=1, le=MAX_FACT_ROWS_PER_GRAPH)
    source_ids: list[UUID] = Field(min_length=1)
    vendors: list[str] = Field(min_length=1, max_length=16)
    observed_at: datetime
    facts: ArtifactObjectReference
    summary: ArtifactObjectReference

    @model_validator(mode="after")
    def validate_objects(self) -> ArtifactGraphPublication:
        if self.facts.kind != "facts" or self.summary.kind != "summary":
            raise ValueError("graph artifact references use the wrong object kind")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("graph artifact source_ids must be unique")
        return self


class ArtifactPublicationRequest(CollectorModel):
    """A complete source inventory published only after immutable uploads."""

    version: Literal[1] = 1
    schema_version: Literal["ct.artifact-manifest.v1"] = (
        ARTIFACT_MANIFEST_SCHEMA_VERSION
    )
    preparation_version: Literal[
        "ct.graph-preparation.v1", "ct.graph-preparation.v2"
    ] = ARTIFACT_PREPARATION_VERSION
    workspace_id: UUID
    agent_id: UUID
    project_id: UUID
    publication_sequence: int = Field(ge=0)
    inventory_state: Literal["complete"] = "complete"
    source_vector: list[SourceVectorEntry] = Field(max_length=1000)
    graphs: list[ArtifactGraphPublication] = Field(max_length=MAX_ARTIFACT_GRAPHS)

    @model_validator(mode="after")
    def validate_inventory(self) -> ArtifactPublicationRequest:
        source_ids = [entry.source_id for entry in self.source_vector]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("artifact source_vector must contain unique sources")
        graph_ids = [graph.graph_id for graph in self.graphs]
        if len(set(graph_ids)) != len(graph_ids):
            raise ValueError("artifact publication graph IDs must be unique")
        known = set(source_ids)
        represented = {
            source_id for graph in self.graphs for source_id in graph.source_ids
        }
        if any(not set(graph.source_ids) <= known for graph in self.graphs):
            raise ValueError("artifact graph references an unknown source")
        if represented != known:
            raise ValueError("complete inventory must represent every source")
        return self


class ArtifactManifestGraph(CollectorModel):
    graph_id: UUID
    fact_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    fact_count: int = Field(ge=1)
    observed_at: datetime
    vendors: list[str] = Field(min_length=1, max_length=16)
    facts: ArtifactObjectReference
    summary: ArtifactObjectReference


class ArtifactManifest(CollectorModel):
    schema_version: Literal["ct.artifact-manifest.v1"]
    preparation_version: Literal["ct.graph-preparation.v1", "ct.graph-preparation.v2"]
    workspace_id: UUID
    project_id: UUID
    publisher_agent_id: UUID
    publication_sequence: int = Field(ge=0)
    snapshot_sequence: int = Field(ge=1)
    published_at: datetime
    inventory_state: Literal["complete"]
    graphs: list[ArtifactManifestGraph] = Field(max_length=MAX_ARTIFACT_GRAPHS)


class ArtifactManifestRequest(CollectorModel):
    workspace_id: UUID
    snapshot_sequence: int | None = Field(default=None, ge=0)
    project_id: UUID | None = None


class ArtifactReadRequest(CollectorModel):
    workspace_id: UUID
    snapshot_sequence: int
    kind: Literal["facts", "summary"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


__all__ = [
    "ARTIFACT_MANIFEST_SCHEMA_VERSION",
    "ARTIFACT_PREPARATION_VERSION",
    "ARTIFACT_RETENTION",
    "ARTIFACT_SUMMARY_SCHEMA_VERSION",
    "MAX_ARTIFACT_BYTES",
    "MAX_SUMMARY_BYTES",
    "ArtifactGraphPublication",
    "ArtifactManifest",
    "ArtifactManifestRequest",
    "ArtifactObjectReference",
    "ArtifactPublicationRequest",
    "ArtifactReadRequest",
    "PreparedGraphSummary",
]
