"""Dashboard-owned shell quote parsing helpers.

Split out from the core analysis shell classifier so the dashboard can reuse
the quote-aware stage splitter for its own command inspection tooling.
"""

from __future__ import annotations


def split_shell_stages(cmd: str) -> list[str]:
    """Split a shell command string on pipeline/stage separators.

    Honours single and double quotes and backslash escapes so separators
    inside quoted regions are preserved.
    """
    stages: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(cmd):
                buf.append(cmd[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < len(cmd):
            buf.append(ch)
            buf.append(cmd[i + 1])
            i += 2
            continue
        if ch == "&" and i + 1 < len(cmd) and cmd[i + 1] == "&":
            stages.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch == "|" and i + 1 < len(cmd) and cmd[i + 1] == "|":
            stages.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch == "|" or ch == ";":
            stages.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    stages.append("".join(buf))
    return stages
