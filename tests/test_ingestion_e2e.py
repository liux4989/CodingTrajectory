from __future__ import annotations

import json
from pathlib import Path

import pytest

from coding_trajectory.ingestion import AmpAdapter, ClaudeCodeAdapter, CodexAdapter
from coding_trajectory.ingestion.models import EventType, Vendor


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    return path


def _write_json(path: Path, record: dict) -> Path:
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("vendor", "source_factory", "expected_user_request", "expected_event_types"),
    [
        (
            Vendor.CODEX_CLI,
            lambda tmp_path: _write_jsonl(
                tmp_path / "codex-session.jsonl",
                [
                    {
                        "timestamp": "2026-03-13T10:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "019c92d8-0250-7291-8585-6f69c1f1e981",
                            "cwd": "/repo",
                            "source": "codex",
                        },
                    },
                    {
                        "timestamp": "2026-03-13T10:00:01Z",
                        "type": "turn_context",
                        "payload": {
                            "turn_id": "turn-1",
                            "approval_policy": "never",
                            "sandbox_policy": {"type": "danger-full-access"},
                        },
                    },
                    {
                        "timestamp": "2026-03-13T10:00:02Z",
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "review ingest pipeline"},
                    },
                    {
                        "timestamp": "2026-03-13T10:00:03Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "arguments": '{"cmd":"pytest"}',
                            "call_id": "call-1",
                        },
                    },
                    {
                        "timestamp": "2026-03-13T10:00:04Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-1",
                            "output": "Process exited with code 0",
                        },
                    },
                    {
                        "timestamp": "2026-03-13T10:00:05Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "turn-1",
                            "last_agent_message": "pipeline verified",
                        },
                    },
                ],
            ),
            "review ingest pipeline",
            {
                EventType.SESSION_STARTED,
                EventType.USER_PROMPT_SUBMITTED,
                EventType.TOOL_CALL_REQUESTED,
                EventType.TOOL_CALL_SUCCEEDED,
                EventType.TASK_COMPLETED,
            },
        ),
        (
            Vendor.AMP,
            lambda tmp_path: _write_json(
                tmp_path / "T-019c3c6b-8e41-7448-8a86-60a4edd6eed2.json",
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
                            "content": [{"type": "text", "text": "review ingest pipeline"}],
                            "meta": {"sentAt": 1770540231591},
                        },
                        {
                            "role": "assistant",
                            "messageId": 1,
                            "content": [
                                {"type": "thinking", "thinking": "Need to inspect logs"},
                                {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {"path": "file.py"}},
                            ],
                            "state": {"type": "complete", "stopReason": "tool_use"},
                            "usage": {
                                "model": "claude-opus-4-6",
                                "inputTokens": 10,
                                "outputTokens": 20,
                                "totalInputTokens": 30,
                            },
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
                            "content": [{"type": "text", "text": "ingest pipeline looks healthy"}],
                            "state": {"type": "complete", "stopReason": "end_turn"},
                            "usage": {
                                "model": "claude-opus-4-6",
                                "inputTokens": 11,
                                "outputTokens": 21,
                                "totalInputTokens": 32,
                            },
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
                },
            ),
            "review ingest pipeline",
            {
                EventType.SESSION_STARTED,
                EventType.USER_PROMPT_SUBMITTED,
                EventType.LLM_REQUEST_STARTED,
                EventType.TOOL_CALL_REQUESTED,
                EventType.TOOL_CALL_SUCCEEDED,
                EventType.AGENT_RESPONSE_COMPLETED,
                EventType.SESSION_ENDED,
            },
        ),
        (
            Vendor.CLAUDE_CODE,
            lambda tmp_path: _write_jsonl(
                tmp_path / "claude-session.jsonl",
                [
                    {
                        "type": "user",
                        "sessionId": "05e58bcb-fe0a-4324-b311-2568aa901c9c",
                        "timestamp": "2026-03-13T10:00:00Z",
                        "message": {"role": "user", "content": "review ingest pipeline"},
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
                                {"type": "thinking", "thinking": "I should inspect the pipeline"},
                                {"type": "tool_use", "id": "tool-1", "name": "TaskCreate", "input": {"prompt": "Inspect pipeline"}},
                                {"type": "text", "text": "Inspecting pipeline"},
                            ],
                            "stop_reason": "end_turn",
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
                        "toolUseResult": {"task": {"id": "task-1", "content": "Inspect pipeline", "status": "running"}},
                        "sourceToolAssistantUUID": "a-1",
                    },
                    {
                        "type": "system",
                        "sessionId": "05e58bcb-fe0a-4324-b311-2568aa901c9c",
                        "timestamp": "2026-03-13T10:00:03Z",
                        "subtype": "local_command",
                        "content": "<command-name>/exit</command-name>",
                        "uuid": "s-1",
                    },
                ],
            ),
            "review ingest pipeline",
            {
                EventType.USER_PROMPT_SUBMITTED,
                EventType.LLM_STREAM_EVENT,
                EventType.TOOL_CALL_REQUESTED,
                EventType.TOOL_CALL_SUCCEEDED,
                EventType.SUBTASK_STARTED,
                EventType.AGENT_RESPONSE_COMPLETED,
                EventType.SESSION_ENDED,
            },
        ),
    ],
)
def test_ingestion_pipeline_normalizes_vendor_sessions_end_to_end(
    tmp_path: Path,
    vendor: Vendor,
    source_factory,
    expected_user_request: str,
    expected_event_types: set[EventType],
) -> None:
    adapters = {
        Vendor.CODEX_CLI: CodexAdapter(),
        Vendor.AMP: AmpAdapter(),
        Vendor.CLAUDE_CODE: ClaudeCodeAdapter(),
    }

    session = adapters[vendor].ingest_file(source_factory(tmp_path))
    turns = session.turns
    events = session.events
    event_types = {event.type for event in events}

    assert session.vendor == vendor
    assert session.started_at <= session.ended_at
    assert turns
    assert turns[0].user_request == expected_user_request
    assert expected_event_types.issubset(event_types)
    assert all(event.session_id == session.session_id for event in events)
