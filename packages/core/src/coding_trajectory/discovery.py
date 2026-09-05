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
from uuid import UUID

from coding_trajectory import debug
from coding_trajectory.discovery_metadata import (
    ProjectDiscoveryItem,
    _claude_project_dir_metadata,
    _codex_config_project_metadata,
    _codex_home,
    _pi_project_path_from_session_header,
    _pi_session_project_metadata,
    _pi_session_root,
)
from coding_trajectory.discovery_paths import (
    _ancestor_dirs_up_to_project_marker,
    _decode_claude_encoded_path,
    _encode_claude_project_path,
    _encode_pi_project_path,
    _is_recent_enough,
    _project_scope_matches_path,
)
from coding_trajectory.ingestion import (
    AmpAdapter,
    ClaudeCodeAdapter,
    CodexAdapter,
    PiAdapter,
)
from coding_trajectory.ingestion.adapters.base import BaseAdapter, SessionHeader
from coding_trajectory.ingestion.common import (
    canonical_json,
    normalize_project_key,
)
from coding_trajectory.ingestion.graph import assemble_project_session_graphs
from coding_trajectory.ingestion.models import (
    Event,
    Item,
    Session,
    SessionGraph,
    Turn,
    Vendor,
    VendorExtensions,
)
from coding_trajectory.ingestion.provenance import SessionProvenance
from coding_trajectory.ingestion.retention import (
    CanonicalRetention,
    compact_usage_mapping,
    retain_event_for_measurements,
    retain_item_for_measurements,
)
from coding_trajectory.ingestion.transcript import (
    stable_event_id as _stable_event_id,
)
from coding_trajectory.ingestion.transcript import (
    stable_item_id as _stable_item_id,
)
from coding_trajectory.ingestion.transcript import (
    stable_turn_id as _stable_turn_id,
)
from coding_trajectory.query import DocumentError, DocumentStore


@dataclass(frozen=True, slots=True)
class DiscoverySource:
    vendor: Vendor
    path: Path
    root_session_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    store: DocumentStore
    sources: list[DiscoverySource]
    # Canonical-id -> source-byte-span mappings, keyed by resolved source
    # path.  Populated only on the compact (measurements) ingestion path.
    provenance: dict[str, SessionProvenance] | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryCandidate:
    """One source selected by cheap path/project discovery, before ingestion."""

    vendor: Vendor
    adapter_cls: type[BaseAdapter]
    path: Path


@dataclass(frozen=True, slots=True)
class _IngestedSession:
    """One logical session reconstructed from one or more source segments."""

    vendor: Vendor
    paths: tuple[Path, ...]
    session: Session


def _vendor_configs() -> list[tuple[Vendor, type[BaseAdapter], Path, str]]:
    home = Path.home()
    amp_root = Path(
        os.environ.get(
            "CT_AMP_LOG_DIR", home / ".coding-trajectory" / "amp" / "sessions"
        )
    ).expanduser()
    return [
        (Vendor.AMP, AmpAdapter, amp_root, "*.jsonl"),
        (Vendor.CODEX_CLI, CodexAdapter, home / ".codex" / "sessions", "*.jsonl"),
        (
            Vendor.CLAUDE_CODE,
            ClaudeCodeAdapter,
            home / ".claude" / "projects",
            "*.jsonl",
        ),
        (Vendor.PI, PiAdapter, home / ".pi" / "agent" / "sessions", "*.jsonl"),
    ]


def _selected_vendor_configs(
    agent_vendor: str | None,
) -> list[tuple[Vendor, type[BaseAdapter], Path, str]]:
    configs = _vendor_configs()
    if agent_vendor is None:
        return configs
    return [config for config in configs if config[0].value == agent_vendor]


def _ingest_sessions(
    candidates: list[tuple[Vendor, type[BaseAdapter], Path]],
    *,
    retention: CanonicalRetention = "trajectory",
) -> tuple[list[_IngestedSession], dict[str, SessionProvenance]]:
    """Ingest candidate files in two passes so forked files can drop the
    inherited-history segment they re-materialize.

    Pass 1 lightly scans each file's turn-starting ids (and parent link). Pass 2
    ingests each file, handing a forked file's adapter its parent's turn-id set
    so it can cut the inherited copy (avoids double-counting turns/tokens and
    re-emitting inherited spawn edges). Vendors without turn-start ids are inert.

    Returns the ingested sessions plus, on the compact path, per-source
    provenance mappings for lazy detail hydration.
    """
    cut_inputs = scan_parent_turn_ids(candidates)

    ingested: list[tuple[Vendor, Path, Session]] = []
    provenance: dict[str, SessionProvenance] = {}
    for vendor, adapter_cls, path in candidates:
        adapter = adapter_cls()
        parent_started = cut_inputs.get(path)
        try:
            session = adapter.ingest_file(
                path,
                parent_started_turn_ids=parent_started,
                retention=retention,
            )
            if retention != "measurements":
                session = stabilize_session(
                    session,
                    vendor=vendor,
                    source=path,
                    retention=retention,
                )
        except Exception as exc:
            debug.warn(
                f"failed to ingest {vendor.value} session log: {exc}",
                code="discovery.ingest_failed",
                severity="error",
                vendor=vendor.value,
                source=str(path),
            )
            continue
        ingested.append((vendor, path, session))
        if adapter.last_provenance is not None:
            provenance[str(path)] = adapter.last_provenance
    return _coalesce_session_segments(ingested), provenance


def _coalesce_session_segments(
    ingested: list[tuple[Vendor, Path, Session]],
) -> list[_IngestedSession]:
    """Collapse physical rollout segments into one logical session.

    Codex can persist a resumed thread in a new JSONL while retaining the
    original session id.  The files are consecutive source segments, not two
    sessions or a parent/child edge.  Keep their source identity for evidence
    lookup, but provide graph assembly a single canonical ``Session``.
    """

    grouped: dict[tuple[Vendor, UUID], list[tuple[Path, Session]]] = {}
    for vendor, path, session in ingested:
        grouped.setdefault((vendor, session.session_id), []).append((path, session))

    coalesced: list[_IngestedSession] = []
    for (vendor, _session_id), segments in grouped.items():
        ordered = sorted(
            segments, key=lambda entry: (entry[1].started_at, str(entry[0]))
        )
        coalesced.append(
            _IngestedSession(
                vendor=vendor,
                paths=tuple(path for path, _session in ordered),
                session=(
                    ordered[0][1]
                    if len(ordered) == 1
                    else merge_session_segments(ordered)
                ),
            )
        )
    return coalesced


def merge_session_segments(segments: list[tuple[Path, Session]]) -> Session:
    """Combine consecutive source segments without inventing a new identity."""

    ordered_sessions = [session for _path, session in segments]
    first = ordered_sessions[0]
    parent_ids = {
        session.parent_session_id
        for session in ordered_sessions
        if session.parent_session_id is not None
    }
    if len(parent_ids) > 1:
        debug.warn(
            "same-session source segments disagree on parent identity",
            code="discovery.session_segment_parent_conflict",
            severity="warning",
            session_id=str(first.session_id),
            source_paths=[str(path) for path, _session in segments],
        )

    events = _dedupe_by_id(
        (event for session in ordered_sessions for event in session.events),
        id_getter=lambda event: event.event_id,
    )
    turns = _dedupe_by_id(
        (turn for session in ordered_sessions for turn in session.turns),
        id_getter=lambda turn: turn.turn_id,
    )
    turns.sort(key=lambda turn: (turn.started_at, turn.sequence, str(turn.turn_id)))
    normalized_turns = [
        turn.model_copy(update={"session_id": first.session_id, "sequence": index})
        for index, turn in enumerate(turns)
    ]
    events.sort(key=lambda event: (event.timestamp, str(event.event_id)))

    return first.model_copy(
        update={
            "model": _last_non_none(session.model for session in ordered_sessions),
            "reasoning_effort": _last_non_none(
                session.reasoning_effort for session in ordered_sessions
            ),
            "agent_name": _last_non_none(
                session.agent_name for session in ordered_sessions
            ),
            "started_at": min(session.started_at for session in ordered_sessions),
            "ended_at": _latest_segment_end(ordered_sessions),
            "parent_session_id": next(iter(parent_ids), None),
            "cwd": _last_non_none(session.cwd for session in ordered_sessions),
            "events": events,
            "turns": normalized_turns,
            "context_usage": _dedupe_model_values(
                observation
                for session in ordered_sessions
                for observation in session.context_usage
            ),
            "context_sources": _dedupe_model_values(
                observation
                for session in ordered_sessions
                for observation in session.context_sources
            ),
            "runtime_observations": _dedupe_model_values(
                observation
                for session in ordered_sessions
                for observation in session.runtime_observations
            ),
            "extensions": _merge_vendor_extensions(ordered_sessions),
            "status": ordered_sessions[-1].status,
        }
    )


def _dedupe_by_id(values, *, id_getter):  # type: ignore[no-untyped-def]
    """Keep the first canonical record for an identical stable id."""

    retained = []
    seen: set[object] = set()
    for value in values:
        identifier = id_getter(value)
        if identifier in seen:
            continue
        seen.add(identifier)
        retained.append(value)
    return retained


def _dedupe_model_values(values):  # type: ignore[no-untyped-def]
    """Deduplicate repeated session-level observations deterministically."""

    retained = []
    seen: set[str] = set()
    for value in values:
        key = canonical_json(value.model_dump(mode="json"))
        if key in seen:
            continue
        seen.add(key)
        retained.append(value)
    return retained


def _last_non_none(values):  # type: ignore[no-untyped-def]
    for value in reversed(list(values)):
        if value is not None:
            return value
    return None


def _latest_segment_end(segments: list[Session]) -> datetime | None:
    ends = [session.ended_at for session in segments if session.ended_at is not None]
    return max(ends) if ends else None


def _merge_vendor_extensions(segments: list[Session]) -> VendorExtensions | None:
    merged: dict[str, object] = {}
    for session in segments:
        if session.extensions is None:
            continue
        _deep_merge_mappings(
            merged,
            session.extensions.model_dump(exclude_none=True),
        )
    return VendorExtensions.model_validate(merged) if merged else None


def _deep_merge_mappings(target: dict[str, object], source: dict[str, object]) -> None:
    for key, value in source.items():
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _deep_merge_mappings(current, value)
        else:
            target[key] = value


def scan_parent_turn_ids(
    candidates: list[tuple[Vendor, type[BaseAdapter], Path]],
) -> dict[Path, set[str] | None]:
    """Pass 1 of the two-pass ingest: per-file parent started-turn-id inputs.

    Full-fidelity re-ingestion (detail hydration, measurement extraction)
    must cut forked files exactly as the original two-pass ingest did, or
    stable ids would shift.  Returns each candidate's
    ``parent_started_turn_ids`` argument.
    """

    started_turn_ids_by_session: dict[UUID, set[str]] = {}
    parent_session_by_path: dict[Path, UUID | None] = {}
    header_scans: list[tuple[BaseAdapter, SessionHeader | None]] = []
    for _vendor, adapter_cls, path in candidates:
        adapter = adapter_cls()
        header = adapter.scan_header(path)
        header_scans.append((adapter, header))
        parent_session_by_path[path] = header.parent_session_id if header else None

    referenced_parent_ids = {
        parent_session_id
        for parent_session_id in parent_session_by_path.values()
        if parent_session_id is not None
    }
    for (_vendor, _adapter_cls, path), (adapter, header) in zip(
        candidates, header_scans, strict=True
    ):
        if header is None or header.session_id not in referenced_parent_ids:
            continue
        started = adapter.scan_started_turn_ids(path)
        if started is not None:
            started_turn_ids_by_session[header.session_id] = started

    result: dict[Path, set[str] | None] = {}
    for _vendor, _adapter_cls, path in candidates:
        parent_session_id = parent_session_by_path.get(path)
        result[path] = (
            started_turn_ids_by_session.get(parent_session_id)
            if parent_session_id is not None
            else None
        )
    return result


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
    scoped_project_key = (
        normalize_project_key(scoped_project) if scoped_project else None
    )
    modified_since = _modified_since(since_days, modified_since=modified_since)

    sessions_by_project: dict[str, list[Session]] = {}
    path_session_meta: list[tuple[Vendor, Path, UUID]] = []

    # Collect candidate files per vendor and ingest via the shared two-pass
    # helper so forked files drop their re-materialized inherited history.
    candidates: list[tuple[Vendor, type[BaseAdapter], Path]] = []
    for vendor, adapter_cls, base_dir, pattern in _selected_vendor_configs(
        agent_vendor
    ):
        for path in _candidate_files(
            vendor,
            base_dir,
            pattern,
            current_dir=current_dir,
            scoped_project=scoped_project,
            scoped_project_key=scoped_project_key,
            modified_since=modified_since,
        ):
            candidates.append((vendor, adapter_cls, path))

    ingested, _provenance = _ingest_sessions(candidates)
    for ingested_session in ingested:
        vendor = ingested_session.vendor
        session = ingested_session.session
        source_path = ingested_session.paths[0]
        project_identifier = infer_project_identifier(
            session, source_path, fallback=scoped_project
        )
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
        path_session_meta.extend(
            (vendor, path, session.session_id) for path in ingested_session.paths
        )

    if not sessions_by_project:
        raise DocumentError(f"no matching coding-agent logs found for {current_dir}")

    session_graphs: list[SessionGraph] = []
    for project_identifier, sessions in sorted(sessions_by_project.items()):
        session_graphs.extend(
            assemble_project_session_graphs(project_identifier, sessions)
        )

    session_to_root: dict[UUID, UUID] = {
        session.session_id: session_graph.root_session_id
        for session_graph in session_graphs
        for session in session_graph.sessions
    }
    sources = [
        DiscoverySource(
            vendor=vendor, path=path, root_session_id=session_to_root.get(session_id)
        )
        for vendor, path, session_id in path_session_meta
    ]

    return DiscoveryResult(
        store=DocumentStore.from_session_graphs(session_graphs), sources=sources
    )


def discover_source_candidates(
    *,
    current_dir: Path,
    global_scope: bool = False,
    project_name: str | None = None,
    since_days: int | None = None,
    modified_since: datetime | None = None,
    agent_vendor: str | None = None,
) -> list[DiscoveryCandidate]:
    """List matching JSONL sources without projecting canonical sessions."""

    current_dir = current_dir.resolve()
    scoped_project = project_name or (None if global_scope else current_dir.name)
    scoped_project_key = (
        normalize_project_key(scoped_project) if scoped_project else None
    )
    cutoff = _modified_since(since_days, modified_since=modified_since)
    candidates: list[DiscoveryCandidate] = []
    for vendor, adapter_cls, base_dir, pattern in _selected_vendor_configs(
        agent_vendor
    ):
        candidates.extend(
            DiscoveryCandidate(vendor=vendor, adapter_cls=adapter_cls, path=path)
            for path in _candidate_files(
                vendor,
                base_dir,
                pattern,
                current_dir=current_dir,
                scoped_project=scoped_project,
                scoped_project_key=scoped_project_key,
                modified_since=cutoff,
            )
        )
    return sorted(candidates, key=lambda value: str(value.path))


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
    scoped_project_key = (
        normalize_project_key(scoped_project) if scoped_project else None
    )
    modified_since = _modified_since(since_days, modified_since=modified_since)
    items: list[ProjectDiscoveryItem] = []

    selected_vendors = {
        vendor
        for vendor, _adapter_cls, _base_dir, _pattern in _selected_vendor_configs(
            agent_vendor
        )
    }
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
    if Vendor.AMP in selected_vendors:
        items.extend(
            _amp_session_project_metadata(
                current_dir=current_dir,
                scoped_project=scoped_project,
                scoped_project_key=scoped_project_key,
                modified_since=modified_since,
            )
        )

    return items


def _normalize_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _modified_since(
    since_days: int | None, *, modified_since: datetime | None = None
) -> datetime | None:
    if modified_since is not None:
        return modified_since.astimezone(timezone.utc)
    if since_days is None:
        return None
    return datetime.now(timezone.utc) - timedelta(days=since_days)


def _all_files(
    base_dir: Path, pattern: str, *, modified_since: datetime | None
) -> list[Path]:
    if not base_dir.is_dir():
        return []
    return [
        path
        for path in sorted(
            base_dir.rglob(pattern), key=lambda item: item.stat().st_mtime, reverse=True
        )
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

    if vendor == Vendor.AMP:
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
        return [path for path in paths if _pi_session_cwd_matches(path, current_dir)]

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
            return _project_scope_matches_path(
                project_path, current_dir, scoped_project, scoped_project_key
            )

    if vendor == Vendor.AMP:
        project_path = _amp_project_path_from_source(path)
        if project_path is not None:
            return _project_scope_matches_path(
                project_path, current_dir, scoped_project, scoped_project_key
            )

    if vendor == Vendor.PI:
        project_path = _pi_project_path_from_source(path)
        if project_path is not None:
            return _project_scope_matches_path(
                project_path, current_dir, scoped_project, scoped_project_key
            )

    if vendor == Vendor.CODEX_CLI:
        project_path = _codex_project_path_from_source(path)
        if project_path is not None:
            return _project_scope_matches_path(
                project_path, current_dir, scoped_project, scoped_project_key
            )

    path_token = _normalize_token(scoped_project_key)
    return any(_normalize_token(part) == path_token for part in path.parts)


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


def _amp_project_path_from_source(path: Path) -> Path | None:
    header = AmpAdapter().scan_header(path)
    return Path(header.cwd) if header and header.cwd else None


def _amp_session_project_metadata(
    *,
    current_dir: Path,
    scoped_project: str | None,
    scoped_project_key: str | None,
    modified_since: datetime | None,
) -> list[ProjectDiscoveryItem]:
    amp_config = next(config for config in _vendor_configs() if config[0] == Vendor.AMP)
    items: list[ProjectDiscoveryItem] = []
    for path in _all_files(amp_config[2], amp_config[3], modified_since=modified_since):
        project_path = _amp_project_path_from_source(path)
        if project_path is None:
            continue
        if scoped_project_key and not _project_scope_matches_path(
            project_path, current_dir, scoped_project, scoped_project_key
        ):
            continue
        project_identifier = normalize_project_key(project_path.name)
        if project_identifier:
            items.append(
                ProjectDiscoveryItem(
                    project_identifier=project_identifier,
                    path=project_path,
                    source_path=path,
                    vendor=Vendor.AMP,
                )
            )
    return items


def infer_project_identifier(
    session: Session, source: Path, *, fallback: str | None
) -> str | None:
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


def _matching_vendor_configs(
    path: Path,
) -> list[tuple[Vendor, type[BaseAdapter], Path, str]]:
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


def discover_store_from_files(
    paths: list[Path],
    *,
    retention: CanonicalRetention = "trajectory",
) -> DiscoveryResult:
    """Build a store from explicit logs while preserving cross-session links.

    ``measurements`` retains the canonical hierarchy and fields required by
    usage, model, runtime, pricing, and reconciliation projections, but drops
    transcript bodies before sessions accumulate into connected components.
    """
    sessions_by_project: dict[str, list[Session]] = {}
    path_session_meta: list[tuple[Vendor, Path, UUID]] = []

    candidates: list[tuple[Vendor, type[BaseAdapter], Path]] = []
    for raw_path in paths:
        path = raw_path.resolve()
        if not path.exists():
            continue
        for vendor, adapter_cls, _base_dir, _pattern in _matching_vendor_configs(path):
            candidates.append((vendor, adapter_cls, path))
            break

    ingested, provenance = _ingest_sessions(candidates, retention=retention)
    for ingested_session in ingested:
        vendor = ingested_session.vendor
        session = ingested_session.session
        source_path = ingested_session.paths[0]
        project_identifier = infer_project_identifier(
            session, source_path, fallback=source_path.stem
        )
        if not project_identifier:
            project_identifier = source_path.stem

        sessions_by_project.setdefault(project_identifier, []).append(session)
        path_session_meta.extend(
            (vendor, path, session.session_id) for path in ingested_session.paths
        )

    if not sessions_by_project:
        raise DocumentError(
            f"no valid log files found for paths: {[str(path) for path in paths]}"
        )

    session_graphs: list[SessionGraph] = []
    for project_identifier, sessions in sorted(sessions_by_project.items()):
        session_graphs.extend(
            assemble_project_session_graphs(project_identifier, sessions)
        )

    session_to_root: dict[UUID, UUID] = {
        session.session_id: session_graph.root_session_id
        for session_graph in session_graphs
        for session in session_graph.sessions
    }
    sources = [
        DiscoverySource(
            vendor=vendor, path=path, root_session_id=session_to_root.get(session_id)
        )
        for vendor, path, session_id in path_session_meta
    ]

    return DiscoveryResult(
        store=DocumentStore.from_session_graphs(session_graphs),
        sources=sources,
        provenance=provenance,
    )


def locate_session_files(
    *,
    session_id: UUID,
    current_dir: Path,
    global_scope: bool = False,
    project_name: str | None = None,
    since_days: int | None = None,
    modified_since: datetime | None = None,
    agent_vendor: str | None = None,
    include_descendants: bool = True,
) -> list[Path]:
    """Locate the source chain or full component for a session id.

    Filename matches are tried first because supported providers normally embed
    the session UUID in the source name.  Sources are validated with the
    identity-only adapter path; titles and other display metadata are irrelevant
    to routing.  A corpus identity scan remains as the compatibility fallback
    for providers whose filenames do not expose canonical session ids.

    The target's parent chain is always retained for correct fork trimming.
    Descendants are included only when the caller needs a graph projection.
    Returns an empty list when the session is not in scope.
    """
    current_dir = current_dir.resolve()
    scoped_project = project_name or (None if global_scope else current_dir.name)
    scoped_project_key = (
        normalize_project_key(scoped_project) if scoped_project else None
    )
    modified_since = _modified_since(since_days, modified_since=modified_since)

    candidates: list[tuple[Vendor, type[BaseAdapter], Path]] = []
    for vendor, adapter_cls, base_dir, pattern in _selected_vendor_configs(
        agent_vendor
    ):
        for path in _candidate_files(
            vendor,
            base_dir,
            pattern,
            current_dir=current_dir,
            scoped_project=scoped_project,
            scoped_project_key=scoped_project_key,
            modified_since=modified_since,
        ):
            candidates.append((vendor, adapter_cls, path))

    target_text = str(session_id).lower()
    direct = [
        candidate
        for candidate in candidates
        if target_text in candidate[2].stem.lower()
    ]
    remaining = [candidate for candidate in candidates if candidate not in direct]
    # A resumed Codex thread can have several physical rollout files with the
    # same canonical session id. Keep one representative for topology and all
    # source paths for historical reconstruction.
    file_by_session: dict[UUID, tuple[Path, UUID | None]] = {}
    paths_by_session: dict[UUID, list[Path]] = {}

    _scan_session_identities(
        direct,
        file_by_session=file_by_session,
        paths_by_session=paths_by_session,
    )
    if session_id in file_by_session and not include_descendants:
        chain = _resolve_direct_parent_chain(
            remaining,
            file_by_session=file_by_session,
            paths_by_session=paths_by_session,
            session_id=session_id,
        )
        if chain is not None:
            return _expand_session_segment_paths(
                chain,
                file_by_session=file_by_session,
                paths_by_session=paths_by_session,
            )

    _scan_session_identities(
        remaining,
        file_by_session=file_by_session,
        paths_by_session=paths_by_session,
    )

    if session_id not in file_by_session:
        return []

    if not include_descendants:
        chain = _session_parent_chain(file_by_session, session_id)
        return (
            _expand_session_segment_paths(
                chain,
                file_by_session=file_by_session,
                paths_by_session=paths_by_session,
            )
            if chain is not None
            else []
        )

    # Walk parent links up to the graph root.
    root = session_id
    seen: set[UUID] = set()
    while root in file_by_session and root not in seen:
        seen.add(root)
        parent = file_by_session[root][1]
        if parent is None or parent not in file_by_session:
            break
        root = parent

    # Collect the whole connected component: the root plus all descendants.
    children: dict[UUID, list[UUID]] = {}
    for sid, (_path, parent) in file_by_session.items():
        if parent is not None and parent in file_by_session:
            children.setdefault(parent, []).append(sid)

    component: list[Path] = []
    stack = [root]
    visited: set[UUID] = set()
    while stack:
        sid = stack.pop()
        if sid in visited:
            continue
        visited.add(sid)
        component.append(file_by_session[sid][0])
        stack.extend(children.get(sid, []))
    return _expand_session_segment_paths(
        component,
        file_by_session=file_by_session,
        paths_by_session=paths_by_session,
    )


def _scan_session_identities(
    candidates: list[tuple[Vendor, type[BaseAdapter], Path]],
    *,
    file_by_session: dict[UUID, tuple[Path, UUID | None]],
    paths_by_session: dict[UUID, list[Path]],
) -> None:
    """Populate routing identities without projecting titles or transcripts."""

    for vendor, adapter_cls, path in candidates:
        try:
            header = adapter_cls().scan_identity(path)
        except Exception as exc:
            debug.warn(
                f"failed to scan session identity for {vendor.value} log: {exc}",
                code="discovery.scan_identity_failed",
                severity="warning",
                vendor=vendor.value,
                source=str(path),
            )
            continue
        if header is None:
            continue
        paths = paths_by_session.setdefault(header.session_id, [])
        if path not in paths:
            paths.append(path)
        file_by_session.setdefault(header.session_id, (path, header.parent_session_id))


def _session_parent_chain(
    file_by_session: dict[UUID, tuple[Path, UUID | None]],
    session_id: UUID,
) -> list[Path] | None:
    """Return root-to-target paths, or ``None`` when a parent is unresolved."""

    chain: list[Path] = []
    current: UUID | None = session_id
    seen: set[UUID] = set()
    while current is not None:
        if current in seen:
            return None
        seen.add(current)
        located = file_by_session.get(current)
        if located is None:
            return None
        path, current = located
        chain.append(path)
    chain.reverse()
    return chain


def _resolve_direct_parent_chain(
    candidates: list[tuple[Vendor, type[BaseAdapter], Path]],
    *,
    file_by_session: dict[UUID, tuple[Path, UUID | None]],
    paths_by_session: dict[UUID, list[Path]],
    session_id: UUID,
) -> list[Path] | None:
    """Resolve filename-addressable parents without scanning unrelated sources."""

    remaining = candidates
    while True:
        chain = _session_parent_chain(file_by_session, session_id)
        if chain is not None:
            return chain

        missing = _first_unresolved_parent(file_by_session, session_id)
        if missing is None:
            return None
        missing_text = str(missing).lower()
        direct = [
            candidate
            for candidate in remaining
            if missing_text in candidate[2].stem.lower()
        ]
        if not direct:
            return None
        _scan_session_identities(
            direct,
            file_by_session=file_by_session,
            paths_by_session=paths_by_session,
        )
        if missing not in file_by_session:
            return None
        direct_paths = {candidate[2] for candidate in direct}
        remaining = [
            candidate for candidate in remaining if candidate[2] not in direct_paths
        ]


def _expand_session_segment_paths(
    representative_paths: list[Path],
    *,
    file_by_session: dict[UUID, tuple[Path, UUID | None]],
    paths_by_session: dict[UUID, list[Path]],
) -> list[Path]:
    """Expand a topology path into its physical source segments."""

    session_by_representative = {
        path: session_id for session_id, (path, _parent) in file_by_session.items()
    }
    expanded: list[Path] = []
    for representative in representative_paths:
        session_id = session_by_representative.get(representative)
        segments = (
            paths_by_session.get(session_id, [representative])
            if session_id is not None
            else [representative]
        )
        expanded.extend(sorted(segments, key=str))
    return expanded


def _first_unresolved_parent(
    file_by_session: dict[UUID, tuple[Path, UUID | None]],
    session_id: UUID,
) -> UUID | None:
    current: UUID | None = session_id
    seen: set[UUID] = set()
    while current is not None and current not in seen:
        seen.add(current)
        located = file_by_session.get(current)
        if located is None:
            return current
        current = located[1]
    return None


def format_discovery_sources(sources: list[DiscoverySource]) -> str:
    if not sources:
        return ""
    lines = ["Discovered coding-agent logs:"]
    for source in sources:
        lines.append(f"- {source.vendor.value}: {source.path}")
    return "\n".join(lines)


def stabilize_session(
    session: Session,
    *,
    vendor: Vendor,
    source: Path,
    retention: CanonicalRetention = "trajectory",
) -> Session:
    # --- stabilize event IDs ---
    event_id_map: dict[object, object] = {}
    events: list[Event] = []
    for index, event in enumerate(session.events):
        stable_event_id = _stable_event_id(
            vendor,
            source,
            index=index,
            timestamp=event.timestamp,
            type=event.type.value,
            actor=event.actor,
            payload=event.payload,
        )
        event_id_map[event.event_id] = stable_event_id
        stable_event = event.model_copy(update={"event_id": stable_event_id})
        if retention == "measurements":
            stable_event = retain_event_for_measurements(stable_event)
        if stable_event is not None:
            events.append(stable_event)

    # --- stabilize turn + item IDs ---
    turn_id_map: dict[object, object] = {}
    turns: list[Turn] = []
    for t_index, turn in enumerate(session.turns):
        stable_turn_id = _stable_turn_id(
            vendor,
            source,
            turn_index=t_index,
            session_id=session.session_id,
            sequence=turn.sequence,
            started_at=turn.started_at,
        )
        turn_id_map[turn.turn_id] = stable_turn_id

        stable_items: list[Item] = []
        for i_index, item in enumerate(turn.items):
            stable_item_id = _stable_item_id(
                vendor,
                source,
                turn_index=t_index,
                item_index=i_index,
                kind=item.kind,
                sequence=item.sequence,
                started_at=item.started_at,
                tool_call_id=getattr(item, "tool_call_id", None),
            )
            stable_item = item.model_copy(
                update={
                    "item_id": stable_item_id,
                    "session_id": session.session_id,
                    "turn_id": stable_turn_id,
                    "event_ids": [event_id_map.get(eid, eid) for eid in item.event_ids],
                }
            )
            if retention == "measurements":
                stable_item = retain_item_for_measurements(stable_item)
            stable_items.append(stable_item)

        user_req_eid = turn.user_request_event_id
        stable_user_req_eid = (
            event_id_map.get(user_req_eid, user_req_eid) if user_req_eid else None
        )

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
                ),
                **(
                    {
                        "usage": compact_usage_mapping(observation.usage),
                        "cumulative_usage": None,
                        "categories": [],
                    }
                    if retention == "measurements"
                    else {}
                ),
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
            "context_sources": (
                [] if retention == "measurements" else session.context_sources
            ),
            "cwd": cwd,
        }
    )
