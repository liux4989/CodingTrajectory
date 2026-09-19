"""Product-owned view state. Core schemas are consumed, not reproduced."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL = "ct.loop.v1"


class CanonicalReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=256)
    turn_id: str | None = Field(default=None, min_length=1, max_length=256)
    item_id: str | None = Field(default=None, min_length=1, max_length=256)
    event_id: str | None = Field(default=None, min_length=1, max_length=256)
    view_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class Investigation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    title: str = Field(min_length=1, max_length=160)
    reference: CanonicalReference
    source: Literal["host_local"] = "host_local"
    revision: Literal["latest"] = "latest"


class CoreQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: str
    params: dict = Field(default_factory=dict)
