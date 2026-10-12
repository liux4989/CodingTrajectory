"""Syntax evidence for external calls, never a reconstruction of execution."""

from __future__ import annotations

import json
from typing import Any

import tree_sitter_javascript
from pydantic import BaseModel
from tree_sitter import Language, Node, Parser


class SourceCall(BaseModel):
    tool_name: str
    expression: str
    source_byte_start: int
    source_byte_end: int
    arguments: Any = None
    arguments_static: bool = False


def _literal(node: Node) -> tuple[Any, bool]:
    text = node.text.decode("utf-8")
    if node.type == "string":
        # Decode literal escapes without evaluating JavaScript. Single quotes
        # and bare object keys are handled by the syntax tree, not regexes.
        raw = text[1:-1]
        raw = raw.replace('\\"', '"') if text.startswith("'") else raw
        if text.startswith("'"):
            raw = raw.replace("\\'", "'").replace('"', '\\"')
        try:
            return json.loads('"' + raw + '"'), True
        except ValueError:
            return None, False
    if node.type in {"number", "true", "false", "null"}:
        try:
            return json.loads(text), True
        except ValueError:
            return None, False
    if node.type == "object":
        result = {}
        for child in node.named_children:
            if child.type == "comment":
                continue
            if child.type != "pair":
                return None, False
            key = child.child_by_field_name("key")
            value = child.child_by_field_name("value")
            if key is None or value is None:
                return None, False
            name, valid = (
                _literal(key)
                if key.type == "string"
                else (key.text.decode(), key.type == "property_identifier")
            )
            decoded, static = _literal(value)
            if not valid or not static or name in result:
                return None, False
            result[name] = decoded
        return result, True
    if node.type == "array":
        children = [n for n in node.named_children if n.type != "comment"]
        values = [_literal(n) for n in children]
        # Array holes are not JSON-shaped argument evidence.
        previous = "["
        holes = False
        for child in node.children:
            if child.type == "comment":
                continue
            if child.type == "," and previous in {"[", ","}:
                holes = True
            previous = child.type
        if any(not valid for _, valid in values) or holes:
            return None, False
        return [value for value, _ in values], True
    return None, False


def _tool_member(node: Node) -> str | None:
    if node.type not in {"member_expression", "subscript_expression"}:
        return None
    owner = node.child_by_field_name("object")
    member = node.child_by_field_name("property") or node.child_by_field_name("index")
    if owner is None or member is None:
        return None
    if owner.type == "identifier" and owner.text == b"tools":
        prefix = ""
    else:
        prefix = _tool_member(owner)
        if prefix is None:
            return None
    if member.type == "property_identifier":
        name = member.text.decode()
    else:
        name, valid = _literal(member)
        if not valid or not isinstance(name, str):
            return None
    return f"{prefix}.{name}" if prefix else name


def source_calls(source: str) -> tuple[list[SourceCall], str]:
    """Find tools.* call sites anywhere in valid JS, including control flow.

    No tool allowlist, source execution, branch expansion, or promise ordering.
    Dynamic member names and aliases remain unresolved in the full source.
    """
    tree = Parser(Language(tree_sitter_javascript.language())).parse(source.encode())
    calls = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.named_children))
        if node.type != "call_expression":
            continue
        function = node.child_by_field_name("function")
        args = node.child_by_field_name("arguments")
        name = _tool_member(function) if function is not None else None
        if name is None or args is None or node.has_error:
            continue
        children = [n for n in args.named_children if n.type != "comment"]
        value, static = _literal(children[0]) if len(children) == 1 else (None, False)
        calls.append(
            SourceCall(
                tool_name=name,
                expression=node.text.decode(),
                source_byte_start=node.start_byte,
                source_byte_end=node.end_byte,
                arguments=value,
                arguments_static=static,
            )
        )
    # Even a valid tree cannot resolve aliases or dynamically selected tools.
    return calls, "partial" if not tree.root_node.has_error else "syntax_errors"


def argument_strings(value: Any) -> set[str]:
    """Stable literal subjects for conservative, explicitly inferred matching."""
    if isinstance(value, str):
        return {value} if len(value) >= 4 else set()
    if isinstance(value, dict):
        return set().union(*(argument_strings(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(argument_strings(v) for v in value))
    return set()
