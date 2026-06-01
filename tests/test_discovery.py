from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from coding_trajectory.discovery import discover_store_from_file, discover_store_from_files
from coding_trajectory.ingestion.adapters.amp import AmpAdapter
from coding_trajectory.ingestion.adapters.gemini import GeminiAdapter
from coding_trajectory.ingestion.adapters.pi import PiAdapter
from coding_trajectory.ingestion.models import Vendor


def _write_pi_session(path: Path) -> str:
    session_id = str(uuid4())
    records = [
        {
            "type": "session",
            "version": 3,
            "id": session_id,
            "timestamp": "2026-06-01T09:00:00.000Z",
            "cwd": "/tmp/project",
        },
        {
            "type": "message",
            "id": "abc00001",
            "parentId": None,
            "timestamp": "2026-06-01T09:00:01.000Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "hello"}],
                "timestamp": 1717232401000,
            },
        },
        {
            "type": "message",
            "id": "abc00002",
            "parentId": "abc00001",
            "timestamp": "2026-06-01T09:00:02.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Hi"}],
                "provider": "anthropic",
                "model": "claude-sonnet-4-5",
                "usage": {"input": 10, "output": 5, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 15},
                "stopReason": "stop",
                "timestamp": 1717232402000,
            },
        },
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return session_id


def test_discover_store_from_file_prefers_vendor_by_path(monkeypatch, tmp_path: Path) -> None:
    pi_root = tmp_path / ".pi" / "agent" / "sessions"
    pi_root.mkdir(parents=True)
    session_path = pi_root / "session.jsonl"
    session_id = _write_pi_session(session_path)

    def fake_vendor_configs():
        return [
            (Vendor.GEMINI_CLI, GeminiAdapter, tmp_path / ".gemini" / "tmp", "session-*.json"),
            (Vendor.AMP, AmpAdapter, tmp_path / ".local" / "share" / "amp" / "threads", "T-*.json"),
            (Vendor.PI, PiAdapter, pi_root, "*.jsonl"),
        ]

    monkeypatch.setattr("coding_trajectory.discovery._vendor_configs", fake_vendor_configs)

    discovery = discover_store_from_file(session_path)

    assert [source.vendor for source in discovery.sources] == [Vendor.PI]
    assert str(next(iter(discovery.store.sessions.values())).session_id) == session_id


def test_discover_store_from_files_prefers_vendor_by_path(monkeypatch, tmp_path: Path) -> None:
    pi_root = tmp_path / ".pi" / "agent" / "sessions"
    pi_root.mkdir(parents=True)
    session_path = pi_root / "session.jsonl"
    session_id = _write_pi_session(session_path)

    def fake_vendor_configs():
        return [
            (Vendor.GEMINI_CLI, GeminiAdapter, tmp_path / ".gemini" / "tmp", "session-*.json"),
            (Vendor.AMP, AmpAdapter, tmp_path / ".local" / "share" / "amp" / "threads", "T-*.json"),
            (Vendor.PI, PiAdapter, pi_root, "*.jsonl"),
        ]

    monkeypatch.setattr("coding_trajectory.discovery._vendor_configs", fake_vendor_configs)

    discovery = discover_store_from_files([session_path])

    assert [source.vendor for source in discovery.sources] == [Vendor.PI]
    assert str(next(iter(discovery.store.sessions.values())).session_id) == session_id
