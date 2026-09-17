"""Validate and import one frozen, privacy-filtered artifact replacement.

The bundle contains only canonical published facts, prepared summaries, and the
metadata needed to recreate source checkpoints. Raw provider records remain on
the source host and are never part of this contract.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from coding_trajectory.control_plane.artifact_protocol import (
    MAX_ARTIFACT_GRAPHS,
    ArtifactGraphPublication,
    ArtifactObjectReference,
    ArtifactPublicationRequest,
    PreparedGraphSummary,
)
from coding_trajectory.control_plane.collector import CloudflareCollectorRemote
from coding_trajectory.control_plane.collector_protocol import (
    CollectorModel,
    ObservationRequest,
    ProjectRegistrationRequest,
    SourceRegistrationRequest,
    SourceVectorEntry,
)
from coding_trajectory.control_plane.published_facts import PublishedFactSet
from coding_trajectory.control_plane.remote import CloudflareRpcClient
from coding_trajectory.ingestion.common import canonical_json


class ReplacementSource(CollectorModel):
    source_key: UUID
    vendor: str = Field(min_length=1, max_length=64)
    observed_at: datetime
    parser_version: str = Field(min_length=1, max_length=128)
    checkpoint_segments: list[int] = Field(min_length=1, max_length=128)
    session_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_segments(self) -> ReplacementSource:
        if any(value < 1 for value in self.checkpoint_segments):
            raise ValueError("replacement checkpoint offsets must be positive")
        return self


class ReplacementGraph(CollectorModel):
    graph_id: UUID
    graph_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fact_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    fact_count: int = Field(ge=1)
    source_keys: list[UUID] = Field(min_length=1)
    vendors: list[str] = Field(min_length=1, max_length=16)
    observed_at: datetime
    facts: ArtifactObjectReference
    summary: ArtifactObjectReference

    @model_validator(mode="after")
    def validate_references(self) -> ReplacementGraph:
        if self.facts.kind != "facts" or self.summary.kind != "summary":
            raise ValueError("replacement graph object kinds are invalid")
        if len(self.source_keys) != len(set(self.source_keys)):
            raise ValueError("replacement graph source keys must be unique")
        if len(self.vendors) != len(set(self.vendors)):
            raise ValueError("replacement graph vendors must be unique")
        return self


class ArtifactReplacement(CollectorModel):
    schema_version: Literal["ct.artifact-replacement.v1"]
    privacy_contract: Literal["ct.published-facts-only.v1"]
    raw_sources_included: Literal[False]
    workspace_id: UUID
    agent_id: UUID
    generated_at: datetime
    since_days: Literal[7]
    project_name: str = Field(min_length=1, max_length=512)
    repository_identity: str | None = Field(default=None, max_length=512)
    project_aliases: list[str] = Field(default_factory=list, max_length=128)
    sources: list[ReplacementSource] = Field(min_length=1, max_length=1000)
    graphs: list[ReplacementGraph] = Field(min_length=1, max_length=MAX_ARTIFACT_GRAPHS)
    object_count: int = Field(ge=2)
    total_bytes: int = Field(ge=2)
    replacement_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_inventory(self) -> ArtifactReplacement:
        source_keys = [source.source_key for source in self.sources]
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("replacement source keys must be unique")
        graph_ids = [graph.graph_id for graph in self.graphs]
        if len(graph_ids) != len(set(graph_ids)):
            raise ValueError("replacement graph IDs must be unique")
        known = set(source_keys)
        represented = {key for graph in self.graphs for key in graph.source_keys}
        if represented != known:
            raise ValueError(
                "replacement complete inventory must represent every source"
            )
        if self.generated_at.utcoffset() != timedelta(0):
            raise ValueError("replacement generated_at must be UTC")
        start = self.generated_at - timedelta(days=self.since_days)
        observed = [source.observed_at for source in self.sources] + [
            graph.observed_at for graph in self.graphs
        ]
        if any(
            value.utcoffset() != timedelta(0)
            or value < start
            or value > self.generated_at
            for value in observed
        ):
            raise ValueError(
                "replacement observations must be inside its seven-day window"
            )
        return self


class ValidatedReplacement:
    def __init__(
        self,
        *,
        root: Path,
        manifest: ArtifactReplacement,
        objects: dict[tuple[str, str], bytes],
    ) -> None:
        self.root = root
        self.manifest = manifest
        self.objects = objects

    def summary(self) -> dict[str, Any]:
        return {
            "schema_version": self.manifest.schema_version,
            "workspace_id": str(self.manifest.workspace_id),
            "agent_id": str(self.manifest.agent_id),
            "replacement_sha256": self.manifest.replacement_sha256,
            "since_days": self.manifest.since_days,
            "sources": len(self.manifest.sources),
            "graphs": len(self.manifest.graphs),
            "objects": len(self.objects),
            "bytes": sum(len(body) for body in self.objects.values()),
            "privacy_contract": self.manifest.privacy_contract,
            "raw_sources_included": self.manifest.raw_sources_included,
        }


def replacement_digest(value: dict[str, Any]) -> str:
    basis = {key: item for key, item in value.items() if key != "replacement_sha256"}
    return hashlib.sha256(canonical_json(basis).encode()).hexdigest()


def load_replacement(path: Path) -> ValidatedReplacement:
    root = path.expanduser().resolve()
    raw = json.loads((root / "replacement.json").read_text(encoding="utf-8"))
    manifest = ArtifactReplacement.model_validate(raw)
    if replacement_digest(raw) != manifest.replacement_sha256:
        raise ValueError("replacement manifest digest mismatch")
    objects: dict[tuple[str, str], bytes] = {}
    referenced: set[Path] = set()
    for graph in manifest.graphs:
        for reference in (graph.facts, graph.summary):
            relative = Path("objects") / reference.kind / f"{reference.sha256}.json"
            body = (root / relative).read_bytes()
            if len(body) != reference.bytes:
                raise ValueError("replacement object byte count mismatch")
            if hashlib.sha256(body).hexdigest() != reference.sha256:
                raise ValueError("replacement object digest mismatch")
            _validate_object(reference.kind, body, graph)
            objects[(reference.kind, reference.sha256)] = body
            referenced.add(relative)
    present = {
        value.relative_to(root)
        for value in (root / "objects").glob("*/*.json")
        if value.is_file()
    }
    if present != referenced:
        raise ValueError("replacement object inventory is not exact")
    if manifest.object_count != len(objects):
        raise ValueError("replacement object count mismatch")
    if manifest.total_bytes != sum(len(body) for body in objects.values()):
        raise ValueError("replacement total byte count mismatch")
    return ValidatedReplacement(root=root, manifest=manifest, objects=objects)


def import_replacement(
    replacement: ValidatedReplacement, *, url: str, access_token: str
) -> dict[str, Any]:
    manifest = replacement.manifest
    rpc = CloudflareRpcClient(url=url, access_token=access_token)
    remote = CloudflareCollectorRemote(url=url, access_token=access_token)
    try:
        _require_clean_or_resumable_target(rpc, manifest)
        project = remote.register_project(
            ProjectRegistrationRequest(
                workspace_id=manifest.workspace_id,
                agent_id=manifest.agent_id,
                display_name=manifest.project_name,
                repository_identity=manifest.repository_identity,
                aliases=manifest.project_aliases,
            )
        )
        vectors: dict[UUID, SourceVectorEntry] = {}
        for source in manifest.sources:
            registration = remote.register_source(
                SourceRegistrationRequest(
                    workspace_id=manifest.workspace_id,
                    agent_id=manifest.agent_id,
                    vendor=source.vendor,
                    native_session_id=f"replacement:{source.source_key}",
                    project_id=project.project_id,
                ),
                idempotency_key=(
                    f"replacement:{manifest.replacement_sha256}:source:{source.source_key}"
                ),
            )
            payload = {
                "kind": "ct.source_checkpoint.v1",
                "source_checkpoint": {"segments": source.checkpoint_segments},
                "session_digest": source.session_digest,
            }
            content = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
            checkpoint_receipt = remote.publish_observation(
                ObservationRequest(
                    workspace_id=manifest.workspace_id,
                    agent_id=manifest.agent_id,
                    source_id=registration.source_id,
                    source_epoch=registration.source_epoch,
                    source_sequence=0,
                    event_id=f"checkpoint:{content}",
                    parser_version=source.parser_version,
                    content_sha256=content,
                    observed_at=source.observed_at,
                    payload=payload,
                ),
                idempotency_key=(
                    f"replacement:{manifest.replacement_sha256}:checkpoint:{source.source_key}"
                ),
            )
            if checkpoint_receipt.outcome not in {"accepted", "duplicate"}:
                raise ValueError("replacement source checkpoint was not accepted")
            vectors[source.source_key] = SourceVectorEntry(
                source_id=registration.source_id,
                source_epoch=registration.source_epoch,
                source_sequence=0,
                content_sha256=content,
            )
        for (kind, sha256), body in replacement.objects.items():
            remote.upload_artifact(kind=kind, sha256=sha256, body=body)
        request = ArtifactPublicationRequest(
            workspace_id=manifest.workspace_id,
            agent_id=manifest.agent_id,
            project_id=project.project_id,
            publication_sequence=0,
            source_vector=list(vectors.values()),
            graphs=[
                ArtifactGraphPublication(
                    graph_id=graph.graph_id,
                    graph_input_sha256=graph.graph_input_sha256,
                    fact_set_digest=graph.fact_set_digest,
                    fact_count=graph.fact_count,
                    source_ids=[vectors[key].source_id for key in graph.source_keys],
                    vendors=graph.vendors,
                    observed_at=graph.observed_at,
                    facts=graph.facts,
                    summary=graph.summary,
                )
                for graph in manifest.graphs
            ],
        )
        receipt = remote.publish_artifacts(
            request,
            idempotency_key=f"replacement:{manifest.replacement_sha256}:publication",
        )
        if receipt.outcome not in {"accepted", "duplicate"}:
            raise ValueError("replacement publication was not accepted")
        return verify_replacement(replacement, url=url, access_token=access_token)
    finally:
        remote.close()
        rpc.close()


def verify_replacement(
    replacement: ValidatedReplacement, *, url: str, access_token: str
) -> dict[str, Any]:
    manifest = replacement.manifest
    rpc = CloudflareRpcClient(url=url, access_token=access_token)
    try:
        snapshot = rpc.call(
            "ct_workspace_snapshot", {"workspace_id": str(manifest.workspace_id)}
        )
        result = rpc.call(
            "ct_artifact_manifest",
            {
                "workspace_id": str(manifest.workspace_id),
                "snapshot_sequence": snapshot["snapshot_sequence"],
            },
        )
        if len(result["manifests"]) != 1:
            raise ValueError("replacement manifest count mismatch")
        remote_manifest = result["manifests"][0]
        if (
            remote_manifest.get("workspace_id") != str(manifest.workspace_id)
            or remote_manifest.get("publisher_agent_id") != str(manifest.agent_id)
            or remote_manifest.get("publication_sequence") != 0
        ):
            raise ValueError("replacement remote publication identity mismatch")
        expected = {
            (
                str(graph.graph_id),
                graph.fact_set_digest,
                graph.fact_count,
                tuple(graph.vendors),
                graph.observed_at.isoformat().replace("+00:00", "Z"),
                graph.facts.kind,
                graph.facts.sha256,
                graph.facts.bytes,
                graph.summary.kind,
                graph.summary.sha256,
                graph.summary.bytes,
            )
            for graph in manifest.graphs
        }
        actual = {
            (
                graph["graph_id"],
                graph["fact_set_digest"],
                graph["fact_count"],
                tuple(graph["vendors"]),
                graph["observed_at"],
                graph["facts"]["kind"],
                graph["facts"]["sha256"],
                graph["facts"]["bytes"],
                graph["summary"]["kind"],
                graph["summary"]["sha256"],
                graph["summary"]["bytes"],
            )
            for graph in remote_manifest["graphs"]
        }
        if actual != expected:
            raise ValueError("replacement remote graph inventory mismatch")
        inventory = rpc.call(
            "ct_project_inventory_snapshot",
            {
                "workspace_id": str(manifest.workspace_id),
                "snapshot_sequence": snapshot["snapshot_sequence"],
            },
        )
        if len(inventory["projects"]) != 1:
            raise ValueError("replacement remote project inventory mismatch")
        project = inventory["projects"][0]
        if (
            project.get("display_name") != manifest.project_name
            or project.get("repository_identity") != manifest.repository_identity
            or project.get("aliases") != manifest.project_aliases
        ):
            raise ValueError("replacement remote project metadata mismatch")
        selected = manifest.graphs[0]
        facts = rpc.call(
            "ct_artifact_read",
            {
                "workspace_id": str(manifest.workspace_id),
                "snapshot_sequence": snapshot["snapshot_sequence"],
                "kind": "facts",
                "sha256": selected.facts.sha256,
            },
        )
        summary = rpc.call(
            "ct_artifact_read",
            {
                "workspace_id": str(manifest.workspace_id),
                "snapshot_sequence": snapshot["snapshot_sequence"],
                "kind": "summary",
                "sha256": selected.summary.sha256,
            },
        )
        PublishedFactSet.model_validate(facts)
        PreparedGraphSummary.model_validate(summary)
        return {
            "status": "verified",
            "workspace_id": str(manifest.workspace_id),
            "snapshot_sequence": snapshot["snapshot_sequence"],
            "projects": 1,
            "graphs": len(expected),
            "objects": len(replacement.objects),
            "bytes": sum(len(body) for body in replacement.objects.values()),
            "selected_graph": str(selected.graph_id),
        }
    finally:
        rpc.close()


def access_token_from_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"missing access token environment variable: {name}")
    return value


def _require_clean_or_resumable_target(
    rpc: CloudflareRpcClient, manifest: ArtifactReplacement
) -> None:
    # This is also the quota preflight. A quota failure stops before any upload.
    workspace_id = str(manifest.workspace_id)
    snapshot = rpc.call("ct_workspace_snapshot", {"workspace_id": workspace_id})
    inventory = rpc.call(
        "ct_project_inventory_snapshot",
        {
            "workspace_id": workspace_id,
            "snapshot_sequence": snapshot["snapshot_sequence"],
        },
    )
    projects = inventory["projects"]
    if snapshot["snapshot_sequence"] == 0 and not projects:
        return
    if len(projects) != 1:
        raise ValueError("replacement target is neither empty nor a resumable import")
    project = projects[0]
    if (
        project.get("display_name") != manifest.project_name
        or project.get("repository_identity") != manifest.repository_identity
        or project.get("aliases") != manifest.project_aliases
    ):
        raise ValueError("replacement target project does not match the frozen import")


def _validate_object(kind: str, body: bytes, graph: ReplacementGraph) -> None:
    value = json.loads(body)
    _reject_private_content(value)
    if kind == "facts":
        facts = PublishedFactSet.model_validate(value)
        if (
            facts.graph_id != graph.graph_id
            or facts.fact_set_digest != graph.fact_set_digest
            or len(facts.rows) != graph.fact_count
        ):
            raise ValueError("replacement fact identity mismatch")
    else:
        summary = PreparedGraphSummary.model_validate(value)
        if (
            summary.graph_id != graph.graph_id
            or summary.fact_set_digest != graph.fact_set_digest
        ):
            raise ValueError("replacement summary identity mismatch")


def _reject_private_content(value: Any, field: str = "") -> None:
    if isinstance(value, str):
        if value.lower().startswith("data:") or any(
            marker in value for marker in ("/Users/", "/home/", "~/", "C:\\")
        ):
            raise ValueError("replacement contains private path or embedded data")
        if len(value) >= 128 and _looks_base64(value):
            raise ValueError("replacement contains an embedded base64 value")
    elif isinstance(value, list):
        for item in value:
            _reject_private_content(item, field)
    elif isinstance(value, dict):
        for key, item in value.items():
            normalized = key.lower().replace("-", "_")
            if item not in (None, "", [], {}) and any(
                marker in normalized
                for marker in (
                    "raw_log",
                    "transcript",
                    "prompt_body",
                    "data_uri",
                    "blob",
                )
            ):
                raise ValueError("replacement contains a forbidden private field")
            _reject_private_content(item, key)


def _looks_base64(value: str) -> bool:
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
    return len(value) % 4 == 0 and set(value) <= allowed


__all__ = [
    "ArtifactReplacement",
    "ReplacementGraph",
    "ReplacementSource",
    "ValidatedReplacement",
    "access_token_from_env",
    "import_replacement",
    "load_replacement",
    "replacement_digest",
    "verify_replacement",
]
