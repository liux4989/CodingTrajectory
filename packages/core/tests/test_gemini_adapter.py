from __future__ import annotations

import json

from coding_trajectory.ingestion.adapters.gemini import GeminiAdapter
from coding_trajectory.ingestion.models import EventConfidence, EventProvenance, EventType


def test_gemini_adapter_emits_session_thought_and_permission_events(tmp_path) -> None:
    path = tmp_path / "session.json"
    path.write_text(
        json.dumps(
            {
                "sessionId": "52ac1b97-47dc-46bc-890b-75a2dbec4731",
                "projectHash": "project-hash",
                "startTime": "2026-03-13T10:00:00Z",
                "lastUpdated": "2026-03-13T10:00:10Z",
                "messages": [
                    {
                        "id": "user-1",
                        "timestamp": "2026-03-13T10:00:01Z",
                        "type": "user",
                        "content": "help me",
                    },
                    {
                        "id": "assistant-1",
                        "timestamp": "2026-03-13T10:00:02Z",
                        "type": "gemini",
                        "content": "I can help",
                        "model": "gemini-2.5-pro",
                        "tokens": {"input": 10, "output": 20, "total": 30},
                        "thoughts": [
                            {
                                "subject": "Plan",
                                "description": "Thinking through the fix",
                                "timestamp": "2026-03-13T10:00:02Z",
                            }
                        ],
                        "toolCalls": [
                            {
                                "id": "tool-1",
                                "name": "run_shell_command",
                                "args": {"cmd": "rm -rf /"},
                                "status": "cancelled",
                                "timestamp": "2026-03-13T10:00:03Z",
                                "result": "[Operation Cancelled] Reason: User did not allow tool call",
                                "resultDisplay": "Denied",
                            }
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    session = GeminiAdapter().ingest_file(path)
    events = session.events

    assert events[0].type == EventType.SESSION_STARTED
    assert any(event.type == EventType.LLM_STREAM_EVENT for event in events)
    denied = next(event for event in events if event.type == EventType.PERMISSION_DENIED)
    assert denied.provenance == EventProvenance.DERIVED
    assert denied.confidence == EventConfidence.LOW
    assert any(event.type == EventType.SESSION_ENDED for event in events)
