"""Bounded typed catalog reads shared by Core inventory and session listings."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from coding_trajectory.contracts import service_contract
from coding_trajectory.control_plane.catalog_protocol import (
    CatalogProject,
    CatalogReadRequest,
    CatalogReadResponse,
    CatalogSelection,
    CatalogSession,
)
from coding_trajectory.control_plane.remote import (
    CloudflareRpcClient,
    RemoteControlPlaneError,
)


class CloudflareCatalogRepository:
    def __init__(self, *, client: CloudflareRpcClient, workspace_id: UUID) -> None:
        self.client = client
        self.workspace_id = workspace_id
        self.selection: CatalogSelection | None = None

    def page(self, **params: Any) -> CatalogReadResponse:
        request = CatalogReadRequest(
            workspace_id=self.workspace_id,
            selection=self.selection.token if self.selection else None,
            **params,
        )
        response = CatalogReadResponse.model_validate(
            self.client.call(
                "ct_catalog_read_v2",
                request.model_dump(
                    mode="json", exclude_none=True, exclude_defaults=True
                ),
            )
        )
        if response.workspace_id != self.workspace_id:
            raise RemoteControlPlaneError("catalog workspace mismatch")
        if self.selection and response.selection != self.selection:
            raise RemoteControlPlaneError("catalog selection mismatch")
        self.selection = response.selection
        return response

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method not in {"project.list", "project.sessions"}:
            raise KeyError(method)
        validated = service_contract(method).validate_request(params)
        kind = "projects" if method == "project.list" else "sessions"
        cursor = None
        rows: list[CatalogProject | CatalogSession] = []
        bytes_read = 0
        # Legacy Core envelopes are all-items. Fail explicitly above the cap;
        # public paged callers use the v2 contract directly.
        for _ in range(50):
            page = self.page(
                kind=kind,
                population="registered" if kind == "projects" else "published",
                limit=200,
                cursor=cursor,
                **validated,
            )
            bytes_read += len(page.model_dump_json().encode())
            if bytes_read > 8 * 1024 * 1024:
                raise RemoteControlPlaneError(
                    "catalog compatibility budget exceeded; use v2 pages"
                )
            for row in page.items:
                if not isinstance(
                    row, CatalogProject if kind == "projects" else CatalogSession
                ):
                    raise RemoteControlPlaneError("catalog result kind mismatch")
                if isinstance(row, CatalogSession) and (
                    row.coverage != "complete" or row.projection is None
                ):
                    raise RemoteControlPlaneError("catalog projection unavailable")
                rows.append(row)
            cursor = page.next_cursor
            if not cursor:
                break
        else:
            raise RemoteControlPlaneError(
                "catalog compatibility budget exceeded; use v2 pages"
            )
        if kind == "projects":
            projects: dict[str, Any] = {}
            for row in rows:
                assert isinstance(row, CatalogProject)
                if row.name in projects:
                    raise RemoteControlPlaneError(
                        "project inventory contains duplicate display names"
                    )
                projects[row.name] = {"path": None, "vendors": row.vendors}
            result = {"items": projects}
        else:
            summaries = [
                row.projection.model_dump(mode="json")
                for row in rows
                if isinstance(row, CatalogSession) and row.projection is not None
            ]
            result = {
                "items": sorted(summaries, key=lambda row: row.get("project") or "")
            }
        return service_contract(method).validate_response(result)
