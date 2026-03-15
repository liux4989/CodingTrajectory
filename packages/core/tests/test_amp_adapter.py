from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coding_trajectory.ingestion.adapters.amp import AmpAdapter
from coding_trajectory.ingestion.models import EventType, StepTextItem, StepToolItem, ToolStatus


def test_amp_adapter_emits_message_lifecycle_events(tmp_path) -> None:
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

    assert any(event.type == EventType.USER_PROMPT_SUBMITTED for event in events)
    assert any(event.type == EventType.TOOL_CALL_REQUESTED for event in events)
    assert any(event.type == EventType.TOOL_CALL_SUCCEEDED for event in events)
    assert any(event.type == EventType.LLM_RESPONSE for event in events)

    # Turns and steps
    assert len(session.turns) == 1
    turn = session.turns[0]
    assert turn.user_request_event_id is not None
    assert turn.event_ids
    assert len(turn.steps) >= 1

    # Thinking in step vendor_data
    steps_with_thinking = [s for s in turn.steps if "thinking" in s.vendor_data]
    assert len(steps_with_thinking) >= 1
    assert any(isinstance(item, StepToolItem) for item in turn.steps[0].items)
    assert any(isinstance(item, StepTextItem) and item.text == "Here is the answer" for item in turn.steps[-1].items)

    # Extensions
    assert session.extensions is not None
    assert session.extensions.amp is not None
    assert session.extensions.amp.thread_id == "T-019c3c6b-8e41-7448-8a86-60a4edd6eed2"


def test_amp_adapter_keeps_step_event_association_without_sent_timestamps(tmp_path) -> None:
    path = tmp_path / "T-019c3c6b-8e41-7448-8a86-60a4edd6eed3.json"
    path.write_text(
        json.dumps(
            {
                "v": 212,
                "id": "T-019c3c6b-8e41-7448-8a86-60a4edd6eed3",
                "created": 1770540207688,
                "messages": [
                    {
                        "role": "user",
                        "messageId": 0,
                        "content": [{"type": "text", "text": "do work"}],
                        "meta": {"sentAt": 1770540231591},
                    },
                    {
                        "role": "assistant",
                        "messageId": 1,
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tool-1",
                                "name": "Read",
                                "input": {"path": "a.py"},
                            }
                        ],
                        "state": {"type": "complete", "stopReason": "tool_use"},
                    },
                    {
                        "role": "user",
                        "messageId": 2,
                        "content": [
                            {
                                "type": "tool_result",
                                "toolUseID": "tool-1",
                                "run": {"status": "done", "result": "ok"},
                            }
                        ],
                    },
                    {
                        "role": "assistant",
                        "messageId": 3,
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tool-2",
                                "name": "Edit",
                                "input": {"path": "a.py"},
                            }
                        ],
                        "state": {"type": "complete", "stopReason": "tool_use"},
                    },
                    {
                        "role": "user",
                        "messageId": 4,
                        "content": [
                            {
                                "type": "tool_result",
                                "toolUseID": "tool-2",
                                "run": {"status": "done", "result": "ok"},
                            }
                        ],
                    },
                    {
                        "role": "assistant",
                        "messageId": 5,
                        "content": [{"type": "text", "text": "done"}],
                        "state": {"type": "complete", "stopReason": "end_turn"},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    session = AmpAdapter().ingest_file(path)

    assert len(session.turns) == 1
    turn = session.turns[0]
    assert len(turn.steps) == 3

    events_by_id = {event.event_id: event for event in session.events}
    unique_timestamps = {event.timestamp for event in session.events}
    assert len(unique_timestamps) > 1

    def _step_tool_ids(step_idx: int) -> set[str]:
        step = turn.steps[step_idx]
        return {
            tool_call_id
            for event_id in step.event_ids
            for tool_call_id in [events_by_id[event_id].payload.get("tool_call_id")]
            if isinstance(tool_call_id, str) and tool_call_id
        }

    assert _step_tool_ids(0) == {"tool-1"}
    assert _step_tool_ids(1) == {"tool-2"}
    assert _step_tool_ids(2) == set()
    assert next(item for item in turn.steps[0].items if isinstance(item, StepToolItem)).status == ToolStatus.COMPLETED
    assert next(item for item in turn.steps[1].items if isinstance(item, StepToolItem)).status == ToolStatus.COMPLETED
