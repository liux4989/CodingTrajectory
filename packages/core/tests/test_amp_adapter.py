from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coding_trajectory.ingestion.adapters.amp import AmpAdapter
from coding_trajectory.ingestion.models import EventConfidence, EventProvenance, EventType


def test_amp_adapter_emits_trace_and_message_lifecycle_events(tmp_path) -> None:
    path = tmp_path / "T-019c3c6b-8e41-7448-8a86-60a4edd6eed2.json"
    path.write_text(
        json.dumps(
            {
                "v": 212,
                "id": "T-019c3c6b-8e41-7448-8a86-60a4edd6eed2",
                "created": 1770540207688,
                "title": "Thread title",
                "agentMode": "smart",
                "env": {
                    "initial": {
                        "trees": [
                            {
                                "displayName": "workspace",
                                "uri": "file:///workspace",
                                "repository": {"url": "git@example.com/repo.git", "ref": "main"},
                            }
                        ],
                        "platform": {"clientVersion": "1.0", "clientType": "cli", "os": "macOS"},
                    }
                },
                "messages": [
                    {
                        "role": "user",
                        "messageId": 0,
                        "content": [{"type": "text", "text": "why?"}],
                        "meta": {"sentAt": 1770540231591},
                    },
                    {
                        "role": "assistant",
                        "messageId": 1,
                        "content": [
                            {"type": "thinking", "thinking": "Need to inspect"},
                            {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {"path": "file.py"}},
                        ],
                        "state": {"type": "complete", "stopReason": "tool_use"},
                        "usage": {"model": "claude-opus-4-6", "inputTokens": 10, "outputTokens": 20, "totalInputTokens": 30},
                    },
                    {
                        "role": "user",
                        "messageId": 2,
                        "content": [{"type": "tool_result", "toolUseID": "tool-1", "run": {"status": "done", "result": "ok"}}],
                        "meta": {"sentAt": 1770540232591},
                    },
                    {
                        "role": "assistant",
                        "messageId": 3,
                        "content": [{"type": "text", "text": "Here is the answer"}],
                        "state": {"type": "complete", "stopReason": "end_turn"},
                        "usage": {"model": "claude-opus-4-6", "inputTokens": 11, "outputTokens": 21, "totalInputTokens": 32},
                    },
                ],
                "meta": {
                    "traces": [
                        {
                            "name": "inference",
                            "id": "trace-1",
                            "startTime": "2026-02-08T08:43:51.594Z",
                            "endTime": "2026-02-08T08:44:00.808Z",
                            "context": {"messageId": 1},
                        },
                        {
                            "name": "tools",
                            "id": "trace-2",
                            "startTime": "2026-02-08T08:44:00.808Z",
                            "endTime": "2026-02-08T08:44:00.818Z",
                            "context": {"messageId": 1},
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    session = AmpAdapter().ingest_file(path)
    events = session.events

    assert events[0].type == EventType.SESSION_STARTED
    llm_started = next(event for event in events if event.type == EventType.LLM_REQUEST_STARTED)
    assert llm_started.provenance == EventProvenance.DERIVED
    assert llm_started.confidence == EventConfidence.MEDIUM
    tool_started = next(event for event in events if event.type == EventType.TOOL_CALL_STARTED)
    assert tool_started.provenance == EventProvenance.DERIVED
    assert tool_started.confidence == EventConfidence.MEDIUM
    assert any(event.type == EventType.TOOL_CALL_SUCCEEDED for event in events)
    assert any(event.type == EventType.AGENT_RESPONSE_COMPLETED for event in events)
    assert any(item.kind == "turn" for item in session.timeline)
    assert {item.id for item in session.timeline if item.kind == "event"} == {
        event.event_id for event in session.events if event.turn_id is None
    }
    assert {item.id for item in session.timeline if item.kind == "turn"} == {turn.turn_id for turn in session.turns}
    assert session.extensions is not None
    assert session.extensions.amp is not None
    assert session.extensions.amp.thread_id == "T-019c3c6b-8e41-7448-8a86-60a4edd6eed2"
