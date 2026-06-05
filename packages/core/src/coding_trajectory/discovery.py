"""Auto-discovery of local coding-agent logs for the current project."""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from coding_trajectory.ingestion import ClaudeCodeAdapter, CodexAdapter, PiAdapter
from coding_trajectory.ingestion.adapters.base import BaseAdapter
from coding_trajectory.ingestion.common import normalize_project_key
from coding_trajectory.ingestion.models import Event, Session, Step, SessionGraph, Turn, Vendor
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
    vendor: Vendor


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

    for vendor, _adapter_cls, base_dir, pattern in _selected_vendor_configs(agent_vendor):
        for path in _candidate_files(
            vendor,
            base_dir,
            pattern,
            current_dir=current_dir,
            scoped_project=scoped_project,
            scoped_project_key=scoped_project_key,
            modified_since=modified_since,
        ):
            project_path = _project_path_from_source(vendor, path)
            project_identifier = _project_identifier_from_path(project_path)
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

            items.append(ProjectDiscoveryItem(project_identifier=key, path=project_path, vendor=vendor))

    return items


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

    # --- stabilize turn + step IDs ---
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

        stable_steps: list[Step] = []
        for s_index, step in enumerate(turn.steps):
            stable_step_id = uuid5(
                NAMESPACE_URL,
                json.dumps(
                    {
                        "vendor": vendor.value,
                        "source": str(source),
                        "turn_index": t_index,
                        "step_index": s_index,
                        "sequence": step.sequence,
                        "timestamp": step.timestamp.isoformat(),
                    },
                    sort_keys=True,
                    default=str,
                ),
            )
            stable_items = []
            for item in step.items:
                stable_items.append(
                    item.model_copy(
                        update={
                            "event_ids": [event_id_map.get(eid, eid) for eid in getattr(item, "event_ids", [])],
                        }
                    )
                )
            stable_steps.append(
                step.model_copy(
                    update={
                        "step_id": stable_step_id,
                        "turn_id": stable_turn_id,
                        "items": stable_items,
                        "event_ids": [event_id_map.get(eid, eid) for eid in step.event_ids],
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
                    "steps": stable_steps,
                }
            )
        )

    cwd = _extract_session_cwd(session, source)
    return session.model_copy(update={"events": events, "turns": turns, "cwd": cwd})
