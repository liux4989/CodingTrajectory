"""Observed structural context composition from canonical session facts."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID

from coding_trajectory.analysis.content_size import (
    event_text_size,
    item_input_size,
    item_output_size,
    item_text_size,
    visible_text_size,
)
from coding_trajectory.analysis.measurements import is_projection_only_item
from coding_trajectory.analysis.tool_summary import summarize_tool_call
from coding_trajectory.analysis.tool_summary_shared import (
    EDIT_FILE,
    LIST_FILES,
    READ_FILE,
    RUN_COMMAND,
    SEARCH_TEXT,
    SESSION_HANDOFF,
    SUBAGENT_TASK,
    TODO_LIST,
    WEB_FETCH,
    WEB_SEARCH,
    WRITE_FILE,
)
from coding_trajectory.ingestion.models import (
    ContextUsageObservation,
    EventType,
    Item,
    Session,
    SessionGraph,
)
from coding_trajectory.metrics.models import ContextCategoryFlat
from coding_trajectory.metrics.pricing import cost_evidence_from_usage
from coding_trajectory.metrics.usage_math import (
    sum_usage_dicts as _sum_usage_values,
)
from coding_trajectory.token_counter import session_scoped


class AnchorOutcome(Enum):
    """Whether the composition was anchored to the provider's used_input_tokens."""

    ANCHORED = "anchored"
    # ``used_input_tokens`` is missing/zero: nothing to anchor to.
    NO_USAGE = "no_usage"
    # No resident conversation (only the starting-context prefix): nothing to scale.
    NO_CONVERSATION = "no_conversation"
    # Provider ``used_input`` is below the starting prefix: visible content
    # overcounts (e.g. reasoning the API stripped), so estimates are retained.
    OVERCOUNT = "overcount"


@dataclass
class _Measure:
    tokens: int = 0
    chars: int = 0
    items: int = 0
    allocated_usage: dict[str, int] | None = None

    def add(
        self,
        *,
        tokens: int,
        chars: int,
        items: int = 1,
        allocated_usage: dict[str, int] | None = None,
    ) -> None:
        self.tokens += max(tokens, 0)
        self.chars += max(chars, 0)
        self.items += max(items, 0)
        self.allocated_usage = _sum_usage(self.allocated_usage, allocated_usage)

    def plus(self, other: "_Measure") -> "_Measure":
        return _Measure(
            tokens=self.tokens + other.tokens,
            chars=self.chars + other.chars,
            items=self.items + other.items,
            allocated_usage=_sum_usage(self.allocated_usage, other.allocated_usage),
        )


_STARTING_CONTEXT_LABELS = {
    "base_system": "Base instructions",
    "developer_instructions": "Developer instructions",
    "agents_md": "AGENTS.md",
    "skills": "Skills",
    "mcp": "Tools / MCP",
    "memory": "Memory",
}
_FILE_CONCEPT_LABELS = {
    READ_FILE: "Files read",
}
_CONTEXT_CONCEPTS = frozenset(_FILE_CONCEPT_LABELS)
_CODE_CHANGE_CONCEPTS = frozenset({EDIT_FILE, WRITE_FILE})
_COORDINATION_CONCEPTS = frozenset({TODO_LIST, SUBAGENT_TASK, SESSION_HANDOFF})
_OUTPUT_CONCEPT_LABELS = {
    SEARCH_TEXT: "Search output",
    LIST_FILES: "File listing output",
    RUN_COMMAND: "Command output",
    WEB_FETCH: "Web fetch output",
    WEB_SEARCH: "Web search output",
}
_OTHER_TOOL_OUTPUT = "other_tool"


@session_scoped
def build_context_composition(
    session_graph: SessionGraph,
    *,
    allocated_usage_by_item: dict[UUID, dict[str, int]] | None = None,
    allocated_usage_by_context_source: dict[str, dict[str, int]] | None = None,
    pricing_model: str | None = None,
    pricing_provider: str | None = None,
) -> tuple[list[ContextCategoryFlat], AnchorOutcome]:
    allocated_usage_by_item = allocated_usage_by_item or {}
    allocated_usage_by_context_source = allocated_usage_by_context_source or {}
    boundaries = {
        session.session_id: _eviction_boundary(session)
        for session in session_graph.sessions
    }
    starting = _starting_context(
        session_graph,
        allocated_usage_by_context_source=allocated_usage_by_context_source,
    )
    user_input = _user_input(
        session_graph,
        allocated_usage_by_item=allocated_usage_by_item,
        boundaries=boundaries,
    )
    agent_work = _agent_work(
        session_graph,
        allocated_usage_by_item=allocated_usage_by_item,
        boundaries=boundaries,
    )
    evicted = user_input[2].plus(agent_work[2])
    categories = [
        _category("starting_context", "Starting context", starting[0], starting[1]),
        _category("user_input", "User input", user_input[0], user_input[1]),
        _category("agent_work", "Agent work", agent_work[0], agent_work[1]),
    ]
    if evicted.items:
        # Pre-compaction content the API evicted is not resident in the final
        # context window, so it carries 0 visible tokens; but its historically
        # billed usage must still reconcile to the stats attribution, so the
        # allocated usage is retained on a separate (non-anchored) category.
        categories.append(
            _category("compacted_history", "Compacted history", evicted, [])
        )
    anchor_outcome = _anchor_composition_to_used_input(categories, session_graph)
    observed_total = sum(category.tokens for category in categories)
    _set_percent(categories, observed_total)
    _assert_context_composition_usage_reconciles(
        categories,
        allocated_usage_by_item,
        allocated_usage_by_context_source,
    )
    _attach_estimated_cost(categories, model=pricing_model, provider=pricing_provider)
    return categories, anchor_outcome


@session_scoped
def context_composition_anchor_outcome(
    session_graph: SessionGraph,
) -> AnchorOutcome:
    """Return the composition anchor outcome without materializing categories.

    Compact graph stats retain the anchor warning but omit each session's
    semantic category tree. The warning depends on only three facts: whether
    provider usage exists, whether any resident conversation content has a
    positive visible-token estimate, and the starting-context token total.
    Non-empty visible text always contributes at least one token, so the
    conversation check can avoid tokenizing every resident item.
    """
    latest = _latest_context_usage_observation(session_graph)
    if latest is None or not latest.used_input_tokens:
        return AnchorOutcome.NO_USAGE

    boundaries = {
        session.session_id: _eviction_boundary(session)
        for session in session_graph.sessions
    }
    if not _has_resident_conversation(session_graph, boundaries=boundaries):
        return AnchorOutcome.NO_CONVERSATION

    starting_tokens = sum(
        (tokens or reported_tokens or 0)
        for session in session_graph.sessions
        for _key, _label, _ts, reported_tokens, _chars, tokens in _context_source_sizes(
            session
        )
    )
    if latest.used_input_tokens - starting_tokens <= 0:
        return AnchorOutcome.OVERCOUNT
    return AnchorOutcome.ANCHORED


def _attach_estimated_cost(
    categories: list[ContextCategoryFlat],
    *,
    model: str | None,
    provider: str | None,
) -> None:
    """Walk the composition tree and price each category's allocated usage.

    Cost is the pricing SoT's estimate over the category's allocated usage
    bucket; the datahub reads it instead of repricing. Parent categories
    inherit their children's allocated usage sum, so their cost reflects the
    whole subtree.
    """
    for category in categories:
        if category.allocated_usage:
            category.estimated_cost = cost_evidence_from_usage(
                category.allocated_usage, model=model, provider=provider
            )
        if category.children:
            _attach_estimated_cost(category.children, model=model, provider=provider)


def _anchor_composition_to_used_input(
    categories: list[ContextCategoryFlat],
    session_graph: SessionGraph,
) -> AnchorOutcome:
    """Scale the conversation portion so the composition sums to the real
    ``used_input_tokens`` (the last API call's input).

    The composition is otherwise a sum of visible-content token estimates; it
    drifts from ``used_input_tokens`` as content accumulates (tokenizer error,
    missing content). Anchoring keeps the starting-context prefix at its own
    estimate (real ``reported_tokens`` for Claude Code's cached prefix, a
    tokenizer estimate for Codex's observed system text) and partitions the
    remaining real conversation total across the user-input and agent-work
    categories by their visible-token proportions - so the composition
    reconciles to the context window by construction. Falls back to the visible
    estimates when there is no usage observation.

    Proportions within the conversation are preserved; only the absolute scale
    is anchored. For sessions whose visible content overcounts (e.g. thinking
    the API stripped, so ``used_input`` is below the visible sum) the prefix
    guard leaves the estimates rather than zeroing the conversation. For
    undercounts (missing content) the missing total is attributed proportionally
    - a best-effort split, not a true per-item attribution.
    """
    latest = _latest_context_usage_observation(session_graph)
    if latest is None or not latest.used_input_tokens:
        return AnchorOutcome.NO_USAGE
    starting = next(
        (category for category in categories if category.key == "starting_context"),
        None,
    )
    base_tokens = starting.tokens if starting is not None else 0
    # ``compacted_history`` carries evicted (non-resident) content with 0 visible
    # tokens; exclude it from the scaled conversation so the anchor only rescales
    # resident user-input and agent-work categories.
    conversation = [
        category
        for category in categories
        if category.key not in ("starting_context", "compacted_history")
    ]
    conversation_visible = sum(category.tokens for category in conversation)
    if conversation_visible <= 0:
        return AnchorOutcome.NO_CONVERSATION
    conversation_real = latest.used_input_tokens - base_tokens
    if conversation_real <= 0:
        return AnchorOutcome.OVERCOUNT
    scale = conversation_real / conversation_visible
    for category in conversation:
        _scale_category_tokens(category, scale)
    return AnchorOutcome.ANCHORED


def _scale_category_tokens(category: ContextCategoryFlat, scale: float) -> None:
    if scale != 1.0:
        category.tokens = max(round(category.tokens * scale), 0)
    for child in category.children:
        _scale_category_tokens(child, scale)


def _latest_context_usage_observation(
    session_graph: SessionGraph,
) -> ContextUsageObservation | None:
    observations = [
        observation
        for session in session_graph.sessions
        for observation in session.context_usage
    ]
    return max(observations, key=lambda item: item.timestamp, default=None)


# Both Claude Code and Codex compactions are full-history evictions. Claude
# Code's ``claude_compact_boundary`` drops nearly all pre-boundary content
# (only a few preserved messages survive). Codex's ``context_compacted``
# replaces the entire conversation with a compact summary plus a small subset
# of retained user messages; all tool calls, tool outputs, reasoning, and
# assistant messages are evicted. In both cases the boundary timestamp is a
# sound signal to exclude pre-boundary items from the (final-window)
# composition.
_COMPACTION_EVICTING_KINDS = frozenset({"claude_compact_boundary", "context_compacted"})


def _eviction_boundary(session: Session) -> datetime | None:
    """Timestamp of the last full-eviction compaction boundary, or None."""
    boundary: datetime | None = None
    for observation in session.runtime_observations:
        if observation.kind not in _COMPACTION_EVICTING_KINDS:
            continue
        if boundary is None or observation.timestamp > boundary:
            boundary = observation.timestamp
    return boundary


def _is_resident(timestamp: datetime, boundary: datetime | None) -> bool:
    """Whether a timestamped item survives the last compaction boundary."""
    return boundary is None or timestamp >= boundary


def _has_resident_conversation(
    session_graph: SessionGraph,
    *,
    boundaries: dict[UUID, datetime | None],
) -> bool:
    """Whether composition would assign positive tokens to conversation."""
    for session in session_graph.sessions:
        boundary = boundaries.get(session.session_id)
        if any(
            event.type == EventType.USER_PROMPT_SUBMITTED
            and _is_resident(event.timestamp, boundary)
            and isinstance(event.payload.get("text"), str)
            and bool(event.payload["text"])
            for event in session.events
        ):
            return True

    has_agent_message_items = any(
        item.kind == "agent_message"
        and _is_resident(item.started_at, boundaries.get(item.session_id))
        for session in session_graph.sessions
        for turn in session.turns
        for item in turn.items
    )
    for session in session_graph.sessions:
        boundary = boundaries.get(session.session_id)
        session_measurements = getattr(session, "measurements", None)
        if not has_agent_message_items:
            if session_measurements is not None:
                if any(
                    _is_resident(size.timestamp, boundary)
                    for size in session_measurements.llm_response_text_sizes
                ):
                    return True
            elif any(
                event.type == EventType.LLM_RESPONSE
                and _is_resident(event.timestamp, boundary)
                and isinstance(event.payload.get("text"), str)
                and bool(event.payload["text"])
                for event in session.events
            ):
                return True
        for turn in session.turns:
            for item in turn.items:
                if not _is_resident(item.started_at, boundary):
                    continue
                if item.kind in {"reasoning", "agent_message"}:
                    if item_text_size(item).chars:
                        return True
                    continue
                if item.kind in {
                    "tool_call",
                    "command_execution",
                    "file_change",
                    "plan",
                } and (item_input_size(item).chars or item_output_size(item).chars):
                    return True
    return False


def _context_source_sizes(
    session: Session,
) -> Iterable[tuple[str, str, datetime, int | None, int, int]]:
    """Yield (key, label, timestamp, reported_tokens, chars, tokens) per source.

    Compact sessions carry measurements instead of source text; both forms
    produce identical rows.
    """
    measurements = getattr(session, "measurements", None)
    if measurements is not None:
        for source in measurements.context_sources:
            yield (
                source.key,
                source.label,
                source.timestamp,
                source.reported_tokens,
                source.chars,
                source.tokens,
            )
        return
    for source in session.context_sources:
        size = visible_text_size(source.text)
        yield (
            source.key,
            source.label,
            source.timestamp,
            source.reported_tokens,
            size.chars,
            size.tokens,
        )


def _starting_context(
    session_graph: SessionGraph,
    *,
    allocated_usage_by_context_source: dict[str, dict[str, int]],
) -> tuple[_Measure, list[ContextCategoryFlat]]:
    buckets: dict[str, _Measure] = defaultdict(_Measure)
    labels: dict[str, str] = {}
    usage_added_keys: set[str] = set()
    for session in session_graph.sessions:
        for (
            key,
            label,
            _ts,
            reported_tokens,
            chars,
            visible_tokens,
        ) in _context_source_sizes(session):
            tokens = visible_tokens or (reported_tokens or 0)
            allocated_usage = None
            if key not in usage_added_keys:
                allocated_usage = allocated_usage_by_context_source.get(key)
                usage_added_keys.add(key)
            buckets[key].add(
                tokens=tokens,
                chars=chars,
                allocated_usage=allocated_usage,
            )
            labels[key] = label
    children = [
        _category(
            key,
            labels.get(key) or _STARTING_CONTEXT_LABELS.get(key) or _label(key),
            buckets[key],
        )
        for key in sorted(buckets, key=lambda key: (-buckets[key].tokens, key))
        if buckets[key].items
    ]
    return _sum(child_measure for child_measure in buckets.values()), children


def _user_input(
    session_graph: SessionGraph,
    *,
    allocated_usage_by_item: dict[UUID, dict[str, int]],
    boundaries: dict[UUID, datetime | None],
) -> tuple[_Measure, list[ContextCategoryFlat], _Measure]:
    buckets: dict[str, _Measure] = defaultdict(_Measure)
    evicted = _Measure()
    prompt_index = 0
    for session in session_graph.sessions:
        boundary = boundaries.get(session.session_id)
        for event in session.events:
            if event.type != EventType.USER_PROMPT_SUBMITTED:
                continue
            text = event.payload.get("text")
            if not isinstance(text, str) or not text:
                continue
            allocated_usage = allocated_usage_by_item.get(event.event_id)
            if not _is_resident(event.timestamp, boundary):
                evicted = evicted.plus(
                    _Measure(items=1, allocated_usage=allocated_usage)
                )
                continue
            key = (
                "user_initial_request"
                if prompt_index == 0
                else "user_follow_up_requests"
            )
            size = event_text_size(event)
            buckets[key].add(
                tokens=size.tokens,
                chars=size.chars,
                allocated_usage=allocated_usage,
            )
            prompt_index += 1
    labels = {
        "user_initial_request": "Initial request",
        "user_follow_up_requests": "Follow-up requests",
    }
    children = [
        _category(key, labels[key], buckets[key])
        for key in labels
        if buckets[key].items
    ]
    return _sum(buckets.values()), children, evicted


def _agent_work(
    session_graph: SessionGraph,
    *,
    allocated_usage_by_item: dict[UUID, dict[str, int]],
    boundaries: dict[UUID, datetime | None],
) -> tuple[_Measure, list[ContextCategoryFlat], _Measure]:
    files: dict[str, _Measure] = defaultdict(_Measure)
    agent: dict[str, _Measure] = defaultdict(_Measure)
    output: dict[str, _Measure] = defaultdict(_Measure)
    evicted = _Measure()
    has_agent_message_items = any(
        item.kind == "agent_message"
        and _is_resident(item.started_at, boundaries.get(item.session_id))
        for session in session_graph.sessions
        for turn in session.turns
        for item in turn.items
    )

    for session in session_graph.sessions:
        boundary = boundaries.get(session.session_id)
        session_measurements = getattr(session, "measurements", None)
        if not has_agent_message_items:
            if session_measurements is not None:
                for size in session_measurements.llm_response_text_sizes:
                    if not _is_resident(size.timestamp, boundary):
                        continue
                    agent["assistant_messages"].add(
                        tokens=size.tokens, chars=size.chars
                    )
            else:
                for event in session.events:
                    if event.type != EventType.LLM_RESPONSE:
                        continue
                    if not _is_resident(event.timestamp, boundary):
                        continue
                    text = event.payload.get("text")
                    if not isinstance(text, str) or not text:
                        continue
                    size = visible_text_size(text)
                    agent["assistant_messages"].add(
                        tokens=size.tokens, chars=size.chars
                    )

        for turn in session.turns:
            for item in turn.items:
                if not _is_resident(item.started_at, boundary):
                    evicted = evicted.plus(
                        _Measure(
                            items=1,
                            allocated_usage=allocated_usage_by_item.get(item.item_id),
                        )
                    )
                    continue
                if item.kind == "reasoning":
                    size = item_text_size(item)
                    agent["assistant_messages"].add(
                        tokens=size.tokens,
                        chars=size.chars,
                        allocated_usage=allocated_usage_by_item.get(item.item_id),
                    )
                    continue
                if item.kind == "agent_message":
                    size = item_text_size(item)
                    agent["assistant_messages"].add(
                        tokens=size.tokens,
                        chars=size.chars,
                        allocated_usage=allocated_usage_by_item.get(item.item_id),
                    )
                    continue
                _add_tool_item(
                    item,
                    files=files,
                    agent=agent,
                    output=output,
                    allocated_usage_by_item=allocated_usage_by_item,
                )

    children = _assemble_agent_categories(files, agent, output)
    return _measure_from_categories(children), children, evicted


def _assemble_agent_categories(
    files: dict[str, _Measure],
    agent: dict[str, _Measure],
    output: dict[str, _Measure],
) -> list[ContextCategoryFlat]:
    """Shape accumulated file/agent/output measures into the composition children tree."""
    file_children = [
        _category(
            _file_category_key(concept),
            _FILE_CONCEPT_LABELS[concept],
            files[concept],
        )
        for concept in _FILE_CONCEPT_LABELS
        if files[concept].items
    ]
    message_children = (
        [
            _category(
                "assistant_messages", "Assistant messages", agent["assistant_messages"]
            )
        ]
        if agent["assistant_messages"].items
        else []
    )
    coordination_children = [
        _category(key, label, agent[key])
        for key, label in (
            ("todolist", "Plans / todos"),
            ("subagenttask", "Subagent results"),
            ("sessionhandoff", "Handoffs"),
        )
        if agent[key].items
    ]
    code_children = [
        _category(key, label, agent[key])
        for key, label in (
            ("editfile", "Edits / patches"),
            ("writefile", "Files written"),
        )
        if agent[key].items
    ]
    agent_children = [
        category
        for category in (
            *message_children,
            _parent("code_changes", "Code changes", code_children),
            _parent("coordination", "Coordination", coordination_children),
        )
        if category is not None
    ]
    output_children = [
        _category(
            f"output_{'command' if concept == RUN_COMMAND else concept.lower()}",
            label,
            output[concept],
        )
        for concept, label in _OUTPUT_CONCEPT_LABELS.items()
        if output[concept].items
    ]
    if output[_OTHER_TOOL_OUTPUT].items:
        output_children.append(
            _category(
                "output_other_tool",
                "Other tool output",
                output[_OTHER_TOOL_OUTPUT],
            )
        )

    children = [
        category
        for category in (
            _parent("files", "Files", file_children),
            _parent("output", "Output", output_children),
            _parent("agent", "Agent", agent_children),
        )
        if category is not None
    ]
    return children


def _add_tool_item(
    item: Item,
    *,
    files: dict[str, _Measure],
    agent: dict[str, _Measure],
    output: dict[str, _Measure],
    allocated_usage_by_item: dict[UUID, dict[str, int]],
) -> None:
    if item.kind not in {"tool_call", "command_execution", "file_change", "plan"}:
        return
    # Static wrapper children are semantic activity projections, not additional
    # provider-visible content blocks. Their original wrapper remains canonical
    # and owns the content measurement.
    if is_projection_only_item(item):
        return
    summary = summarize_tool_call(item) or {}
    concept = str(summary.get("name") or item.tool_name or item.kind)
    input_size = item_input_size(item)
    output_size = item_output_size(item)
    allocated_usage = allocated_usage_by_item.get(item.item_id)
    # Tool-use input args (the command, file path, search query, etc.) are part
    # of the resident tool_use block in every API request, so they are sized
    # alongside the output for every concept - not just edits/coordination.
    # Previously non-edit tools counted output only, undercounting Bash commands
    # and other input-heavy tool calls by their full argument size.
    tokens = input_size.tokens + output_size.tokens
    chars = input_size.chars + output_size.chars

    if concept in _CONTEXT_CONCEPTS:
        files[concept].add(
            tokens=tokens,
            chars=chars,
            allocated_usage=allocated_usage,
        )
        return
    if concept in _OUTPUT_CONCEPT_LABELS:
        output[concept].add(
            tokens=tokens,
            chars=chars,
            allocated_usage=allocated_usage,
        )
        return
    if concept in _CODE_CHANGE_CONCEPTS:
        agent[concept.lower()].add(
            tokens=tokens,
            chars=chars,
            allocated_usage=allocated_usage,
        )
        return
    if concept in _COORDINATION_CONCEPTS:
        agent[concept.lower()].add(
            tokens=tokens,
            chars=chars,
            allocated_usage=allocated_usage,
        )
        return
    output[_OTHER_TOOL_OUTPUT].add(
        tokens=tokens,
        chars=chars,
        allocated_usage=allocated_usage,
    )


def _file_category_key(concept: str) -> str:
    if concept in _CODE_CHANGE_CONCEPTS:
        return concept.lower()
    return f"context_{concept.lower()}"


def _parent(
    key: str,
    label: str,
    children: list[ContextCategoryFlat],
) -> ContextCategoryFlat | None:
    if not children:
        return None
    return _category(key, label, _measure_from_categories(children), children)


def _category(
    key: str,
    label: str,
    measure: _Measure,
    children: list[ContextCategoryFlat] | None = None,
) -> ContextCategoryFlat:
    return ContextCategoryFlat(
        key=key,
        label=label,
        tokens=measure.tokens,
        allocated_usage=measure.allocated_usage,
        observed_chars=measure.chars,
        items=measure.items,
        confidence="estimated_tokens",
        source="canonical visible content",
        children=children or [],
    )


def _set_percent(categories: Iterable[ContextCategoryFlat], denominator: int) -> None:
    for category in categories:
        category.percent = (
            round((category.tokens / denominator) * 100, 1) if denominator else None
        )
        _set_percent(category.children, denominator)


def _measure_from_categories(categories: Iterable[ContextCategoryFlat]) -> _Measure:
    return _sum(
        _Measure(
            tokens=category.tokens,
            chars=category.observed_chars,
            items=category.items,
            allocated_usage=category.allocated_usage,
        )
        for category in categories
    )


def _assert_context_composition_usage_reconciles(
    categories: list[ContextCategoryFlat],
    allocated_usage_by_item: dict[UUID, dict[str, int]],
    allocated_usage_by_context_source: dict[str, dict[str, int]],
) -> None:
    if not allocated_usage_by_item and not allocated_usage_by_context_source:
        return
    expected = _sum_usage_values(
        (
            _sum_usage_values(allocated_usage_by_context_source.values()),
            _sum_usage_values(allocated_usage_by_item.values()),
        )
    )
    actual = _sum_usage_values(
        category.allocated_usage or {} for category in categories
    )
    assert actual == expected, (
        "context composition allocated usage must reconcile to stats attribution"
    )


def _sum(measures: Iterable[_Measure]) -> _Measure:
    total = _Measure()
    for measure in measures:
        total = total.plus(measure)
    return total


def _sum_usage(
    left: dict[str, int] | None,
    right: dict[str, int] | None,
) -> dict[str, int] | None:
    if left is None and right is None:
        return None
    result: dict[str, int] = {}
    for source in (left or {}, right or {}):
        for key, value in source.items():
            result[key] = result.get(key, 0) + max(value, 0)
    return result or None


def _label(value: str) -> str:
    return value.replace("_", " ").strip().title()
