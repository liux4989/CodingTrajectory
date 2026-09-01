"""Claude Code subagent and sidechain mechanism helpers."""

from __future__ import annotations

import json
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel

from coding_trajectory.ingestion.models import (
    ClaudeCodeExtensions,
    Vendor,
    VendorExtensions,
)


class ClaudeSubagentInput(BaseModel):
    source_path: str
    is_subagent_file: bool = False
    parent_session_id: UUID | None = None
    raw_session_id: UUID
    team_name: str | None = None
    is_sidechain: bool | None = None
    permission_mode: str | None = None
    mode: str | None = None
    last_prompt: str | None = None
    parent_uuid: str | None = None
    request_id: str | None = None
    agent_name: str | None = None
    agent_role: str | None = None
    description: str | None = None
    title: str | None = None
    tool_use_id: str | None = None
    spawn_depth: int | None = None


def canonical_session_ids(mechanism: ClaudeSubagentInput) -> tuple[UUID, UUID | None]:
    if not mechanism.is_subagent_file or mechanism.parent_session_id is None:
        return mechanism.raw_session_id, None

    canonical_session_id = uuid5(
        NAMESPACE_URL,
        json.dumps(
            {
                "vendor": Vendor.CLAUDE_CODE.value,
                "kind": "claude_subagent_session",
                "source": mechanism.source_path,
                "raw_session_id": str(mechanism.raw_session_id),
                "parent_session_id": str(mechanism.parent_session_id),
                "agent_name": mechanism.agent_name,
            },
            sort_keys=True,
        ),
    )
    return canonical_session_id, mechanism.parent_session_id


def session_identity(mechanism: ClaudeSubagentInput) -> tuple[UUID | None, UUID | None]:
    """VendorSessionMechanism entry point: ``(session_id, parent_session_id)``."""
    return canonical_session_ids(mechanism)


def extensions(mechanism: ClaudeSubagentInput) -> VendorExtensions:
    return VendorExtensions(
        claude_code=ClaudeCodeExtensions(
            team_name=mechanism.team_name,
            is_sidechain=mechanism.is_sidechain,
            permission_mode=mechanism.permission_mode,
            mode=mechanism.mode,
            last_prompt=mechanism.last_prompt,
            parent_uuid=mechanism.parent_uuid,
            request_id=mechanism.request_id,
            tool_use_id=mechanism.tool_use_id,
            spawn_depth=mechanism.spawn_depth,
            agent_name=mechanism.agent_name,
            agent_role=mechanism.agent_role,
            description=mechanism.description,
            title=mechanism.title,
        )
    )
