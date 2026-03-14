"""Consumer layer — post-processes raw ingested Sessions into enriched form.

Adapters (ingestion layer) translate raw log formats into canonical Events with
a free ``payload`` dict.  The consumer layer reads those payloads, understands
the per-vendor conventions, and populates the typed detail fields (``tool_call``,
``llm``, ``text``), event category, and tree-linkage IDs (``event_group_id``,
``parent_event_id``) directly on each Event.

The raw ``payload`` is never removed — clients can still read the full timeline
at any fidelity level.

Usage::

    from coding_trajectory.consumer import enrich_session

    session = adapter.ingest_file(path)
    enrich_session(session)   # mutates events in place
"""

from coding_trajectory.consumer.base import SessionEnricher, enrich_session

__all__ = ["SessionEnricher", "enrich_session"]
