"""Explicit configuration for the single Supabase API authority."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, HttpUrl, SecretStr


class ApiConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    api_key: SecretStr
    access_token: SecretStr
    workspace_id: UUID

    @classmethod
    def from_environment(cls) -> ApiConfiguration:
        names = {
            "url": "CT_SUPABASE_URL",
            "api_key": "CT_SUPABASE_ANON_KEY",
            "access_token": "CT_ACCESS_TOKEN",
            "workspace_id": "CT_REMOTE_WORKSPACE_ID",
        }
        missing = [name for name in names.values() if not os.environ.get(name)]
        if missing:
            raise ValueError(
                "Supabase API configuration requires " + ", ".join(missing)
            )
        # Avoid Pydantic errors echoing invalid secret inputs.
        try:
            return cls.model_validate(
                {key: os.environ[name] for key, name in names.items()}
            )
        except ValueError:
            raise ValueError("Supabase API configuration is invalid") from None

    def runtime_options(
        self, *, local_evidence: bool = False, current_dir: Path | None = None
    ) -> dict[str, Any]:
        from coding_trajectory.control_plane.http_service import RemoteRuntimeFactory

        return RemoteRuntimeFactory(
            url=str(self.url),
            api_key=self.api_key.get_secret_value(),
            workspace_id=self.workspace_id,
        ).runtime_options(
            self.access_token.get_secret_value(),
            local_evidence=local_evidence,
            current_dir=current_dir,
        )
