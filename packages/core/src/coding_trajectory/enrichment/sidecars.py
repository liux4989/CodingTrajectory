"""Sidecar plugin interface for vendor-specific enrichment."""

from __future__ import annotations

from abc import ABC
from typing import Any

from pydantic import BaseModel, Field

from coding_trajectory.enrichment.models import EnrichmentNote
from coding_trajectory.ingestion.models import Event, Session, Step, Turn


class SidecarPayload(BaseModel):
    data: dict[str, Any] = Field(default_factory=dict)
    notes: list[EnrichmentNote] = Field(default_factory=list)


class ResourceSidecars(BaseModel):
    namespaces: dict[str, SidecarPayload] = Field(default_factory=dict)

    @property
    def notes(self) -> list[EnrichmentNote]:
        result: list[EnrichmentNote] = []
        for payload in self.namespaces.values():
            result.extend(payload.notes)
        return result


class SidecarPlugin(ABC):
    """Narrowed plugin that returns vendor-specific sidecar data only."""

    namespace: str

    def enrich_session(self, session: Session, *, store: Any = None) -> SidecarPayload | None:
        return None

    def enrich_turn(self, turn: Turn, *, store: Any = None) -> SidecarPayload | None:
        return None

    def enrich_step(self, step: Step, *, store: Any = None) -> SidecarPayload | None:
        return None

    def enrich_event(self, event: Event, *, store: Any = None) -> SidecarPayload | None:
        return None


def collect_sidecars(
    plugins: list[SidecarPlugin],
    resource: Session | Turn | Step | Event,
    *,
    store: Any = None,
) -> ResourceSidecars:
    sidecars = ResourceSidecars()
    for plugin in plugins:
        payload: SidecarPayload | None = None
        if isinstance(resource, Session):
            payload = plugin.enrich_session(resource, store=store)
        elif isinstance(resource, Turn):
            payload = plugin.enrich_turn(resource, store=store)
        elif isinstance(resource, Step):
            payload = plugin.enrich_step(resource, store=store)
        elif isinstance(resource, Event):
            payload = plugin.enrich_event(resource, store=store)
        if payload is not None:
            sidecars.namespaces[plugin.namespace] = payload
    return sidecars
