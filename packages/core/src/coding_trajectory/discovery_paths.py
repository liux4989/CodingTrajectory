"""Pure path utilities for log discovery.

Leaf helpers with no dependency on :mod:`coding_trajectory.discovery`:
project-marker classification, vendor CWD path encoding/decoding, and the
modified-since recency check. Kept in a sibling module so the main
discovery module reads as orchestration over vendor configs, and so the
vendor-specific encoding rules (Claude Code / Pi replace ``/`` with
``-`` and prepend ``-``) live in one auditable place.

These are internal helpers re-imported by :mod:`coding_trajectory.discovery`;
they are not part of the public discovery API.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from coding_trajectory.ingestion.common import normalize_project_key

_PROJECT_MARKERS = (
    ".git",
    ".hg",
    ".svn",
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
)

_SYSTEM_ROOTS = (Path("/tmp"), Path("/var"), Path("/private"), Path("/usr"))


def _has_project_marker(path: Path) -> bool:
    home = Path.home()
    for ancestor in (path, *path.parents):
        for marker in _PROJECT_MARKERS:
            if (ancestor / marker).exists():
                return True
        if ancestor == home or ancestor == ancestor.parent:
            break
    return False


def _classify_project_path(project_path: Path) -> str | None:
    """Classify a recorded path as a real project, a temporary chat, or junk.

    Returns ``"project"``, ``"temporary"``, or ``None`` (filter out).
    """
    resolved = project_path.expanduser()
    if resolved == Path.home() or resolved == Path(resolved.anchor):
        return None
    if any(part.startswith(".") and part != ".." for part in resolved.parts):
        return None
    for sysroot in _SYSTEM_ROOTS:
        if resolved == sysroot or sysroot in resolved.parents:
            return None
    if _has_project_marker(resolved):
        return "project"
    return "temporary"


def _ancestor_dirs_up_to_project_marker(start: Path) -> list[Path]:
    """Return start and its ancestors up to (and including) the first project marker.

    Stops at home directory or filesystem root.
    """
    home = Path.home()
    ancestors: list[Path] = []
    current = start.resolve()

    while True:
        ancestors.append(current)

        # Stop if we found a project marker at this level
        has_marker = any((current / marker).exists() for marker in _PROJECT_MARKERS)
        if has_marker:
            break

        # Stop at home or root
        if current == home or current == current.parent:
            break

        current = current.parent

    return ancestors


def _encode_claude_project_path(project_path: Path) -> str:
    """Encode an absolute path into Claude Code's directory naming format.

    Replaces '/' with '-' and prepends '-' for the leading slash.
    Example: /Users/foo/bar -> -Users-foo-bar
    """
    absolute = str(project_path.resolve())
    return "-" + absolute.lstrip("/").replace("/", "-")


def _encode_pi_project_path(project_path: Path) -> str:
    """Encode an absolute path into Pi's directory naming format.

    Same encoding as Claude Code: replace '/' with '-', prepend '-'.
    """
    absolute = str(project_path.resolve())
    return "-" + absolute.lstrip("/").replace("/", "-")


def _decode_claude_encoded_path(encoded: str) -> str | None:
    """Decode a Claude Code encoded CWD path.

    Claude Code stores sessions under .claude/projects/<encoded-cwd>/ where the
    CWD is encoded by replacing every '/' with '-'.  When a directory name itself
    contains a hyphen (e.g. 'gh-worktree') the encoding is ambiguous.

    We resolve the ambiguity by greedily walking the real filesystem: at each
    level we try the shortest token-sequence that names an existing child, which
    matches the common case of simple names before reaching hyphenated ones.
    """
    if not encoded:
        return None
    # Leading '-' represents the leading '/' of an absolute path.
    stripped = encoded.lstrip("-")
    tokens = stripped.split("-")

    def _walk(current: Path, idx: int) -> str | None:
        if idx == len(tokens):
            return str(current)
        for end in range(idx + 1, len(tokens) + 1):
            segment = "-".join(tokens[idx:end])
            candidate = current / segment
            if candidate.exists():
                result = _walk(candidate, end)
                if result is not None:
                    return result
        return None

    resolved = _walk(Path("/"), 0)
    if resolved:
        return resolved
    # Fallback: simple replacement (original behaviour) so we never regress on
    # paths where the directory no longer exists on this machine.
    return "/" + stripped.replace("-", "/")


def _is_recent_enough(path: Path, modified_since: datetime | None) -> bool:
    if modified_since is None:
        return True
    try:
        modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return False
    return modified_at >= modified_since


def _project_identifier_from_path(project_path: Path | None) -> str | None:
    if project_path is None:
        return None
    name = project_path.name
    return name if name else None


def _project_scope_matches_path(
    project_path: Path,
    current_dir: Path,
    scoped_project: str | None,
    scoped_project_key: str,
) -> bool:
    try:
        resolved = project_path.resolve()
    except OSError:
        resolved = project_path
    if resolved == current_dir:
        return True
    if normalize_project_key(resolved.name) == scoped_project_key:
        return True
    if scoped_project and normalize_project_key(
        scoped_project
    ) == normalize_project_key(resolved.name):
        return True
    return False
