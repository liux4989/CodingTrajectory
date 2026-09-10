"""Explicit configuration for the single Cloudflare API authority."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, HttpUrl, SecretStr


class ApiConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    access_token: SecretStr
    workspace_id: UUID

    @classmethod
    def from_environment(cls) -> ApiConfiguration:
        from coding_trajectory.control_plane.connections import resolve_credentials

        credentials = resolve_credentials()
        return cls(
            url=credentials.profile.cloudflare_url,
            access_token=credentials.access_token,
            workspace_id=credentials.profile.workspace_id,
        )

    def runtime_options(
        self, *, local_evidence: bool = False, current_dir: Path | None = None
    ) -> dict[str, Any]:
        from coding_trajectory.control_plane.http_service import RemoteRuntimeFactory

        factory = RemoteRuntimeFactory(
            url=str(self.url),
            workspace_id=self.workspace_id,
        )
        options = factory.runtime_options(
            self.access_token.get_secret_value(),
            local_evidence=local_evidence,
            current_dir=current_dir,
        )
        return options
