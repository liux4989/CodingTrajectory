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


def migrate_live_references(value: object) -> object:
    """Discard obsolete pins in persisted product state, not current requests.

    Historical measurements and configuration revisions remain unchanged;
    their canonical references now resolve the latest retained local evidence.
    """
    if isinstance(value, dict):
        reference = value.get("reference")
        if isinstance(reference, dict):
            reference.pop("view_manifest_sha256", None)
        for child in value.values():
            migrate_live_references(child)
    elif isinstance(value, list):
        for child in value:
            migrate_live_references(child)
    return value
