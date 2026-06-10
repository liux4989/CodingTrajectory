"""Infer context category attribution from canonical ingestion facts."""

from __future__ import annotations

from collections import Counter
import os
import re
from typing import Any

from coding_trajectory.analysis.tool_summary import summarize_tool_call
from coding_trajectory.analysis.tool_summary_shell import primary_stage, safe_split, shell_cmd
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
from coding_trajectory.ingestion.models import EventType, SessionGraph, ToolCallItem
from coding_trajectory.metrics.context_stats._common import (
    percent,
)
from coding_trajectory.metrics.context_stats.command_families import (
    BUILD_TOKENS,
    COMMAND_RUNNERS,
    DEPENDENCY_TOKENS,
    PACKAGE_MANAGERS,
    RUNNER_SUBWORDS,
    TEST_TOKENS,
)
from coding_trajectory.metrics.models import ContextCategoryFlat


_CONTEXT_SOURCE_LABELS: dict[str, str] = {
    READ_FILE: "Files read",
    SEARCH_TEXT: "Search results",
    LIST_FILES: "File listings",
    WEB_FETCH: "Web pages fetched",
    WEB_SEARCH: "Web search results",
}

_AGENT_RESPONSE_LABELS: dict[str, str] = {
    "final_answer": "Final answers",
    "progress_update": "Progress updates",
    "assistant_message": "Other assistant messages",
}

_TOOL_CONCEPT_LABELS: dict[str, str] = {
    EDIT_FILE: "Edits / patches",
    WRITE_FILE: "Files written",
    TODO_LIST: "Plans / todos",
    SUBAGENT_TASK: "Subagent results",
    SESSION_HANDOFF: "Handoffs",
}

_COMMAND_FAMILY_LABELS: dict[str, str] = {
    "tests": "Tests",
    "build": "Build / typecheck / lint",
    "cli_report": "CLI / report inspection",
    "code_fix": "Formatters / fixers",
    "dependency": "Package manager",
    "diagnostic": "Runtime diagnostics",
    "external": "External interaction",
    "repo": "Git / repo commands",
    "runtime": "App / service runtime",
    "other": "Other command output",
}

_CONTEXT_CONCEPTS: frozenset[str] = frozenset(
    {READ_FILE, SEARCH_TEXT, LIST_FILES, WEB_FETCH, WEB_SEARCH}
)
_CODE_CHANGE_CONCEPTS: frozenset[str] = frozenset({EDIT_FILE, WRITE_FILE})
_COORDINATION_CONCEPTS: frozenset[str] = frozenset(
    {TODO_LIST, SUBAGENT_TASK, SESSION_HANDOFF}
)


def build_inferred_context_categories(
    session_graph: SessionGraph,
    used_tokens: int,
    context_window: int,
) -> list[ContextCategoryFlat]:
    setup_raw = Counter[str]()
    for session in session_graph.sessions:
        for source in session.context_sources:
            setup_raw[source.key] += _estimate_text_tokens(source.text)

    denominator = context_window or used_tokens
    setup_tokens = min(sum(setup_raw.values()), used_tokens)
    residual_tokens = max(used_tokens - setup_tokens, 0)

    prompt_raw, agent_raw, tool_raw = _conversation_raw_tokens(session_graph)
    scaled = _scaled_context_tokens(
        {
            **{f"prompt:{source}": tokens for source, tokens in prompt_raw.items() if _is_user_input_source(source)},
            **{f"context:{source}": tokens for source, tokens in prompt_raw.items() if not _is_user_input_source(source)},
            **{f"agent:{source}": tokens for source, tokens in agent_raw.items()},
            **{f"tool:{tool_name}": tokens for tool_name, tokens in tool_raw.items()},
        },
        target_total=residual_tokens,
    )

    def _leaves(prefix: str, keys: list[str], label_fn: Any) -> list[ContextCategoryFlat]:
        return _category_children(
            [
                (f"{prefix}_{_slug(key)}", label_fn(key), scaled[f"{prefix}:{key}"])
                for key in sorted(keys, key=lambda name: (-scaled[f"{prefix}:{name}"], name))
            ],
            denominator=denominator,
        )

    def _parent(key: str, label: str, children: list[ContextCategoryFlat]) -> tuple[str, str, int, list[ContextCategoryFlat]]:
        return key, label, sum(child.tokens for child in children), children

    setup_children = _category_children(
        [
            ("base_system", "Base instructions", setup_raw["base_system"]),
            ("developer_instructions", "Developer instructions", setup_raw["developer_instructions"]),
            ("agents_md", "AGENTS.md", setup_raw["agents_md"]),
            ("skills", "Skills", setup_raw["skills"]),
            ("mcp", "Tools / MCP", setup_raw["mcp"]),
            ("memory", "Memory", setup_raw["memory"]),
        ],
        denominator=denominator,
        keep_zero_keys={"base_system", "developer_instructions", "agents_md", "skills", "mcp", "memory"},
    )
    prompt_children = _leaves(
        "prompt",
        [source for source in prompt_raw if _is_user_input_source(source)],
        _prompt_label,
    )
    context_children = _leaves(
        "context",
        [source for source in prompt_raw if not _is_user_input_source(source)],
        lambda source: _CONTEXT_SOURCE_LABELS.get(source, _tool_label(source)),
    )
    response_children = _leaves(
        "agent",
        list(agent_raw),
        lambda source: _AGENT_RESPONSE_LABELS.get(source, _tool_label(source)),
    )

    code_children = _leaves("tool", [k for k in tool_raw if k in _CODE_CHANGE_CONCEPTS], _TOOL_CONCEPT_LABELS.get)
    command_context_children = _leaves(
        "tool",
        [
            k for k in tool_raw
            if k.startswith(f"{RUN_COMMAND}:cli_report")
        ],
        lambda key: _COMMAND_FAMILY_LABELS[key.split(":", 1)[1]],
    )
    code_command_children = _leaves(
        "tool",
        [k for k in tool_raw if k.startswith(f"{RUN_COMMAND}:code_fix")],
        lambda key: _COMMAND_FAMILY_LABELS[key.split(":", 1)[1]],
    )
    code_children = code_children + code_command_children

    verification_children = _leaves(
        "tool",
        [
            k for k in tool_raw
            if k.startswith(f"{RUN_COMMAND}:tests") or k.startswith(f"{RUN_COMMAND}:build")
        ],
        lambda key: _COMMAND_FAMILY_LABELS[key.split(":", 1)[1]],
    )
    repository_children = _leaves(
        "tool",
        [k for k in tool_raw if k.startswith(f"{RUN_COMMAND}:repo")],
        lambda key: _COMMAND_FAMILY_LABELS[key.split(":", 1)[1]],
    )
    dependency_children = _leaves(
        "tool",
        [
            k for k in tool_raw
            if k.startswith(f"{RUN_COMMAND}:dependency") or k.startswith(f"{RUN_COMMAND}:diagnostic")
        ],
        lambda key: _COMMAND_FAMILY_LABELS[key.split(":", 1)[1]],
    )
    runtime_children = _leaves(
        "tool",
        [k for k in tool_raw if k.startswith(f"{RUN_COMMAND}:runtime")],
        lambda key: _COMMAND_FAMILY_LABELS[key.split(":", 1)[1]],
    )
    external_children = _leaves(
        "tool",
        [k for k in tool_raw if k.startswith(f"{RUN_COMMAND}:external")],
        lambda key: _COMMAND_FAMILY_LABELS[key.split(":", 1)[1]],
    )
    command_other_leaves = _leaves(
        "tool",
        [k for k in tool_raw if k.startswith(f"{RUN_COMMAND}:other:")],
        lambda key: key.split(":", 2)[2] or "command",
    )
    coordination_children = _leaves("tool", [k for k in tool_raw if k in _COORDINATION_CONCEPTS], _TOOL_CONCEPT_LABELS.get)
    classified = _CODE_CHANGE_CONCEPTS | _COORDINATION_CONCEPTS
    other_children = _leaves(
        "tool",
        [k for k in tool_raw if k not in classified and not k.startswith(f"{RUN_COMMAND}:")],
        _tool_label,
    )

    output_children = command_context_children + _category_children(
        [
            _parent("verification", "Verification", verification_children),
            _parent("repository_operations", "Repository operations", repository_children),
            _parent("dependency_environment", "Dependency / environment", dependency_children),
            _parent("execution_runtime", "Execution / app runtime", runtime_children),
            _parent("external_interaction", "External interaction", external_children),
            _parent("command_other", "Other command output", command_other_leaves),
            _parent("tool_other", "Other / unclassified", other_children),
        ],
        denominator=denominator,
    )
    agent_children = _category_children(
        [
            _parent("agent_messages", "Agent messages", response_children),
            _parent("code_changes", "Code changes", code_children),
            _parent("coordination", "Coordination", coordination_children),
        ],
        denominator=denominator,
    )
    work_children = _category_children(
        [
            _parent("files", "Files", context_children),
            _parent("output", "Output", output_children),
            _parent("agent", "Agent", agent_children),
        ],
        denominator=denominator,
    )

    return _category_children(
        [
            _parent("starting_context", "Starting context", setup_children),
            _parent("user_input", "User input", prompt_children),
            _parent("agent_work", "Agent work", work_children),
        ],
        denominator=denominator,
    )


def _conversation_raw_tokens(
    session_graph: SessionGraph,
) -> tuple[Counter[str], Counter[str], Counter[str]]:
    prompt_raw = Counter[str]()
    agent_raw = Counter[str]()
    tool_raw = Counter[str]()
    user_count = 0
    for session in session_graph.sessions:
        tool_names_by_id: dict[str, str] = {}
        tool_inputs_by_id: dict[str, Any] = {}
        for event in session.events:
            if event.type == EventType.USER_PROMPT_SUBMITTED:
                user_tokens, file_tokens = _user_prompt_tokens(
                    _as_str(event.payload.get("text")) or ""
                )
                prompt_raw["user_initial_request" if user_count == 0 else "user_follow_up_requests"] += user_tokens
                prompt_raw["prompt_file_link"] += file_tokens
                user_count += 1
            elif event.type == EventType.LLM_RESPONSE:
                agent_raw[_assistant_response_key(event.payload)] += _estimate_text_tokens(
                    _as_str(event.payload.get("text")) or ""
                )
            elif event.type == EventType.TOOL_CALL_REQUESTED:
                call_id = _as_str(event.payload.get("tool_call_id"))
                tool_name = _as_str(event.payload.get("tool_name")) or "tool"
                if call_id:
                    tool_names_by_id[call_id] = tool_name
                    tool_inputs_by_id[call_id] = event.payload.get("input")
            elif event.type in {EventType.TOOL_CALL_SUCCEEDED, EventType.TOOL_CALL_FAILED}:
                call_id = _as_str(event.payload.get("tool_call_id"))
                tool_name = tool_names_by_id.get(call_id or "", "tool")
                output_tokens = _estimate_text_tokens(_stringify_tool_output(event.payload.get("output")))
                tool_input = tool_inputs_by_id.get(call_id or "")
                summary = _context_tool_summary(tool_name, tool_input)
                concept = (_as_str(summary.get("name")) if summary else None) or tool_name
                if concept in _CONTEXT_CONCEPTS:
                    prompt_raw[concept] += output_tokens
                elif concept == RUN_COMMAND:
                    family, head = _command_bucket(tool_input)
                    key = f"{RUN_COMMAND}:other:{head}" if family == "other" else f"{RUN_COMMAND}:{family}"
                    tool_raw[key] += output_tokens
                else:
                    tool_raw[concept] += output_tokens
    return prompt_raw, agent_raw, tool_raw


_CLI_REPORT_HEADS: frozenset[str] = frozenset({"ct"})
_CODE_FIX_TOKENS: frozenset[str] = frozenset({"fmt", "format", "fix", "fixer"})
_DIAGNOSTIC_HEADS: frozenset[str] = frozenset(
    {"pwd", "date", "which", "where", "whoami", "uname", "env", "printenv"}
)
_EXTERNAL_HEADS: frozenset[str] = frozenset(
    {
        "curl", "wget", "http", "https", "wrangler", "aws", "gcloud", "az", "fly",
        "flyctl", "vercel", "netlify", "ssh", "scp", "rsync",
    }
)
_RUNTIME_TOKENS: frozenset[str] = frozenset(
    {"dev", "serve", "server", "start", "up", "runserver", "preview"}
)


def _command_head(tokens: list[str]) -> str:
    index = 0
    while index < len(tokens) and "=" in tokens[index] and not tokens[index].startswith("-"):
        index += 1
    if index < len(tokens) and tokens[index] in COMMAND_RUNNERS:
        index += 1
        while index < len(tokens) and tokens[index] in RUNNER_SUBWORDS:
            index += 1
    if index + 2 < len(tokens) and tokens[index] in {"python", "python3"} and tokens[index + 1] == "-m":
        return tokens[index + 2]
    return tokens[index] if index < len(tokens) else "command"


def _command_bucket(tool_input: Any) -> tuple[str, str]:
    cmd = shell_cmd(tool_input)
    if not cmd:
        return "other", "command"
    tokens = [os.path.basename(token.lower()) for token in safe_split(primary_stage(cmd))]
    if not tokens:
        return "other", "command"
    head = _command_head(tokens)
    token_set = set(tokens)
    if head in _CLI_REPORT_HEADS:
        return "cli_report", head
    if head in {"git", "gh", "hg", "svn"} or tokens[0] in {"git", "gh", "hg", "svn"}:
        return "repo", head
    if token_set & TEST_TOKENS:
        return "tests", head
    if token_set & _CODE_FIX_TOKENS:
        return "code_fix", head
    if token_set & BUILD_TOKENS:
        return "build", head
    if token_set & PACKAGE_MANAGERS and token_set & DEPENDENCY_TOKENS:
        return "dependency", head
    if head in _DIAGNOSTIC_HEADS or "--version" in token_set or "-v" in token_set:
        return "diagnostic", head
    if head in _EXTERNAL_HEADS:
        return "external", head
    if token_set & _RUNTIME_TOKENS:
        return "runtime", head
    return "other", head


def _prompt_label(source: str) -> str:
    if source == "user_initial_request":
        return "Initial request"
    if source == "user_follow_up_requests":
        return "Follow-up requests"
    if source == "prompt_file_link":
        return "Referenced prompt files"
    return _tool_label(source)


def _is_user_input_source(source: str) -> bool:
    return source.startswith("user_") or source == "prompt_file_link"


def _assistant_response_key(payload: dict[str, Any]) -> str:
    phase = _as_str(payload.get("phase"))
    if phase == "final_answer":
        return "final_answer"
    if phase:
        return "progress_update"
    return "assistant_message"


_MARKDOWN_FILE_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def _user_prompt_tokens(text: str) -> tuple[int, int]:
    file_spans: list[tuple[int, int]] = []
    file_tokens = 0
    for match in _MARKDOWN_FILE_LINK_RE.finditer(text):
        target = match.group(1).strip()
        if not _looks_like_file_reference(target):
            continue
        file_spans.append(match.span())
        file_tokens += _estimate_text_tokens(match.group(0))

    if not file_spans:
        return _estimate_text_tokens(text), 0

    prompt_parts: list[str] = []
    cursor = 0
    for start, end in file_spans:
        prompt_parts.append(text[cursor:start])
        cursor = end
    prompt_parts.append(text[cursor:])
    return _estimate_text_tokens("".join(prompt_parts).strip()), file_tokens


def _looks_like_file_reference(target: str) -> bool:
    if not target or "://" in target or target.startswith("#"):
        return False
    path = target.split("#", 1)[0].split("?", 1)[0]
    if "/" in path:
        return True
    return "." in path.rsplit("/", 1)[-1]


def _context_tool_summary(tool_name: str, tool_input: Any) -> dict[str, Any] | None:
    from datetime import datetime as _dt
    from uuid import uuid4 as _uuid4
    return summarize_tool_call(ToolCallItem(
        session_id=_uuid4(), turn_id=_uuid4(), sequence=0, started_at=_dt.min,
        tool_name=tool_name, input=tool_input,
    ))


def _category_children(
    specs: list[
        tuple[str, str, int]
        | tuple[str, str, int, list[ContextCategoryFlat]]
    ],
    *,
    denominator: int,
    keep_zero_keys: set[str] | None = None,
) -> list[ContextCategoryFlat]:
    keep_zero_keys = keep_zero_keys or set()
    categories: list[ContextCategoryFlat] = []
    for spec in specs:
        key, label, tokens = spec[:3]
        children = spec[3] if len(spec) >= 4 else []
        if tokens <= 0 and key not in keep_zero_keys and not children:
            continue
        categories.append(
            ContextCategoryFlat(
                key=key,
                label=label,
                tokens=max(int(tokens), 0),
                percent=percent(max(int(tokens), 0), denominator),
                confidence="estimated_tokens",
                children=children,
            )
        )
    return categories


def _scaled_context_tokens(raw_tokens: dict[str, int], *, target_total: int) -> dict[str, int]:
    raw_total = sum(max(value, 0) for value in raw_tokens.values())
    if raw_total <= 0 or target_total <= 0:
        return {key: 0 for key in raw_tokens}

    scaled = {
        key: int((max(value, 0) * target_total) // raw_total)
        for key, value in raw_tokens.items()
    }
    remainder = target_total - sum(scaled.values())
    fractional_order = sorted(
        raw_tokens,
        key=lambda key: ((max(raw_tokens[key], 0) * target_total) / raw_total) % 1,
        reverse=True,
    )
    for key in fractional_order[:remainder]:
        scaled[key] += 1
    return scaled


def _tool_label(tool_name: str) -> str:
    return tool_name.replace("_", " ")


def _slug(value: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in value.lower()).strip("_") or "tool"


def _stringify_tool_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _estimate_text_tokens(text: str) -> int:
    return max(round(len(text) / 4), 1) if text else 0


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
