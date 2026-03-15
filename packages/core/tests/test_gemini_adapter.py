from __future__ import annotations

import json

from coding_trajectory.ingestion.adapters.gemini import GeminiAdapter
from coding_trajectory.ingestion.models import EventType, StepTextItem, StepToolItem, ToolStatus


def test_gemini_adapter_emits_user_tool_and_llm_events(tmp_path) -> None:
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
                                "args": {"cmd": "ls"},
                                "status": "success",
                                "timestamp": "2026-03-13T10:00:03Z",
                                "result": "file.py",
                                "resultDisplay": "file.py",
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

    assert any(event.type == EventType.USER_PROMPT_SUBMITTED for event in events)
    assert any(event.type == EventType.TOOL_CALL_REQUESTED for event in events)
    assert any(event.type == EventType.TOOL_CALL_SUCCEEDED for event in events)
    assert any(event.type == EventType.LLM_RESPONSE for event in events)

    # Turns
    assert len(session.turns) == 1
    turn = session.turns[0]
    assert turn.event_ids
    assert len(turn.steps) == 1
    step = turn.steps[0]

    # Thoughts go into step vendor_data, not as events
    assert "thoughts" in step.vendor_data
    assert any(isinstance(item, StepTextItem) and item.text == "I can help" for item in step.items)
    tool_item = next(item for item in step.items if isinstance(item, StepToolItem))
    assert tool_item.tool_name == "run_shell_command"
    assert tool_item.status == ToolStatus.COMPLETED


def test_gemini_adapter_cancelled_tool_becomes_failed(tmp_path) -> None:
    path = tmp_path / "session.json"
    path.write_text(
        json.dumps(
            {
                "sessionId": "52ac1b97-47dc-46bc-890b-75a2dbec4731",
                "startTime": "2026-03-13T10:00:00Z",
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

    # cancelled → TOOL_CALL_FAILED
    failed = [e for e in events if e.type == EventType.TOOL_CALL_FAILED]
    assert len(failed) == 1
    step = session.turns[0].steps[0]
    tool_item = next(item for item in step.items if isinstance(item, StepToolItem))
    assert tool_item.status == ToolStatus.FAILED
