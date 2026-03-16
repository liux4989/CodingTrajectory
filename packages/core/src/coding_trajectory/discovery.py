"""Auto-discovery of local coding-agent logs for the current project."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from coding_trajectory.ingestion import AmpAdapter, ClaudeCodeAdapter, CodexAdapter, GeminiAdapter
from coding_trajectory.ingestion.adapters.base import BaseAdapter
from coding_trajectory.ingestion.models import Event, Session, Step, Trajectory, Turn, Vendor
from coding_trajectory.query import DocumentError, DocumentStore
from coding_trajectory.trajectory import assemble_project_trajectories

_SEARCH_LIMIT = 100


@dataclass(frozen=True, slots=True)
class DiscoverySource:
    vendor: Vendor
    path: Path


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    store: DocumentStore
    sources: list[DiscoverySource]


def _vendor_configs() -> list[tuple[Vendor, type[BaseAdapter], Path, str]]:
    home = Path.home()
    return [
        (Vendor.CODEX_CLI, CodexAdapter, home / ".codex" / "sessions", "*.jsonl"),
        (Vendor.CLAUDE_CODE, ClaudeCodeAdapter, home / ".claude" / "projects", "*.jsonl"),
        (Vendor.GEMINI_CLI, GeminiAdapter, home / ".gemini" / "tmp", "session-*.json"),
        (Vendor.AMP, AmpAdapter, home / ".local" / "share" / "amp" / "threads", "T-*.json"),
    ]


def discover_store(*, current_dir: Path, global_scope: bool = False) -> DiscoveryResult:
    current_dir = current_dir.resolve()
    current_project = normalize_project_key(current_dir.name)

    sessions_by_project: dict[str, list[Session]] = {}
    sources: list[DiscoverySource] = []

    for vendor, adapter_cls, base_dir, pattern in _vendor_configs():
        for path in _recent_files(base_dir, pattern):
            if not global_scope and not _matches_project(path, current_dir, current_project):
                continue

            adapter = adapter_cls()
            try:
                session = stabilize_session(adapter.ingest_file(path), vendor=vendor, source=path)
            except Exception:
                continue

            project_identifier = infer_project_identifier(session, path, fallback=current_dir.name if not global_scope else None)
            if project_identifier is None:
                if global_scope:
                    project_identifier = f"unknown-{vendor.value}"
                else:
                    continue

            key = normalize_project_key(project_identifier)
            if not key:
                continue

            sessions_by_project.setdefault(project_identifier, []).append(
                session.model_copy(update={"trajectory_id": uuid5(NAMESPACE_URL, f"coding-trajectory:{key}")})
            )
            sources.append(DiscoverySource(vendor=vendor, path=path))

    if not sessions_by_project:
        raise DocumentError(f"no matching coding-agent logs found for {current_dir}")

    trajectories: list[Trajectory] = []
    for project_identifier, sessions in sorted(sessions_by_project.items()):
        trajectories.extend(assemble_project_trajectories(project_identifier, sessions))

    return DiscoveryResult(store=DocumentStore.from_trajectories(trajectories), sources=sources)


def normalize_project_key(value: str) -> str:
    collapsed = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", value.strip())
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", collapsed).strip("-").lower()
    return normalized


def _normalize_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _recent_files(base_dir: Path, pattern: str) -> list[Path]:
    if not base_dir.is_dir():
        return []
    return sorted(base_dir.rglob(pattern), key=lambda item: item.stat().st_mtime, reverse=True)[:_SEARCH_LIMIT]


def _matches_project(path: Path, current_dir: Path, current_project: str) -> bool:
    if _file_contains(path, str(current_dir)):
        return True
    token = _normalize_token(current_project)
    return any(_normalize_token(part) == token for part in path.parts)


def _file_contains(path: Path, needle: str) -> bool:
    try:
        return needle in path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def infer_project_identifier(session: Session, source: Path, *, fallback: str | None) -> str | None:
    if session.extensions and session.extensions.amp:
        amp = session.extensions.amp
        if amp.workspace_name:
            return amp.workspace_name
        if amp.workspace_id and amp.workspace_id.startswith("file://"):
            return Path(amp.workspace_id.removeprefix("file://")).name

    cwd = _extract_session_cwd(session)
    if cwd:
        return Path(cwd).name

    if session.vendor == Vendor.CLAUDE_CODE and len(source.parts) >= 2:
        parent = source.parent.name
        if parent:
            return parent

    if fallback:
        return fallback

    return None


def _extract_session_cwd(session: Session) -> str | None:
    # Check vendor extensions first (more reliable than scanning event payloads)
    if session.extensions:
        if session.extensions.codex and session.extensions.codex.cwd:
            return session.extensions.codex.cwd

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
    return None


def discover_store_from_file(path: Path) -> DiscoveryResult:
    """Build a store from a single explicit log file, auto-detecting the vendor."""
    path = path.resolve()
    if not path.exists():
        raise DocumentError(f"log file not found: {path}")

    for vendor, adapter_cls, _base_dir, _pattern in _vendor_configs():
        adapter = adapter_cls()
        try:
            session = stabilize_session(adapter.ingest_file(path), vendor=vendor, source=path)
        except Exception:
            continue

        project_identifier = infer_project_identifier(session, path, fallback=path.stem)
        if not project_identifier:
            project_identifier = path.stem

        key = normalize_project_key(project_identifier)
        session = session.model_copy(update={"trajectory_id": uuid5(NAMESPACE_URL, f"coding-trajectory:{key}")})
        trajectories = assemble_project_trajectories(project_identifier, [session])
        store = DocumentStore.from_trajectories(trajectories)
        source = DiscoverySource(vendor=vendor, path=path)
        return DiscoveryResult(store=store, sources=[source])

    raise DocumentError(f"no adapter could parse log file: {path}")


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

    return session.model_copy(update={"events": events, "turns": turns})
