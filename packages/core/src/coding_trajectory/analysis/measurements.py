"""Exact content measurements extracted from full-fidelity sessions.

Evidence projections size visible content with the session graph's effective
tokenizer.  This module computes those primitives from a transient
full-fidelity session so the compact graph can answer the same projections
without resident bodies.  Callers must scope the graph's token counter
(``scoped_counter(counter_for_session_graph(graph))``) around extraction;
the compact graph retains the context-usage observations the counter
resolves from, so measured and full-fidelity paths tokenize identically.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from coding_trajectory.analysis.content_size import (
    item_input_text,
    item_output_text,
    output_is_truncated,
    reported_token_count,
    tool_input_summary,
    visible_text_size,
)
from coding_trajectory.analysis.projection_utils import truncate_text_preview
from coding_trajectory.analysis.tool_summary import summarize_tool_call
from coding_trajectory.ingestion.models import (
    AgentMessageItem,
    ContextSourceMeasurement,
    EventTextMeasurement,
    EventType,
    Item,
    ItemMeasurements,
    Session,
    SessionMeasurements,
)


class MeasurementMismatchError(RuntimeError):
    """The full-fidelity measurement source diverged from the compact graph."""


_TOOL_SHAPED_KINDS = frozenset(
    {"tool_call", "command_execution", "file_change", "plan"}
)
_OVERVIEW_TEXT_PREVIEW_LEN = 220


def extract_item_measurements(item: Item) -> ItemMeasurements:
    """Measure one item's discarded bodies with the active token counter."""

    input_text = item_input_text(item)
    output_text = item_output_text(item)
    text = getattr(item, "text", None) or ""
    input_size = visible_text_size(input_text)
    output_size = visible_text_size(output_text)
    text_size = visible_text_size(text)

    tool_summary: dict[str, Any] | None = None
    input_summary: str | None = None
    if item.kind in _TOOL_SHAPED_KINDS:
        tool_summary = summarize_tool_call(item)
        input_summary = tool_input_summary(
            getattr(item, "command", None)
            if item.kind == "command_execution"
            else getattr(item, "input", None)
        )
    text_preview: str | None = None
    if isinstance(item, AgentMessageItem):
        preview = truncate_text_preview(item.text, max_len=_OVERVIEW_TEXT_PREVIEW_LEN)
        text_preview = preview or None

    return ItemMeasurements(
        input_chars=input_size.chars,
        input_tokens=input_size.tokens,
        output_chars=output_size.chars,
        output_tokens=output_size.tokens,
        text_chars=text_size.chars,
        text_tokens=text_size.tokens,
        projection_only=is_projection_only_item(item),
        output_truncated=output_is_truncated(output_text),
        output_original_tokens=reported_token_count(output_text),
        input_summary=input_summary,
        text_preview=text_preview,
        tool_summary=tool_summary,
    )


def is_projection_only_item(item: Item) -> bool:
    """Whether an item is a semantic child of provider-visible wrapper content."""

    measurements = item.measurements
    if measurements is not None and measurements.projection_only:
        return True
    vendor_data = item.vendor_data
    activity = vendor_data.get("activity") if isinstance(vendor_data, dict) else None
    provenance = activity.get("provenance") if isinstance(activity, dict) else None
    return isinstance(provenance, dict) and bool(provenance.get("parent_tool_call_id"))


def extract_session_measurements(session: Session) -> SessionMeasurements:
    """Measure session-level content: context sources and dropped LLM events."""

    sources = [
        ContextSourceMeasurement(
            timestamp=source.timestamp,
            key=source.key,
            label=source.label,
            reported_tokens=source.reported_tokens,
            chars=(size := visible_text_size(source.text)).chars,
            tokens=size.tokens,
        )
        for source in session.context_sources
    ]
    llm_text_sizes: list[EventTextMeasurement] = []
    llm_count = 0
    for event in session.events:
        if event.type != EventType.LLM_RESPONSE:
            continue
        llm_count += 1
        text = event.payload.get("text")
        if not isinstance(text, str) or not text:
            continue
        size = visible_text_size(text)
        llm_text_sizes.append(
            EventTextMeasurement(
                timestamp=event.timestamp,
                chars=size.chars,
                tokens=size.tokens,
            )
        )
    return SessionMeasurements(
        context_sources=sources,
        llm_response_count=llm_count,
        llm_response_text_sizes=llm_text_sizes,
    )


def attach_measurements(session: Session, full_session: Session) -> None:
    """Attach measurements extracted from ``full_session`` onto a compact one.

    Both sessions derive from the same source with canonical stable ids, so
    item and session identities line up exactly.  Sessions whose ids diverge
    (a fence-unsafe source) raise instead of attaching partial measurements.
    """

    if full_session.session_id != session.session_id:
        raise MeasurementMismatchError(
            f"measurement source session mismatch: {full_session.session_id}"
        )
    by_id: dict[UUID, Item] = {
        item.item_id: item for turn in full_session.turns for item in turn.items
    }
    for turn in session.turns:
        for item in turn.items:
            full_item = by_id.get(item.item_id)
            if full_item is None:
                raise MeasurementMismatchError(
                    f"measurement source is missing item {item.item_id}"
                )
            item.measurements = extract_item_measurements(full_item)
    session.measurements = extract_session_measurements(full_session)


__all__ = [
    "MeasurementMismatchError",
    "attach_measurements",
    "extract_item_measurements",
    "extract_session_measurements",
    "is_projection_only_item",
]
