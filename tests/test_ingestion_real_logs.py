from __future__ import annotations

import re
from pathlib import Path

import pytest

from coding_trajectory.ingestion import AmpAdapter, ClaudeCodeAdapter, CodexAdapter, GeminiAdapter
from coding_trajectory.ingestion.adapters.base import BaseAdapter
from coding_trajectory.ingestion.models import EventType, Vendor

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_MAX_LOGS = 3

_VENDOR_CONFIGS: list[tuple[Vendor, type[BaseAdapter], Path, str]] = [
    (Vendor.CODEX_CLI, CodexAdapter, Path.home() / ".codex" / "sessions", "*.jsonl"),
    (Vendor.CLAUDE_CODE, ClaudeCodeAdapter, Path.home() / ".claude" / "projects", "*.jsonl"),
    (Vendor.GEMINI_CLI, GeminiAdapter, Path.home() / ".gemini" / "tmp", "session-*.json"),
    (Vendor.AMP, AmpAdapter, Path.home() / ".local" / "share" / "amp" / "threads", "T-*.json"),
]


def _recent_files(base_dir: Path, pattern: str) -> list[Path]:
    if not base_dir.is_dir():
        return []
    return sorted(base_dir.rglob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)


def _file_contains(path: Path, needle: str) -> bool:
    try:
        return needle in path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def _normalize_project_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _path_matches_project(path: Path) -> bool:
    project_token = _normalize_project_token(PROJECT_ROOT.name)
    return any(_normalize_project_token(part) == project_token for part in path.parts)


def _find_project_logs(base_dir: Path, pattern: str) -> list[Path]:
    project_root = str(PROJECT_ROOT)
    found: list[Path] = []
    for path in _recent_files(base_dir, pattern)[:100]:
        if _file_contains(path, project_root) or _path_matches_project(path):
            found.append(path)
            if len(found) >= _MAX_LOGS:
                break
    return found


def _build_params() -> list:
    params = []
    for vendor, adapter_cls, base_dir, pattern in _VENDOR_CONFIGS:
        logs = _find_project_logs(base_dir, pattern)
        if not logs:
            params.append(
                pytest.param(
                    vendor,
                    adapter_cls,
                    None,
                    id=f"{vendor.value}-no-logs",
                    marks=pytest.mark.skip(reason=f"No {vendor.value} log found for {PROJECT_ROOT}"),
                )
            )
        else:
            for i, log in enumerate(logs, 1):
                params.append(pytest.param(vendor, adapter_cls, log, id=f"{vendor.value}-log{i}"))
    return params


@pytest.mark.parametrize(("vendor", "adapter_cls", "source"), _build_params())
def test_ingestion_pipeline_real_project_logs(
    vendor: Vendor,
    adapter_cls: type[BaseAdapter],
    source: Path,
) -> None:
    session = adapter_cls().ingest_file(source)

    # --- Basic identity ---
    assert session.vendor == vendor
    assert session.session_id is not None
    assert session.started_at is not None
    assert session.ended_at is None or session.started_at <= session.ended_at

    # --- Events: non-empty, unique IDs, consistent session_id ---
    assert session.events, f"No events parsed from {source}"
    event_ids = [e.event_id for e in session.events]
    assert len(event_ids) == len(set(event_ids)), "Duplicate event_ids detected"
    assert all(e.session_id == session.session_id for e in session.events)
    assert any(e.type == EventType.USER_PROMPT_SUBMITTED for e in session.events)

    # --- Turns: non-empty, unique IDs, internally consistent ---
    assert session.turns, f"No turns produced from {source}"
    turn_ids = [t.turn_id for t in session.turns]
    assert len(turn_ids) == len(set(turn_ids)), "Duplicate turn_ids detected"
    assert all(t.event_ids for t in session.turns), "Every turn must reference at least one event"
    assert all(
        t.started_at <= (t.ended_at or t.started_at) for t in session.turns
    ), "Turn started_at must not exceed ended_at"

    # --- Turn ↔ event linkage is consistent ---
    turn_id_set = {t.turn_id for t in session.turns}
    session_event_id_set = {e.event_id for e in session.events}

    for event in session.events:
        if event.turn_id is not None:
            assert event.turn_id in turn_id_set, (
                f"event.turn_id {event.turn_id} references a turn not in session.turns"
            )

    all_turn_event_ids = {eid for t in session.turns for eid in t.event_ids}
    assert all_turn_event_ids <= session_event_id_set, (
        "Turn.event_ids contains IDs not present in session.events"
    )

    # --- Timeline coverage: every turn appears ---
    if session.timeline:
        timeline_ids = {item.id for item in session.timeline}
        for turn in session.turns:
            assert turn.turn_id in timeline_ids, f"Turn {turn.turn_id} is missing from session.timeline"

    # --- Payload sanity: all events carry a dict payload ---
    assert all(isinstance(e.payload, dict) for e in session.events), "Event payload must always be a dict"
