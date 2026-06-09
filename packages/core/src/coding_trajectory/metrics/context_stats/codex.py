"""Codex CLI context stats — uses preserved prompt_block + token_count events."""

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
from coding_trajectory.ingestion.models import Event, EventType, SessionGraph, StepToolItem, Vendor
from coding_trajectory.metrics.context_stats._common import (
    message_stats,
    model_context_window,
    percent,
    runtime_stats,
    token_usage_from_mapping,
)
from coding_trajectory.metrics.models import (
    ContextCategoryFlat,
    ContextModelStatsFlat,
    ContextWindowStatsFlat,
    QuotaStatsFlat,
    SessionContextStatsFlat,
)


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
    "git": "Git / repo commands",
    "package": "Package / dependency commands",
    "other": "Other command output",
}

_CONTEXT_CONCEPTS: frozenset[str] = frozenset(
    {READ_FILE, SEARCH_TEXT, LIST_FILES, WEB_FETCH, WEB_SEARCH}
)
_CODE_CHANGE_CONCEPTS: frozenset[str] = frozenset({EDIT_FILE, WRITE_FILE})
_COORDINATION_CONCEPTS: frozenset[str] = frozenset(
    {TODO_LIST, SUBAGENT_TASK, SESSION_HANDOFF}
)


def build_codex_context_stats(session_graph: SessionGraph) -> dict[str, Any]:
    latest_usage_event = _latest_codex_token_count_event(session_graph)
    latest_metrics = (
        latest_usage_event.payload.get("metrics") if latest_usage_event is not None else None
    )
    latest_metrics = latest_metrics if isinstance(latest_metrics, dict) else {}
    latest_usage = token_usage_from_mapping(
        latest_metrics.get("last_token_usage")
        if isinstance(latest_metrics.get("last_token_usage"), dict)
        else {}
    )
    context_window = _as_int(latest_metrics.get("model_context_window"))
    if context_window == 0:
        context_window = _latest_task_started_context_window(session_graph) or 0

    model = _codex_model(session_graph) or _fallback_model_from_step(session_graph)
    if context_window == 0:
        context_window = model_context_window(model, provider="openai") or 0

    categories = _codex_context_categories(session_graph, latest_usage.input_tokens, context_window)
    quota = _quota_stats_from_latest_event(latest_usage_event)
    runtime = runtime_stats(session_graph)
    messages = message_stats(session_graph)

    warnings = [
        "Category token counts are estimated from Codex JSONL text and classified by inferred intent; tool-result tokens come from outputs (so edits/patches can look small even for large changes), and buckets are scaled to the latest context window residual.",
    ]
    if not categories:
        warnings.append("No Codex prompt blocks were found for context category breakdown.")

    return SessionContextStatsFlat(
        root_session_id=session_graph.root_session_id,
        vendor=Vendor.CODEX_CLI.value,
        model=ContextModelStatsFlat(
            name=model,
            context_window_tokens=context_window or None,
        ),
        context_window=ContextWindowStatsFlat(
            used_tokens=latest_usage.input_tokens,
            used_percent=percent(latest_usage.input_tokens, context_window),
            source="latest_token_count",
            categories=categories,
        ),
        runtime=runtime,
        messages=messages,
        usage=latest_usage,
        quota=quota,
        warnings=warnings,
    ).model_dump(mode="json")


def _latest_codex_token_count_event(session_graph: SessionGraph) -> Event | None:
    events = [
        event
        for session in session_graph.sessions
        for event in session.events
        if event.vendor_source == Vendor.CODEX_CLI
        and event.type == EventType.VENDOR_RAW
        and event.payload.get("raw_type") == "token_count"
    ]
    return max(events, key=lambda item: item.timestamp) if events else None


def _latest_task_started_context_window(session_graph: SessionGraph) -> int | None:
    values = [
        _as_int(event.payload.get("model_context_window"))
        for session in session_graph.sessions
        for event in session.events
        if event.vendor_source == Vendor.CODEX_CLI
        and event.type == EventType.VENDOR_RAW
        and event.payload.get("raw_type") == "task_started"
    ]
    values = [value for value in values if value > 0]
    return values[-1] if values else None


def _codex_model(session_graph: SessionGraph) -> str | None:
    for session in session_graph.sessions:
        for event in sorted(session.events, key=lambda item: item.timestamp):
            if event.vendor_source != Vendor.CODEX_CLI:
                continue
            if event.payload.get("raw_type") != "token_count":
                continue
            metrics = event.payload.get("metrics")
            if not isinstance(metrics, dict):
                continue
            model = _as_str(metrics.get("model"))
            if model:
                return model
    return None


def _fallback_model_from_step(session_graph: SessionGraph) -> str | None:
    for session in session_graph.sessions:
        for turn in session.turns:
            for step in turn.steps:
                data = (step.vendor_data or {}).get("metrics")
                if isinstance(data, dict):
                    model = _as_str(data.get("model"))
                    if model:
                        return model
    return None


def _codex_context_categories(
    session_graph: SessionGraph,
    used_tokens: int,
    context_window: int,
) -> list[ContextCategoryFlat]:
    setup_raw = Counter[str]()
    for session in session_graph.sessions:
        for event in session.events:
            if event.vendor_source != Vendor.CODEX_CLI:
                continue
            if event.type != EventType.VENDOR_RAW or event.payload.get("raw_type") != "prompt_block":
                continue
            text = event.payload.get("text")
            if not isinstance(text, str) or not text:
                continue
            setup_raw[_codex_setup_key(event.payload)] += _estimate_text_tokens(text)

    denominator = context_window or used_tokens
    setup_tokens = min(sum(setup_raw.values()), used_tokens)
    residual_tokens = max(used_tokens - setup_tokens, 0)

    prompt_raw, agent_raw, tool_raw = _codex_conversation_raw_tokens(session_graph)
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
    command_children = _leaves(
        "tool",
        [k for k in tool_raw if k.startswith(f"{RUN_COMMAND}:")],
        lambda key: _COMMAND_FAMILY_LABELS.get(key.split(":", 1)[1], "Other command output"),
    )
    coordination_children = _leaves("tool", [k for k in tool_raw if k in _COORDINATION_CONCEPTS], _TOOL_CONCEPT_LABELS.get)
    classified = _CODE_CHANGE_CONCEPTS | _COORDINATION_CONCEPTS
    other_children = _leaves(
        "tool",
        [k for k in tool_raw if k not in classified and not k.startswith(f"{RUN_COMMAND}:")],
        _tool_label,
    )

    tool_results_children = _category_children(
        [
            _parent("context_gathered", "Context gathered", context_children),
            _parent("code_changes", "Code changes", code_children),
            _parent("command_output", "Command output", command_children),
            _parent("coordination", "Coordination", coordination_children),
            _parent("tool_other", "Other / unclassified", other_children),
        ],
        denominator=denominator,
    )
    agent_children = _category_children(
        [
            _parent("agent_messages", "Agent messages", response_children),
            _parent("tool_results", "Tool results", tool_results_children),
        ],
        denominator=denominator,
    )

    return _category_children(
        [
            _parent("starting_context", "Starting context", setup_children),
            _parent("user_input", "User input", prompt_children),
            _parent("agent_work", "Agent work", agent_children),
        ],
        denominator=denominator,
    )


def _codex_setup_key(payload: dict[str, Any]) -> str:
    block = _as_str(payload.get("prompt_block")) or ""
    role = _as_str(payload.get("prompt_role")) or ""
    text = payload.get("text")
    haystack = f"{block}\n{text if isinstance(text, str) else ''}".lower()
    if block == "base_instructions":
        return "base_system"
    if "agents.md" in haystack:
        return "agents_md"
    if "skills_instructions" in block or "### available skills" in haystack:
        return "skills"
    if "plugins_instructions" in block or "### available plugins" in haystack:
        return "mcp"
    if "memory_summary" in haystack or "memory layout" in haystack or "## memory" in haystack:
        return "memory"
    if "mcp" in haystack or "tools are grouped" in haystack:
        return "mcp"
    if role == "developer":
        return "developer_instructions"
    return "base_system"


def _codex_conversation_raw_tokens(
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
            if event.vendor_source != Vendor.CODEX_CLI:
                continue
            if event.type == EventType.USER_PROMPT_SUBMITTED:
                user_tokens, file_tokens = _codex_user_prompt_tokens(
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
                    tool_raw[f"{RUN_COMMAND}:{_command_family(tool_input)}"] += output_tokens
                else:
                    tool_raw[concept] += output_tokens
    return prompt_raw, agent_raw, tool_raw


_TEST_TOKENS: frozenset[str] = frozenset(
    {"pytest", "jest", "vitest", "mocha", "rspec", "phpunit", "unittest", "tox", "ctest", "test"}
)
_BUILD_TOKENS: frozenset[str] = frozenset({
    "tsc", "mypy", "ruff", "eslint", "flake8", "pylint", "black", "isort", "prettier",
    "make", "cmake", "webpack", "rollup", "vite", "esbuild", "clippy",
    "build", "compile", "lint", "typecheck", "check", "vet",
})
_PACKAGE_MANAGERS: frozenset[str] = frozenset({
    "npm", "pnpm", "yarn", "bun", "pip", "pip3", "uv", "poetry", "pipenv",
    "cargo", "gem", "bundle", "brew", "conda", "apt", "apt-get",
})
_INSTALL_TOKENS: frozenset[str] = frozenset({"install", "add", "ci", "sync", "get"})


def _command_family(tool_input: Any) -> str:
    cmd = shell_cmd(tool_input)
    if not cmd:
        return "other"
    tokens = [os.path.basename(token.lower()) for token in safe_split(primary_stage(cmd))]
    if not tokens:
        return "other"
    if tokens[0] in {"git", "gh", "hg", "svn"}:
        return "git"
    token_set = set(tokens)
    if token_set & _TEST_TOKENS:
        return "tests"
    if token_set & _BUILD_TOKENS:
        return "build"
    if token_set & _PACKAGE_MANAGERS and token_set & _INSTALL_TOKENS:
        return "package"
    return "other"


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


def _codex_user_prompt_tokens(text: str) -> tuple[int, int]:
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
    return summarize_tool_call(StepToolItem(tool_name=tool_name, input=tool_input))


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


def _latest_user_request_text(session_graph: SessionGraph) -> str | None:
    user_events = [
        event
        for session in session_graph.sessions
        for event in session.events
        if event.vendor_source == Vendor.CODEX_CLI and event.type == EventType.USER_PROMPT_SUBMITTED
    ]
    if not user_events:
        return None
    return _as_str(max(user_events, key=lambda item: item.timestamp).payload.get("text"))


def _label_snippet(text: str | None, *, limit: int = 80) -> str:
    if not text:
        return "-"
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1].rstrip() + "..."


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


def _quota_stats_from_latest_event(event: Event | None) -> QuotaStatsFlat | None:
    if event is None:
        return None
    quota = event.payload.get("quota")
    if not isinstance(quota, dict):
        return None
    primary = quota.get("primary") if isinstance(quota.get("primary"), dict) else {}
    secondary = quota.get("secondary") if isinstance(quota.get("secondary"), dict) else {}
    return QuotaStatsFlat(
        plan_type=_as_str(quota.get("plan_type")),
        primary_used_percent=_as_float(primary.get("used_percent")),
        secondary_used_percent=_as_float(secondary.get("used_percent")),
        resets_at=_as_int(primary.get("resets_at")) or None,
    )


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _as_float(value: Any) -> float | None:
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return float(value)
    return None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
