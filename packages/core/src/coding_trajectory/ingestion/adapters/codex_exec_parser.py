"""Static parser for Codex ``exec`` wrapper JavaScript.

Reconstructs the nested tool calls inside a ``tools.*``/``Promise.all`` code
cell without evaluating it; template literals and anything dynamic fail
closed. Extracted from the Codex adapter, which consumes only
:class:`StaticExecInvocation` and :func:`extract_static_exec_invocations`.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

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
        "write_stdin",
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
_CODEX_WEB_BROWSE_OPERATIONS: frozenset[str] = frozenset(
    {"click", "find", "open", "screenshot"}
)


class StaticExecInvocation(BaseModel):
    """One non-evaluated nested tool call inside a Codex ``exec`` wrapper."""

    method: str
    tool_name: str
    item_kind: str
    input: Any = None
    command: str | None = None
    cwd: str | None = None
    source_offset: int


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
) -> StaticExecInvocation | None:
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
        return StaticExecInvocation(
            method=method,
            tool_name="exec_command",
            item_kind="command_execution",
            input=command_input,
            command=command,
            cwd=cwd,
            source_offset=source_offset,
        )

    if method == "write_stdin":
        # ``write_stdin`` has a stable transport shape.  In particular, an
        # empty ``chars`` payload is Codex's background-terminal poll. Codex
        # rollouts have used both ``process_id`` and ``session_id`` as the
        # terminal identity; preserve which identity field was supplied so
        # activity projection never conflates their numeric values.
        if not isinstance(argument, dict):
            return None
        identities = [
            (key, argument.get(key))
            for key in ("process_id", "session_id")
            if key in argument
        ]
        chars = argument.get("chars")
        identity = identities[0][1] if len(identities) == 1 else None
        if (
            len(identities) != 1
            or not isinstance(identity, (str, int))
            or isinstance(identity, bool)
            or (isinstance(identity, str) and not identity.strip())
            or (isinstance(identity, int) and identity <= 0)
            or not isinstance(chars, str)
        ):
            return None
        return StaticExecInvocation(
            method=method,
            tool_name="write_stdin",
            item_kind="command_execution",
            input=argument,
            source_offset=source_offset,
        )

    if method == "web__run":
        if not isinstance(argument, dict):
            return None
        tool_name = _static_web_tool_name(argument)
        if tool_name is None:
            return None
        return StaticExecInvocation(
            method=method,
            tool_name=tool_name,
            item_kind="tool_call",
            input=argument,
            source_offset=source_offset,
        )

    if method == "apply_patch":
        patch_input = argument if isinstance(argument, dict) else {"patch": argument}
        return StaticExecInvocation(
            method=method,
            tool_name="apply_patch",
            item_kind="file_change",
            input=patch_input,
            source_offset=source_offset,
        )

    if method == "update_plan":
        if not isinstance(argument, dict):
            return None
        return StaticExecInvocation(
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
        return StaticExecInvocation(
            method=method,
            tool_name="spawn_agent" if method == "spawn_agent" else "collab_agent",
            item_kind="tool_call",
            input=input_data,
            source_offset=source_offset,
        )

    return None


def _parse_static_exec_tool_call(
    source: str, offset: int
) -> tuple[StaticExecInvocation, int] | None:
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
) -> tuple[list[StaticExecInvocation], int] | None:
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
    invocations: list[StaticExecInvocation] = []
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


def _is_safe_text_await(source: str, tokens: list[tuple[str, int, int]]) -> bool:
    """Recognize only ``text(await tools.apply_patch(...))`` containment.

    Codex commonly displays a patch result through ``text``.  This is not a
    general expression parser: accepting arbitrary parenthesized awaits would
    allow a wrapper's control flow to be mistaken for a direct nested call.
    """

    if len(tokens) < 3 or tokens[-3][0] != "text" or tokens[-2][0] != "await":
        return False
    between = _skip_js_trivia(source, tokens[-3][2])
    return between is not None and between < len(source) and source[between] == "("


def extract_static_exec_invocations(value: Any) -> list[StaticExecInvocation] | None:
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

    invocations: list[StaticExecInvocation] = []
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
        previous = _previous_non_trivia_character(value, tokens[-2][1])
        direct_await = previous in {None, "=", ";"}
        text_await = previous == "(" and _is_safe_text_await(value, tokens)
        if not direct_await and not text_await:
            return None
        parsed = _parse_static_exec_tool_call(value, tokens[-1][1])
        if parsed is None:
            return None
        invocation, offset = parsed
        # A parenthesized await is safe only in the narrow patch-result form
        # above.  Keep all other nested expression forms fail closed.
        if text_await and invocation.method != "apply_patch":
            return None
        invocations.append(invocation)

    return invocations or None
