"""Codex CLI adapter — reads ~/.codex/sessions/**/*.jsonl rollout files."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from itertools import chain
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel

from coding_trajectory.ingestion.adapters._shared import (
    SHARED_FILE_TOOL_NAMES,
    SHARED_PLAN_TOOL_NAMES,
    ToolTaxonomy,
    content_block_texts,
    extract_uuid_text,
    non_empty_str,
    preview_text,
)
from coding_trajectory.ingestion.adapters.base import BaseAdapter, SessionHeader
from coding_trajectory.ingestion.assembly import AssemblyHooks, assemble_session
from coding_trajectory.ingestion.common import (
    extract_exit_code,
    infer_tool_success,
    parse_iso_timestamp,
    parse_timestamp,
    source_is_living,
)
from coding_trajectory.ingestion.models import (
    ContextSourceObservation,
    ContextUsageObservation,
    RuntimeObservation,
    Session,
    SessionStatus,
    ToolStatus,
    TurnStatus,
    Vendor,
)
from coding_trajectory.ingestion.provenance import RecordSpan, SessionProvenance
from coding_trajectory.ingestion.retention import CanonicalRetention
from coding_trajectory.ingestion.transcript import TranscriptRecord
from coding_trajectory.ingestion.vendor_mechanisms.codex_multi_agent import (
    CodexMultiAgentInput,
    CodexThreadSpawn,
)
from coding_trajectory.ingestion.vendor_mechanisms.codex_multi_agent import (
    extensions as codex_extensions,
)
from coding_trajectory.ingestion.vendor_mechanisms.codex_multi_agent import (
    parent_session_id as codex_parent_session_id,
)
from coding_trajectory.ingestion.vendor_mechanisms.usage_metrics import (
    context_usage_observation,
    normalize_codex_token_count,
)

logger = logging.getLogger(__name__)

_DEFAULT_CODEX_SESSION_INDEX = Path.home() / ".codex" / "session_index.jsonl"
_CODEX_PREVIEW_MAX_LEN = 96

_CODEX_TOOL_TAXONOMY = ToolTaxonomy(
    plan_names=SHARED_PLAN_TOOL_NAMES,
    file_change_names=SHARED_FILE_TOOL_NAMES
    | frozenset(
        {
            "read_file",
            "read_many_files",
            "replace",
            "write_file",
            "edit_file",
            "create_file",
            "apply_patch",
        }
    ),
)

_CODEX_EXEC_STATIC_EXTRACTOR = "codex_exec_static_v2"
_NATIVE_EXEC_MATCH_GRACE_SECONDS = 2
_CODEX_GROUPABLE_COMMAND_SOURCES: frozenset[str] = frozenset(
    {"agent", "unified_exec_startup"}
)
_CODEX_STATIC_EXEC_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "apply_patch",
        "exec_command",
        "followup_task",
        "interrupt_agent",
        "send_message",
        "spawn_agent",
        "update_plan",
        "wait_agent",
        "web__run",
    }
)
_CODEX_STATIC_COLLAB_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "followup_task",
        "interrupt_agent",
        "send_message",
        "spawn_agent",
        "wait_agent",
    }
)
_CODEX_STATIC_COLLAB_NATIVE_TOOLS: dict[str, frozenset[str]] = {
    "followup_task": frozenset({"resume_agent", "send_input"}),
    "interrupt_agent": frozenset({"close_agent"}),
    "send_message": frozenset({"send_input"}),
    "spawn_agent": frozenset({"spawn_agent"}),
    "wait_agent": frozenset({"wait"}),
}
_CODEX_WEB_BROWSE_OPERATIONS: frozenset[str] = frozenset(
    {"click", "find", "open", "screenshot"}
)
_JS_CONTROL_WORDS: frozenset[str] = frozenset(
    {
        "catch",
        "do",
        "finally",
        "for",
        "function",
        "if",
        "switch",
        "try",
        "while",
    }
)


class _StaticExecInvocation(BaseModel):
    """One non-evaluated nested tool call inside a Codex ``exec`` wrapper."""

    method: str
    tool_name: str
    item_kind: str
    input: Any = None
    command: str | None = None
    cwd: str | None = None
    source_offset: int


@dataclass
class _PendingExecWrapper:
    """A static ``exec`` code cell awaiting its wrapper result."""

    call_id: str
    started_at: datetime
    call_record: TranscriptRecord
    invocations: list[_StaticExecInvocation]
    turn_id: str | None = None
    matched_native_indices: set[int] = field(default_factory=set)
    derived_records: dict[int, TranscriptRecord] = field(default_factory=dict)
    closed: bool = False
    completed_at: datetime | None = None


def _is_js_identifier_start(value: str) -> bool:
    return value == "_" or value == "$" or value.isalpha()


def _is_js_identifier_part(value: str) -> bool:
    return _is_js_identifier_start(value) or value.isdigit()


def _skip_js_trivia(source: str, offset: int) -> int | None:
    """Advance over whitespace and comments without interpreting JavaScript."""

    while offset < len(source):
        if source[offset].isspace():
            offset += 1
            continue
        if source.startswith("//", offset):
            newline = source.find("\n", offset + 2)
            return len(source) if newline < 0 else _skip_js_trivia(source, newline + 1)
        if source.startswith("/*", offset):
            close = source.find("*/", offset + 2)
            if close < 0:
                return None
            offset = close + 2
            continue
        break
    return offset


def _read_js_identifier(source: str, offset: int) -> tuple[str, int] | None:
    if offset >= len(source) or not _is_js_identifier_start(source[offset]):
        return None
    end = offset + 1
    while end < len(source) and _is_js_identifier_part(source[end]):
        end += 1
    return source[offset:end], end


def _read_js_string(source: str, offset: int) -> tuple[str, int] | None:
    """Read a strict single- or double-quoted JavaScript literal.

    Template literals are deliberately unsupported: interpolations would turn a
    historical reconstruction into code evaluation.
    """

    if offset >= len(source) or source[offset] not in {"'", '"'}:
        return None
    quote = source[offset]
    value: list[str] = []
    cursor = offset + 1
    simple_escapes = {
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
        "0": "\0",
    }
    while cursor < len(source):
        character = source[cursor]
        if character == quote:
            return "".join(value), cursor + 1
        if character != "\\":
            value.append(character)
            cursor += 1
            continue
        cursor += 1
        if cursor >= len(source):
            return None
        escaped = source[cursor]
        if escaped == "u":
            digits = source[cursor + 1 : cursor + 5]
            if len(digits) != 4 or any(
                digit not in "0123456789abcdefABCDEF" for digit in digits
            ):
                return None
            value.append(chr(int(digits, 16)))
            cursor += 5
            continue
        if escaped == "x":
            digits = source[cursor + 1 : cursor + 3]
            if len(digits) != 2 or any(
                digit not in "0123456789abcdefABCDEF" for digit in digits
            ):
                return None
            value.append(chr(int(digits, 16)))
            cursor += 3
            continue
        value.append(simple_escapes.get(escaped, escaped))
        cursor += 1
    return None


def _read_js_literal(source: str, offset: int) -> tuple[Any, int] | None:
    offset = _skip_js_trivia(source, offset)
    if offset is None or offset >= len(source):
        return None
    string = _read_js_string(source, offset)
    if string is not None:
        return string
    for token, value in (("true", True), ("false", False), ("null", None)):
        if source.startswith(token, offset):
            end = offset + len(token)
            if end == len(source) or not _is_js_identifier_part(source[end]):
                return value, end
    match = re.match(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", source[offset:])
    if match is None:
        return None
    end = offset + len(match.group(0))
    literal = match.group(0)
    return (float(literal) if "." in literal else int(literal)), end


def _read_js_static_value(source: str, offset: int) -> tuple[Any, int] | None:
    """Read a JSON-shaped JavaScript literal without evaluating source code."""

    offset = _skip_js_trivia(source, offset)
    if offset is None or offset >= len(source):
        return None
    literal = _read_js_literal(source, offset)
    if literal is not None:
        return literal
    if source[offset] == "{":
        return _read_js_static_object(source, offset)
    if source[offset] == "[":
        return _read_js_static_array(source, offset)
    return None


def _read_js_static_object(
    source: str, offset: int
) -> tuple[dict[str, Any], int] | None:
    """Read a strict object literal whose keys and values are static literals."""

    offset = _skip_js_trivia(source, offset)
    if offset is None or offset >= len(source) or source[offset] != "{":
        return None
    offset += 1
    values: dict[str, Any] = {}
    while True:
        offset = _skip_js_trivia(source, offset)
        if offset is None or offset >= len(source):
            return None
        if source[offset] == "}":
            return values, offset + 1
        key_string = _read_js_string(source, offset)
        if key_string is not None:
            key, offset = key_string
        else:
            key_identifier = _read_js_identifier(source, offset)
            if key_identifier is None:
                return None
            key, offset = key_identifier
        if key in values:
            return None
        offset = _skip_js_trivia(source, offset)
        if offset is None or offset >= len(source) or source[offset] != ":":
            return None
        parsed = _read_js_static_value(source, offset + 1)
        if parsed is None:
            return None
        nested_value, offset = parsed
        values[key] = nested_value
        offset = _skip_js_trivia(source, offset)
        if offset is None or offset >= len(source):
            return None
        if source[offset] == "}":
            return values, offset + 1
        if source[offset] != ",":
            return None
        offset += 1


def _read_js_static_array(source: str, offset: int) -> tuple[list[Any], int] | None:
    """Read a strict array literal whose elements are static literals."""

    offset = _skip_js_trivia(source, offset)
    if offset is None or offset >= len(source) or source[offset] != "[":
        return None
    offset += 1
    values: list[Any] = []
    while True:
        offset = _skip_js_trivia(source, offset)
        if offset is None or offset >= len(source):
            return None
        if source[offset] == "]":
            return values, offset + 1
        parsed = _read_js_static_value(source, offset)
        if parsed is None:
            return None
        nested_value, offset = parsed
        values.append(nested_value)
        offset = _skip_js_trivia(source, offset)
        if offset is None or offset >= len(source):
            return None
        if source[offset] == "]":
            return values, offset + 1
        if source[offset] != ",":
            return None
        offset += 1


def _read_static_exec_call_argument(
    source: str,
    offset: int,
    *,
    allow_identifier: bool,
) -> tuple[Any, int] | None:
    """Read one static tool argument and its closing call parenthesis.

    ``apply_patch`` commonly receives a literal patch stored in a local
    variable. The variable name is deliberately retained only as an opaque
    reference: CT can prove the tool call, not evaluate the variable.
    """

    offset = _skip_js_trivia(source, offset)
    if offset is None or offset >= len(source):
        return None
    parsed = _read_js_static_value(source, offset)
    if parsed is not None:
        argument, offset = parsed
    elif allow_identifier:
        identifier = _read_js_identifier(source, offset)
        if identifier is None:
            return None
        name, offset = identifier
        argument = {"_static_reference": name}
    else:
        return None
    offset = _skip_js_trivia(source, offset)
    if offset is None or offset >= len(source) or source[offset] != ")":
        return None
    return argument, offset + 1


def _static_web_tool_name(argument: dict[str, Any]) -> str | None:
    if "search_query" in argument or "image_query" in argument:
        return "web_search"
    if any(operation in argument for operation in _CODEX_WEB_BROWSE_OPERATIONS):
        return "web_fetch"
    # Weather, finance, and future operation shapes are not interchangeable
    # with a TUI WebSearchCell. Keep their raw wrapper visible until CT gains
    # an evidence-preserving activity type for them.
    return None


def _build_static_exec_invocation(
    method: str,
    argument: Any,
    *,
    source_offset: int,
) -> _StaticExecInvocation | None:
    """Normalize one statically proven nested tool invocation.

    This registry intentionally recognizes only tools with a stable CT item
    shape. Unknown nested tools keep their outer ``exec`` wrapper visible.
    """

    if method == "exec_command":
        if not isinstance(argument, dict):
            return None
        command = argument.get("cmd")
        cwd = argument.get("workdir")
        if not isinstance(command, str) or not command.strip():
            return None
        if cwd is not None and not isinstance(cwd, str):
            return None
        command_input: dict[str, Any] = {"cmd": command}
        if cwd is not None:
            command_input["workdir"] = cwd
        return _StaticExecInvocation(
            method=method,
            tool_name="exec_command",
            item_kind="command_execution",
            input=command_input,
            command=command,
            cwd=cwd,
            source_offset=source_offset,
        )

    if method == "web__run":
        if not isinstance(argument, dict):
            return None
        tool_name = _static_web_tool_name(argument)
        if tool_name is None:
            return None
        return _StaticExecInvocation(
            method=method,
            tool_name=tool_name,
            item_kind="tool_call",
            input=argument,
            source_offset=source_offset,
        )

    if method == "apply_patch":
        patch_input = argument if isinstance(argument, dict) else {"patch": argument}
        return _StaticExecInvocation(
            method=method,
            tool_name="apply_patch",
            item_kind="file_change",
            input=patch_input,
            source_offset=source_offset,
        )

    if method == "update_plan":
        if not isinstance(argument, dict):
            return None
        return _StaticExecInvocation(
            method=method,
            tool_name="update_plan",
            item_kind="plan",
            input=argument,
            source_offset=source_offset,
        )

    if method in _CODEX_STATIC_COLLAB_TOOL_NAMES:
        if not isinstance(argument, dict):
            return None
        input_data = {"action": method, **argument}
        return _StaticExecInvocation(
            method=method,
            tool_name="spawn_agent" if method == "spawn_agent" else "collab_agent",
            item_kind="tool_call",
            input=input_data,
            source_offset=source_offset,
        )

    return None


def _parse_static_exec_tool_call(
    source: str, offset: int
) -> tuple[_StaticExecInvocation, int] | None:
    """Read one known ``tools.<method>(literal)`` call at ``offset``."""

    tools = _read_js_identifier(source, offset)
    if tools is None or tools[0] != "tools":
        return None
    _, cursor = tools
    cursor = _skip_js_trivia(source, cursor)
    if cursor is None or cursor >= len(source) or source[cursor] != ".":
        return None
    method = _read_js_identifier(source, cursor + 1)
    if method is None:
        return None
    method_name, cursor = method
    if method_name not in _CODEX_STATIC_EXEC_TOOL_NAMES:
        return None
    cursor = _skip_js_trivia(source, cursor)
    if cursor is None or cursor >= len(source) or source[cursor] != "(":
        return None
    parsed_argument = _read_static_exec_call_argument(
        source,
        cursor + 1,
        allow_identifier=method_name == "apply_patch",
    )
    if parsed_argument is None:
        return None
    argument, cursor = parsed_argument
    invocation = _build_static_exec_invocation(
        method_name,
        argument,
        source_offset=offset,
    )
    if invocation is None:
        return None
    return invocation, cursor


def _parse_static_exec_promise_all(
    source: str, offset: int
) -> tuple[list[_StaticExecInvocation], int] | None:
    """Read a literal ``await Promise.all([tools.<method>(...), ...])``."""

    promise = _read_js_identifier(source, offset)
    if promise is None or promise[0] != "Promise":
        return None
    _, cursor = promise
    cursor = _skip_js_trivia(source, cursor)
    if cursor is None or cursor >= len(source) or source[cursor] != ".":
        return None
    method = _read_js_identifier(source, cursor + 1)
    if method is None or method[0] != "all":
        return None
    _, cursor = method
    cursor = _skip_js_trivia(source, cursor)
    if cursor is None or cursor >= len(source) or source[cursor] != "(":
        return None
    cursor = _skip_js_trivia(source, cursor + 1)
    if cursor is None or cursor >= len(source) or source[cursor] != "[":
        return None
    cursor += 1
    invocations: list[_StaticExecInvocation] = []
    while True:
        cursor = _skip_js_trivia(source, cursor)
        if cursor is None or cursor >= len(source):
            return None
        if source[cursor] == "]":
            cursor += 1
            break
        parsed = _parse_static_exec_tool_call(source, cursor)
        if parsed is None:
            return None
        invocation, cursor = parsed
        invocations.append(invocation)
        cursor = _skip_js_trivia(source, cursor)
        if cursor is None or cursor >= len(source):
            return None
        if source[cursor] == "]":
            cursor += 1
            break
        if source[cursor] != ",":
            return None
        cursor += 1
    cursor = _skip_js_trivia(source, cursor)
    if cursor is None or cursor >= len(source) or source[cursor] != ")":
        return None
    if not invocations:
        return None
    return invocations, cursor + 1


def _previous_non_trivia_character(source: str, offset: int) -> str | None:
    cursor = offset - 1
    while cursor >= 0 and source[cursor].isspace():
        cursor -= 1
    return source[cursor] if cursor >= 0 else None


def _has_possible_tool_reference(source: str, offset: int) -> bool:
    """Conservatively identify a remaining tool/alias reference in tail code."""

    return re.search(r"\btools\b", source[offset:]) is not None


def _extract_static_exec_invocations(value: Any) -> list[_StaticExecInvocation] | None:
    """Extract statically proven nested calls from a historical ``exec`` cell.

    This is intentionally a recognizer, not a JavaScript evaluator. It accepts
    direct awaited calls and a literal ``await Promise.all([...])`` list. Inputs
    must be static literals except an opaque local variable passed to
    ``apply_patch``. Control flow, callbacks, aliases, unknown tools, and any
    unrecognized call form fail closed. An opaque display tail after the last
    proven invocation is ignored only when it contains no further ``tools.``
    reference. This preserves common ``text(result.output)`` and
    ``for (...) text(...)`` wrappers without treating their output formatting
    as another activity.
    """

    if not isinstance(value, str) or not value.strip():
        return None

    invocations: list[_StaticExecInvocation] = []
    tokens: list[tuple[str, int, int]] = []
    offset = 0
    while offset < len(value):
        skipped = _skip_js_trivia(value, offset)
        if skipped is None:
            return None
        offset = skipped
        if offset >= len(value):
            break
        if value[offset] in {"'", '"'}:
            string = _read_js_string(value, offset)
            if string is None:
                return None
            _, offset = string
            continue
        if value[offset] == "`":
            if invocations and not _has_possible_tool_reference(value, offset + 1):
                break
            return None
        if (
            value.startswith("=>", offset)
            or value.startswith("&&", offset)
            or value.startswith("||", offset)
        ):
            if invocations and not _has_possible_tool_reference(value, offset + 2):
                break
            return None
        if value[offset] == "?":
            if invocations and not _has_possible_tool_reference(value, offset + 1):
                break
            return None
        identifier = _read_js_identifier(value, offset)
        if identifier is None:
            offset += 1
            continue
        token, end = identifier
        if token in _JS_CONTROL_WORDS:
            if invocations and not _has_possible_tool_reference(value, end):
                break
            return None
        tokens.append((token, offset, end))
        offset = end
        if token == "Promise" and len(tokens) >= 2 and tokens[-2][0] == "await":
            if _previous_non_trivia_character(value, tokens[-2][1]) not in {
                None,
                "=",
                ";",
            }:
                return None
            parsed_promise_all = _parse_static_exec_promise_all(value, tokens[-1][1])
            if parsed_promise_all is None:
                return None
            nested, offset = parsed_promise_all
            invocations.extend(nested)
            continue
        if token != "tools":
            continue
        if len(tokens) < 2 or tokens[-2][0] != "await":
            return None
        if _previous_non_trivia_character(value, tokens[-2][1]) not in {None, "=", ";"}:
            return None
        parsed = _parse_static_exec_tool_call(value, tokens[-1][1])
        if parsed is None:
            return None
        invocation, offset = parsed
        invocations.append(invocation)

    return invocations or None


def _native_command_text(value: Any) -> str | None:
    """Normalize a native CommandExecution payload to its shell command text."""

    if isinstance(value, str) and value.strip():
        return value.strip()
    if not isinstance(value, list):
        return None
    parts = [part for part in value if isinstance(part, str)]
    for index, part in enumerate(parts[:-1]):
        if part == "-lc" and parts[index + 1].strip():
            return parts[index + 1].strip()
    return " ".join(parts).strip() or None


def _command_match_key(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _codex_command_activity_source(value: Any) -> str:
    """Map Codex's native command origin to the shared cell authority.

    Codex TUI groups only agent and unified-exec-startup commands. Historical
    user-shell and unrecognized sources remain individual boundaries.
    """

    source = _as_non_empty_str(value)
    if source is not None and source.lower() in _CODEX_GROUPABLE_COMMAND_SOURCES:
        return "agent"
    return "unknown"


def _codex_item_kind(*, tool_name: str | None, inner_type: str) -> str:
    # Codex native inner types outrank the tool-name taxonomy.
    if inner_type == "local_shell_call":
        return "command_execution"
    if inner_type == "reasoning":
        return "reasoning"
    return _CODEX_TOOL_TAXONOMY.classify(tool_name)


def _parse_json_blob(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _tool_status(
    value: Any, *, default: ToolStatus = ToolStatus.REQUESTED
) -> ToolStatus:
    normalized = (
        re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value).replace("-", "_").lower()
        if isinstance(value, str)
        else None
    )
    if normalized == "completed":
        return ToolStatus.COMPLETED
    if normalized in {"failed", "declined"}:
        return ToolStatus.FAILED
    if normalized == "in_progress":
        return ToolStatus.IN_PROGRESS
    return default


def _tool_result_status(
    payload: dict[str, Any], output: Any, *, exec_wrapper: bool = False
) -> ToolStatus:
    if isinstance(payload.get("success"), bool):
        return ToolStatus.COMPLETED if payload["success"] else ToolStatus.FAILED
    status = _tool_status(payload.get("status"), default=ToolStatus.COMPLETED)
    if status != ToolStatus.COMPLETED:
        return status
    # Custom ``exec`` cells often keep their own transport status as
    # ``completed`` even when the JavaScript body failed.  This establishes
    # only the wrapper's result—it must never be applied to a statically
    # reconstructed nested action.
    if exec_wrapper and _is_exec_wrapper_failure(output):
        return ToolStatus.FAILED
    success = infer_tool_success(output)
    return ToolStatus.FAILED if success is False else ToolStatus.COMPLETED


def _walk_text_values(value: Any) -> Iterator[str]:
    """Yield text leaves from a JSON-like tool result without coercing data."""

    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _walk_text_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_text_values(nested)


def _is_exec_syntax_error(output: Any) -> bool:
    """Return whether a failed exec wrapper could not parse before running.

    A failed wrapper normally cannot establish the outcome of a nested call:
    post-processing such as ``text(r.content)`` can fail after a native action
    succeeded. A JavaScript syntax error is different—the body never executes,
    so the raw ``exec`` failure must remain visible rather than becoming a
    derived unknown action.
    """

    return any(
        "script error" in text.lower() and "syntaxerror" in text.lower()
        for text in _walk_text_values(output)
    )


def _is_exec_wrapper_failure(output: Any) -> bool:
    """Return whether a custom exec wrapper reports its own failure."""

    return any(
        "script failed" in text.lower() or "script error:" in text.lower()
        for text in _walk_text_values(output)
    )


def _has_explicit_exec_wrapper_result(output: Any) -> bool:
    """Return whether an exec wrapper carries result content beyond its banner.

    ``Script completed`` is a runtime status for the JavaScript wrapper, not
    outcome evidence for a nested call.  A single lexically known nested call
    can instead use its wrapper output as a historical fallback only when the
    wrapper also persisted actual result content.
    """

    for text in _walk_text_values(output):
        cleaned = re.sub(
            r"^\s*script completed\s*\n(?:wall time[^\n]*\n)?output:\s*",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
        if cleaned:
            return True
    return False


def _extract_message_text(payload: dict[str, Any]) -> str | None:
    message = payload.get("message")
    return message if isinstance(message, str) and message else None


def _extract_response_text(payload: dict[str, Any]) -> str | None:
    content = payload.get("content")
    if not isinstance(content, list):
        return None
    return content_block_texts(content, text_type="output_text")


_as_non_empty_str = non_empty_str


def _extract_nested_map(payload: dict[str, Any], *keys: str) -> dict[str, Any] | None:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if isinstance(current, dict) else None


_extract_uuid_text = extract_uuid_text


def _codex_session_title(
    session_id: UUID,
    index_path: Path = _DEFAULT_CODEX_SESSION_INDEX,
) -> str | None:
    """Return the explicit Codex thread name from its local name index only."""
    if not index_path.is_file():
        return None

    title: str | None = None
    try:
        with index_path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("id") != str(session_id):
                    continue
                candidate = _as_non_empty_str(
                    record.get("thread_name")
                ) or _as_non_empty_str(record.get("title"))
                if candidate is not None:
                    title = candidate
    except OSError:
        return None
    return title


def _codex_session_preview(transcript: Iterable[TranscriptRecord]) -> str | None:
    """Return a bounded first-user-message preview without inventing a title."""
    for record in transcript:
        if record.kind != "user_message":
            continue
        text = _codex_preview_text(record.data.get("text"))
        if text is None:
            continue
        return text
    return None


def _codex_preview_text(value: Any) -> str | None:
    return preview_text(value, max_len=_CODEX_PREVIEW_MAX_LEN)


def _extract_content_text(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    texts = [
        part.get("text")
        for part in value
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    return " ".join(text for text in texts if text).strip() or None


def _capture_codex_session_preview(state: Any, value: Any) -> None:
    if state.session_preview is None:
        state.session_preview = _codex_preview_text(value)


def _codex_multi_agent_input(
    meta: dict[str, Any], ctx: dict[str, Any], *, session_id: UUID
) -> CodexMultiAgentInput:
    sandbox_policy = (
        ctx.get("sandbox_policy") if isinstance(ctx.get("sandbox_policy"), dict) else {}
    )
    source = meta.get("source")
    thread_spawn_raw = (
        _extract_nested_map(source, "subagent", "thread_spawn")
        if isinstance(source, dict)
        else None
    )
    source_name = _as_non_empty_str(source)
    collaboration_mode = ctx.get("collaboration_mode")
    return CodexMultiAgentInput(
        sandbox_id=_as_non_empty_str(meta.get("id")),
        sandbox_mode=_as_non_empty_str(sandbox_policy.get("type")),
        approval_policy=_as_non_empty_str(ctx.get("approval_policy")),
        collaboration_mode=(
            _as_non_empty_str(collaboration_mode.get("mode"))
            if isinstance(collaboration_mode, dict)
            else _as_non_empty_str(collaboration_mode)
        ),
        multi_agent_version=_as_non_empty_str(
            ctx.get("multi_agent_version") or meta.get("multi_agent_version")
        ),
        multi_agent_mode=_as_non_empty_str(
            ctx.get("multi_agent_mode") or meta.get("multi_agent_mode")
        ),
        agent_path=_as_non_empty_str(meta.get("agent_path"))
        or (
            _as_non_empty_str(thread_spawn_raw.get("agent_path"))
            if thread_spawn_raw is not None
            else None
        ),
        agent_nickname=_as_non_empty_str(meta.get("nickname"))
        or _as_non_empty_str(meta.get("agent_nickname")),
        agent_role=_as_non_empty_str(meta.get("agent_role")) or source_name,
        cwd=_as_non_empty_str(meta.get("cwd")),
        title=_codex_session_title(session_id),
        forked_from_id=_extract_uuid_text(meta.get("forked_from_id")),
        thread_spawn=(
            CodexThreadSpawn(
                parent_thread_id=_extract_uuid_text(
                    thread_spawn_raw.get("parent_thread_id")
                ),
                depth=thread_spawn_raw.get("depth")
                if isinstance(thread_spawn_raw.get("depth"), int)
                else None,
                agent_path=_as_non_empty_str(thread_spawn_raw.get("agent_path")),
                agent_nickname=_as_non_empty_str(
                    thread_spawn_raw.get("agent_nickname")
                ),
                agent_role=_as_non_empty_str(thread_spawn_raw.get("agent_role")),
            )
            if thread_spawn_raw is not None
            else None
        ),
    )


def _session_forked_from_id(records: Iterable[dict]) -> str | None:
    for record in records:
        if record.get("type") != "session_meta":
            continue
        ffid = (
            record.get("payload", {}).get("forked_from_id")
            if isinstance(record.get("payload"), dict)
            else None
        )
        return _extract_uuid_text(ffid)
    return None


def _iter_own_records(
    records: Iterable[tuple[dict, RecordSpan]],
    parent_started_turn_ids: set[str],
) -> Iterator[tuple[dict, RecordSpan]]:
    """Stream ``_cut_inherited_records`` semantics without materializing records.

    Keeps the leading ``session_meta`` prefix, drops the inherited segment a
    forked rollout re-materializes, and passes through everything from the
    first foreign ``task_started`` on.  Non-forks pass through unchanged.
    """
    iterator = iter(records)
    head: list[tuple[dict, RecordSpan]] = []
    leading = 0
    prefix_open = True
    meta_seen = False
    forked_from: str | None = None
    for pair in iterator:
        record = pair[0]
        head.append(pair)
        if record.get("type") == "session_meta":
            if not meta_seen:
                meta_seen = True
                payload = record.get("payload")
                ffid = (
                    payload.get("forked_from_id") if isinstance(payload, dict) else None
                )
                forked_from = _extract_uuid_text(ffid)
        else:
            prefix_open = False
        if prefix_open:
            leading += 1
        if meta_seen and not prefix_open:
            break
    if not meta_seen or forked_from is None:
        yield from head
        yield from iterator
        return
    yield from head[:leading]
    for record, _span in chain(head[leading:], iterator):
        payload = record.get("payload") or {}
        if payload.get("type") != "task_started":
            continue
        turn_id = payload.get("turn_id")
        if isinstance(turn_id, str) and turn_id not in parent_started_turn_ids:
            yield record, _span
            break
    else:
        # Fork has no own turns (inherited only): keep just its session_meta.
        return
    yield from iterator


def _cut_inherited_records(
    records: list[dict], parent_started_turn_ids: set[str] | None
) -> list[dict]:
    """Drop the inherited-history segment a forked rollout re-materializes.

    A forked continuation window copies the source's recent turns verbatim
    (including their ``task_started``/``task_complete``/``token_count``/
    ``sub_agent_activity`` records). Re-projecting that copy double-counts
    turns/tokens and re-emits inherited spawn edges. The fork's own turns begin
    at the first ``task_started`` whose ``turn_id`` is absent from the parent's
    raw ``task_started`` set (validated: a clean cut for every fork - all
    preceding turns are inherited, all from here are own, and no ``spawn_agent``
    call lands in the dropped segment).

    The leading ``session_meta`` record(s) are always kept (they are the fork's
    own). When the parent set is unavailable (single-file ingestion) or the file
    is not a fork, records are returned unchanged.
    """
    if parent_started_turn_ids is None:
        return records
    if _session_forked_from_id(records) is None:
        return records
    leading = 0
    for record in records:
        if record.get("type") == "session_meta":
            leading += 1
            continue
        break
    first_foreign = None
    for index in range(leading, len(records)):
        payload = records[index].get("payload") or {}
        if payload.get("type") != "task_started":
            continue
        turn_id = payload.get("turn_id")
        if isinstance(turn_id, str) and turn_id not in parent_started_turn_ids:
            first_foreign = index
            break
    if first_foreign is None:
        # Fork has no own turns (inherited only): keep just its session_meta.
        return records[:leading]
    return records[:leading] + records[first_foreign:]


def _derive_session_status(turns: list) -> SessionStatus:
    """Map a thread's current turn state to reversible session liveness."""

    return (
        SessionStatus.LIVING
        if any(turn.status == TurnStatus.RUNNING for turn in turns)
        else SessionStatus.NOT_LIVING
    )


def _codex_prompt_block_name(text: str, index: int) -> str:
    stripped = text.lstrip()
    if stripped.startswith("<") and ">" in stripped:
        tag = stripped[1 : stripped.index(">")].strip().split()[0]
        if tag:
            return tag
    return f"developer_block_{index}"


def _codex_user_prompt_block_name(text: str) -> str | None:
    stripped = text.lstrip()
    if stripped.startswith("# AGENTS.md instructions"):
        return "agents_md"
    return None


_CONTEXT_SOURCE_LABELS = {
    "base_system": "Base instructions",
    "developer_instructions": "Developer instructions",
    "agents_md": "AGENTS.md",
    "skills": "Skills",
    "mcp": "Tools / MCP",
    "memory": "Memory",
}


def _codex_context_source_key(*, block: str, role: str, text: str) -> str:
    haystack = f"{block}\n{text}".lower()
    if block == "base_instructions":
        return "base_system"
    if "agents.md" in haystack:
        return "agents_md"
    if "skills_instructions" in block or "### available skills" in haystack:
        return "skills"
    if "plugins_instructions" in block or "### available plugins" in haystack:
        return "mcp"
    if (
        "memory_summary" in haystack
        or "memory layout" in haystack
        or "## memory" in haystack
    ):
        return "memory"
    if "mcp" in haystack or "tools are grouped" in haystack:
        return "mcp"
    if role == "developer":
        return "developer_instructions"
    return "base_system"


def _context_source_observation(
    *,
    timestamp: Any,
    block: str,
    role: str,
    text: str,
) -> ContextSourceObservation:
    key = _codex_context_source_key(block=block, role=role, text=text)
    return ContextSourceObservation(
        timestamp=timestamp,
        key=key,
        label=_CONTEXT_SOURCE_LABELS[key],
        text=text,
        source="codex_prompt_block",
    )


def _record_context_source(
    state: Any,
    observation: ContextSourceObservation,
    *,
    block: str,
    role: str,
) -> None:
    """Keep one observation per (role, block_name); first emission wins.

    Codex re-injects the base/developer/AGENTS.md prompt blocks after a context
    compaction. Each re-injection shares the same (role, block_name) identity as
    the resident prefix block, so per-block dedup collapses them. The first
    emission is kept: the block is resident from first injection through end of
    session (Codex re-attaches it after every compaction), so the earliest
    timestamp is what makes the accounting attribute its per-call cost across
    every API call that carried the block.
    """
    state.context_source_by_block.setdefault((role, block), observation)


class CodexAdapter(BaseAdapter):
    """Ingest Codex CLI JSONL rollout files from ~/.codex/sessions/."""

    vendor = Vendor.CODEX_CLI

    @dataclass
    class _ParseState:
        session_meta: dict[str, Any] = field(default_factory=dict)
        turn_context: dict[str, Any] = field(default_factory=dict)
        session_id: UUID = field(default_factory=uuid4)
        context_window_tokens: int | None = None
        context_usage: list[ContextUsageObservation] = field(default_factory=list)
        runtime_observations: list[RuntimeObservation] = field(default_factory=list)
        # The first persisted user message is a display preview, never an
        # inferred thread name. Current Codex rollouts can encode it as either
        # a legacy user_message event or a native UserMessage item.
        session_preview: str | None = None
        projected_turn_ids: set[str] = field(default_factory=set)
        # Most recent reasoning effort seen on a turn_context record (real
        # string only). Drives effort_changed observation emission: a new turn
        # whose effort differs from this baseline marks a cache-key change-point.
        prev_effort: str | None = None
        multi_agent_version: str | None = None
        multi_agent_mode: str | None = None
        # Last cumulative ``total_token_usage`` seen on a Codex token_count
        # event. Codex occasionally re-emits an identical snapshot (cumulative
        # unchanged, last_token_usage repeated) for a non-billable repeat;
        # tracking the prior lets us drop the stale copy before accounting.
        prev_total_token_usage: dict[str, int] | None = None
        # One resident slot per (role, block_name); first emission wins. Codex
        # re-injects base/developer/AGENTS.md blocks after each compaction, so
        # per-block dedup keeps only the first (resident-from-first-injection)
        # copy — its timestamp drives per-call cost attribution.
        context_source_by_block: dict[tuple[str, str], ContextSourceObservation] = (
            field(default_factory=dict)
        )
        # child agent_thread_id -> spawn tool-call call_id, captured from
        # sub_agent_activity{kind:started} events. Backs the forked_from edge
        # origin with the real spawn call instead of the parent's last tool call.
        spawn_links: dict[str, str] = field(default_factory=dict)
        # Open ``custom_tool_call(name=exec)`` wrapper cells that passed the
        # strict static recognizer. Native Codex items can attach before the
        # wrapper output arrives; older JSONL falls back to derived-static
        # activities at wrapper completion.
        pending_exec_wrappers: dict[str, _PendingExecWrapper] = field(
            default_factory=dict
        )
        # Every custom ``exec`` call, including cells whose JavaScript cannot
        # be statically parsed. Its wrapper result can still be failed even
        # though it gives no nested-tool outcome.
        exec_wrapper_call_ids: set[str] = field(default_factory=set)
        # Direct function calls sometimes receive a terminal ThreadItem whose
        # item id is exactly the response-item call id (for example,
        # ``spawn_agent`` -> ``SubAgentActivity``). Keep the original call as
        # the canonical action and enrich it from that stronger terminal fact.
        direct_function_calls: dict[str, TranscriptRecord] = field(
            default_factory=dict
        )
        native_direct_result_records: dict[str, TranscriptRecord] = field(
            default_factory=dict
        )
        native_direct_output_authoritative: set[str] = field(default_factory=set)
        # Native CommandExecution ids already emitted from item_started. A
        # later item_completed updates the same canonical item rather than
        # creating a second command activity.
        native_command_ids: set[str] = field(default_factory=set)
        # Native CommandExecution id -> static wrapper invocation. Needed when
        # an item_started arrives after an old wrapper's derived placeholder.
        native_command_bindings: dict[str, tuple[_PendingExecWrapper, int]] = field(
            default_factory=dict
        )
        # Native non-command item ids and their optional static-wrapper child
        # binding. The tuple key keeps FileChange/Plan/WebSearch ids separate
        # even if a provider reuses an identifier across item variants.
        native_activity_ids: set[tuple[str, str]] = field(default_factory=set)
        native_activity_bindings: dict[
            tuple[str, str], tuple[_PendingExecWrapper, int]
        ] = field(default_factory=dict)

    def ingest_file(
        self,
        path: Path,
        *,
        parent_started_turn_ids: set[str] | None = None,
        retention: CanonicalRetention = "trajectory",
    ) -> Session:
        self._reset_ingest_state()
        self.last_provenance: SessionProvenance | None = None
        if retention == "measurements":
            records: Iterable[tuple[dict, RecordSpan | None]] = self._iter_record_spans(
                path
            )
            if parent_started_turn_ids is not None:
                records = _iter_own_records(records, parent_started_turn_ids)
        else:
            records = (
                (record, None)
                for record in _cut_inherited_records(
                    self._load_records(path), parent_started_turn_ids
                )
            )
        state = self._ParseState()
        transcript = self._build_transcript(records, state)
        return self._build_session(path, transcript, state, retention=retention)

    def build_canonical_session(
        self,
        source: Path,
        records: Iterable[dict],
        *,
        parent_started_turn_ids: set[str] | None = None,
    ) -> Session:
        """In-memory-record seam: cut inherited fork history, then assemble."""
        self._reset_ingest_state()
        self.last_provenance: SessionProvenance | None = None
        cut = _cut_inherited_records(list(records), parent_started_turn_ids)
        state = self._ParseState()
        transcript = self._build_transcript(
            ((record, None) for record in cut), state
        )
        return self._build_session(source, transcript, state, retention="trajectory")

    def scan_started_turn_ids(self, source: Path) -> set[str] | None:
        started: set[str] = set()
        for record in self._iter_records(source):
            payload = record.get("payload") or {}
            if payload.get("type") == "task_started" and isinstance(
                payload.get("turn_id"), str
            ):
                started.add(payload["turn_id"])
        return started

    def scan_identity(self, source: Path) -> SessionHeader | None:
        """Read the leading ``session_meta`` without searching for a title."""

        for record in self._iter_records(source):
            if record.get("type") != "session_meta":
                continue
            meta = record.get("payload") or {}
            if not isinstance(meta, dict):
                return None
            try:
                session_id = UUID(meta.get("id"))
            except (ValueError, TypeError):
                return None
            mechanism = _codex_multi_agent_input(meta, {}, session_id=session_id)
            return SessionHeader(
                session_id=session_id,
                vendor=Vendor.CODEX_CLI,
                parent_session_id=codex_parent_session_id(mechanism),
                title=mechanism.title,
                cwd=mechanism.cwd,
            )
        return None

    def scan_header(self, source: Path) -> SessionHeader | None:
        """Read static identity and explicit title without deriving one from a message."""
        return self.scan_identity(source)

    def _build_session(
        self,
        source: Path,
        transcript: list[TranscriptRecord],
        state: _ParseState | None = None,
        *,
        retention: CanonicalRetention = "trajectory",
    ) -> Session:
        state = state or self._ParseState()
        if not transcript:
            raise ValueError(
                f"CodexAdapter: no transcript records parsed from {source}"
            )

        meta = state.session_meta
        ctx = state.turn_context
        mechanism = _codex_multi_agent_input(meta, ctx, session_id=state.session_id)
        if state.multi_agent_version is not None:
            mechanism.multi_agent_version = state.multi_agent_version
        if state.multi_agent_mode is not None:
            mechanism.multi_agent_mode = state.multi_agent_mode
        parent_session_id = codex_parent_session_id(mechanism)
        extensions = codex_extensions(mechanism)
        if extensions.codex is not None:
            extensions.codex.preview = state.session_preview or _codex_session_preview(
                transcript
            )
            if state.spawn_links:
                extensions.codex.spawn_links = dict(state.spawn_links)

        hooks = AssemblyHooks(
            active_status=(
                TurnStatus.RUNNING
                if source_is_living(source)
                else TurnStatus.INCOMPLETE
            ),
            default_previous_turn_status=TurnStatus.INTERRUPTED,
            # Codex's authoritative turn delimiter is the task_started/task_complete
            # lifecycle boundary; user_message is an in-turn item. Prefer lifecycle
            # mode so turns (incl. compaction-only turns) project correctly and
            # spawn calls are turn-attributed.
            prefer_lifecycle=True,
            extensions=extensions,
            parent_session_id=parent_session_id,
            runtime_observations=state.runtime_observations,
            session_fields={
                "model": _as_non_empty_str(ctx.get("model")),
                "reasoning_effort": _as_non_empty_str(ctx.get("effort")),
                "agent_name": extensions.codex.agent_nickname
                if extensions.codex
                else None,
            },
            build_context_usage=lambda _records: state.context_usage,
            build_context_sources=lambda _context: list(
                state.context_source_by_block.values()
            ),
            build_session_fields=lambda context: {
                "status": _derive_session_status(context.turns)
            },
            provenance_sink=lambda provenance: setattr(
                self, "last_provenance", provenance
            ),
        )
        return assemble_session(
            vendor=Vendor.CODEX_CLI,
            source=source,
            session_id=state.session_id,
            transcript=transcript,
            retention=retention,
            hooks=hooks,
        )

    def _build_transcript(
        self,
        records: Iterable[tuple[dict, RecordSpan | None]],
        state: _ParseState,
    ) -> list[TranscriptRecord]:
        """Extract only CT-useful transcript facts from Codex JSONL records."""
        transcript: list[TranscriptRecord] = []
        for record, span in records:
            before = len(transcript)
            self._translate_record(record, state, transcript)
            if span is not None:
                for entry in transcript[before:]:
                    entry.origin = span
        return transcript

    def _translate_record(
        self,
        record: dict,
        state: _ParseState,
        transcript: list[TranscriptRecord],
    ) -> None:
        outer_type = record.get("type", "")
        payload = record.get("payload") or {}
        ts = parse_iso_timestamp(record.get("timestamp"))

        if outer_type == "session_meta":
            self._handle_session_meta(payload, ts, state, transcript)
            return

        if outer_type == "turn_context":
            self._handle_turn_context(payload, ts, state)
            return

        if ts is None:
            return

        if outer_type == "event_msg":
            self._handle_event_msg(payload, ts, state, transcript)

        elif outer_type == "response_item":
            self._handle_response_item(payload, ts, state, transcript)

        elif outer_type == "compacted":
            self._handle_compacted(payload, ts, state, transcript)

    @staticmethod
    def _activity_data(
        *,
        outcome: str,
        fidelity: str,
        provenance: dict[str, Any],
        source: str = "agent",
        activity_kind: str | None = "command",
        wrapper_status: str | None = None,
    ) -> dict[str, Any]:
        activity: dict[str, Any] = {
            "source": source,
            "outcome": outcome,
            "fidelity": fidelity,
            "provenance": provenance,
        }
        if activity_kind is not None:
            activity["kind"] = activity_kind
        if wrapper_status is not None:
            activity["wrapper_status"] = wrapper_status
        return {"activity": activity}

    @staticmethod
    def _native_command_outcome(*, status: ToolStatus, exit_code: int | None) -> str:
        if exit_code == 0:
            return "succeeded"
        if exit_code is not None or status == ToolStatus.FAILED:
            return "failed"
        return "unknown"

    @staticmethod
    def _native_activity_outcome(*, status: ToolStatus) -> str:
        if status == ToolStatus.COMPLETED:
            return "succeeded"
        if status == ToolStatus.FAILED:
            return "failed"
        return "unknown"

    @staticmethod
    def _native_terminal_status(item: dict[str, Any], *, completed: bool) -> ToolStatus:
        """Normalize explicit native terminal status without wrapper inference."""

        if isinstance(item.get("success"), bool):
            return ToolStatus.COMPLETED if item["success"] else ToolStatus.FAILED
        return _tool_status(
            item.get("status"),
            default=ToolStatus.COMPLETED if completed else ToolStatus.IN_PROGRESS,
        )

    @staticmethod
    def _native_item_timing(
        payload: dict[str, Any], timestamp: datetime
    ) -> tuple[datetime, datetime, dict[str, int]]:
        """Normalize optional rollout timing while retaining raw evidence.

        Historical terminal items often carry both epoch-millisecond endpoints
        on the enclosing event. The canonical item interval uses those values;
        provenance retains the original numbers for lossless inspection.
        """

        raw_started = payload.get("started_at_ms")
        raw_completed = payload.get("completed_at_ms")
        started_at = parse_timestamp(raw_started) or timestamp
        completed_at = parse_timestamp(raw_completed) or timestamp
        if completed_at < started_at:
            # Do not manufacture a negative canonical interval from malformed
            # telemetry. The raw values remain available in provenance below.
            started_at = completed_at
        timing = {
            key: value
            for key, value in (
                ("started_at_ms", raw_started),
                ("completed_at_ms", raw_completed),
            )
            if isinstance(value, int) and not isinstance(value, bool)
        }
        if len(timing) == 2:
            duration = timing["completed_at_ms"] - timing["started_at_ms"]
            if duration >= 0:
                timing["duration_ms"] = duration
        return started_at, completed_at, timing

    @staticmethod
    def _pending_exec_wrapper_candidates(
        state: _ParseState,
        *,
        timestamp: datetime,
        predicate: Callable[[_StaticExecInvocation], bool],
        turn_id: str | None = None,
    ) -> list[tuple[_PendingExecWrapper, int]]:
        candidates: list[tuple[_PendingExecWrapper, int]] = []
        for wrapper in state.pending_exec_wrappers.values():
            # A native item cannot precede the wrapper that lexically contains
            # it. When both records carry a turn id, crossing turns is likewise
            # not evidence of the same nested call.
            if timestamp < wrapper.started_at:
                continue
            if (
                turn_id is not None
                and wrapper.turn_id is not None
                and turn_id != wrapper.turn_id
            ):
                continue
            if (
                wrapper.closed
                and wrapper.completed_at is not None
                and (timestamp - wrapper.completed_at).total_seconds()
                > _NATIVE_EXEC_MATCH_GRACE_SECONDS
            ):
                continue
            for index, invocation in enumerate(wrapper.invocations):
                if index in wrapper.matched_native_indices:
                    continue
                if predicate(invocation):
                    candidates.append((wrapper, index))
        return candidates

    def _match_pending_exec_wrapper(
        self,
        state: _ParseState,
        *,
        command: str | None,
        timestamp: datetime,
        turn_id: str | None = None,
    ) -> tuple[_PendingExecWrapper, int] | None:
        key = _command_match_key(command)
        if key is None:
            return None
        candidates = self._pending_exec_wrapper_candidates(
            state,
            timestamp=timestamp,
            predicate=lambda invocation: _command_match_key(invocation.command) == key,
            turn_id=turn_id,
        )
        if len(candidates) != 1:
            return None
        wrapper, index = candidates[0]
        wrapper.matched_native_indices.add(index)
        return wrapper, index

    def _match_pending_exec_wrapper_invocation(
        self,
        state: _ParseState,
        *,
        timestamp: datetime,
        predicate: Callable[[_StaticExecInvocation], bool],
        turn_id: str | None = None,
    ) -> tuple[_PendingExecWrapper, int] | None:
        candidates = self._pending_exec_wrapper_candidates(
            state,
            timestamp=timestamp,
            predicate=predicate,
            turn_id=turn_id,
        )
        if len(candidates) != 1:
            return None
        wrapper, index = candidates[0]
        wrapper.matched_native_indices.add(index)
        return wrapper, index

    @staticmethod
    def _hide_expanded_exec_wrapper(wrapper: _PendingExecWrapper) -> None:
        vendor_data = wrapper.call_record.data.setdefault("vendor_data", {})
        if not isinstance(vendor_data, dict):
            return
        vendor_data["activity"] = {
            "hidden_from_overview": True,
            "fidelity": "observed_wrapper",
            "reason": "static_exec_expansion",
            "child_count": len(wrapper.invocations),
            "extractor": _CODEX_EXEC_STATIC_EXTRACTOR,
        }

    @staticmethod
    def _static_activity_input(invocation: _StaticExecInvocation) -> Any:
        """Keep fallback activity useful without copying a full patch payload."""

        if invocation.method != "apply_patch":
            return invocation.input
        if isinstance(invocation.input, dict):
            reference = invocation.input.get("_static_reference")
            if isinstance(reference, str) and reference:
                return {"patch_reference": reference}
        return {"patch_reference": "literal"}

    def _append_derived_exec_activities(
        self,
        wrapper: _PendingExecWrapper,
        *,
        timestamp: datetime,
        wrapper_status: ToolStatus,
        wrapper_output: Any,
        transcript: list[TranscriptRecord],
    ) -> None:
        """Emit typed fallback facts only where source evidence permits it."""

        # A single static invocation plus result payload is an observed wrapper
        # outcome. Multiple invocations remain ambiguous even when their shared
        # code cell completed, because the output cannot safely be attributed.
        observed_wrapper_result = (
            len(wrapper.invocations) == 1
            and wrapper_status == ToolStatus.COMPLETED
            and _has_explicit_exec_wrapper_result(wrapper_output)
        )

        for index, invocation in enumerate(wrapper.invocations):
            if index in wrapper.matched_native_indices:
                continue
            tool_call_id = f"{wrapper.call_id}:nested:{index}"
            input_data = self._static_activity_input(invocation)
            record_data: dict[str, Any] = {
                "tool_name": invocation.tool_name,
                "tool_call_id": tool_call_id,
                "input": input_data,
                "status": (
                    ToolStatus.COMPLETED.value
                    if observed_wrapper_result
                    else ToolStatus.REQUESTED.value
                ),
                "item_kind": invocation.item_kind,
                "vendor_data": self._activity_data(
                    outcome=("succeeded" if observed_wrapper_result else "unknown"),
                    fidelity=(
                        "observed_wrapper"
                        if observed_wrapper_result
                        else "derived_static"
                    ),
                    activity_kind=(
                        "command"
                        if invocation.item_kind == "command_execution"
                        else None
                    ),
                    wrapper_status=(
                        ToolStatus.FAILED.value
                        if wrapper_status == ToolStatus.FAILED
                        else None
                    ),
                    provenance={
                        "parent_tool_call_id": wrapper.call_id,
                        "parent_tool_name": "exec",
                        "nested_method": invocation.method,
                        "nested_index": index,
                        "source_offset": invocation.source_offset,
                        "extractor": _CODEX_EXEC_STATIC_EXTRACTOR,
                        **(
                            {"wrapper_result_observed": True}
                            if observed_wrapper_result
                            else {}
                        ),
                    },
                ),
            }
            if invocation.command is not None:
                record_data["command"] = input_data
            activity_record = TranscriptRecord(
                sequence=len(transcript),
                timestamp=timestamp,
                vendor=Vendor.CODEX_CLI,
                role="assistant",
                kind="tool_call",
                data=record_data,
                fidelity="derived",
            )
            transcript.append(activity_record)
            wrapper.derived_records[index] = activity_record
            if observed_wrapper_result:
                transcript.append(
                    TranscriptRecord(
                        sequence=len(transcript),
                        timestamp=timestamp,
                        vendor=Vendor.CODEX_CLI,
                        role="tool",
                        kind="tool_result",
                        data={
                            "tool_call_id": tool_call_id,
                            "tool_name": invocation.tool_name,
                            "output": wrapper_output,
                            "status": ToolStatus.COMPLETED.value,
                        },
                        fidelity="derived",
                    )
                )

    def _handle_static_exec_wrapper_output(
        self,
        payload: dict,
        timestamp: datetime,
        state: _ParseState,
        transcript: list[TranscriptRecord],
    ) -> None:
        call_id = _as_non_empty_str(payload.get("call_id"))
        if call_id is None:
            return
        wrapper = state.pending_exec_wrappers.get(call_id)
        if wrapper is None:
            return
        wrapper.closed = True
        wrapper.completed_at = timestamp
        wrapper_status = _tool_result_status(
            payload,
            payload.get("output"),
            exec_wrapper=True,
        )
        if wrapper_status == ToolStatus.FAILED and _is_exec_syntax_error(
            payload.get("output")
        ):
            # A parse failure occurs before any nested tools can execute. Keep
            # the raw failed exec row instead of hiding it behind a lexical
            # WebSearch/command fallback that never ran.
            vendor_data = wrapper.call_record.data.setdefault("vendor_data", {})
            if isinstance(vendor_data, dict):
                vendor_data.update(
                    self._activity_data(
                        outcome="failed",
                        fidelity="observed_wrapper",
                        activity_kind=None,
                        provenance={
                            "tool_call_id": wrapper.call_id,
                            "reason": "exec_syntax_error_before_nested_execution",
                            "extractor": _CODEX_EXEC_STATIC_EXTRACTOR,
                        },
                    )
                )
            return
        # The public wrapper is evidence, not a parallel visible action, once
        # every statically proven nested activity has its own canonical row.
        self._append_derived_exec_activities(
            wrapper,
            timestamp=timestamp,
            wrapper_status=wrapper_status,
            wrapper_output=payload.get("output"),
            transcript=transcript,
        )
        self._hide_expanded_exec_wrapper(wrapper)

    def _handle_native_command_execution(
        self,
        payload: dict,
        timestamp: datetime,
        state: _ParseState,
        transcript: list[TranscriptRecord],
        *,
        completed: bool,
    ) -> None:
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "CommandExecution":
            return
        command_id = _as_non_empty_str(item.get("id"))
        if command_id is None:
            return
        command = _native_command_text(item.get("command"))
        exit_code = item.get("exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            exit_code = None
        status = _tool_status(
            item.get("status"),
            default=ToolStatus.COMPLETED if completed else ToolStatus.IN_PROGRESS,
        )
        started_at, completed_at, timing = self._native_item_timing(payload, timestamp)
        turn_id = _as_non_empty_str(payload.get("turn_id"))
        outcome = self._native_command_outcome(status=status, exit_code=exit_code)
        matching = state.native_command_bindings.get(command_id)
        if matching is None:
            matching = self._match_pending_exec_wrapper(
                state,
                command=command,
                timestamp=completed_at,
                turn_id=turn_id,
            )
            if matching is not None:
                state.native_command_bindings[command_id] = matching

        if matching is not None:
            wrapper, index = matching
            derived = wrapper.derived_records.get(index)
            if derived is not None:
                # A late native lifecycle resolves a prior derived placeholder
                # without creating a duplicate command item.
                activity = self._activity_data(
                    outcome=outcome,
                    fidelity="derived_matched_native",
                    provenance={
                        "parent_tool_call_id": wrapper.call_id,
                        "parent_tool_name": "exec",
                        "nested_index": index,
                        "native_command_id": command_id,
                        "extractor": _CODEX_EXEC_STATIC_EXTRACTOR,
                        **timing,
                    },
                )
                derived.timestamp = started_at
                derived.data.update(
                    {
                        "status": status.value,
                        "exit_code": exit_code,
                        "output": item.get("formatted_output") or item.get("stdout"),
                        "vendor_data": activity,
                    }
                )
                if completed:
                    transcript.append(
                        TranscriptRecord(
                            sequence=len(transcript),
                            timestamp=completed_at,
                            vendor=Vendor.CODEX_CLI,
                            role="tool",
                            kind="tool_result",
                            data={
                                "tool_call_id": derived.data["tool_call_id"],
                                "tool_name": "exec_command",
                                "output": item.get("formatted_output")
                                or item.get("stdout"),
                                "exit_code": exit_code,
                                "status": status.value,
                                "vendor_data": activity,
                            },
                        )
                    )
                return

        command_input: dict[str, Any] = {}
        if command is not None:
            command_input["cmd"] = command
        cwd = _as_non_empty_str(item.get("cwd"))
        if cwd is not None:
            command_input["workdir"] = cwd.removeprefix("file://")
        native_data = self._activity_data(
            outcome=outcome,
            fidelity="observed_native",
            source=_codex_command_activity_source(item.get("source")),
            provenance={
                "native_command_id": command_id,
                "source": "event_msg.item_completed"
                if completed
                else "event_msg.item_started",
                "source_kind": _as_non_empty_str(item.get("source")),
                **timing,
            },
        )
        if command_id in state.native_command_ids:
            if completed:
                transcript.append(
                    TranscriptRecord(
                        sequence=len(transcript),
                        timestamp=completed_at,
                        vendor=Vendor.CODEX_CLI,
                        role="tool",
                        kind="tool_result",
                        data={
                            "tool_call_id": command_id,
                            "tool_name": "exec_command",
                            "output": item.get("formatted_output")
                            or item.get("stdout"),
                            "exit_code": exit_code,
                            "status": status.value,
                            "vendor_data": native_data,
                        },
                    )
                )
            return

        state.native_command_ids.add(command_id)
        transcript.append(
            TranscriptRecord(
                sequence=len(transcript),
                timestamp=started_at,
                vendor=Vendor.CODEX_CLI,
                role="assistant",
                kind="tool_call",
                data={
                    "tool_name": "exec_command",
                    "tool_call_id": command_id,
                    "input": command_input,
                    "command": command_input,
                    "status": (
                        ToolStatus.IN_PROGRESS.value if completed else status.value
                    ),
                    "item_kind": "command_execution",
                    "vendor_data": native_data,
                },
                fidelity="derived" if completed else "observed",
            )
        )
        if completed:
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=completed_at,
                    vendor=Vendor.CODEX_CLI,
                    role="tool",
                    kind="tool_result",
                    data={
                        "tool_call_id": command_id,
                        "tool_name": "exec_command",
                        "output": item.get("formatted_output") or item.get("stdout"),
                        "exit_code": exit_code,
                        "status": status.value,
                        "vendor_data": native_data,
                    },
                )
            )

    def _resolve_derived_exec_activity(
        self,
        wrapper: _PendingExecWrapper,
        index: int,
        *,
        timestamp: datetime,
        native_type: str,
        native_id: str,
        tool_name: str,
        item_kind: str,
        input_data: Any,
        status: ToolStatus,
        outcome: str,
        transcript: list[TranscriptRecord],
        completed: bool,
        output: Any = None,
        path: str | None = None,
        operation: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> bool:
        """Resolve a late native item onto its prior static wrapper child."""

        derived = wrapper.derived_records.get(index)
        if derived is None:
            return False
        canonical_started_at = started_at or timestamp
        canonical_completed_at = completed_at or timestamp
        activity = self._activity_data(
            outcome=outcome,
            fidelity="derived_matched_native",
            activity_kind=None,
            provenance={
                "parent_tool_call_id": wrapper.call_id,
                "parent_tool_name": "exec",
                "nested_index": index,
                "native_item_id": native_id,
                "native_item_type": native_type,
                "extractor": _CODEX_EXEC_STATIC_EXTRACTOR,
                **(provenance or {}),
            },
        )
        derived.timestamp = canonical_started_at
        derived.data.update(
            {
                "tool_name": tool_name,
                "input": input_data,
                "item_kind": item_kind,
                "status": status.value,
                "vendor_data": activity,
            }
        )
        if output is not None:
            derived.data["output"] = output
        if path is not None:
            derived.data["path"] = path
        if operation is not None:
            derived.data["operation"] = operation
        if not completed:
            return True
        transcript.append(
            TranscriptRecord(
                sequence=len(transcript),
                timestamp=canonical_completed_at,
                vendor=Vendor.CODEX_CLI,
                role="tool",
                kind="tool_result",
                data={
                    "tool_call_id": derived.data["tool_call_id"],
                    "tool_name": tool_name,
                    "output": output,
                    "status": status.value,
                    "path": path,
                    "operation": operation,
                    "vendor_data": activity,
                },
            )
        )
        return True

    def _record_native_activity(
        self,
        *,
        state: _ParseState,
        transcript: list[TranscriptRecord],
        timestamp: datetime,
        native_type: str,
        native_id: str,
        tool_name: str,
        item_kind: str,
        input_data: Any,
        status: ToolStatus,
        completed: bool,
        predicate: Callable[[_StaticExecInvocation], bool],
        output: Any = None,
        path: str | None = None,
        operation: str | None = None,
        turn_id: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> None:
        """Project a non-command Codex ThreadItem and bind a static fallback."""

        native_key = (native_type, native_id)
        outcome = self._native_activity_outcome(status=status)
        canonical_started_at = started_at or timestamp
        canonical_completed_at = completed_at or timestamp
        matching = state.native_activity_bindings.get(native_key)
        if matching is None:
            matching = self._match_pending_exec_wrapper_invocation(
                state,
                timestamp=canonical_completed_at,
                predicate=predicate,
                turn_id=turn_id,
            )
            if matching is not None:
                state.native_activity_bindings[native_key] = matching

        if matching is not None:
            wrapper, index = matching
            if self._resolve_derived_exec_activity(
                wrapper,
                index,
                timestamp=timestamp,
                native_type=native_type,
                native_id=native_id,
                tool_name=tool_name,
                item_kind=item_kind,
                input_data=input_data,
                status=status,
                outcome=outcome,
                transcript=transcript,
                completed=completed,
                output=output,
                path=path,
                operation=operation,
                started_at=canonical_started_at,
                completed_at=canonical_completed_at,
                provenance=provenance,
            ):
                state.native_activity_ids.add(native_key)
                return

        activity = self._activity_data(
            outcome=outcome,
            fidelity="observed_native",
            activity_kind=None,
            provenance={
                "native_item_id": native_id,
                "native_item_type": native_type,
                "source": (
                    "event_msg.item_completed"
                    if completed
                    else "event_msg.item_started"
                ),
                **(provenance or {}),
            },
        )
        if native_key in state.native_activity_ids:
            if completed:
                transcript.append(
                    TranscriptRecord(
                        sequence=len(transcript),
                        timestamp=canonical_completed_at,
                        vendor=Vendor.CODEX_CLI,
                        role="tool",
                        kind="tool_result",
                        data={
                            "tool_call_id": native_id,
                            "tool_name": tool_name,
                            "output": output,
                            "status": status.value,
                            "path": path,
                            "operation": operation,
                            "vendor_data": activity,
                        },
                    )
                )
            return

        state.native_activity_ids.add(native_key)
        transcript.append(
            TranscriptRecord(
                sequence=len(transcript),
                timestamp=canonical_started_at,
                vendor=Vendor.CODEX_CLI,
                role="assistant",
                kind="tool_call",
                data={
                    "tool_name": tool_name,
                    "tool_call_id": native_id,
                    "input": input_data,
                    "status": (
                        ToolStatus.IN_PROGRESS.value if completed else status.value
                    ),
                    "item_kind": item_kind,
                    "path": path,
                    "operation": operation,
                    "vendor_data": activity,
                },
                fidelity="derived" if completed else "observed",
            )
        )
        if completed:
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=canonical_completed_at,
                    vendor=Vendor.CODEX_CLI,
                    role="tool",
                    kind="tool_result",
                    data={
                        "tool_call_id": native_id,
                        "tool_name": tool_name,
                        "output": output,
                        "status": status.value,
                        "path": path,
                        "operation": operation,
                        "vendor_data": activity,
                    },
                )
            )

    @staticmethod
    def _native_file_change_input(
        item: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None, str | None]:
        changes = item.get("changes")
        rows: list[tuple[str, str | None]] = []
        if isinstance(changes, dict):
            # Historical rollout JSONL stores a path-indexed change object.
            for raw_path, raw_change in changes.items():
                if not isinstance(raw_path, str) or not raw_path:
                    continue
                operation = (
                    _as_non_empty_str(raw_change.get("type"))
                    if isinstance(raw_change, dict)
                    else None
                )
                rows.append((raw_path, operation))
        elif isinstance(changes, list):
            # The app-server ThreadItem schema uses a list of FileUpdateChange
            # objects with ``path`` and ``kind`` fields.
            for raw_change in changes:
                if not isinstance(raw_change, dict):
                    continue
                raw_path = _as_non_empty_str(raw_change.get("path"))
                if raw_path is None:
                    continue
                operation = _as_non_empty_str(
                    raw_change.get("kind") or raw_change.get("type")
                )
                rows.append((raw_path, operation))
        else:
            return {}, None, None
        rows.sort(key=lambda row: row[0])
        paths = [path for path, _ in rows]
        operations = [operation for _, operation in rows if operation is not None]
        input_data: dict[str, Any] = {}
        if paths:
            input_data["paths"] = paths
        if operations:
            input_data["operations"] = operations
        return (
            input_data,
            paths[0] if len(paths) == 1 else None,
            operations[0] if len(operations) == 1 else None,
        )

    def _handle_native_file_change(
        self,
        payload: dict,
        timestamp: datetime,
        state: _ParseState,
        transcript: list[TranscriptRecord],
        *,
        completed: bool,
    ) -> None:
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "FileChange":
            return
        native_id = _as_non_empty_str(item.get("id"))
        if native_id is None:
            return
        input_data, path, operation = self._native_file_change_input(item)
        output = {
            key: item[key]
            for key in ("stdout", "stderr")
            if _as_non_empty_str(item.get(key)) is not None
        } or None
        status = _tool_status(
            item.get("status"),
            default=ToolStatus.COMPLETED if completed else ToolStatus.IN_PROGRESS,
        )
        started_at, completed_at, timing = self._native_item_timing(payload, timestamp)
        self._record_native_activity(
            state=state,
            transcript=transcript,
            timestamp=timestamp,
            native_type="FileChange",
            native_id=native_id,
            tool_name="apply_patch",
            item_kind="file_change",
            input_data=input_data,
            status=status,
            completed=completed,
            predicate=lambda invocation: invocation.method == "apply_patch",
            output=output,
            path=path,
            operation=operation,
            turn_id=_as_non_empty_str(payload.get("turn_id")),
            started_at=started_at,
            completed_at=completed_at,
            provenance={"native_item_kind": "FileChange", **timing},
        )

    @staticmethod
    def _normalize_web_correlation_value(value: Any) -> str | None:
        """Normalize query/pattern values without evaluating wrapper code."""

        text = _as_non_empty_str(value)
        if text is None:
            return None
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
            text = text[1:-1].strip()
        return text or None

    @classmethod
    def _static_web_search_queries(cls, input_data: Any) -> set[str]:
        if not isinstance(input_data, dict):
            return set()
        queries = {
            query
            for query in (
                cls._normalize_web_correlation_value(input_data.get("query")),
                cls._normalize_web_correlation_value(input_data.get("q")),
            )
            if query is not None
        }
        for key in ("search_query", "image_query"):
            entries = input_data.get(key)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                query = cls._normalize_web_correlation_value(
                    entry.get("q") or entry.get("query")
                )
                if query is not None:
                    queries.add(query)
        return queries

    @classmethod
    def _static_web_operation_values(
        cls,
        input_data: Any,
        operation: str,
        fields: tuple[str, ...],
    ) -> set[str]:
        if not isinstance(input_data, dict):
            return set()
        entries = input_data.get(operation)
        if not isinstance(entries, list):
            return set()
        values: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for field_name in fields:
                value = cls._normalize_web_correlation_value(entry.get(field_name))
                if value is not None:
                    values.add(value)
        return values

    @classmethod
    def _extension_web_correlation_values(
        cls,
        query: str | None,
        action: dict[str, Any],
    ) -> set[str]:
        """Collect every query/pattern value carried by an Extension item."""

        values = {
            value
            for value in (
                cls._normalize_web_correlation_value(query),
                cls._normalize_web_correlation_value(action.get("query")),
                cls._normalize_web_correlation_value(action.get("pattern")),
                cls._normalize_web_correlation_value(action.get("url")),
            )
            if value is not None
        }
        action_queries = action.get("queries")
        if isinstance(action_queries, list):
            for raw_query in action_queries:
                value = cls._normalize_web_correlation_value(raw_query)
                if value is not None:
                    values.add(value)
        return values

    @classmethod
    def _static_web_matches_extension(
        cls,
        invocation: _StaticExecInvocation,
        *,
        action_type: str | None,
        query_values: set[str],
    ) -> bool:
        """Match a native Extension item only on compatible static evidence."""

        if invocation.method != "web__run" or not isinstance(invocation.input, dict):
            return False
        if action_type == "search":
            return bool(query_values & cls._static_web_search_queries(invocation.input))
        if action_type == "openPage":
            return bool(
                query_values
                & cls._static_web_operation_values(
                    invocation.input, "open", ("ref_id", "url", "uri")
                )
            )
        if action_type == "findInPage":
            return bool(
                query_values
                & cls._static_web_operation_values(
                    invocation.input, "find", ("pattern",)
                )
            )
        if action_type == "other":
            # Codex's historical Extension schema does not expose a more
            # specific action for click/screenshot; timestamps + turn scope
            # still make a single pending matching wrapper unambiguous.
            return "click" in invocation.input or "screenshot" in invocation.input
        return False

    def _handle_native_web_search(
        self,
        payload: dict,
        timestamp: datetime,
        state: _ParseState,
        transcript: list[TranscriptRecord],
        *,
        completed: bool,
    ) -> None:
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "WebSearch":
            return
        native_id = _as_non_empty_str(item.get("id"))
        query = _as_non_empty_str(item.get("query"))
        if native_id is None or query is None:
            return
        input_data: dict[str, Any] = {"query": query}
        if item.get("action") is not None:
            input_data["action"] = item["action"]
        output = {"results": item["results"]} if "results" in item else None
        status = _tool_status(
            item.get("status"),
            default=ToolStatus.COMPLETED if completed else ToolStatus.IN_PROGRESS,
        )
        started_at, completed_at, timing = self._native_item_timing(payload, timestamp)
        self._record_native_activity(
            state=state,
            transcript=transcript,
            timestamp=timestamp,
            native_type="WebSearch",
            native_id=native_id,
            tool_name="web_search",
            item_kind="tool_call",
            input_data=input_data,
            status=status,
            completed=completed,
            predicate=lambda invocation: (
                invocation.method == "web__run"
                and invocation.tool_name == "web_search"
                and self._normalize_web_correlation_value(query)
                in self._static_web_search_queries(invocation.input)
            ),
            output=output,
            turn_id=_as_non_empty_str(payload.get("turn_id")),
            started_at=started_at,
            completed_at=completed_at,
            provenance={"native_item_kind": "WebSearch", **timing},
        )

    def _handle_native_extension_web_search(
        self,
        payload: dict,
        timestamp: datetime,
        state: _ParseState,
        transcript: list[TranscriptRecord],
        *,
        completed: bool,
    ) -> None:
        """Normalize the historical Extension spelling of Codex web activity.

        Older Desktop JSONL persists web actions as an ``Extension`` item with
        ``kind=web.search`` rather than a ``WebSearch`` ThreadItem. Its
        terminal record is authoritative: the action, query, result cards, and
        timing all come from the provider event, not the JavaScript wrapper.
        """

        item = payload.get("item")
        if (
            not isinstance(item, dict)
            or item.get("type") != "Extension"
            or item.get("kind") != "web.search"
        ):
            return
        native_id = _as_non_empty_str(item.get("id"))
        if native_id is None:
            return
        action = item.get("action")
        action_data = action if isinstance(action, dict) else {}
        action_type = _as_non_empty_str(action_data.get("type"))
        query = _as_non_empty_str(item.get("query"))
        query_values = self._extension_web_correlation_values(query, action_data)
        input_data: dict[str, Any] = {
            "kind": "web.search",
            "action": action_data,
        }
        if query is not None:
            input_data["query"] = query
        output = {"results": item.get("results")} if "results" in item else None
        started_at, completed_at, timing = self._native_item_timing(payload, timestamp)
        self._record_native_activity(
            state=state,
            transcript=transcript,
            timestamp=timestamp,
            native_type="Extension:web.search",
            native_id=native_id,
            tool_name="web_search" if action_type == "search" else "web_fetch",
            item_kind="tool_call",
            input_data=input_data,
            # Extension/web.search has no per-item status field. A terminal
            # record carrying its result cards is Codex's direct completion
            # evidence, including when wrapper output later fails to render.
            status=ToolStatus.COMPLETED if completed else ToolStatus.IN_PROGRESS,
            completed=completed,
            predicate=lambda invocation: self._static_web_matches_extension(
                invocation,
                action_type=action_type,
                query_values=query_values,
            ),
            output=output,
            turn_id=_as_non_empty_str(payload.get("turn_id")),
            started_at=started_at,
            completed_at=completed_at,
            provenance={
                "native_item_type": "Extension",
                "native_item_kind": "web.search",
                "native_action_type": action_type,
                **timing,
            },
        )

    def _handle_native_terminal_item(
        self,
        payload: dict,
        timestamp: datetime,
        state: _ParseState,
        transcript: list[TranscriptRecord],
    ) -> None:
        """Project other completed Codex ThreadItems without wrapper guessing.

        This is intentionally terminal-only. These historical item spellings
        often have no ``item_started`` counterpart, and their completed record
        already contains the authoritative input, output, status, and timing.
        Message/reasoning/runtime item types retain their dedicated transcript
        paths below so this decoder does not manufacture duplicate content.
        """

        item = payload.get("item")
        if not isinstance(item, dict):
            return
        item_type = _as_non_empty_str(item.get("type"))
        native_id = _as_non_empty_str(item.get("id"))
        if item_type is None or native_id is None:
            return

        # These either have a dedicated handler above or are content/runtime
        # records rather than standalone user-visible actions.
        if item_type in {
            "AgentMessage",
            "CommandExecution",
            "ContextCompaction",
            "FileChange",
            "Plan",
            "Reasoning",
            "UserMessage",
            "WebSearch",
            "CollabAgentToolCall",
        }:
            return

        started_at, completed_at, timing = self._native_item_timing(payload, timestamp)
        turn_id = _as_non_empty_str(payload.get("turn_id"))
        status = self._native_terminal_status(item, completed=True)
        input_data: dict[str, Any]
        output: Any = None
        tool_name: str
        native_type = item_type
        provenance: dict[str, Any] = {
            "native_item_type": item_type,
            **timing,
        }

        if item_type == "Extension":
            kind = _as_non_empty_str(item.get("kind"))
            if kind == "web.search":
                # Handled above with action-specific correlation semantics.
                return
            native_type = f"Extension:{kind or 'unknown'}"
            tool_name = f"extension.{kind}" if kind is not None else "extension"
            input_data = {
                key: item[key]
                for key in ("kind", "query", "action")
                if item.get(key) is not None
            }
            output = {
                key: item[key]
                for key in ("results", "result", "error")
                if item.get(key) is not None
            } or None
            provenance["native_item_kind"] = kind

        elif item_type == "McpToolCall":
            server = _as_non_empty_str(item.get("server"))
            tool = _as_non_empty_str(item.get("tool"))
            tool_name = (
                f"mcp__{server}__{tool}"
                if server is not None and tool is not None
                else "mcp_tool_call"
            )
            input_data = {
                key: item[key]
                for key in ("server", "tool", "arguments", "read_only_hint")
                if item.get(key) is not None
            }
            output = {
                key: item[key]
                for key in ("result", "error")
                if item.get(key) is not None
            } or None

        elif item_type == "DynamicToolCall":
            namespace = _as_non_empty_str(item.get("namespace"))
            tool = _as_non_empty_str(item.get("tool"))
            tool_name = (
                f"dynamic__{namespace}__{tool}"
                if namespace is not None and tool is not None
                else "dynamic_tool_call"
            )
            input_data = {
                key: item[key]
                for key in ("namespace", "tool", "arguments")
                if item.get(key) is not None
            }
            output = {
                key: item[key]
                for key in ("content_items", "error")
                if item.get(key) is not None
            } or None

        elif item_type == "ImageView":
            tool_name = "view_image"
            input_data = {"path": item["path"]} if item.get("path") is not None else {}

        elif item_type == "SubAgentActivity":
            kind = _as_non_empty_str(item.get("kind"))
            agent_thread_id = _as_non_empty_str(item.get("agent_thread_id"))
            tool_name = "collab_agent"
            input_data = {
                key: value
                for key, value in {
                    "action": kind,
                    "session": agent_thread_id,
                    "agent_path": item.get("agent_path"),
                }.items()
                if value is not None
            }
            if kind == "started" and agent_thread_id is not None:
                state.spawn_links.setdefault(agent_thread_id, native_id)
            provenance["native_item_kind"] = kind

        else:
            # Preserve a future terminal action rather than silently dropping
            # it. Content/runtime variants above stay excluded deliberately.
            tool_name = f"codex_native.{item_type}"
            input_data = {
                key: value
                for key, value in item.items()
                if key
                not in {
                    "id",
                    "status",
                    "success",
                    "result",
                    "results",
                    "error",
                    "content_items",
                }
            }
            output = {
                key: item[key]
                for key in ("result", "results", "error", "content_items")
                if item.get(key) is not None
            } or None

        self._record_native_activity(
            state=state,
            transcript=transcript,
            timestamp=timestamp,
            native_type=native_type,
            native_id=native_id,
            tool_name=tool_name,
            item_kind="tool_call",
            input_data=input_data,
            status=status,
            completed=True,
            predicate=lambda _invocation: False,
            output=output,
            turn_id=turn_id,
            started_at=started_at,
            completed_at=completed_at,
            provenance=provenance,
        )

    def _handle_native_plan(
        self,
        payload: dict,
        timestamp: datetime,
        state: _ParseState,
        transcript: list[TranscriptRecord],
        *,
        completed: bool,
    ) -> None:
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "Plan":
            return
        native_id = _as_non_empty_str(item.get("id"))
        text = _as_non_empty_str(item.get("text"))
        if native_id is None:
            return
        input_data = {"text": text} if text is not None else {}
        status = _tool_status(
            item.get("status"),
            default=ToolStatus.COMPLETED if completed else ToolStatus.IN_PROGRESS,
        )
        started_at, completed_at, timing = self._native_item_timing(payload, timestamp)
        self._record_native_activity(
            state=state,
            transcript=transcript,
            timestamp=timestamp,
            native_type="Plan",
            native_id=native_id,
            tool_name="update_plan",
            item_kind="plan",
            input_data=input_data,
            status=status,
            completed=completed,
            predicate=lambda invocation: invocation.method == "update_plan",
            turn_id=_as_non_empty_str(payload.get("turn_id")),
            started_at=started_at,
            completed_at=completed_at,
            provenance={"native_item_kind": "Plan", **timing},
        )

    def _handle_native_collab_agent_tool_call(
        self,
        payload: dict,
        timestamp: datetime,
        state: _ParseState,
        transcript: list[TranscriptRecord],
        *,
        completed: bool,
    ) -> None:
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "CollabAgentToolCall":
            return
        native_id = _as_non_empty_str(item.get("id"))
        action = _as_non_empty_str(item.get("tool"))
        if native_id is None or action is None:
            return
        input_data: dict[str, Any] = {"action": action}
        for key in (
            "sender_thread_id",
            "receiver_thread_ids",
            "receiver_agents",
            "agents_states",
            "prompt",
            "model",
            "reasoning_effort",
        ):
            if item.get(key) is not None:
                input_data[key] = item[key]
        status = _tool_status(
            item.get("status"),
            default=ToolStatus.COMPLETED if completed else ToolStatus.IN_PROGRESS,
        )
        started_at, completed_at, timing = self._native_item_timing(payload, timestamp)
        self._record_native_activity(
            state=state,
            transcript=transcript,
            timestamp=timestamp,
            native_type="CollabAgentToolCall",
            native_id=native_id,
            tool_name="spawn_agent" if action == "spawn_agent" else "collab_agent",
            item_kind="tool_call",
            input_data=input_data,
            status=status,
            completed=completed,
            predicate=lambda invocation: (
                action
                in _CODEX_STATIC_COLLAB_NATIVE_TOOLS.get(invocation.method, frozenset())
            ),
            turn_id=_as_non_empty_str(payload.get("turn_id")),
            started_at=started_at,
            completed_at=completed_at,
            provenance={"native_item_kind": "CollabAgentToolCall", **timing},
        )

    def _handle_response_item(
        self,
        payload: dict,
        ts: datetime,
        state: _ParseState,
        transcript: list[TranscriptRecord],
    ) -> None:
        """Project a Codex ``response_item`` record into transcript facts."""
        inner_type = payload.get("type", "")

        if inner_type == "function_call":
            tool_name = payload.get("name")
            tool_input = _parse_json_blob(payload.get("arguments"))
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="assistant",
                    kind="tool_call",
                    data={
                        "tool_name": tool_name,
                        "tool_call_id": payload.get("call_id"),
                        "input": tool_input,
                        "item_kind": _codex_item_kind(
                            tool_name=tool_name, inner_type=inner_type
                        ),
                    },
                )
            )

        elif inner_type == "function_call_output":
            raw_output = payload.get("output")
            output = _parse_json_blob(raw_output)
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="tool",
                    kind="tool_result",
                    data={
                        "tool_call_id": payload.get("call_id"),
                        "exit_code": extract_exit_code(raw_output),
                        "output": output,
                        "status": _tool_result_status(payload, raw_output).value,
                    },
                )
            )

        elif inner_type == "custom_tool_call":
            tool_name = payload.get("name")
            tool_input = _parse_json_blob(payload.get("input"))
            call_record = TranscriptRecord(
                sequence=len(transcript),
                timestamp=ts,
                vendor=Vendor.CODEX_CLI,
                role="assistant",
                kind="tool_call",
                data={
                    "tool_name": tool_name,
                    "tool_call_id": payload.get("call_id"),
                    "input": tool_input,
                    "item_kind": _codex_item_kind(
                        tool_name=tool_name, inner_type=inner_type
                    ),
                },
            )
            transcript.append(call_record)
            call_id = _as_non_empty_str(payload.get("call_id"))
            if tool_name == "exec" and call_id is not None:
                state.exec_wrapper_call_ids.add(call_id)
            invocations = (
                _extract_static_exec_invocations(tool_input)
                if tool_name == "exec"
                else None
            )
            if call_id is not None and invocations is not None:
                state.pending_exec_wrappers[call_id] = _PendingExecWrapper(
                    call_id=call_id,
                    started_at=ts,
                    call_record=call_record,
                    invocations=invocations,
                    turn_id=_as_non_empty_str(state.turn_context.get("turn_id")),
                )

        elif inner_type == "custom_tool_call_output":
            raw_output = payload.get("output")
            call_id = _as_non_empty_str(payload.get("call_id"))
            wrapper_status = _tool_result_status(
                payload,
                raw_output,
                exec_wrapper=(
                    call_id is not None and call_id in state.exec_wrapper_call_ids
                ),
            )
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="tool",
                    kind="tool_result",
                    data={
                        "tool_name": payload.get("name"),
                        "tool_call_id": payload.get("call_id"),
                        "exit_code": extract_exit_code(raw_output),
                        "output": _parse_json_blob(raw_output),
                        "status": wrapper_status.value,
                    },
                )
            )
            self._handle_static_exec_wrapper_output(payload, ts, state, transcript)

        elif inner_type == "tool_search_call":
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="assistant",
                    kind="tool_call",
                    data={
                        "tool_name": "tool_search",
                        "tool_call_id": payload.get("call_id"),
                        "input": payload.get("arguments"),
                        "status": _tool_status(payload.get("status")).value,
                        "item_kind": "tool_call",
                    },
                )
            )

        elif inner_type == "tool_search_output":
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="tool",
                    kind="tool_result",
                    data={
                        "tool_name": "tool_search",
                        "tool_call_id": payload.get("call_id"),
                        "output": payload.get("tools"),
                        "status": _tool_result_status(
                            payload, payload.get("tools")
                        ).value,
                    },
                )
            )

        elif inner_type == "web_search_call":
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="assistant",
                    kind="tool_call",
                    data={
                        "tool_name": "web_search",
                        "tool_call_id": f"web_search:{len(transcript)}",
                        "input": payload.get("action"),
                        "status": _tool_status(
                            payload.get("status"),
                            default=ToolStatus.COMPLETED,
                        ).value,
                        "item_kind": "tool_call",
                    },
                )
            )

        elif inner_type == "local_shell_call":
            command_source = _codex_command_activity_source(payload.get("source"))
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="assistant",
                    kind="tool_call",
                    data={
                        "tool_name": "local_shell",
                        "tool_call_id": payload.get("call_id"),
                        "input": payload.get("action"),
                        "command": payload.get("action"),
                        "status": _tool_status(payload.get("status")).value,
                        "item_kind": "command_execution",
                        "vendor_data": {
                            "activity": {
                                "kind": "command",
                                "source": command_source,
                                "fidelity": "observed_native",
                                "provenance": {
                                    "source": "response_item.local_shell_call",
                                    "source_kind": _as_non_empty_str(
                                        payload.get("source")
                                    ),
                                },
                            }
                        },
                    },
                )
            )

        elif inner_type == "image_generation_call":
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="assistant",
                    kind="tool_call",
                    data={
                        "tool_name": "image_generation",
                        "tool_call_id": payload.get("id"),
                        "input": {"revised_prompt": payload.get("revised_prompt")},
                        "output": payload.get("result"),
                        "status": _tool_status(
                            payload.get("status"),
                            default=ToolStatus.COMPLETED,
                        ).value,
                        "item_kind": "tool_call",
                    },
                )
            )

        elif inner_type == "reasoning":
            state.runtime_observations.append(
                RuntimeObservation(timestamp=ts, kind="reasoning")
            )
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="assistant",
                    kind="tool_call",
                    data={
                        "tool_name": "reasoning",
                        "tool_call_id": f"reasoning:{len(transcript)}",
                        "text": payload.get("content") or payload.get("text"),
                        "item_kind": "reasoning",
                    },
                )
            )
            return

        elif inner_type == "message":
            message_role = payload.get("role")
            if message_role in {"developer", "system"}:
                content = payload.get("content")
                if isinstance(content, list):
                    for index, item in enumerate(content):
                        if not isinstance(item, dict):
                            continue
                        text = item.get("text")
                        if not isinstance(text, str) or not text:
                            continue
                        block_name = _codex_prompt_block_name(text, index)
                        _record_context_source(
                            state,
                            _context_source_observation(
                                timestamp=ts,
                                block=block_name,
                                role=message_role,
                                text=text,
                            ),
                            block=block_name,
                            role=message_role,
                        )
                        transcript.append(
                            TranscriptRecord(
                                sequence=len(transcript),
                                timestamp=ts,
                                vendor=Vendor.CODEX_CLI,
                                role="runtime",
                                kind="runtime",
                                data={
                                    "raw_type": "prompt_block",
                                    "prompt_role": message_role,
                                    "prompt_block": block_name,
                                    "text": text,
                                },
                                fidelity="synthetic",
                            )
                        )
            elif message_role == "user":
                content = payload.get("content")
                if isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        text = item.get("text")
                        if not isinstance(text, str) or not text:
                            continue
                        block_name = _codex_user_prompt_block_name(text)
                        if block_name is None:
                            continue
                        _record_context_source(
                            state,
                            _context_source_observation(
                                timestamp=ts,
                                block=block_name,
                                role=message_role,
                                text=text,
                            ),
                            block=block_name,
                            role=message_role,
                        )
                        transcript.append(
                            TranscriptRecord(
                                sequence=len(transcript),
                                timestamp=ts,
                                vendor=Vendor.CODEX_CLI,
                                role="runtime",
                                kind="runtime",
                                data={
                                    "raw_type": "prompt_block",
                                    "prompt_role": message_role,
                                    "prompt_block": block_name,
                                    "text": text,
                                },
                                fidelity="synthetic",
                            )
                        )
            elif message_role == "assistant":
                phase = payload.get("phase")
                text = _extract_response_text(payload)
                transcript.append(
                    TranscriptRecord(
                        sequence=len(transcript),
                        timestamp=ts,
                        vendor=Vendor.CODEX_CLI,
                        role="assistant",
                        kind="assistant_message",
                        data={
                            "text": text,
                            "phase": phase,
                        },
                    )
                )

    def _handle_compacted(
        self,
        payload: dict,
        ts: datetime,
        state: _ParseState,
        transcript: list[TranscriptRecord],
    ) -> None:
        """Project a Codex ``compacted`` rollout record.

        Codex writes this record via ``replace_compacted_history`` after every
        compaction (local, remote v1/v2, and token-budget). It carries the
        replacement history, window chain metadata, and (for local compaction)
        the summary text. The ``context_compacted`` event_msg already produces
        the runtime observation that drives compaction counting and the
        eviction boundary; this handler ensures the record is not silently
        ignored and records the window metadata for future use.

        The ``replacement_history`` items are intentionally NOT re-projected
        here: they overlap with pre-compaction ``response_item`` records already
        in the transcript, and the eviction boundary (driven by
        ``context_compacted``) correctly marks those originals as evicted.
        Re-projecting would double-count the surviving subset.
        """
        message = _as_non_empty_str(payload.get("message"))
        window_number = payload.get("window_number")
        window_id = _as_non_empty_str(payload.get("window_id"))
        transcript.append(
            TranscriptRecord(
                sequence=len(transcript),
                timestamp=ts,
                vendor=Vendor.CODEX_CLI,
                role="runtime",
                kind="runtime",
                data={
                    "raw_type": "compacted",
                    "compaction_message": message,
                    "window_number": window_number,
                    "window_id": window_id,
                },
                fidelity="synthetic",
            )
        )

    def _handle_event_msg(
        self,
        payload: dict,
        ts: datetime,
        state: _ParseState,
        transcript: list[TranscriptRecord],
    ) -> None:
        """Project a Codex ``event_msg`` record into transcript facts."""
        inner_type = payload.get("type", "")
        turn_id = payload.get("turn_id") or state.turn_context.get("turn_id")

        if inner_type == "item_started":
            self._handle_native_command_execution(
                payload,
                ts,
                state,
                transcript,
                completed=False,
            )
            self._handle_native_file_change(
                payload,
                ts,
                state,
                transcript,
                completed=False,
            )
            self._handle_native_web_search(
                payload,
                ts,
                state,
                transcript,
                completed=False,
            )
            self._handle_native_extension_web_search(
                payload,
                ts,
                state,
                transcript,
                completed=False,
            )
            self._handle_native_plan(
                payload,
                ts,
                state,
                transcript,
                completed=False,
            )
            self._handle_native_collab_agent_tool_call(
                payload,
                ts,
                state,
                transcript,
                completed=False,
            )

        elif inner_type == "item_completed":
            item = payload.get("item")
            if isinstance(item, dict) and item.get("type") == "UserMessage":
                _capture_codex_session_preview(
                    state, _extract_content_text(item.get("content"))
                )
            self._handle_native_command_execution(
                payload,
                ts,
                state,
                transcript,
                completed=True,
            )
            self._handle_native_file_change(
                payload,
                ts,
                state,
                transcript,
                completed=True,
            )
            self._handle_native_web_search(
                payload,
                ts,
                state,
                transcript,
                completed=True,
            )
            self._handle_native_extension_web_search(
                payload,
                ts,
                state,
                transcript,
                completed=True,
            )
            self._handle_native_plan(
                payload,
                ts,
                state,
                transcript,
                completed=True,
            )
            self._handle_native_collab_agent_tool_call(
                payload,
                ts,
                state,
                transcript,
                completed=True,
            )
            self._handle_native_terminal_item(
                payload,
                ts,
                state,
                transcript,
            )

        elif inner_type == "user_message":
            _capture_codex_session_preview(state, _extract_message_text(payload))
            turn_id_text = _as_non_empty_str(turn_id)
            starts_turn = (
                turn_id_text is None or turn_id_text not in state.projected_turn_ids
            )
            if turn_id_text is not None:
                state.projected_turn_ids.add(turn_id_text)
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="user",
                    kind="user_message",
                    data={
                        "turn_id_raw": turn_id,
                        "text": _extract_message_text(payload),
                        "previous_turn_status": TurnStatus.INTERRUPTED.value,
                        "starts_turn": starts_turn,
                    },
                )
            )

        elif inner_type == "agent_message":
            return

        elif inner_type == "task_complete":
            state.runtime_observations.append(
                RuntimeObservation(
                    timestamp=ts,
                    kind="turn_completed",
                    turn_id_raw=_as_non_empty_str(payload.get("turn_id")),
                    duration_ms=(
                        payload.get("duration_ms")
                        if isinstance(payload.get("duration_ms"), int)
                        else None
                    ),
                    time_to_first_token_ms=(
                        payload.get("time_to_first_token_ms")
                        if isinstance(payload.get("time_to_first_token_ms"), int)
                        else None
                    ),
                )
            )
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="runtime",
                    kind="task_complete",
                    data={
                        "turn_id_raw": payload.get("turn_id"),
                        "raw_type": "task_complete",
                        "text": payload.get("last_agent_message"),
                        "status": TurnStatus.COMPLETED.value,
                    },
                    fidelity="synthetic",
                )
            )

        elif inner_type == "token_count":
            info = payload.get("info")
            # Codex occasionally re-emits a token_count snapshot whose
            # cumulative ``total_token_usage`` is byte-identical to the
            # prior event's (a stale re-emission, not a new model call);
            # its ``last_token_usage`` repeats too, so counting it would
            # double-charge the call. Drop it before any accounting.
            total_usage = (
                info.get("total_token_usage") if isinstance(info, dict) else None
            )
            if (
                isinstance(total_usage, dict)
                and total_usage == state.prev_total_token_usage
            ):
                return
            if isinstance(total_usage, dict):
                state.prev_total_token_usage = total_usage
            normalized_metrics = normalize_codex_token_count(
                model=state.turn_context.get("model"),
                info=info,
            )
            usage_record = TranscriptRecord(
                sequence=len(transcript),
                timestamp=ts,
                vendor=Vendor.CODEX_CLI,
                role="runtime",
                kind="usage",
                data={
                    "turn_id_raw": turn_id,
                    "raw_type": "token_count",
                    **normalized_metrics,
                    "vendor_data": {
                        "metrics": normalized_metrics.get("metrics"),
                    }
                    if normalized_metrics.get("metrics")
                    else {},
                },
                fidelity="synthetic",
            )
            observation = context_usage_observation(
                timestamp=ts,
                source="codex_token_count",
                normalized=normalized_metrics,
                source_event_id=usage_record.record_id,
                provider="openai",
            )
            if observation is not None:
                if observation.context_window_tokens is None:
                    observation.context_window_tokens = state.context_window_tokens
                state.context_usage.append(observation)
            transcript.append(usage_record)

        elif inner_type == "context_compacted":
            state.runtime_observations.append(
                RuntimeObservation(timestamp=ts, kind="context_compacted")
            )
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="runtime",
                    kind="runtime",
                    data={
                        "turn_id_raw": turn_id,
                        "raw_type": "context_compacted",
                    },
                    fidelity="synthetic",
                )
            )

        elif inner_type == "turn_aborted":
            state.runtime_observations.append(
                RuntimeObservation(
                    timestamp=ts,
                    kind="turn_aborted",
                    turn_id_raw=_as_non_empty_str(payload.get("turn_id")) or turn_id,
                    duration_ms=(
                        payload.get("duration_ms")
                        if isinstance(payload.get("duration_ms"), int)
                        else None
                    ),
                    reason=_as_non_empty_str(payload.get("reason")),
                )
            )
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="runtime",
                    kind="task_complete",
                    data={
                        "turn_id_raw": payload.get("turn_id") or turn_id,
                        "raw_type": "turn_aborted",
                        "status": TurnStatus.INTERRUPTED.value,
                    },
                    fidelity="synthetic",
                )
            )

        elif inner_type == "thread_rolled_back":
            state.runtime_observations.append(
                RuntimeObservation(
                    timestamp=ts,
                    kind="thread_rolled_back",
                    num_turns=(
                        payload.get("num_turns")
                        if isinstance(payload.get("num_turns"), int)
                        else None
                    ),
                )
            )

        elif inner_type == "task_started":
            context_window = payload.get("model_context_window")
            if isinstance(context_window, int) and not isinstance(context_window, bool):
                state.context_window_tokens = context_window
            state.runtime_observations.append(
                RuntimeObservation(
                    timestamp=ts,
                    kind="turn_started",
                    turn_id_raw=_as_non_empty_str(payload.get("turn_id")) or turn_id,
                    trace_id=_as_non_empty_str(payload.get("trace_id")),
                )
            )
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="runtime",
                    kind="turn_started",
                    data={
                        "turn_id_raw": turn_id,
                        "raw_type": "task_started",
                        "model_context_window": payload.get("model_context_window"),
                        "collaboration_mode_kind": payload.get(
                            "collaboration_mode_kind"
                        ),
                    },
                    fidelity="synthetic",
                )
            )

        elif inner_type == "sub_agent_activity":
            # kind=started carries the spawned child's agent_thread_id
            # (== child session id) and event_id (== spawn tool-call
            # call_id). Record the link so the forked_from edge origin
            # can resolve to the real spawn call, not the parent's last
            # tool call.
            if payload.get("kind") == "started":
                child_id = _as_non_empty_str(payload.get("agent_thread_id"))
                spawn_call_id = _as_non_empty_str(payload.get("event_id"))
                if child_id and spawn_call_id and child_id not in state.spawn_links:
                    state.spawn_links[child_id] = spawn_call_id

    def _handle_session_meta(
        self,
        payload: dict,
        ts: datetime | None,
        state: _ParseState,
        transcript: list[TranscriptRecord],
    ) -> None:
        """Capture the first session_meta record and its base_instructions block."""
        if state.session_meta:
            return
        sid_str = payload.get("id")
        if sid_str:
            try:
                state.session_id = UUID(sid_str)
            except ValueError:
                pass
        state.session_meta = payload
        base_instructions = payload.get("base_instructions")
        base_text = (
            base_instructions.get("text")
            if isinstance(base_instructions, dict)
            else None
        )
        if ts is not None and isinstance(base_text, str) and base_text:
            _record_context_source(
                state,
                _context_source_observation(
                    timestamp=ts,
                    block="base_instructions",
                    role="system",
                    text=base_text,
                ),
                block="base_instructions",
                role="system",
            )
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=ts,
                    vendor=Vendor.CODEX_CLI,
                    role="runtime",
                    kind="runtime",
                    data={
                        "raw_type": "prompt_block",
                        "prompt_role": "system",
                        "prompt_block": "base_instructions",
                        "text": base_text,
                    },
                    fidelity="synthetic",
                )
            )

    def _handle_turn_context(
        self,
        payload: dict,
        ts: datetime | None,
        state: _ParseState,
    ) -> None:
        """Record turn_context and detect reasoning-effort change-points.

        Codex emits a fresh turn_context per turn carrying the active
        ``effort``; a value differing from the prior turn's marks a cache-key
        change (the warm prefix is served from a different effort-bucket cache).
        """
        state.turn_context = payload
        multi_agent_version = _as_non_empty_str(payload.get("multi_agent_version"))
        if multi_agent_version is not None:
            state.multi_agent_version = multi_agent_version
        multi_agent_mode = _as_non_empty_str(payload.get("multi_agent_mode"))
        if multi_agent_mode is not None:
            state.multi_agent_mode = multi_agent_mode
        effort = _as_non_empty_str(payload.get("effort"))
        if (
            effort is not None
            and state.prev_effort is not None
            and effort != state.prev_effort
            and ts is not None
        ):
            state.runtime_observations.append(
                RuntimeObservation(
                    timestamp=ts,
                    kind="effort_changed",
                    turn_id_raw=_as_non_empty_str(payload.get("turn_id")),
                    effort_from=state.prev_effort,
                    effort_to=effort,
                )
            )
        if effort is not None:
            state.prev_effort = effort
