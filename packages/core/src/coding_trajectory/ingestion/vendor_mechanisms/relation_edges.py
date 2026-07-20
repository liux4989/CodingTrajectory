"""Provider-specific edge semantics used by canonical graph assembly."""

from __future__ import annotations

from pydantic import BaseModel

from coding_trajectory.ingestion.models import Vendor


class RelationEdgeInput(BaseModel):
    child_is_sidechain: bool = False
    child_parent_session_id_present: bool = False
    parent_vendor: Vendor | None = None
    origin_tool_name: str | None = None


def classify_edge_type(mechanism: RelationEdgeInput) -> str:
    if mechanism.origin_tool_name is not None:
        tool_name = mechanism.origin_tool_name.strip().lower()
        if tool_name in {"agent", "task", "spawn_agent", "spawnagent"}:
            return "spawned_subagent"
        if tool_name in {"handoff", "handoff_to"}:
            return "handoff_to"
        if tool_name in {"resume", "resume_agent"}:
            return "resumed_from"

    if mechanism.child_is_sidechain and mechanism.parent_vendor == Vendor.CLAUDE_CODE:
        return "sidechain_of"

    return "sidechain_of"


def is_root_session_candidate(
    *, parent_session_id_present: bool, is_sidechain: bool
) -> bool:
    return not parent_session_id_present and not is_sidechain
