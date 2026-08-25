"""Vendor metadata path resolution for cleanup targets (codex/claude/pi)."""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path


def _remove_codex_project_config_entry(config_path: Path, project_path: str) -> None:
    text = config_path.read_text(encoding="utf-8")
    header = f'[projects."{project_path}"]'
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    index = 0
    removed = False
    while index < len(lines):
        if lines[index].strip() != header:
            output.append(lines[index])
            index += 1
            continue
        removed = True
        index += 1
        while index < len(lines) and not lines[index].lstrip().startswith("["):
            index += 1
    if removed:
        config_path.write_text("".join(output), encoding="utf-8")


# ---------------------------------------------------------------------------
# Path discovery helpers
# ---------------------------------------------------------------------------


def _codex_config_paths_by_project() -> dict[str, list[Path]]:
    paths_by_project: dict[str, set[Path]] = {}
    config_path = _codex_config_path()
    if not config_path.is_file():
        return {}
    try:
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}

    projects = config.get("projects")
    if not isinstance(projects, dict):
        return {}
    for raw_path in projects:
        if not isinstance(raw_path, str) or not raw_path:
            continue
        project_name = Path(raw_path).expanduser().name
        if not project_name:
            continue
        paths_by_project.setdefault(_normalize_project_key(project_name), set()).add(
            config_path
        )
    return {project: sorted(paths) for project, paths in paths_by_project.items()}


def _claude_metadata_paths_by_project() -> dict[str, list[Path]]:
    paths_by_project: dict[str, set[Path]] = {}
    base = Path.home() / ".claude" / "projects"
    if not base.is_dir():
        return {}
    for path in base.iterdir():
        if not path.is_dir():
            continue
        decoded = _decode_claude_encoded_path(path.name)
        if decoded is None:
            continue
        project_name = Path(decoded).name
        if not project_name:
            continue
        paths_by_project.setdefault(_normalize_project_key(project_name), set()).add(
            path
        )
    return {project: sorted(paths) for project, paths in paths_by_project.items()}


def _decode_claude_encoded_path(encoded: str) -> str | None:
    if not encoded:
        return None
    stripped = encoded.lstrip("-")
    if not stripped:
        return None
    return "/" + stripped.replace("-", "/")


def _pi_metadata_paths_by_project() -> dict[str, list[Path]]:
    paths_by_project: dict[str, set[Path]] = {}
    session_root, custom_session_dir = _pi_session_root()
    if not session_root.is_dir():
        return {}

    if custom_session_dir:
        for path in session_root.iterdir():
            if not path.is_file() or path.suffix != ".jsonl":
                continue
            project_key = _project_key_from_pi_session_file(path)
            if project_key:
                paths_by_project.setdefault(project_key, set()).add(path)
    else:
        for path in session_root.iterdir():
            if not path.is_dir():
                continue
            project_key = _project_key_from_pi_project_dir(path)
            if project_key:
                paths_by_project.setdefault(project_key, set()).add(path)

    return {project: sorted(paths) for project, paths in paths_by_project.items()}


def _pi_session_root() -> tuple[Path, bool]:
    configured = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
    if configured:
        return Path(configured).expanduser(), True

    agent_dir = _pi_agent_dir()
    settings_path = agent_dir / "settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        settings = {}
    if isinstance(settings, dict):
        session_dir = settings.get("sessionDir")
        if isinstance(session_dir, str) and session_dir:
            return _resolve_pi_settings_path(session_dir, base_dir=agent_dir), True

    return agent_dir / "sessions", False


def _pi_agent_dir() -> Path:
    configured = os.environ.get("PI_CODING_AGENT_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".pi" / "agent"


def _resolve_pi_settings_path(raw_path: str, *, base_dir: Path) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return base_dir / path


def _project_key_from_pi_project_dir(path: Path) -> str | None:
    for session_file in sorted(path.iterdir()):
        if not session_file.is_file() or session_file.suffix != ".jsonl":
            continue
        project_key = _project_key_from_pi_session_file(session_file)
        if project_key:
            return project_key
    decoded = _decode_pi_encoded_path(path.name)
    if decoded is None:
        return None
    project_name = Path(decoded).name
    return _normalize_project_key(project_name) if project_name else None


def _project_key_from_pi_session_file(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8") as handle:
            for raw_line in handle:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                record = json.loads(stripped)
                if not isinstance(record, dict) or record.get("type") != "session":
                    return None
                cwd = record.get("cwd")
                if not isinstance(cwd, str) or not cwd:
                    return None
                project_name = Path(cwd).expanduser().name
                return _normalize_project_key(project_name) if project_name else None
    except (OSError, json.JSONDecodeError):
        return None
    return None


def _decode_pi_encoded_path(encoded: str) -> str | None:
    stripped = encoded.strip("-")
    if not stripped:
        return None
    return "/" + stripped.replace("-", "/")


def _codex_config_path() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser() / "config.toml"
    return Path.home() / ".codex" / "config.toml"


def _normalize_project_key(value: str) -> str:
    collapsed = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", value.strip())
    return re.sub(r"[^a-zA-Z0-9]+", "-", collapsed).strip("-").lower()


# ---------------------------------------------------------------------------
# Payload construction
# ---------------------------------------------------------------------------
