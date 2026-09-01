"""Vendor-specific mechanism interpreters for the ingestion layer.

Layer boundary (per the PRD): vendor mechanisms own vendor-specific
enrichment — canonical extensions, parent-session linkage, and edge
evidence — and must stay out of the core canonical models in
``coding_trajectory.ingestion.models``. Adapters extract raw vendor facts
into a mechanism's pydantic ``*Input`` model; the mechanism interprets them
into canonical shapes (``VendorExtensions``, canonical/parent session ids).

Every vendor session-mechanism module conforms to
:class:`VendorSessionMechanism`:

- a pydantic ``*Input`` model carrying the extracted vendor facts,
- ``session_identity(input) -> (session_id, parent_session_id)``,
- ``extensions(input) -> VendorExtensions``.

``relation_edges`` is intentionally not part of this protocol: it already
operates on normalized, vendor-agnostic facts (``RelationEdgeInput``)
consumed by canonical graph assembly, so there is no vendor-specific Input
model to route through the protocol.
"""

from __future__ import annotations

from typing import Protocol, TypeVar
from uuid import UUID

from coding_trajectory.ingestion.models import VendorExtensions

__all__ = ["VendorSessionMechanism"]

MechanismInputT_contra = TypeVar("MechanismInputT_contra", contravariant=True)


class VendorSessionMechanism(Protocol[MechanismInputT_contra]):
    """Shared shape of vendor session-mechanism interpreters.

    Implementations are module-level functions (see ``claude_subagent`` and
    ``codex_multi_agent``); the protocol documents the contract adapters can
    rely on. ``session_identity`` returns ``(session_id, parent_session_id)``
    where the session id may be ``None`` when the mechanism input does not
    carry a parseable one — the adapter remains authoritative for the
    session id in that case.
    """

    def session_identity(
        self, mechanism: MechanismInputT_contra
    ) -> tuple[UUID | None, UUID | None]: ...

    def extensions(self, mechanism: MechanismInputT_contra) -> VendorExtensions: ...
