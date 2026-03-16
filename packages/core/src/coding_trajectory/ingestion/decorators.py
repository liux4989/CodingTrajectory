"""Session decorators that enrich canonical Sessions with vendor-specific data."""

from __future__ import annotations

from abc import ABC, abstractmethod
from uuid import UUID

from coding_trajectory.ingestion.models import (
    ClaudeCodeExtensions,
    Session,
    Vendor,
    VendorExtensions,
)


class BaseDecorator(ABC):
    @abstractmethod
    def apply(self, session: Session, registry: dict[UUID, Session]) -> Session:
        ...


class VendorDecorator(BaseDecorator, ABC):
    """Only applies when session.vendor matches self.vendor."""

    vendor: Vendor

    def apply(self, session: Session, registry: dict[UUID, Session]) -> Session:
        if session.vendor != self.vendor:
            return session
        return self._enrich(session, registry)

    @abstractmethod
    def _enrich(self, session: Session, registry: dict[UUID, Session]) -> Session:
        ...


class ClaudeCodeDecorator(VendorDecorator):
    vendor = Vendor.CLAUDE_CODE

    def _enrich(self, session: Session, registry: dict[UUID, Session]) -> Session:
        ext = session.extensions
        if ext is None or ext.claude_code is None:
            return session
        cc = ext.claude_code
        if not cc.is_sidechain or session.parent_session_id is not None:
            return session
        parent = self._resolve_parent(session, cc, registry)
        if parent is None:
            return session
        return session.model_copy(update={"parent_session_id": parent.session_id})

    def _resolve_parent(self, session: Session, cc: ClaudeCodeExtensions, registry: dict[UUID, Session]) -> Session | None:
        candidates: list[Session] = []
        for candidate in registry.values():
            if candidate.session_id == session.session_id:
                continue
            if candidate.vendor != Vendor.CLAUDE_CODE:
                continue
            cext = candidate.extensions
            if cext is None or cext.claude_code is None:
                continue
            ccc = cext.claude_code
            if ccc.is_sidechain:
                continue
            if cc.team_name and ccc.team_name and cc.team_name != ccc.team_name:
                continue
            candidates.append(candidate)

        # Only match via deterministic parent_uuid → event payload "uuid" link
        if cc.parent_uuid:
            for candidate in candidates:
                for event in candidate.events:
                    if event.payload.get("uuid") == cc.parent_uuid:
                        return candidate

        return None
