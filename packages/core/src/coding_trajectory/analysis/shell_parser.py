"""Quote-aware parsing helpers for shell command analysis."""

from __future__ import annotations


def split_shell_stages(command: str) -> list[str]:
    """Split a shell command on unquoted pipeline and stage separators."""
    stages: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if quote:
            buffer.append(char)
            if char == "\\" and index + 1 < len(command):
                buffer.append(command[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            buffer.append(char)
            index += 1
            continue
        if char == "\\" and index + 1 < len(command):
            buffer.append(char)
            buffer.append(command[index + 1])
            index += 2
            continue
        if (
            char in {"&", "|"}
            and index + 1 < len(command)
            and command[index + 1] == char
        ):
            stages.append("".join(buffer))
            buffer = []
            index += 2
            continue
        if char in {"|", ";"}:
            stages.append("".join(buffer))
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1
    stages.append("".join(buffer))
    return stages
