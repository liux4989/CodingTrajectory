"""Recorded tool labels and shell tokenization shared by evidence views."""

from __future__ import annotations

import shlex


def tool_identity_label(name: str) -> str:
    """Keep operations distinguishable in bounded provider-qualified labels.

    Double-underscore namespaces are structural evidence, not a semantic
    mapping. The grouping key still uses the complete recorded name.
    """
    parts = name.split("__")
    if len(parts) > 1 and all(parts):
        return f"{parts[-1]} ({'.'.join(parts[:-1])})"
    return name


def safe_split(cmd: str) -> list[str]:
    """Parse shell argument tokens without executing source."""
    try:
        return shlex.split(cmd, posix=True)
    except ValueError:
        return cmd.split()
