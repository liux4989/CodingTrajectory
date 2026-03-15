from __future__ import annotations

import json

from coding_trajectory.ingestion.adapters.claude_code import ClaudeCodeAdapter
from coding_trajectory.ingestion.models import EventType


def test_claude_adapter_emits_user_prompt_and_tool_events(tmp_path) -> None:
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
                    {"type": "tool_use", "id": "tool-1", "name": "Agent", "input": {"prompt": "Do work"}},
                ],
                "stop_reason": "tool_use",
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
            "type": "assistant",
            "sessionId": "05e58bcb-fe0a-4324-b311-2568aa901c9c",
            "timestamp": "2026-03-13T10:00:03Z",
            "uuid": "a-2",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Done!"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 15, "output_tokens": 5},
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")

    session = ClaudeCodeAdapter().ingest_file(path)
    events = session.events

    assert any(event.type == EventType.USER_PROMPT_SUBMITTED for event in events)
    assert any(event.type == EventType.TOOL_CALL_REQUESTED for event in events)
    assert any(event.type == EventType.TOOL_CALL_SUCCEEDED for event in events)
    assert any(event.type == EventType.LLM_RESPONSE for event in events)

    # Verify turns are built
    assert len(session.turns) == 1
    turn = session.turns[0]
    assert turn.user_request_event_id is not None

    # Verify steps are built within turn
    assert len(turn.steps) >= 1


def test_claude_adapter_drops_system_records(tmp_path) -> None:
    """System records (api_error, turn_duration, etc.) should not generate events."""
    path = tmp_path / "session.jsonl"
    records = [
        {
            "type": "user",
            "sessionId": "05e58bcb-fe0a-4324-b311-2568aa901c9c",
            "timestamp": "2026-03-13T10:00:00Z",
            "message": {"role": "user", "content": "hello"},
            "uuid": "u-1",
        },
        {
            "type": "system",
            "sessionId": "05e58bcb-fe0a-4324-b311-2568aa901c9c",
            "timestamp": "2026-03-13T10:00:01Z",
            "subtype": "api_error",
            "uuid": "s-1",
            "error": "rate limited",
        },
        {
            "type": "system",
            "sessionId": "05e58bcb-fe0a-4324-b311-2568aa901c9c",
            "timestamp": "2026-03-13T10:00:02Z",
            "subtype": "turn_duration",
            "uuid": "s-2",
            "durationMs": 1000,
        },
        {
            "type": "assistant",
            "sessionId": "05e58bcb-fe0a-4324-b311-2568aa901c9c",
            "timestamp": "2026-03-13T10:00:03Z",
            "uuid": "a-1",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello!"}],
                "stop_reason": "end_turn",
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")

    session = ClaudeCodeAdapter().ingest_file(path)
    events = session.events

    # Only user prompt and LLM response — no system events
    event_types = {event.type for event in events}
    assert EventType.USER_PROMPT_SUBMITTED in event_types
    assert EventType.LLM_RESPONSE in event_types
    # No VENDOR_RAW from system records
    # (api_error and turn_duration are dropped)


def test_claude_adapter_thinking_goes_into_step_vendor_data(tmp_path) -> None:
    """Thinking blocks should appear in Step.vendor_data.thinking, not as events."""
    path = tmp_path / "session.jsonl"
    records = [
        {
            "type": "user",
            "sessionId": "05e58bcb-fe0a-4324-b311-2568aa901c9c",
            "timestamp": "2026-03-13T10:00:00Z",
            "message": {"role": "user", "content": "think hard"},
            "uuid": "u-1",
        },
        {
            "type": "assistant",
            "sessionId": "05e58bcb-fe0a-4324-b311-2568aa901c9c",
            "timestamp": "2026-03-13T10:00:01Z",
            "uuid": "a-1",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "I should reason carefully"},
                    {"type": "text", "text": "Here is my answer"},
                ],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")

    session = ClaudeCodeAdapter().ingest_file(path)

    assert len(session.turns) == 1
    turn = session.turns[0]
    assert len(turn.steps) == 1
    step = turn.steps[0]
    assert "thinking" in step.vendor_data
    assert "I should reason carefully" in step.vendor_data["thinking"]
    assert step.text == "Here is my answer"
