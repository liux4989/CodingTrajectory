"""Plugin interfaces for enrichment sidecars."""

from __future__ import annotations

from abc import ABC

from coding_trajectory.ingestion.models import Event, Session, Step, Trajectory, Turn
from coding_trajectory.query import DocumentStore

from .models import EnrichmentOverlay


class EnrichmentPlugin(ABC):
    """Base plugin for resource-sidecar enrichment.

    Plugins must never mutate canonical resources. They return overlay data that
    is attached beside canonical payloads under their namespace.
    """

    namespace: str

    def enrich_trajectory(
        self,
        trajectory: Trajectory,
        *,
        store: DocumentStore | None = None,
    ) -> EnrichmentOverlay | None:
        return None

    def enrich_session(
        self,
        session: Session,
        *,
        store: DocumentStore | None = None,
    ) -> EnrichmentOverlay | None:
        return None

    def enrich_turn(
        self,
        turn: Turn,
        *,
        store: DocumentStore | None = None,
    ) -> EnrichmentOverlay | None:
        return None

    def enrich_step(
        self,
        step: Step,
        *,
        store: DocumentStore | None = None,
    ) -> EnrichmentOverlay | None:
        return None

    def enrich_event(
        self,
        event: Event,
        *,
        store: DocumentStore | None = None,
    ) -> EnrichmentOverlay | None:
        return None
