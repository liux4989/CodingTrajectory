"""Supabase-backed, snapshot-pinned portable project inventory."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from coding_trajectory.contracts import service_contract
from coding_trajectory.control_plane.remote import (
    RemoteControlPlaneError,
    SupabaseRpcClient,
)
from coding_trajectory.ingestion.common import format_datetime


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


class SupabaseProjectInventoryRepository:
    """Serve ``project.list`` from one pinned remote workspace sequence."""

    def __init__(
        self,
        *,
        client: SupabaseRpcClient,
        workspace_id: UUID,
        snapshot_sequence: int | None = None,
    ) -> None:
        if snapshot_sequence is not None and snapshot_sequence < 0:
            raise ValueError("snapshot_sequence must not be negative")
        self._client = client
        self.workspace_id = workspace_id
        self.snapshot_sequence = snapshot_sequence

    def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Act as the project-inventory authority handler."""

        if method != "project.list":
            raise KeyError(
                f"remote project inventory does not support method: {method}"
            )
        return self.project_list(params)

    def project_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """Return the existing ``project.list`` result shape without host paths."""

        validated = service_contract("project.list").validate_request(params)
        if "modified_since" in validated:
            validated["modified_since"] = format_datetime(validated["modified_since"])
        request = {"workspace_id": str(self.workspace_id), **validated}
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

        items: dict[str, dict[str, Any]] = {}
        for project in snapshot.projects:
            if project.display_name in items:
                raise RemoteControlPlaneError(
                    "project inventory contains duplicate display names"
                )
            items[project.display_name] = {
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
            "content_scope": "compact",
        }
