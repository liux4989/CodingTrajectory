"""Observed structural context composition from canonical session facts."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

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

    def add(self, *, tokens: int, chars: int, items: int = 1) -> None:
        self.tokens += max(tokens, 0)
        self.chars += max(chars, 0)
        self.items += max(items, 0)

    def plus(self, other: "_Measure") -> "_Measure":
        return _Measure(
            tokens=self.tokens + other.tokens,
            chars=self.chars + other.chars,
            items=self.items + other.items,
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
    SEARCH_TEXT: "Search results",
    LIST_FILES: "File listings",
    WEB_FETCH: "Web pages fetched",
    WEB_SEARCH: "Web search results",
}
_COMMAND_FAMILY_LABELS = {
    "cli_report": "CLI / report inspection",
    "tests": "Tests",
    "build": "Build / typecheck / lint",
    "code_fix": "Formatters / fixers",
    "repository": "Repository operations",
    "dependency": "Package management",
    "diagnostic": "Runtime diagnostics",
    "external": "External interaction",
    "runtime": "Execution / app runtime",
    "other": "Other command output",
}
_CONTEXT_CONCEPTS = frozenset(_FILE_CONCEPT_LABELS)
_CODE_CHANGE_CONCEPTS = frozenset({EDIT_FILE, WRITE_FILE})
_COORDINATION_CONCEPTS = frozenset({TODO_LIST, SUBAGENT_TASK, SESSION_HANDOFF})


def build_context_composition(session_graph: SessionGraph) -> list[ContextCategoryFlat]:
    starting = _starting_context(session_graph)
    user_input = _user_input(session_graph)
    agent_work = _agent_work(session_graph)
    observed_total = starting[0].plus(user_input[0]).plus(agent_work[0]).tokens
    categories = [
        _category("starting_context", "Starting context", starting[0], starting[1]),
        _category("user_input", "User input", user_input[0], user_input[1]),
        _category("agent_work", "Agent work", agent_work[0], agent_work[1]),
    ]
    _set_percent(categories, observed_total)
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


def _user_input(session_graph: SessionGraph) -> tuple[_Measure, list[ContextCategoryFlat]]:
    buckets: dict[str, _Measure] = defaultdict(_Measure)
    prompt_index = 0
    for session in session_graph.sessions:
        for event in session.events:
            if event.type != EventType.USER_PROMPT_SUBMITTED:
                continue
            text = event.payload.get("text")
            if not isinstance(text, str) or not text:
                continue
            key = "user_initial_request" if prompt_index == 0 else "user_follow_up_requests"
            size = visible_text_size(text)
            buckets[key].add(tokens=size.tokens, chars=size.chars)
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


def _agent_work(session_graph: SessionGraph) -> tuple[_Measure, list[ContextCategoryFlat]]:
    files: dict[str, _Measure] = defaultdict(_Measure)
    commands: dict[str, _Measure] = defaultdict(_Measure)
    agent: dict[str, _Measure] = defaultdict(_Measure)
    other_tools = _Measure()

    for session in session_graph.sessions:
        for event in session.events:
            if event.type != EventType.LLM_RESPONSE:
                continue
            text = event.payload.get("text")
            if not isinstance(text, str) or not text:
                continue
            phase = event.payload.get("phase")
            key = (
                "final_answer"
                if phase == "final_answer"
                else "progress_update"
                if isinstance(phase, str) and phase
                else "assistant_message"
            )
            size = visible_text_size(text)
            agent[key].add(tokens=size.tokens, chars=size.chars)

        for turn in session.turns:
            for item in turn.items:
                if item.kind == "reasoning":
                    text = item.text or ""
                    size = visible_text_size(text)
                    agent["reasoning"].add(tokens=size.tokens, chars=size.chars)
                    continue
                _add_tool_item(item, files=files, commands=commands, agent=agent, other=other_tools)

    file_children = [
        _category(
            f"context_{concept.lower()}",
            _FILE_CONCEPT_LABELS[concept],
            files[concept],
        )
        for concept in _FILE_CONCEPT_LABELS
        if files[concept].items
    ]
    output_children = _command_children(commands)
    if other_tools.items:
        output_children.append(_category("tool_other", "Other tool output", other_tools))

    message_children = [
        _category(key, label, agent[key])
        for key, label in (
            ("final_answer", "Final answers"),
            ("progress_update", "Progress updates"),
            ("assistant_message", "Other assistant messages"),
            ("reasoning", "Reasoning"),
        )
        if agent[key].items
    ]
    code_children = [
        _category(key, label, agent[key])
        for key, label in (
            ("editfile", "Edits / patches"),
            ("writefile", "Files written"),
            ("code_fix", "Formatters / fixers"),
        )
        if agent[key].items
    ]
    coordination_children = [
        _category(key, label, agent[key])
        for key, label in (
            ("todolist", "Plans / todos"),
            ("subagenttask", "Subagent results"),
            ("sessionhandoff", "Handoffs"),
        )
        if agent[key].items
    ]
    agent_children = [
        category
        for category in (
            _parent("agent_messages", "Agent messages", message_children),
            _parent("code_changes", "Code changes", code_children),
            _parent("coordination", "Coordination", coordination_children),
        )
        if category is not None
    ]

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
    commands: dict[str, _Measure],
    agent: dict[str, _Measure],
    other: _Measure,
) -> None:
    if item.kind not in {"tool_call", "command_execution", "file_change", "plan"}:
        return
    summary = summarize_tool_call(item) or {}
    concept = str(summary.get("name") or item.tool_name or item.kind)
    input_size = item_input_size(item)
    output_size = item_output_size(item)

    if concept in _CONTEXT_CONCEPTS:
        files[concept].add(tokens=output_size.tokens, chars=output_size.chars)
        return
    if concept == RUN_COMMAND:
        family = str(summary.get("command_family") or "other")
        target = agent["code_fix"] if family == "code_fix" else commands[family]
        target.add(tokens=output_size.tokens, chars=output_size.chars)
        return
    if concept in _CODE_CHANGE_CONCEPTS:
        key = concept.lower()
        agent[key].add(
            tokens=input_size.tokens + output_size.tokens,
            chars=input_size.chars + output_size.chars,
        )
        return
    if concept in _COORDINATION_CONCEPTS:
        key = concept.lower()
        agent[key].add(
            tokens=input_size.tokens + output_size.tokens,
            chars=input_size.chars + output_size.chars,
        )
        return
    other.add(tokens=output_size.tokens, chars=output_size.chars)


def _command_children(commands: dict[str, _Measure]) -> list[ContextCategoryFlat]:
    verification = [
        _category(f"tool_runcommand_{key}", _COMMAND_FAMILY_LABELS[key], commands[key])
        for key in ("tests", "build")
        if commands[key].items
    ]
    dependency = [
        _category(f"tool_runcommand_{key}", _COMMAND_FAMILY_LABELS[key], commands[key])
        for key in ("dependency", "diagnostic")
        if commands[key].items
    ]
    children = [
        _category("tool_runcommand_cli_report", _COMMAND_FAMILY_LABELS["cli_report"], commands["cli_report"])
        if commands["cli_report"].items
        else None,
        _parent("verification", "Verification", verification),
        _category("repository_operations", _COMMAND_FAMILY_LABELS["repository"], commands["repository"])
        if commands["repository"].items
        else None,
        _parent("dependency_environment", "Dependency / environment", dependency),
        _category("execution_runtime", _COMMAND_FAMILY_LABELS["runtime"], commands["runtime"])
        if commands["runtime"].items
        else None,
        _category("external_interaction", _COMMAND_FAMILY_LABELS["external"], commands["external"])
        if commands["external"].items
        else None,
        _category("command_other", _COMMAND_FAMILY_LABELS["other"], commands["other"])
        if commands["other"].items
        else None,
    ]
    return [child for child in children if child is not None]


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
        observed_chars=measure.chars,
        items=measure.items,
        confidence="estimated_tokens",
        source="canonical visible content",
        children=children or [],
    )


def _set_percent(categories: Iterable[ContextCategoryFlat], denominator: int) -> None:
    for category in categories:
        category.percent = round((category.tokens / denominator) * 100, 1) if denominator else None
        _set_percent(category.children, denominator)


def _measure_from_categories(categories: Iterable[ContextCategoryFlat]) -> _Measure:
    return _sum(
        _Measure(tokens=category.tokens, chars=category.observed_chars, items=category.items)
        for category in categories
    )


def _sum(measures: Iterable[_Measure]) -> _Measure:
    total = _Measure()
    for measure in measures:
        total = total.plus(measure)
    return total


def _label(value: str) -> str:
    return value.replace("_", " ").strip().title()
