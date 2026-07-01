"""Observed structural context composition from canonical session facts."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable
from uuid import UUID

from coding_trajectory.analysis.content_size import (
    item_input_size,
    item_output_size,
    visible_text_size,
)
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
from coding_trajectory.ingestion.models import EventType, Item, SessionGraph
from coding_trajectory.metrics.models import ContextCategoryFlat


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
    WEB_FETCH: "Web fetch output",
    WEB_SEARCH: "Web search output",
}
_OUTPUT_FAMILY_LABELS = {
    "cli_report": "CLI reports",
    "search": "Search output",
    "list_files": "File listing output",
    "read_file": "File read command output",
    "tests": "Test output",
    "build": "Build / lint output",
    "code_fix": "Formatter / fixer output",
    "repository": "Repository output",
    "dependency": "Dependency output",
    "diagnostic": "Diagnostic output",
    "external": "External service output",
    "runtime": "Runtime output",
    "other": "Other command output",
}


def build_context_composition(
    session_graph: SessionGraph,
    *,
    allocated_usage_by_item: dict[UUID, dict[str, int]] | None = None,
) -> list[ContextCategoryFlat]:
    allocated_usage_by_item = allocated_usage_by_item or {}
    starting = _starting_context(session_graph)
    user_input = _user_input(
        session_graph,
        allocated_usage_by_item=allocated_usage_by_item,
    )
    agent_work = _agent_work(
        session_graph,
        allocated_usage_by_item=allocated_usage_by_item,
    )
    observed_total = starting[0].plus(user_input[0]).plus(agent_work[0]).tokens
    categories = [
        _category("starting_context", "Starting context", starting[0], starting[1]),
        _category("user_input", "User input", user_input[0], user_input[1]),
        _category("agent_work", "Agent work", agent_work[0], agent_work[1]),
    ]
    _set_percent(categories, observed_total)
    _assert_context_composition_usage_reconciles(
        categories,
        allocated_usage_by_item,
    )
    return categories


def _starting_context(
    session_graph: SessionGraph,
) -> tuple[_Measure, list[ContextCategoryFlat]]:
    buckets: dict[str, _Measure] = defaultdict(_Measure)
    labels: dict[str, str] = {}
    for session in session_graph.sessions:
        for source in session.context_sources:
            size = visible_text_size(source.text)
            buckets[source.key].add(tokens=size.tokens, chars=size.chars)
            labels[source.key] = source.label
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
) -> tuple[_Measure, list[ContextCategoryFlat]]:
    buckets: dict[str, _Measure] = defaultdict(_Measure)
    prompt_index = 0
    for session in session_graph.sessions:
        for event in session.events:
            if event.type != EventType.USER_PROMPT_SUBMITTED:
                continue
            text = event.payload.get("text")
            if not isinstance(text, str) or not text:
                continue
            key = (
                "user_initial_request"
                if prompt_index == 0
                else "user_follow_up_requests"
            )
            size = visible_text_size(text)
            buckets[key].add(
                tokens=size.tokens,
                chars=size.chars,
                allocated_usage=allocated_usage_by_item.get(event.event_id),
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
    return _sum(buckets.values()), children


def _agent_work(
    session_graph: SessionGraph,
    *,
    allocated_usage_by_item: dict[UUID, dict[str, int]],
) -> tuple[_Measure, list[ContextCategoryFlat]]:
    files: dict[str, _Measure] = defaultdict(_Measure)
    agent: dict[str, _Measure] = defaultdict(_Measure)
    output: dict[str, _Measure] = defaultdict(_Measure)
    has_agent_message_items = any(
        item.kind == "agent_message"
        for session in session_graph.sessions
        for turn in session.turns
        for item in turn.items
    )

    for session in session_graph.sessions:
        if not has_agent_message_items:
            for event in session.events:
                if event.type != EventType.LLM_RESPONSE:
                    continue
                text = event.payload.get("text")
                if not isinstance(text, str) or not text:
                    continue
                size = visible_text_size(text)
                agent["assistant_messages"].add(tokens=size.tokens, chars=size.chars)

        for turn in session.turns:
            for item in turn.items:
                if item.kind == "reasoning":
                    text = item.text or ""
                    size = visible_text_size(text)
                    agent["assistant_messages"].add(
                        tokens=size.tokens,
                        chars=size.chars,
                        allocated_usage=allocated_usage_by_item.get(item.item_id),
                    )
                    continue
                if item.kind == "agent_message":
                    text = item.text or ""
                    size = visible_text_size(text)
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
        _category(f"output_{concept.lower()}", label, output[concept])
        for concept, label in _OUTPUT_CONCEPT_LABELS.items()
        if output[concept].items
    ]
    output_children.extend(
        _category(f"output_{key}", label, output[key])
        for key, label in _OUTPUT_FAMILY_LABELS.items()
        if output[key].items
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
    return _measure_from_categories(children), children


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
    summary = summarize_tool_call(item) or {}
    concept = str(summary.get("name") or item.tool_name or item.kind)
    input_size = item_input_size(item)
    output_size = item_output_size(item)
    allocated_usage = allocated_usage_by_item.get(item.item_id)

    if concept in _CONTEXT_CONCEPTS:
        files[concept].add(
            tokens=output_size.tokens,
            chars=output_size.chars,
            allocated_usage=allocated_usage,
        )
        return
    if concept in _OUTPUT_CONCEPT_LABELS:
        output[concept].add(
            tokens=output_size.tokens,
            chars=output_size.chars,
            allocated_usage=allocated_usage,
        )
        return
    if concept in _CODE_CHANGE_CONCEPTS:
        key = concept.lower()
        agent[key].add(
            tokens=input_size.tokens + output_size.tokens,
            chars=input_size.chars + output_size.chars,
            allocated_usage=allocated_usage,
        )
        return
    if concept in _COORDINATION_CONCEPTS:
        key = concept.lower()
        agent[key].add(
            tokens=input_size.tokens + output_size.tokens,
            chars=input_size.chars + output_size.chars,
            allocated_usage=allocated_usage,
        )
        return
    output[_output_family_key(summary, concept)].add(
        tokens=output_size.tokens,
        chars=output_size.chars,
        allocated_usage=allocated_usage,
    )


def _file_category_key(concept: str) -> str:
    if concept in _CODE_CHANGE_CONCEPTS:
        return concept.lower()
    return f"context_{concept.lower()}"


def _output_family_key(summary: dict[str, object], concept: str) -> str:
    if concept != RUN_COMMAND:
        return "other"
    family = summary.get("command_family")
    return (
        family
        if isinstance(family, str) and family in _OUTPUT_FAMILY_LABELS
        else "other"
    )


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
) -> None:
    if not allocated_usage_by_item:
        return
    expected = _sum_usage_values(allocated_usage_by_item.values())
    actual = _sum_usage_values(
        category.allocated_usage or {} for category in categories
    )
    assert (
        actual == expected
    ), "context composition allocated usage must reconcile to resident item usage"


def _sum_usage_values(items: Iterable[dict[str, int]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in items:
        for key, value in item.items():
            result[key] = result.get(key, 0) + max(value, 0)
    return {key: value for key, value in result.items() if value > 0}


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
