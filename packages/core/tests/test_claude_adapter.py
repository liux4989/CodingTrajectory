from __future__ import annotations

import json

from coding_trajectory.ingestion.adapters.claude_code import ClaudeCodeAdapter
from coding_trajectory.ingestion.models import EventConfidence, EventProvenance, EventType


def test_claude_adapter_emits_stream_subtask_and_resume_events(tmp_path) -> None:
    path = tmp_path / "session.jsonl"
    records = [
        {
            "type": "user",
            "sessionId": "05e58bcb-fe0a-4324-b311-2568aa901c9c",
            "timestamp": "2026-03-13T10:00:00Z",
            "message": {"role": "user", "content": "ship it"},
            "uuid": "u-1",
            "permissionMode": "acceptEdits",
        },
        {
            "type": "assistant",
            "sessionId": "05e58bcb-fe0a-4324-b311-2568aa901c9c",
            "timestamp": "2026-03-13T10:00:01Z",
            "uuid": "a-1",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "I should create a task"},
                    {"type": "tool_use", "id": "tool-1", "name": "TaskCreate", "input": {"prompt": "Do work"}},
                ],
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        },
        {
            "type": "user",
            "sessionId": "05e58bcb-fe0a-4324-b311-2568aa901c9c",
            "timestamp": "2026-03-13T10:00:02Z",
            "uuid": "u-2",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "created"}],
            },
            "toolUseResult": {"task": {"id": "task-1", "content": "Do work", "status": "running"}},
            "sourceToolAssistantUUID": "a-1",
        },
        {
            "type": "system",
            "sessionId": "05e58bcb-fe0a-4324-b311-2568aa901c9c",
            "timestamp": "2026-03-13T10:00:03Z",
            "subtype": "local_command",
            "content": "<command-name>/resume</command-name>",
            "uuid": "s-1",
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")

    session = ClaudeCodeAdapter().ingest_file(path)
    events = session.events

    assert any(event.type == EventType.LLM_STREAM_EVENT for event in events)
    assert any(event.type == EventType.TOOL_CALL_REQUESTED for event in events)
    assert any(event.type == EventType.SUBTASK_STARTED for event in events)
    resumed = next(event for event in events if event.type == EventType.SESSION_RESUMED)
    assert resumed.provenance == EventProvenance.DERIVED
    assert resumed.confidence == EventConfidence.MEDIUM
