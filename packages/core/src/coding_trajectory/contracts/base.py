"""Shared base models for the versioned service contracts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ContractModel(BaseModel):
    """Response base that permits additive fields within one method version."""

    model_config = ConfigDict(extra="allow")


class RequestModel(BaseModel):
    """Strict request base; parameter changes require a method-version bump."""

    model_config = ConfigDict(extra="forbid")
