"""Vendor-specific project-metadata discovery.

Extracts per-vendor project-metadata functions from
:mod:`coding_trajectory.discovery` so the main discovery module reads as
orchestration over vendor configs. Each vendor keeps its config-file /
directory-layout parsing local (PRD: "vendor-specific path/logic should be
isolated"):

- Codex: ``~/.codex/config.toml`` project list
- Claude Code: ``~/.claude/projects/<encoded-cwd>/`` directory listing
- Pi: ``~/.pi/agent/sessions/**/*.jsonl`` session-header cwd extraction

These are internal helpers re-imported by :mod:`coding_trajectory.discovery`;
``ProjectDiscoveryItem`` is re-exported for backwards compatibility.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from coding_trajectory.discovery_paths import (
    _classify_project_path,
    _decode_claude_encoded_path,
    _is_recent_enough,
    _project_identifier_from_path,
    _project_scope_matches_path,
)
from coding_trajectory.ingestion.common import normalize_project_key
from coding_trajectory.ingestion.models import Vendor


@dataclass(frozen=True, slots=True)
class ProjectDiscoveryItem:
    project_identifier: str
    path: Path | None
    source_path: Path
    vendor: Vendor
    category: str = "project"


def _metadata_item(
    *,
    project_path: Path | None,
    source_path: Path,
    vendor: Vendor,
    current_dir: Path,
    scoped_project: str | None,
    scoped_project_key: str | None,
) -> ProjectDiscoveryItem | None:
    project_identifier = _project_identifier_from_path(project_path)
    if project_identifier is None:
        if scoped_project is None:
            return None
        project_identifier = scoped_project

    key = normalize_project_key(project_identifier)
    if not key:
        return None
    if scoped_project_key and (
        project_path is None
        or not _project_scope_matches_path(
            project_path, current_dir, scoped_project, scoped_project_key
        )
    ):
        return None

    if project_path is not None:
        category = _classify_project_path(project_path)
        if category is None:
            return None
    else:
        category = "project"

    return ProjectDiscoveryItem(
        project_identifier=key,
        path=project_path,
        source_path=source_path,
        vendor=vendor,
        category=category,
    )


def _codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex"


def _codex_config_project_metadata(
    *,
    current_dir: Path,
    scoped_project: str | None,
    scoped_project_key: str | None,
    modified_since: datetime | None,
) -> list[ProjectDiscoveryItem]:
    config_path = _codex_home() / "config.toml"
    if not config_path.is_file() or not _is_recent_enough(config_path, modified_since):
        return []
    try:
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return []

    projects = config.get("projects")
    if not isinstance(projects, dict):
        return []

    items: list[ProjectDiscoveryItem] = []
    for raw_path in projects:
        if not isinstance(raw_path, str) or not raw_path:
            continue
        project_path = Path(raw_path).expanduser()
        item = _metadata_item(
            project_path=project_path,
            source_path=config_path,
            vendor=Vendor.CODEX_CLI,
            current_dir=current_dir,
            scoped_project=scoped_project,
            scoped_project_key=scoped_project_key,
        )
        if item is not None:
            items.append(item)
    return items


def _claude_project_dir_metadata(
    *,
    current_dir: Path,
    scoped_project: str | None,
    scoped_project_key: str | None,
    modified_since: datetime | None,
) -> list[ProjectDiscoveryItem]:
    base = Path.home() / ".claude" / "projects"
    if not base.is_dir():
        return []

    items: list[ProjectDiscoveryItem] = []
    for path in sorted(base.iterdir()):
        if not path.is_dir() or not _is_recent_enough(path, modified_since):
            continue
        decoded = _decode_claude_encoded_path(path.name)
        project_path = Path(decoded) if decoded else None
        item = _metadata_item(
            project_path=project_path,
            source_path=path,
            vendor=Vendor.CLAUDE_CODE,
            current_dir=current_dir,
            scoped_project=scoped_project,
            scoped_project_key=scoped_project_key,
        )
        if item is not None:
            items.append(item)
    return items


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


def _pi_session_root() -> tuple[Path, bool]:
    configured = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
    if configured:
        return Path(configured).expanduser(), True

    agent_dir = _pi_agent_dir()
    settings_path = agent_dir / "settings.json"
    try:
        with settings_path.open(encoding="utf-8") as handle:
            settings = json.load(handle)
    except (OSError, json.JSONDecodeError):
        settings = {}
    if isinstance(settings, dict):
        session_dir = settings.get("sessionDir")
        if isinstance(session_dir, str) and session_dir:
            return _resolve_pi_settings_path(session_dir, base_dir=agent_dir), True

    return agent_dir / "sessions", False


def _pi_session_files(session_root: Path, *, custom_session_dir: bool) -> list[Path]:
    if not session_root.is_dir():
        return []
    if custom_session_dir:
        return sorted(
            path
            for path in session_root.iterdir()
            if path.is_file() and path.suffix == ".jsonl"
        )

    files: list[Path] = []
    for project_dir in sorted(session_root.iterdir()):
        if not project_dir.is_dir():
            continue
        files.extend(
            sorted(
                path
                for path in project_dir.iterdir()
                if path.is_file() and path.suffix == ".jsonl"
            )
        )
    return files


def _pi_project_path_from_session_header(path: Path) -> Path | None:
    try:
        with path.open(encoding="utf-8") as handle:
            for raw_line in handle:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError:
                    return None
                if record.get("type") != "session":
                    return None
                cwd = record.get("cwd")
                return Path(cwd) if isinstance(cwd, str) and cwd else None
    except OSError:
        return None
    return None


def _pi_session_project_metadata(
    *,
    current_dir: Path,
    scoped_project: str | None,
    scoped_project_key: str | None,
    modified_since: datetime | None,
) -> list[ProjectDiscoveryItem]:
    session_root, custom_session_dir = _pi_session_root()
    items: list[ProjectDiscoveryItem] = []
    for path in _pi_session_files(session_root, custom_session_dir=custom_session_dir):
        if not _is_recent_enough(path, modified_since):
            continue
        project_path = _pi_project_path_from_session_header(path)
        item = _metadata_item(
            project_path=project_path,
            source_path=path,
            vendor=Vendor.PI,
            current_dir=current_dir,
            scoped_project=scoped_project,
            scoped_project_key=scoped_project_key,
        )
        if item is not None:
            items.append(item)
    return items
