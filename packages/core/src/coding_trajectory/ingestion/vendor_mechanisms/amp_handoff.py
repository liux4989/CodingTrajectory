"""Amp handoff and parent-thread mechanism helpers."""

from __future__ import annotations

from uuid import UUID, uuid4

from pydantic import BaseModel

from coding_trajectory.ingestion.models import AmpExtensions, VendorExtensions


class AmpHandoffInput(BaseModel):
    thread_id: str | None = None
    thread_version: int | None = None
    parent_thread_id: str | None = None
    workspace_id: str | None = None
    workspace_name: str | None = None
    git_url: str | None = None
    git_ref: str | None = None
    agent_version: str | None = None
    client_type: str | None = None
    os_platform: str | None = None
    title: str | None = None
    agent_mode: str | None = None


def thread_session_id(mechanism: AmpHandoffInput) -> UUID:
    return _parse_thread_uuid(mechanism.thread_id) or uuid4()


def parent_session_id(mechanism: AmpHandoffInput) -> UUID | None:
    return _parse_thread_uuid(mechanism.parent_thread_id)


def extensions(mechanism: AmpHandoffInput) -> VendorExtensions:
    return VendorExtensions(
        amp=AmpExtensions(
            thread_id=mechanism.thread_id,
            thread_version=mechanism.thread_version,
            parent_thread_id=mechanism.parent_thread_id,
            workspace_id=mechanism.workspace_id,
            workspace_name=mechanism.workspace_name,
            git_url=mechanism.git_url,
            git_ref=mechanism.git_ref,
            agent_version=mechanism.agent_version,
            client_type=mechanism.client_type,
            os_platform=mechanism.os_platform,
            title=mechanism.title,
            agent_mode=mechanism.agent_mode,
        )
    )


def _parse_thread_uuid(value: str | None) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(value.removeprefix("T-"))
    except ValueError:
        return None
