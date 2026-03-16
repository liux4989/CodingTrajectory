"""Typed models for trajectory-wide structural analysis."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from coding_trajectory.enrichment.models import EnrichmentNote


class SessionTreeNode(BaseModel):
    session_id: UUID
    parent_session_id: UUID | None = None
    child_session_ids: list[UUID] = Field(default_factory=list)
    depth: int = 0
    incoming_edge_type: str | None = None
    is_root: bool = False
    is_leaf: bool = False


class SessionTree(BaseModel):
    root_session_id: UUID | None = None
    root_session_ids: list[UUID] = Field(default_factory=list)
    leaf_session_ids: list[UUID] = Field(default_factory=list)
    nodes_by_session_id: dict[UUID, SessionTreeNode] = Field(default_factory=dict)


class CrossSessionOperation(BaseModel):
    index: int
    kind: Literal["cross_session_edge"] = "cross_session_edge"
    edge_type: str
    source_session_id: UUID
    target_session_id: UUID
    source_turn_id: UUID | None = None
    source_step_id: UUID | None = None
    source_event_id: UUID | None = None
    tool_name: str | None = None
    provenance: str
    confidence: str
    evidence_event_ids: list[UUID] = Field(default_factory=list)


class TrajectoryStructure(BaseModel):
    trajectory_id: UUID
    session_tree: SessionTree = Field(default_factory=SessionTree)
    operations: list[CrossSessionOperation] = Field(default_factory=list)

    multi_agent_mode: Literal["single_session", "cross_session"] = "single_session"
    topology: Literal["single_session", "linear", "branching"] = "single_session"
    has_observed_spawn: bool = False
    vendor_set: list[str] = Field(default_factory=list)
    edge_type_counts: dict[str, int] = Field(default_factory=dict)

    notes: list[EnrichmentNote] = Field(default_factory=list)
