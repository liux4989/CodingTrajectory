"""Bounded argument evidence, independent of tool-name categorization."""

from __future__ import annotations

import json
import re
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field

from coding_trajectory.analysis.projection_utils import truncate_text_preview
from coding_trajectory.ingestion.models import Item

_LIMIT = 280
_SECRET_KEY = re.compile(
    r"(?i)(?:^|[_-])(?:password|passwd|secret|api[_-]?key|authorization|cookie|"
    r"token|access_token|refresh_token|invitation|invite_url)(?:$|[_-])"
)


class ToolArgumentDetail(BaseModel):
    description: str = Field(max_length=_LIMIT)
    kind: Literal["command", "tool"]
    scope: str | None = Field(default=None, max_length=_LIMIT)
    truncated: bool = False


def sanitize_argument_preview(value: str, *, cwd: str | None = None) -> str | None:
    """Redact credentials and bound text before any display or retention."""
    normalized = re.sub(
        r"https?://[^\s'\"\\]+", lambda match: _safe_url(match.group(0)), value
    )
    normalized = re.sub(
        r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/-]+=*", r"\1 [redacted]", normalized
    )
    normalized = re.sub(
        r"(?i)((?:[\"']?(?:password|passwd|token|secret|api[-_]?key|authorization|cookie)"
        r"[\"']?\s*[:=]\s*)|--(?:password|passwd|token|secret|api[-_]?key|authorization|cookie)\s+)"
        r"(\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        r"\1[redacted]",
        normalized,
    )
    return truncate_text_preview(normalized, max_len=_LIMIT) or None


def _safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        return "[url]"
    return urlunsplit((parsed.scheme, f"{host}{port}", parsed.path, "", ""))


def tool_argument_detail(item: Item) -> ToolArgumentDetail | None:
    """Extract literal arguments; never infer a semantic tool concept."""
    value = (
        item.command
        if item.kind == "command_execution"
        else getattr(item, "input", None)
    )
    if value is None:
        return None
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        # Providers may serialize arguments as JSON text. Parse only complete
        # JSON values; code and shell text stay literal evidence.
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            pass
    if item.kind == "command_execution":
        command = value if isinstance(value, str) else None
        if isinstance(value, dict):
            command = next(
                (
                    value[key]
                    for key in ("cmd", "command", "shell")
                    if isinstance(value.get(key), str) and value[key].strip()
                ),
                None,
            )
        if command:
            # Preserve quoted arguments, pipeline operators, and statement
            # boundaries. Whitespace compaction must not make a heredoc script
            # look like a single shell command or change its argument quoting.
            preview_command = command.replace("\r\n", "\n").replace("\n", " \\n ")
            scope = (
                value.get("workdir") or value.get("cwd")
                if isinstance(value, dict)
                else None
            )
            safe_scope = (
                sanitize_argument_preview(scope) if isinstance(scope, str) else None
            )
            if safe_scope:
                scope_key = "workdir" if value.get("workdir") else "cwd"
                preview_command += f"; {scope_key}={safe_scope}"
            description = sanitize_argument_preview(preview_command)
            return (
                ToolArgumentDetail(
                    description=description,
                    kind="command",
                    scope=safe_scope,
                    truncated=len(" ".join(preview_command.split())) > _LIMIT,
                )
                if description
                else None
            )

    leaves: list[tuple[str, str, bool]] = []
    omitted = False

    def visit(current: Any, path: str, depth: int) -> None:
        nonlocal omitted
        if len(leaves) >= 32 or depth > 6:
            omitted = True
            return
        if isinstance(current, dict):
            for index, (key, child) in enumerate(current.items()):
                if index >= 16:
                    omitted = True
                    break
                key = str(key)
                if _SECRET_KEY.search(key):
                    child = "[redacted]"
                visit(child, f"{path}.{key}" if path else key, depth + 1)
        elif isinstance(current, list):
            omitted |= len(current) > 4
            for index, child in enumerate(current[:4]):
                visit(child, f"{path}[{index}]", depth + 1)
        elif isinstance(current, str) and current.strip():
            leaves.append((path, current, True))
        elif current is not None and isinstance(current, (int, float, bool)):
            leaves.append((path, str(current), False))

    visit(value, "", 0)
    # Prefer actual text over envelope enums and numeric transport controls.
    # Root arguments precede nested copies of the same provider evidence.
    leaves.sort(
        key=lambda leaf: (
            not leaf[2],
            leaf[0].rsplit(".", 1)[-1] in {"kind", "type", "status"},
            leaf[1].rstrip().endswith(("...", "…")),
            leaf[0].count(".") + leaf[0].count("["),
        )
    )
    seen: set[str] = set()
    parts: list[str] = []
    for path, content, _ in leaves:
        if content in seen:
            continue
        seen.add(content)
        parts.append(f"{path}={content}" if path else content)
    literal = "; ".join(parts)
    description = sanitize_argument_preview(literal)
    return (
        ToolArgumentDetail(
            description=description,
            kind="tool",
            truncated=omitted or len(" ".join(literal.split())) > _LIMIT,
        )
        if description
        else None
    )
