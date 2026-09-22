"""Immutable graph-artifact and atomic snapshot-manifest contracts.

Collectors remain the compute authority: they parse the complete available
inventory, validate canonical facts, and prepare list summaries.  Cloudflare
stores those immutable values and only publishes a manifest after every object
is present.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from coding_trajectory.contracts.prepared_api import (
    MAX_API_RESPONSE_BYTES,
    PreparedMethod,
    PreparedObject,
)
from coding_trajectory.control_plane.collector_protocol import (
    CollectorModel,
    SourceVectorEntry,
)
from coding_trajectory.control_plane.fact_constants import (
    FACT_SET_SCHEMA_VERSION,
    MAX_FACT_ROWS_PER_GRAPH,
)

ARTIFACT_PREPARATION_VERSION = "ct.graph-preparation.v6"
ARTIFACT_SUMMARY_SCHEMA_VERSION = "ct.prepared-summary.v2"
ARTIFACT_MANIFEST_SCHEMA_VERSION = "ct.artifact-manifest.v2"
ARTIFACT_RETENTION = 3
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_SUMMARY_BYTES = 4 * 1024 * 1024
MAX_ARTIFACT_GRAPHS = 512
MAX_ARTIFACT_ALIASES = 262_144


class ArtifactReadinessReference(CollectorModel):
    kind: Literal["facts", "summary", "api"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(ge=1, le=MAX_ARTIFACT_BYTES)
    requires_index: bool = False


class ArtifactReadinessRequest(CollectorModel):
    workspace_id: UUID
    agent_id: UUID
    objects: list[ArtifactReadinessReference] = Field(min_length=1, max_length=512)


class ArtifactReadinessResponse(CollectorModel):
    # Positional results attest current authority state, not a retention lease.
    ready: list[Annotated[bool, Field(strict=True)]] = Field(
        min_length=1, max_length=512
    )


class PreparedGraphSummary(CollectorModel):
    """Small, validated read projection stored separately from graph facts."""

    schema_version: Literal["ct.prepared-summary.v2"] = ARTIFACT_SUMMARY_SCHEMA_VERSION
    preparation_version: Literal[
        "ct.graph-preparation.v3",
        "ct.graph-preparation.v4",
        "ct.graph-preparation.v5",
        "ct.graph-preparation.v6",
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


class ArtifactGraphMetadata(CollectorModel):
    graph_id: UUID
    graph_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fact_schema_version: Literal["ct.published_facts.v2"] = FACT_SET_SCHEMA_VERSION
    fact_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    fact_count: int = Field(ge=1, le=MAX_FACT_ROWS_PER_GRAPH)
    source_ids: list[UUID] = Field(min_length=1)
    vendors: list[str] = Field(min_length=1, max_length=16)
    observed_at: datetime
    facts: ArtifactObjectReference
    summary: ArtifactObjectReference


class ArtifactGraphPublication(ArtifactGraphMetadata):
    api_methods: list[PreparedMethod] = Field(max_length=131_072)
    api_objects: list[PreparedObject] = Field(max_length=131_072)

    @model_validator(mode="after")
    def validate_objects(self) -> ArtifactGraphPublication:
        if self.facts.kind != "facts" or self.summary.kind != "summary":
            raise ValueError("graph artifact references use the wrong object kind")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("graph artifact source_ids must be unique")
        objects = {ref.sha256 for ref in self.api_objects}
        if len(objects) != len(self.api_objects):
            raise ValueError("duplicate prepared API object")
        for method in self.api_methods:
            if (method.index is None) == (method.error is None):
                raise ValueError(
                    "prepared method requires exactly one index or size error"
                )
            if method.index and method.index.sha256 not in objects:
                raise ValueError("prepared method index is not uploaded")
        return self


class ArtifactPublicationRequest(CollectorModel):
    """A complete source inventory published only after immutable uploads."""

    version: Literal[1] = 1
    schema_version: Literal["ct.artifact-manifest.v2"] = (
        ARTIFACT_MANIFEST_SCHEMA_VERSION
    )
    preparation_version: Literal[
        "ct.graph-preparation.v3",
        "ct.graph-preparation.v4",
        "ct.graph-preparation.v5",
        "ct.graph-preparation.v6",
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
    api_methods: list[PreparedMethod]
    api_objects: list[PreparedObject]


class ArtifactManifest(CollectorModel):
    schema_version: Literal["ct.artifact-manifest.v2"]
    preparation_version: Literal[
        "ct.graph-preparation.v3",
        "ct.graph-preparation.v4",
        "ct.graph-preparation.v5",
        "ct.graph-preparation.v6",
    ]
    workspace_id: UUID
    project_id: UUID
    publisher_agent_id: UUID
    publication_sequence: int = Field(ge=0)
    snapshot_sequence: int = Field(ge=1)
    published_at: datetime
    inventory_state: Literal["complete"]
    graphs: list[ArtifactManifestGraph] = Field(max_length=MAX_ARTIFACT_GRAPHS)

    @model_validator(mode="before")
    @classmethod
    def expand_compact(cls, value: Any) -> Any:
        if (
            isinstance(value, dict)
            and value.get("schema_version") == "ct.artifact-manifest.v3"
        ):
            return {
                **value,
                "schema_version": ARTIFACT_MANIFEST_SCHEMA_VERSION,
                "graphs": [expand_graph(graph) for graph in value["graphs"]],
            }
        return value


Position = Annotated[int, Field(strict=True, ge=0, le=131_071)]
ObjectHash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ObjectBytes = Annotated[int, Field(strict=True, ge=1, le=MAX_API_RESPONSE_BYTES)]


class CompactApi(CollectorModel):
    """Ordered tables; a null object position denotes remote_result_too_large."""

    objects: list[tuple[ObjectHash, ObjectBytes]] = Field(max_length=131_072)
    methods: list[tuple[str, int]] = Field(max_length=131_072)
    scopes: list[str] = Field(max_length=131_072)
    entries: list[tuple[Position, Position, str | None, Position | None]] = Field(
        max_length=131_072
    )

    @model_validator(mode="after")
    def validate_positions(self) -> CompactApi:
        if len({item[0] for item in self.objects}) != len(self.objects):
            raise ValueError("duplicate prepared API object")
        for method, scope, _, index in self.entries:
            if (
                method >= len(self.methods)
                or scope >= len(self.scopes)
                or (index is not None and index >= len(self.objects))
            ):
                raise ValueError("invalid compact API table position")
        return self


class CompactGraphPublication(ArtifactGraphMetadata):
    api: CompactApi


class CompactPublicationRequest(ArtifactPublicationRequest):
    schema_version: Literal["ct.artifact-manifest.v3"] = "ct.artifact-manifest.v3"
    graphs: list[CompactGraphPublication] = Field(max_length=MAX_ARTIFACT_GRAPHS)


def compact_graph(graph: dict[str, Any]) -> dict[str, Any]:
    objects = graph["api_objects"]
    positions = {ref["sha256"]: i for i, ref in enumerate(objects)}
    methods: dict[tuple[str, int], int] = {}
    scopes: dict[str, int] = {}
    entries = []
    for method in graph["api_methods"]:
        key = (method["method"], method["method_version"])
        method_pos = methods.setdefault(key, len(methods))
        scope_pos = scopes.setdefault(method["scope"], len(scopes))
        ref = method.get("index")
        if ref is not None and objects[positions[ref["sha256"]]] != ref:
            raise ValueError("inconsistent prepared index reference")
        entries.append(
            (
                method_pos,
                scope_pos,
                method.get("turn_id"),
                positions[ref["sha256"]] if ref else None,
            )
        )
    return {
        **{k: v for k, v in graph.items() if k not in {"api_objects", "api_methods"}},
        "api": {
            "objects": [(ref["sha256"], ref["bytes"]) for ref in objects],
            "methods": list(methods),
            "scopes": list(scopes),
            "entries": entries,
        },
    }


def expand_graph(graph: dict[str, Any]) -> dict[str, Any]:
    api = CompactApi.model_validate(graph["api"])
    objects = [
        PreparedObject(sha256=hash_, bytes=size).model_dump()
        for hash_, size in api.objects
    ]
    methods = [
        {
            "method": api.methods[m][0],
            "method_version": api.methods[m][1],
            "scope": api.scopes[s],
            "turn_id": turn,
            "index": objects[index] if index is not None else None,
            "error": None if index is not None else "remote_result_too_large",
        }
        for m, s, turn, index in api.entries
    ]
    return {
        **{k: v for k, v in graph.items() if k != "api"},
        "api_objects": objects,
        "api_methods": methods,
    }


def compact_publication(request: ArtifactPublicationRequest) -> dict[str, Any]:
    value = request.model_dump(mode="json")
    value["schema_version"] = "ct.artifact-manifest.v3"
    value["graphs"] = [compact_graph(graph) for graph in value["graphs"]]
    return CompactPublicationRequest.model_validate(value).model_dump(mode="json")


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
