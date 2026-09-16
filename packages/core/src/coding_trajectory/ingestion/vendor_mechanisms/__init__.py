"""Vendor-specific mechanism interpreters for the ingestion layer.

Layer boundary (per the PRD): vendor mechanisms own vendor-specific
enrichment — canonical extensions, parent-session linkage, and edge
evidence — and must stay out of the core canonical models in
``coding_trajectory.ingestion.models``. Adapters extract raw vendor facts
into a mechanism's pydantic ``*Input`` model; the mechanism interprets them
into canonical shapes through vendor-specific identity helpers and
``extensions`` functions.

``relation_edges`` instead operates on normalized, vendor-agnostic facts
(``RelationEdgeInput``) consumed by canonical graph assembly, so it does not
need a vendor-specific Input model.
"""
