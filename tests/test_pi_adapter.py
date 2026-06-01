from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from coding_trajectory.ingestion.adapters.pi import PiAdapter
from coding_trajectory.ingestion.models import StepTextItem, ToolStatus, Vendor


def _write_session(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / "session.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def _session(path: Path):
    return PiAdapter().ingest_file(path)


def _step_texts(path: Path) -> list[str]:
    session = PiAdapter().ingest_file(path)
    return [
        item.text
        for turn in session.turns
        for step in turn.steps
        for item in step.items
        if isinstance(item, StepTextItem)
    ]


def _step_item_kinds(path: Path) -> list[list[str]]:
    session = PiAdapter().ingest_file(path)
    return [[item.kind for item in step.items] for step in session.turns[0].steps]


def test_pi_adapter_parses_session_header(tmp_path: Path) -> None:
    session_uuid = str(uuid4())
    records = [
        {"type": "session", "version": 3, "id": session_uuid,
         "timestamp": "2026-06-01T09:00:00.000Z", "cwd": "/tmp/project"},
        {"type": "message", "id": "abc00001", "parentId": None,
         "timestamp": "2026-06-01T09:00:01.000Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "hello"}],
                     "timestamp": 1717232401000}},
        {"type": "message", "id": "abc00002", "parentId": "abc00001",
         "timestamp": "2026-06-01T09:00:02.000Z",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "Hi there!"}],
                     "provider": "anthropic", "model": "claude-sonnet-4-5",
                     "usage": {"input": 10, "output": 5, "cacheRead": 0,
                               "cacheWrite": 0, "totalTokens": 15},
                     "stopReason": "stop",
                     "timestamp": 1717232402000}},
    ]

    session = _session(_write_session(tmp_path, records))

    assert session.vendor == Vendor.PI
    assert str(session.session_id) == session_uuid
    assert session.extensions is not None
    assert session.extensions.pi is not None
    assert session.extensions.pi.cwd == "/tmp/project"
    assert session.extensions.pi.session_file is not None
    assert len(session.turns) == 1
    assert len(session.turns[0].steps) == 1


def test_pi_adapter_parses_user_and_assistant_text(tmp_path: Path) -> None:
    records = [
        {"type": "session", "version": 3, "id": str(uuid4()),
         "timestamp": "2026-06-01T09:00:00.000Z", "cwd": "/tmp/project"},
        {"type": "message", "id": "abc00001", "parentId": None,
         "timestamp": "2026-06-01T09:00:01.000Z",
         "message": {"role": "user",
                     "content": "Hello world",
                     "timestamp": 1717232401000}},
        {"type": "message", "id": "abc00002", "parentId": "abc00001",
         "timestamp": "2026-06-01T09:00:02.000Z",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "Hello!"}],
                     "provider": "anthropic", "model": "claude-sonnet-4-5",
                     "usage": {"input": 10, "output": 5, "cacheRead": 0,
                               "cacheWrite": 0, "totalTokens": 15},
                     "stopReason": "stop",
                     "timestamp": 1717232402000}},
    ]

    assert _step_texts(_write_session(tmp_path, records)) == ["Hello!"]


def test_pi_adapter_handles_tool_calls_and_results(tmp_path: Path) -> None:
    records = [
        {"type": "session", "version": 3, "id": str(uuid4()),
         "timestamp": "2026-06-01T09:00:00.000Z", "cwd": "/tmp/project"},
        {"type": "message", "id": "abc00001", "parentId": None,
         "timestamp": "2026-06-01T09:00:01.000Z",
         "message": {"role": "user",
                     "content": [{"type": "text", "text": "list files"}],
                     "timestamp": 1717232401000}},
        {"type": "message", "id": "abc00002", "parentId": "abc00001",
         "timestamp": "2026-06-01T09:00:02.000Z",
         "message": {"role": "assistant",
                     "content": [
                         {"type": "text", "text": "Running ls..."},
                         {"type": "toolCall", "id": "call_00_abc123",
                          "name": "bash", "arguments": {"command": "ls"}},
                     ],
                     "provider": "anthropic", "model": "claude-sonnet-4-5",
                     "usage": {"input": 50, "output": 20, "cacheRead": 0,
                               "cacheWrite": 0, "totalTokens": 70, "cost": 0.0123},
                     "stopReason": "toolUse",
                     "timestamp": 1717232402000}},
        {"type": "message", "id": "abc00003", "parentId": "abc00002",
         "timestamp": "2026-06-01T09:00:03.000Z",
         "message": {"role": "toolResult", "toolCallId": "call_00_abc123",
                     "toolName": "bash",
                     "content": [{"type": "text", "text": "file1.txt\nfile2.txt"}],
                     "isError": False, "timestamp": 1717232403000}},
    ]

    session = _session(_write_session(tmp_path, records))

    assert session.vendor == Vendor.PI
    assert len(session.turns) == 1
    step_kinds = [item.kind for step in session.turns[0].steps for item in step.items]
    assert step_kinds == ["text", "tool"]
    # Tool result updates the tool item attached to previous step
    tool_item = session.turns[0].steps[0].items[1]
    assert tool_item.output is not None
    assert tool_item.status == "completed"
    usage = session.turns[0].steps[0].vendor_data["metrics"]["usage"]
    assert usage["input_tokens"] == 50
    assert usage["output_tokens"] == 20
    assert usage["total_tokens"] == 70


def test_pi_adapter_captures_unified_bash_execution_messages(tmp_path: Path) -> None:
    records = [
        {"type": "session", "version": 3, "id": str(uuid4()),
         "timestamp": "2026-06-01T09:00:00.000Z", "cwd": "/tmp/project"},
        {"type": "message", "id": "abc00001", "parentId": None,
         "timestamp": "2026-06-01T09:00:01.000Z",
         "message": {"role": "user",
                     "content": [{"type": "text", "text": "run test"}],
                     "timestamp": 1717232401000}},
        {"type": "message", "id": "abc00002", "parentId": "abc00001",
         "timestamp": "2026-06-01T09:00:02.000Z",
         "message": {"role": "assistant",
                     "content": [
                         {"type": "toolCall", "id": "call_00_abc123",
                          "name": "bash", "arguments": {"command": "pytest"}},
                     ],
                     "provider": "anthropic", "model": "claude-sonnet-4-5",
                     "usage": {"input": 50, "output": 20, "cacheRead": 0,
                               "cacheWrite": 0, "totalTokens": 70},
                     "stopReason": "toolUse",
                     "timestamp": 1717232402000}},
        {"type": "message", "id": "abc00003", "parentId": "abc00002",
         "timestamp": "2026-06-01T09:00:03.000Z",
         "message": {"role": "bashExecution", "command": "pytest",
                     "output": "3 passed", "exitCode": 0, "cancelled": False,
                     "truncated": False, "timestamp": 1717232403000}},
    ]

    session = _session(_write_session(tmp_path, records))

    tools = [item for step in session.turns[0].steps for item in step.items if item.kind == "tool"]
    assert len(tools) == 1
    assert tools[0].tool_call_id == "call_00_abc123"
    assert tools[0].status == ToolStatus.COMPLETED
    assert tools[0].output == "$ pytest\n3 passed"


def test_pi_adapter_tracks_model_changes(tmp_path: Path) -> None:
    records = [
        {"type": "session", "version": 3, "id": str(uuid4()),
         "timestamp": "2026-06-01T09:00:00.000Z", "cwd": "/tmp/project"},
        {"type": "model_change", "id": "mc000001", "parentId": None,
         "timestamp": "2026-06-01T09:00:00.500Z",
         "provider": "anthropic", "modelId": "claude-sonnet-4-5"},
        {"type": "message", "id": "abc00001", "parentId": "mc000001",
         "timestamp": "2026-06-01T09:00:01.000Z",
         "message": {"role": "user",
                     "content": [{"type": "text", "text": "hello"}],
                     "timestamp": 1717232401000}},
        {"type": "message", "id": "abc00002", "parentId": "abc00001",
         "timestamp": "2026-06-01T09:00:02.000Z",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "Hi!"}],
                     "provider": "anthropic", "model": "claude-sonnet-4-5",
                     "usage": {"input": 10, "output": 5, "cacheRead": 0,
                               "cacheWrite": 0, "totalTokens": 15},
                     "stopReason": "stop",
                     "timestamp": 1717232402000}},
    ]

    session = _session(_write_session(tmp_path, records))

    assert session.extensions is not None
    assert session.extensions.pi is not None
    assert session.extensions.pi.provider == "anthropic"
    assert session.extensions.pi.model == "claude-sonnet-4-5"


def test_pi_adapter_captures_thinking_blocks(tmp_path: Path) -> None:
    records = [
        {"type": "session", "version": 3, "id": str(uuid4()),
         "timestamp": "2026-06-01T09:00:00.000Z", "cwd": "/tmp/project"},
        {"type": "message", "id": "abc00001", "parentId": None,
         "timestamp": "2026-06-01T09:00:01.000Z",
         "message": {"role": "user",
                     "content": [{"type": "text", "text": "solve problem"}],
                     "timestamp": 1717232401000}},
        {"type": "message", "id": "abc00002", "parentId": "abc00001",
         "timestamp": "2026-06-01T09:00:02.000Z",
         "message": {"role": "assistant",
                     "content": [
                         {"type": "thinking", "thinking": "Let me analyze this..."},
                         {"type": "text", "text": "Here is the solution"},
                     ],
                     "provider": "anthropic", "model": "claude-sonnet-4-5",
                     "usage": {"input": 100, "output": 50, "cacheRead": 0,
                               "cacheWrite": 0, "totalTokens": 150},
                     "stopReason": "stop",
                     "timestamp": 1717232402000}},
    ]

    session = _session(_write_session(tmp_path, records))

    assert len(session.turns) == 1
    assert len(session.turns[0].steps) == 1
    vendor_data = session.turns[0].steps[0].vendor_data
    assert "thinking" in vendor_data
    assert vendor_data["thinking"] == ["Let me analyze this..."]


def test_pi_adapter_skips_non_message_entries(tmp_path: Path) -> None:
    session_uuid = str(uuid4())
    records = [
        {"type": "session", "version": 3, "id": session_uuid,
         "timestamp": "2026-06-01T09:00:00.000Z", "cwd": "/tmp/project"},
        {"type": "compaction", "id": "comp0001", "parentId": None,
         "timestamp": "2026-06-01T09:00:00.100Z",
         "summary": "earlier work...", "firstKeptEntryId": "abc00001", "tokensBefore": 50000},
        {"type": "message", "id": "abc00001", "parentId": "comp0001",
         "timestamp": "2026-06-01T09:00:01.000Z",
         "message": {"role": "user",
                     "content": [{"type": "text", "text": "continue"}],
                     "timestamp": 1717232401000}},
        {"type": "message", "id": "abc00002", "parentId": "abc00001",
         "timestamp": "2026-06-01T09:00:02.000Z",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "Continuing work"}],
                     "provider": "anthropic", "model": "claude-sonnet-4-5",
                     "usage": {"input": 10, "output": 5, "cacheRead": 0,
                               "cacheWrite": 0, "totalTokens": 15},
                     "stopReason": "stop",
                     "timestamp": 1717232402000}},
    ]

    session = _session(_write_session(tmp_path, records))

    assert session.vendor == Vendor.PI
    assert len(session.turns) == 1


def test_pi_adapter_parses_session_name(tmp_path: Path) -> None:
    records = [
        {"type": "session", "version": 3, "id": str(uuid4()),
         "timestamp": "2026-06-01T09:00:00.000Z", "cwd": "/tmp/project"},
        {"type": "session_info", "id": "si000001", "parentId": None,
         "timestamp": "2026-06-01T09:00:00.100Z", "name": "Refactor auth module"},
        {"type": "message", "id": "abc00001", "parentId": "si000001",
         "timestamp": "2026-06-01T09:00:01.000Z",
         "message": {"role": "user",
                     "content": [{"type": "text", "text": "start"}],
                     "timestamp": 1717232401000}},
        {"type": "message", "id": "abc00002", "parentId": "abc00001",
         "timestamp": "2026-06-01T09:00:02.000Z",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "OK"}],
                     "provider": "anthropic", "model": "claude-sonnet-4-5",
                     "usage": {"input": 10, "output": 5, "cacheRead": 0,
                               "cacheWrite": 0, "totalTokens": 15},
                     "stopReason": "stop",
                     "timestamp": 1717232402000}},
    ]

    session = _session(_write_session(tmp_path, records))

    assert session.extensions is not None
    assert session.extensions.pi is not None
    assert session.extensions.pi.title == "Refactor auth module"
