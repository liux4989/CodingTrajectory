"""Codex fork, spawn, and collaboration mechanism helpers."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel

from coding_trajectory.ingestion.models import CodexExtensions, VendorExtensions


class CodexThreadSpawn(BaseModel):
    parent_thread_id: str | None = None
    depth: int | None = None
    agent_path: str | None = None
    agent_nickname: str | None = None
    agent_role: str | None = None


class CodexMultiAgentInput(BaseModel):
    sandbox_id: str | None = None
    sandbox_mode: str | None = None
    approval_policy: str | None = None
    collaboration_mode: str | None = None
    multi_agent_version: str | None = None
    multi_agent_mode: str | None = None
    agent_path: str | None = None
    agent_nickname: str | None = None
    agent_role: str | None = None
    cwd: str | None = None
    title: str | None = None
    forked_from_id: str | None = None
    thread_spawn: CodexThreadSpawn | None = None


def parent_session_id(mechanism: CodexMultiAgentInput) -> UUID | None:
    return _parse_uuid(mechanism.forked_from_id) or _parse_uuid(
        mechanism.thread_spawn.parent_thread_id if mechanism.thread_spawn else None
    )


def extensions(mechanism: CodexMultiAgentInput) -> VendorExtensions:
    thread_spawn = mechanism.thread_spawn
    return VendorExtensions(
        codex=CodexExtensions(
            sandbox_id=mechanism.sandbox_id,
            sandbox_mode=mechanism.sandbox_mode,
            approval_policy=mechanism.approval_policy,
            collaboration_mode=mechanism.collaboration_mode,
            multi_agent_version=mechanism.multi_agent_version,
            multi_agent_mode=mechanism.multi_agent_mode,
            agent_path=mechanism.agent_path
            or (thread_spawn.agent_path if thread_spawn else None),
            agent_nickname=mechanism.agent_nickname
            or (thread_spawn.agent_nickname if thread_spawn else None),
            agent_role=mechanism.agent_role
            or (thread_spawn.agent_role if thread_spawn else None),
            cwd=mechanism.cwd,
            title=mechanism.title,
            forked_from_id=mechanism.forked_from_id,
            spawn_parent_thread_id=thread_spawn.parent_thread_id
            if thread_spawn
            else None,
            spawn_depth=thread_spawn.depth if thread_spawn else None,
            spawn_agent_nickname=thread_spawn.agent_nickname if thread_spawn else None,
            spawn_agent_role=thread_spawn.agent_role if thread_spawn else None,
        )
    )


def _parse_uuid(value: str | None) -> UUID | None:
    if not value:
        return None
    for candidate in (value, value.removeprefix("T-")):
        try:
            return UUID(candidate)
        except ValueError:
            continue
    return None
