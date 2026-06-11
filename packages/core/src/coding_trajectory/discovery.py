"""Auto-discovery of local coding-agent logs for the current project."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from coding_trajectory.ingestion import ClaudeCodeAdapter, CodexAdapter, PiAdapter
from coding_trajectory.ingestion.adapters.base import BaseAdapter, SessionHeader
from coding_trajectory.ingestion.common import normalize_project_key
from coding_trajectory.ingestion.models import Event, Item, Session, SessionGraph, Turn, Vendor
from coding_trajectory.query import DocumentError, DocumentStore
from coding_trajectory.ingestion.graph import assemble_project_session_graphs


@dataclass(frozen=True, slots=True)
class DiscoverySource:
    vendor: Vendor
    path: Path
    root_session_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    store: DocumentStore
    sources: list[DiscoverySource]


@dataclass(frozen=True, slots=True)
class ProjectDiscoveryItem:
    project_identifier: str
    path: Path | None
    source_path: Path
    vendor: Vendor
    category: str = "project"


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


def _vendor_configs() -> list[tuple[Vendor, type[BaseAdapter], Path, str]]:
    home = Path.home()
    return [
        (Vendor.CODEX_CLI, CodexAdapter, home / ".codex" / "sessions", "*.jsonl"),
        (Vendor.CLAUDE_CODE, ClaudeCodeAdapter, home / ".claude" / "projects", "*.jsonl"),
        (Vendor.PI, PiAdapter, home / ".pi" / "agent" / "sessions", "*.jsonl"),
    ]


def _selected_vendor_configs(agent_vendor: str | None) -> list[tuple[Vendor, type[BaseAdapter], Path, str]]:
    configs = _vendor_configs()
    if agent_vendor is None:
        return configs
    return [config for config in configs if config[0].value == agent_vendor]


def discover_store(
    *,
    current_dir: Path,
    global_scope: bool = False,
    project_name: str | None = None,
    since_days: int | None = None,
    modified_since: datetime | None = None,
    agent_vendor: str | None = None,
) -> DiscoveryResult:
    current_dir = current_dir.resolve()
    scoped_project = project_name or (None if global_scope else current_dir.name)
    scoped_project_key = normalize_project_key(scoped_project) if scoped_project else None
    modified_since = _modified_since(since_days, modified_since=modified_since)

    sessions_by_project: dict[str, list[Session]] = {}
    path_session_meta: list[tuple[Vendor, Path, UUID]] = []

    for vendor, adapter_cls, base_dir, pattern in _selected_vendor_configs(agent_vendor):
        for path in _candidate_files(
            vendor,
            base_dir,
            pattern,
            current_dir=current_dir,
            scoped_project=scoped_project,
            scoped_project_key=scoped_project_key,
            modified_since=modified_since,
        ):
            adapter = adapter_cls()
            try:
                session = stabilize_session(adapter.ingest_file(path), vendor=vendor, source=path)
            except Exception:
                continue

            project_identifier = infer_project_identifier(session, path, fallback=scoped_project)
            if project_identifier is None:
                if scoped_project is None:
                    project_identifier = f"unknown-{vendor.value}"
                else:
                    continue

            key = normalize_project_key(project_identifier)
            if not key:
                continue
            if scoped_project_key and key != scoped_project_key:
                continue

            sessions_by_project.setdefault(project_identifier, []).append(session)
            path_session_meta.append((vendor, path, session.session_id))

    if not sessions_by_project:
        raise DocumentError(f"no matching coding-agent logs found for {current_dir}")

    session_graphs: list[SessionGraph] = []
    for project_identifier, sessions in sorted(sessions_by_project.items()):
        session_graphs.extend(assemble_project_session_graphs(project_identifier, sessions))

    session_to_root: dict[UUID, UUID] = {
        session.session_id: session_graph.root_session_id
        for session_graph in session_graphs
        for session in session_graph.sessions
    }
    sources = [
        DiscoverySource(vendor=vendor, path=path, root_session_id=session_to_root.get(session_id))
        for vendor, path, session_id in path_session_meta
    ]

    return DiscoveryResult(store=DocumentStore.from_session_graphs(session_graphs), sources=sources)


@dataclass(frozen=True, slots=True)
class SessionMetadataGroup:
    project_identifier: str
    root_session_id: UUID
    session_ids: list[UUID]
    vendors: list[str]
    title: str | None = None


def discover_session_metadata(
    *,
    current_dir: Path,
    global_scope: bool = False,
    project_name: str | None = None,
    since_days: int | None = None,
    modified_since: datetime | None = None,
    agent_vendor: str | None = None,
) -> list[SessionMetadataGroup]:
    """List session-graph metadata via header-only scans (no transcript projection)."""
    current_dir = current_dir.resolve()
    scoped_project = project_name or (None if global_scope else current_dir.name)
    scoped_project_key = normalize_project_key(scoped_project) if scoped_project else None
    modified_since = _modified_since(since_days, modified_since=modified_since)

    headers_by_project: dict[str, list[tuple[SessionHeader, Vendor, Path]]] = {}
    for vendor, adapter_cls, base_dir, pattern in _selected_vendor_configs(agent_vendor):
        for path in _candidate_files(
            vendor,
            base_dir,
            pattern,
            current_dir=current_dir,
            scoped_project=scoped_project,
            scoped_project_key=scoped_project_key,
            modified_since=modified_since,
        ):
            adapter = adapter_cls()
            try:
                header = adapter.scan_header(path)
            except Exception:
                continue
            if header is None:
                continue

            project_identifier = _session_project_identifier(vendor, path, header, fallback=scoped_project)
            if project_identifier is None:
                if scoped_project is None:
                    project_identifier = f"unknown-{vendor.value}"
                else:
                    continue

            key = normalize_project_key(project_identifier)
            if not key:
                continue
            if scoped_project_key and key != scoped_project_key:
                continue

            headers_by_project.setdefault(project_identifier, []).append((header, vendor, path))

    groups: list[SessionMetadataGroup] = []
    for project_identifier, entries in sorted(headers_by_project.items()):
        groups.extend(_group_session_headers(project_identifier, entries))
    return sorted(groups, key=lambda group: (group.project_identifier, str(group.root_session_id)))


def _session_project_identifier(
    vendor: Vendor, source: Path, header: SessionHeader, *, fallback: str | None
) -> str | None:
    if vendor == Vendor.CLAUDE_CODE:
        project_path = _claude_project_path_from_source(source)
        if project_path is not None and project_path.name:
            return project_path.name
    if header.cwd:
        name = Path(header.cwd).name
        if name:
            return name
    return fallback


def _group_session_headers(
    project_identifier: str, entries: list[tuple[SessionHeader, Vendor, Path]]
) -> list[SessionMetadataGroup]:
    key = normalize_project_key(project_identifier)
    header_by_id = {header.session_id: header for header, _vendor, _path in entries}
    parent: dict[UUID, UUID] = {sid: sid for sid in header_by_id}

    def find(node: UUID) -> UUID:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: UUID, b: UUID) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for header in header_by_id.values():
        if header.parent_session_id is not None and header.parent_session_id in header_by_id:
            union(header.session_id, header.parent_session_id)

    components: dict[UUID, list[SessionHeader]] = {}
    for header in header_by_id.values():
        components.setdefault(find(header.session_id), []).append(header)

    groups: list[SessionMetadataGroup] = []
    for component in components.values():
        ordered = sorted(component, key=lambda h: str(h.session_id))
        root = next((h for h in ordered if h.parent_session_id is None), ordered[0])
        title = root.title or next((h.title for h in ordered if h.title), None)
        vendors = sorted({h.vendor.value for h in ordered})
        groups.append(
            SessionMetadataGroup(
                project_identifier=key,
                root_session_id=root.session_id,
                session_ids=[h.session_id for h in ordered],
                vendors=vendors,
                title=title,
            )
        )
    return groups


def discover_project_metadata(
    *,
    current_dir: Path,
    global_scope: bool = True,
    project_name: str | None = None,
    since_days: int | None = None,
    modified_since: datetime | None = None,
    agent_vendor: str | None = None,
) -> list[ProjectDiscoveryItem]:
    """Discover project/vendor metadata without building full session graphs."""
    current_dir = current_dir.resolve()
    scoped_project = project_name or (None if global_scope else current_dir.name)
    scoped_project_key = normalize_project_key(scoped_project) if scoped_project else None
    modified_since = _modified_since(since_days, modified_since=modified_since)
    items: list[ProjectDiscoveryItem] = []

    selected_vendors = {vendor for vendor, _adapter_cls, _base_dir, _pattern in _selected_vendor_configs(agent_vendor)}
    if Vendor.CODEX_CLI in selected_vendors:
        items.extend(
            _codex_config_project_metadata(
                current_dir=current_dir,
                scoped_project=scoped_project,
                scoped_project_key=scoped_project_key,
                modified_since=modified_since,
            )
        )
    if Vendor.CLAUDE_CODE in selected_vendors:
        items.extend(
            _claude_project_dir_metadata(
                current_dir=current_dir,
                scoped_project=scoped_project,
                scoped_project_key=scoped_project_key,
                modified_since=modified_since,
            )
        )
    if Vendor.PI in selected_vendors:
        items.extend(
            _pi_session_project_metadata(
                current_dir=current_dir,
                scoped_project=scoped_project,
                scoped_project_key=scoped_project_key,
                modified_since=modified_since,
            )
        )

    return items


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
        or not _project_scope_matches_path(project_path, current_dir, scoped_project, scoped_project_key)
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
        return sorted(path for path in session_root.iterdir() if path.is_file() and path.suffix == ".jsonl")

    files: list[Path] = []
    for project_dir in sorted(session_root.iterdir()):
        if not project_dir.is_dir():
            continue
        files.extend(sorted(path for path in project_dir.iterdir() if path.is_file() and path.suffix == ".jsonl"))
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


def _normalize_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _modified_since(since_days: int | None, *, modified_since: datetime | None = None) -> datetime | None:
    if modified_since is not None:
        return modified_since.astimezone(timezone.utc)
    if since_days is None:
        return None
    return datetime.now(timezone.utc) - timedelta(days=since_days)


def _all_files(base_dir: Path, pattern: str, *, modified_since: datetime | None) -> list[Path]:
    if not base_dir.is_dir():
        return []
    return [
        path
        for path in sorted(base_dir.rglob(pattern), key=lambda item: item.stat().st_mtime, reverse=True)
        if _is_recent_enough(path, modified_since)
    ]


def _candidate_files(
    vendor: Vendor,
    base_dir: Path,
    pattern: str,
    *,
    current_dir: Path,
    scoped_project: str | None,
    scoped_project_key: str | None,
    modified_since: datetime | None,
) -> list[Path]:
    if scoped_project_key is None:
        return _all_files(base_dir, pattern, modified_since=modified_since)
    if not base_dir.is_dir():
        return []

    # Direct directory lookup for Claude Code and Pi
    if vendor == Vendor.CLAUDE_CODE:
        return _claude_candidate_files_direct(
            base_dir, pattern, current_dir=current_dir, modified_since=modified_since
        )

    if vendor == Vendor.PI:
        return _pi_candidate_files_direct(
            base_dir, pattern, current_dir=current_dir, modified_since=modified_since
        )

    if vendor == Vendor.CODEX_CLI:
        # Pre-filter: check if current_dir is tracked in config.toml
        if not _codex_tracks_project(current_dir):
            return []

    # Fallback: scan and filter (for unknown vendors or when direct lookup fails)
    paths = _all_files(base_dir, pattern, modified_since=modified_since)
    return [
        path
        for path in paths
        if _path_matches_project_scope(
            vendor,
            path,
            current_dir,
            scoped_project=scoped_project,
            scoped_project_key=scoped_project_key,
        )
    ]


def _claude_candidate_files_direct(
    base_dir: Path,
    pattern: str,
    *,
    current_dir: Path,
    modified_since: datetime | None,
) -> list[Path]:
    """Direct lookup for Claude Code: encode CWD and check specific directory."""
    for ancestor in _ancestor_dirs_up_to_project_marker(current_dir):
        encoded = _encode_claude_project_path(ancestor)
        target_dir = base_dir / encoded
        if target_dir.is_dir():
            files = _all_files(target_dir, pattern, modified_since=modified_since)
            if files:
                return files
    return []


def _pi_candidate_files_direct(
    base_dir: Path,
    pattern: str,
    *,
    current_dir: Path,
    modified_since: datetime | None,
) -> list[Path]:
    """Direct lookup for Pi: encode CWD and check specific directory."""
    session_root, custom_session_dir = _pi_session_root()
    # Use actual session root (may be custom), not the default base_dir
    scan_root = session_root

    if custom_session_dir:
        # Custom session dir: all files are flat, need to parse each to check CWD
        paths = _all_files(scan_root, pattern, modified_since=modified_since)
        return [
            path for path in paths
            if _pi_session_cwd_matches(path, current_dir)
        ]

    # Default structure: sessions are under <base>/<encoded-project>/
    for ancestor in _ancestor_dirs_up_to_project_marker(current_dir):
        encoded = _encode_pi_project_path(ancestor)
        target_dir = scan_root / encoded
        if target_dir.is_dir():
            files = _all_files(target_dir, pattern, modified_since=modified_since)
            if files:
                return files
    return []


def _pi_session_cwd_matches(path: Path, current_dir: Path) -> bool:
    """Check if a Pi session file's CWD matches current_dir or its ancestors."""
    project_path = _pi_project_path_from_session_header(path)
    if project_path is None:
        return False
    try:
        resolved = project_path.resolve()
    except OSError:
        resolved = project_path
    current_resolved = current_dir.resolve()
    return resolved == current_resolved or resolved in current_resolved.parents


def _codex_tracks_project(current_dir: Path) -> bool:
    """Check if Codex config.toml tracks current_dir or any of its ancestors."""
    config_path = _codex_home() / "config.toml"
    if not config_path.is_file():
        return False
    try:
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return False

    projects = config.get("projects")
    if not isinstance(projects, dict):
        return False

    current_resolved = current_dir.resolve()
    ancestors = {current_resolved, *current_resolved.parents}

    for raw_path in projects:
        if not isinstance(raw_path, str) or not raw_path:
            continue
        try:
            project_path = Path(raw_path).expanduser().resolve()
        except OSError:
            continue
        if project_path in ancestors:
            return True

    return False


def _is_recent_enough(path: Path, modified_since: datetime | None) -> bool:
    if modified_since is None:
        return True
    try:
        modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return False
    return modified_at >= modified_since


def _path_matches_project_scope(
    vendor: Vendor,
    path: Path,
    current_dir: Path,
    *,
    scoped_project: str | None,
    scoped_project_key: str,
) -> bool:
    if vendor == Vendor.CLAUDE_CODE:
        project_path = _claude_project_path_from_source(path)
        if project_path is not None:
            return _project_scope_matches_path(project_path, current_dir, scoped_project, scoped_project_key)

    if vendor == Vendor.PI:
        project_path = _pi_project_path_from_source(path)
        if project_path is not None:
            return _project_scope_matches_path(project_path, current_dir, scoped_project, scoped_project_key)

    if vendor == Vendor.CODEX_CLI:
        project_path = _codex_project_path_from_source(path)
        if project_path is not None:
            return _project_scope_matches_path(project_path, current_dir, scoped_project, scoped_project_key)

    path_token = _normalize_token(scoped_project_key)
    return any(_normalize_token(part) == path_token for part in path.parts)


def _project_path_from_source(vendor: Vendor, path: Path) -> Path | None:
    if vendor == Vendor.CLAUDE_CODE:
        return _claude_project_path_from_source(path)
    if vendor == Vendor.PI:
        return _pi_project_path_from_source(path)
    if vendor == Vendor.CODEX_CLI:
        return _codex_project_path_from_source(path)
    return None


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
    if scoped_project and normalize_project_key(scoped_project) == normalize_project_key(resolved.name):
        return True
    return False


def _claude_project_path_from_source(path: Path) -> Path | None:
    base = Path.home() / ".claude" / "projects"
    try:
        encoded = path.resolve().relative_to(base).parts[0]
    except (IndexError, ValueError):
        return None
    decoded = _decode_claude_encoded_path(encoded)
    return Path(decoded) if decoded else None


def _pi_project_path_from_source(path: Path) -> Path | None:
    base = Path.home() / ".pi" / "agent" / "sessions"
    try:
        encoded = path.resolve().relative_to(base).parts[0]
    except (IndexError, ValueError):
        return None
    stripped = encoded.strip("-")
    if not stripped:
        return None
    return Path("/" + stripped.replace("-", "/"))


def _codex_project_path_from_source(path: Path, *, max_records: int = 8) -> Path | None:
    try:
        with path.open(encoding="utf-8") as handle:
            for index, raw_line in enumerate(handle):
                if index >= max_records:
                    break
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                payload = record.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return Path(cwd)
    except OSError:
        return None
    return None


def infer_project_identifier(session: Session, source: Path, *, fallback: str | None) -> str | None:
# For Claude Code the source path encodes the CWD authoritatively; event payloads
    # can contain misleading cwds (e.g. when a session runs inside .claude/projects/).
    # Path structure: .claude/projects/<encoded-cwd>/<session-uuid>[/subagents]/file.jsonl
    if session.vendor == Vendor.CLAUDE_CODE:
        base = Path.home() / ".claude" / "projects"
        try:
            encoded = source.relative_to(base).parts[0]
            decoded = _decode_claude_encoded_path(encoded)
            if decoded:
                name = Path(decoded).name
                if name:
                    return name
        except ValueError:
            pass

    # session.cwd is already resolved by stabilize_session (has access to source)
    if session.cwd:
        return Path(session.cwd).name

    if fallback:
        return fallback

    return None


def _extract_session_cwd(session: Session, source: Path | None = None) -> str | None:
    # Check vendor extensions first (more reliable than scanning event payloads)
    if session.extensions:
        if session.extensions.codex and session.extensions.codex.cwd:
            return session.extensions.codex.cwd
        if session.extensions.pi and session.extensions.pi.cwd:
            return session.extensions.pi.cwd
    for event in session.events:
        payload = cast(dict[str, object], event.payload)
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
        raw = payload.get("raw")
        if isinstance(raw, dict):
            raw_cwd = raw.get("cwd")
            if isinstance(raw_cwd, str) and raw_cwd:
                return raw_cwd

    # Claude Code encodes the CWD as the first path component under .claude/projects/
    # Structure: .claude/projects/<encoded-cwd>/<session-uuid>[/subagents]/file.jsonl
    if source and session.vendor == Vendor.CLAUDE_CODE:
        base = Path.home() / ".claude" / "projects"
        try:
            encoded = source.relative_to(base).parts[0]
            return _decode_claude_encoded_path(encoded)
        except ValueError:
            pass

    return None


def _matching_vendor_configs(path: Path) -> list[tuple[Vendor, type[BaseAdapter], Path, str]]:
    configs = _vendor_configs()
    matched = [
        config
        for config in configs
        if _path_matches_vendor_config(path, base_dir=config[2], pattern=config[3])
    ]
    return matched or configs



def _path_matches_vendor_config(path: Path, *, base_dir: Path, pattern: str) -> bool:
    try:
        relative = path.resolve().relative_to(base_dir.resolve())
    except ValueError:
        return False
    return fnmatch.fnmatch(relative.name, pattern)



def discover_store_from_file(path: Path) -> DiscoveryResult:
    """Build a store from a single explicit log file, auto-detecting the vendor."""
    path = path.resolve()
    if not path.exists():
        raise DocumentError(f"log file not found: {path}")

    for vendor, adapter_cls, _base_dir, _pattern in _matching_vendor_configs(path):
        adapter = adapter_cls()
        try:
            session = stabilize_session(adapter.ingest_file(path), vendor=vendor, source=path)
        except Exception:
            continue

        project_identifier = infer_project_identifier(session, path, fallback=path.stem)
        if not project_identifier:
            project_identifier = path.stem

        session_graphs = assemble_project_session_graphs(project_identifier, [session])
        store = DocumentStore.from_session_graphs(session_graphs)
        root_session_id = session_graphs[0].root_session_id
        source = DiscoverySource(vendor=vendor, path=path, root_session_id=root_session_id)
        return DiscoveryResult(store=store, sources=[source])

    raise DocumentError(f"no adapter could parse log file: {path}")


def discover_store_from_files(paths: list[Path]) -> DiscoveryResult:
    """Build a store from multiple explicit log files, preserving cross-session links."""
    sessions_by_project: dict[str, list[Session]] = {}
    path_session_meta: list[tuple[Vendor, Path, UUID]] = []

    for raw_path in paths:
        path = raw_path.resolve()
        if not path.exists():
            continue

        for vendor, adapter_cls, _base_dir, _pattern in _matching_vendor_configs(path):
            adapter = adapter_cls()
            try:
                session = stabilize_session(adapter.ingest_file(path), vendor=vendor, source=path)
            except Exception:
                continue

            project_identifier = infer_project_identifier(session, path, fallback=path.stem)
            if not project_identifier:
                project_identifier = path.stem

            sessions_by_project.setdefault(project_identifier, []).append(session)
            path_session_meta.append((vendor, path, session.session_id))
            break

    if not sessions_by_project:
        raise DocumentError(f"no valid log files found for paths: {[str(path) for path in paths]}")

    session_graphs: list[SessionGraph] = []
    for project_identifier, sessions in sorted(sessions_by_project.items()):
        session_graphs.extend(assemble_project_session_graphs(project_identifier, sessions))

    session_to_root: dict[UUID, UUID] = {
        session.session_id: session_graph.root_session_id
        for session_graph in session_graphs
        for session in session_graph.sessions
    }
    sources = [
        DiscoverySource(vendor=vendor, path=path, root_session_id=session_to_root.get(session_id))
        for vendor, path, session_id in path_session_meta
    ]

    return DiscoveryResult(store=DocumentStore.from_session_graphs(session_graphs), sources=sources)


def format_discovery_sources(sources: list[DiscoverySource]) -> str:
    if not sources:
        return ""
    lines = ["Discovered coding-agent logs:"]
    for source in sources:
        lines.append(f"- {source.vendor.value}: {source.path}")
    return "\n".join(lines)


def stabilize_session(session: Session, *, vendor: Vendor, source: Path) -> Session:
    # --- stabilize event IDs ---
    event_id_map: dict[object, object] = {}
    events: list[Event] = []
    for index, event in enumerate(session.events):
        stable_event_id = uuid5(
            NAMESPACE_URL,
            json.dumps(
                {
                    "vendor": vendor.value,
                    "source": str(source),
                    "index": index,
                    "timestamp": event.timestamp.isoformat(),
                    "type": event.type.value,
                    "actor": event.actor,
                    "payload": event.payload,
                },
                sort_keys=True,
                default=str,
            ),
        )
        event_id_map[event.event_id] = stable_event_id
        events.append(event.model_copy(update={"event_id": stable_event_id}))

    # --- stabilize turn + item IDs ---
    turn_id_map: dict[object, object] = {}
    turns: list[Turn] = []
    for t_index, turn in enumerate(session.turns):
        stable_turn_id = uuid5(
            NAMESPACE_URL,
            json.dumps(
                {
                    "vendor": vendor.value,
                    "source": str(source),
                    "turn_index": t_index,
                    "session_id": str(session.session_id),
                    "sequence": turn.sequence,
                    "started_at": turn.started_at.isoformat(),
                },
                sort_keys=True,
                default=str,
            ),
        )
        turn_id_map[turn.turn_id] = stable_turn_id

        stable_items: list[Item] = []
        for i_index, item in enumerate(turn.items):
            stable_item_id = uuid5(
                NAMESPACE_URL,
                json.dumps(
                    {
                        "vendor": vendor.value,
                        "source": str(source),
                        "turn_index": t_index,
                        "item_index": i_index,
                        "kind": item.kind,
                        "sequence": item.sequence,
                        "started_at": item.started_at.isoformat(),
                        "tool_call_id": getattr(item, "tool_call_id", None),
                    },
                    sort_keys=True,
                    default=str,
                ),
            )
            stable_items.append(
                item.model_copy(
                    update={
                        "item_id": stable_item_id,
                        "session_id": session.session_id,
                        "turn_id": stable_turn_id,
                        "event_ids": [event_id_map.get(eid, eid) for eid in item.event_ids],
                    }
                )
            )

        user_req_eid = turn.user_request_event_id
        stable_user_req_eid = event_id_map.get(user_req_eid, user_req_eid) if user_req_eid else None

        turns.append(
            turn.model_copy(
                update={
                    "turn_id": stable_turn_id,
                    "user_request_event_id": stable_user_req_eid,
                    "event_ids": [event_id_map.get(eid, eid) for eid in turn.event_ids],
                    "items": stable_items,
                }
            )
        )

    context_usage = [
        observation.model_copy(
            update={
                "source_event_id": event_id_map.get(
                    observation.source_event_id,
                    observation.source_event_id,
                )
            }
        )
        for observation in session.context_usage
    ]
    cwd = _extract_session_cwd(session, source)
    return session.model_copy(
        update={
            "events": events,
            "turns": turns,
            "context_usage": context_usage,
            "cwd": cwd,
        }
    )
