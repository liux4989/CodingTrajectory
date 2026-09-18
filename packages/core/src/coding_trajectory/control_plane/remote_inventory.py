"""Cloudflare-backed, snapshot-pinned portable project inventory."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from coding_trajectory.contracts import service_contract
from coding_trajectory.control_plane.remote import (
    CloudflareRpcClient,
    RemoteControlPlaneError,
)
from coding_trajectory.ingestion.common import normalize_project_key


class RemoteProject(BaseModel):
    """One portable project revision visible in a workspace snapshot."""

    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    revision: int = Field(gt=0)
    display_name: str = Field(min_length=1)
    repository_identity: str | None = None
    aliases: list[str] = Field(default_factory=list)
    published_sequence: int = Field(gt=0)
    modified_at: datetime
    vendors: list[str] = Field(default_factory=list)


class RemoteProjectInventorySnapshot(BaseModel):
    """RPC response for one immutable workspace project inventory."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    snapshot_sequence: int = Field(ge=0)
    projects: list[RemoteProject]


class CloudflareProjectInventoryRepository:
    """Serve ``project.list`` from one pinned remote workspace sequence."""

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
        self._projects: list[RemoteProject] | None = None

    def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Act as the project-inventory authority handler."""

        if method != "project.list":
            raise KeyError(
                f"remote project inventory does not support method: {method}"
            )
        return self.project_list(params)

    def projects(self) -> list[RemoteProject]:
        if self._projects is not None:
            return self._projects
        request: dict[str, Any] = {"workspace_id": str(self.workspace_id)}
        if self.snapshot_sequence is not None:
            request["snapshot_sequence"] = self.snapshot_sequence
        raw = self._client.call("ct_project_inventory_snapshot", request)
        snapshot = RemoteProjectInventorySnapshot.model_validate(raw)
        if snapshot.workspace_id != self.workspace_id:
            raise RemoteControlPlaneError("project inventory workspace mismatch")
        if (
            self.snapshot_sequence is not None
            and snapshot.snapshot_sequence != self.snapshot_sequence
        ):
            raise RemoteControlPlaneError("project inventory snapshot mismatch")
        self.snapshot_sequence = snapshot.snapshot_sequence
        self._projects = snapshot.projects
        return self._projects

    def resolve_name(self, name: str) -> str | None:
        key = normalize_project_key(name)
        matches = {
            str(project.project_id)
            for project in self.projects()
            if any(
                normalize_project_key(label) == key
                for label in [project.display_name, *project.aliases]
            )
        }
        if len(matches) > 1:
            raise ValueError("ambiguous project name; use project_id")
        return next(iter(matches), None)

    def project_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """Return ID-keyed project metadata; names are presentation only."""
        validated = service_contract("project.list").validate_request(params)
        selected_id = validated.get("project_id")
        if validated.get("project_name"):
            selected_id = self.resolve_name(validated["project_name"])
            if selected_id is None:
                return {"items": {}}

        items: dict[str, dict[str, Any]] = {}
        for project in self.projects():
            project_id = str(project.project_id)
            if selected_id and selected_id != project_id:
                continue
            if (
                validated.get("agent_vendor")
                and validated["agent_vendor"] not in project.vendors
            ):
                continue
            if (
                validated.get("modified_since")
                and project.modified_at < validated["modified_since"]
            ):
                continue
            items[project_id] = {
                "project_id": project_id,
                "display_name": project.display_name,
                "path": None,
                "vendors": sorted(set(project.vendors)),
            }
        return service_contract("project.list").validate_response({"items": items})

    def metadata(self) -> dict[str, Any] | None:
        """Return transport metadata once the repository has pinned a sequence."""

        if self.snapshot_sequence is None:
            return None
        return {
            "workspace_id": str(self.workspace_id),
            "snapshot_sequence": self.snapshot_sequence,
            "source": "remote",
            "freshness": "authoritative",
            "content_scope": "facts",
        }
