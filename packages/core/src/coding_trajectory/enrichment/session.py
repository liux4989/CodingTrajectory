"""Session enrichment helpers."""

from __future__ import annotations

from typing import Iterable

from coding_trajectory.ingestion.models import Session
from coding_trajectory.query import DocumentStore

from .base import EnrichmentPlugin
from .models import EnrichedSession, EnrichmentNote, EnrichmentOverlay
from .trajectory import _merge_plugin_overlay


def build_enriched_session(
    canonical: Session,
    *,
    structural: dict | None = None,
    derived: dict | None = None,
    agent_specific: dict | None = None,
    notes: Iterable[EnrichmentNote] | None = None,
    plugins: Iterable[EnrichmentPlugin] = (),
    store: DocumentStore | None = None,
) -> EnrichedSession:
    overlay = EnrichmentOverlay(
        structural=dict(structural or {}),
        derived=dict(derived or {}),
        agent_specific=dict(agent_specific or {}),
        notes=list(notes or []),
    )

    for plugin in plugins:
        plugin_overlay = plugin.enrich_session(canonical, store=store)
        _merge_plugin_overlay(overlay, plugin.namespace, plugin_overlay)

    return EnrichedSession(session_id=canonical.session_id, enrichment=overlay)
