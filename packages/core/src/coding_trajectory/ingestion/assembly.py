"""Shared session-assembly pipeline for ingestion adapters.

Every adapter's ``_build_session`` performed the same orchestration around the
transcript IR: inline stabilization on the measurements path, event/turn
projection, provenance capture, compact usage remapping, cwd resolution, and
``Session`` construction.  ``assemble_session`` owns that skeleton once;
per-vendor variation is expressed through :class:`AssemblyHooks`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from coding_trajectory.ingestion.models import (
    ContextSourceObservation,
    ContextUsageObservation,
    Event,
    RuntimeObservation,
    Session,
    Turn,
    TurnStatus,
    Vendor,
    VendorExtensions,
)
from coding_trajectory.ingestion.provenance import SessionProvenance
from coding_trajectory.ingestion.retention import (
    CanonicalRetention,
    compact_context_usage_observation,
)
from coding_trajectory.ingestion.transcript import (
    TranscriptRecord,
    TranscriptStabilizer,
    build_session_provenance,
    compact_session_cwd,
    events_from_transcript,
    project_transcript,
)


@dataclass(frozen=True, slots=True)
class AssemblyContext:
    """Intermediate pipeline products exposed to assembly hooks."""

    session_id: UUID
    started_at: datetime
    ended_at: datetime
    events: list[Event]
    turns: list[Turn]
    context_usage: list[ContextUsageObservation]


@dataclass(slots=True)
class AssemblyHooks:
    """Per-vendor variation points for :func:`assemble_session`.

    All fields are optional; defaults reproduce the plain projection shared by
    every vendor.
    """

    # project_transcript variation (Codex lifecycle turns / live statuses).
    active_status: TurnStatus | None = None
    default_previous_turn_status: TurnStatus = TurnStatus.COMPLETED
    prefer_lifecycle: bool = False
    # Static session facts.
    extensions: VendorExtensions | None = None
    parent_session_id: UUID | None = None
    runtime_observations: list[RuntimeObservation] = field(default_factory=list)
    # Extra Session fields (model, reasoning_effort, agent_name, status).
    session_fields: dict[str, Any] = field(default_factory=dict)
    # Builds usage observations from the transcript (pre-remap).
    build_context_usage: (
        Callable[[list[TranscriptRecord]], list[ContextUsageObservation]] | None
    ) = None
    # Builds trajectory-only context sources from pipeline products.
    build_context_sources: (
        Callable[[AssemblyContext], list[ContextSourceObservation]] | None
    ) = None
    # Computes Session fields that depend on the projected turns (e.g. Codex
    # session status); merged over ``session_fields``.
    build_session_fields: Callable[[AssemblyContext], dict[str, Any]] | None = None
    # Post-projection turn decoration (e.g. Claude team-state attachment).
    decorate_turns: Callable[[list[Turn]], None] | None = None
    # Receives the compact provenance so the adapter can publish it as
    # ``last_provenance``; invoked only on the measurements path.
    provenance_sink: Callable[[SessionProvenance], None] | None = None


def assemble_session(
    *,
    vendor: Vendor,
    source: Path,
    session_id: UUID,
    transcript: list[TranscriptRecord],
    retention: CanonicalRetention = "trajectory",
    hooks: AssemblyHooks | None = None,
) -> Session:
    """Assemble one canonical session from a vendor transcript.

    Owns the steps every adapter duplicated: stabilizer creation (measurements
    only), event/turn projection, provenance capture, started/ended min/max,
    compact usage remap, compact cwd resolution, and ``Session`` construction.
    The transcript must be non-empty; adapters validate and raise their own
    vendor-named error beforehand.
    """
    hooks = hooks or AssemblyHooks()
    started_at = min(record.timestamp for record in transcript)
    ended_at = max(record.timestamp for record in transcript)
    compact = (
        TranscriptStabilizer(vendor=vendor, source=source)
        if retention == "measurements"
        else None
    )
    events = events_from_transcript(
        session_id=session_id, records=transcript, stabilizer=compact
    )
    turns = project_transcript(
        session_id=session_id,
        vendor=vendor,
        records=transcript,
        active_status=hooks.active_status,
        default_previous_turn_status=hooks.default_previous_turn_status,
        prefer_lifecycle=hooks.prefer_lifecycle,
        compact=compact,
    )
    if compact is not None and hooks.provenance_sink is not None:
        hooks.provenance_sink(
            build_session_provenance(
                session_id=session_id,
                vendor=vendor,
                source=source,
                stabilizer=compact,
                turns=turns,
            )
        )
    if hooks.decorate_turns is not None:
        hooks.decorate_turns(turns)

    context_usage = (
        hooks.build_context_usage(transcript)
        if hooks.build_context_usage is not None
        else []
    )
    if compact is not None:
        context_usage = [
            compact_context_usage_observation(observation, compact.event_ids)
            for observation in context_usage
        ]
    context = AssemblyContext(
        session_id=session_id,
        started_at=started_at,
        ended_at=ended_at,
        events=events,
        turns=turns,
        context_usage=context_usage,
    )
    context_sources: list[ContextSourceObservation] = []
    if compact is None and hooks.build_context_sources is not None:
        context_sources = hooks.build_context_sources(context)

    session_fields = dict(hooks.session_fields)
    if hooks.build_session_fields is not None:
        session_fields.update(hooks.build_session_fields(context))

    return Session(
        session_id=session_id,
        vendor=vendor,
        started_at=started_at,
        ended_at=ended_at,
        parent_session_id=hooks.parent_session_id,
        events=events,
        turns=turns,
        context_usage=context_usage,
        context_sources=context_sources,
        runtime_observations=hooks.runtime_observations,
        extensions=hooks.extensions,
        cwd=(
            compact_session_cwd(
                vendor=vendor,
                source=source,
                extensions=hooks.extensions,
                payload_cwd=compact.cwd,
            )
            if compact is not None
            else None
        ),
        **session_fields,
    )
