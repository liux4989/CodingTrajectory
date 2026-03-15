from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from coding_trajectory.ingestion.adapters.codex import CodexAdapter
from coding_trajectory.ingestion.common import extract_exit_code
from coding_trajectory.ingestion.models import EventType


def test_codex_adapter_normalizes_failures_and_task_completion(tmp_path) -> None:
    path = tmp_path / "session.jsonl"
    records = [
        {
            "timestamp": "2026-03-13T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "019c92d8-0250-7291-8585-6f69c1f1e981", "cwd": "/repo"},
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
            "payload": {"type": "user_message", "message": "fix the bug"},
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
                "output": "Process exited with code 1",
            },
        },
        {
            "timestamp": "2026-03-13T10:00:05Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-1",
                "last_agent_message": "Done",
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")

    session = CodexAdapter().ingest_file(path)
    events = session.events

    assert any(event.type == EventType.USER_PROMPT_SUBMITTED for event in events)
    failure = next(event for event in events if event.type == EventType.TOOL_CALL_FAILED)
    assert failure.payload["exit_code"] == 1

    # Turns are built
    assert len(session.turns) == 1
    turn = session.turns[0]
    assert turn.user_request_event_id is not None

    # One step in the turn
    assert len(turn.steps) == 1
    step = turn.steps[0]
    # The last_agent_message from task_complete should be the step text
    assert step.text == "Done"


def test_codex_adapter_spawn_agent_tool_call_requested(tmp_path) -> None:
    path = tmp_path / "session.jsonl"
    records = [
        {
            "timestamp": "2026-03-13T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "019c92d8-0250-7291-8585-6f69c1f1e981", "cwd": "/repo"},
        },
        {
            "timestamp": "2026-03-13T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "spawn a worker"},
        },
        {
            "timestamp": "2026-03-13T10:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "spawn_agent",
                "arguments": '{"role":"implementer","nickname":"agent-1","task":"fix bug"}',
                "call_id": "call-spawn-1",
            },
        },
        {
            "timestamp": "2026-03-13T10:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-spawn-1",
                "output": '{"agent_id":"a-123","nickname":"agent-1"}',
            },
        },
        {
            "timestamp": "2026-03-13T10:00:10Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-1",
                "last_agent_message": "Spawned agent successfully",
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")

    session = CodexAdapter().ingest_file(path)
    events = session.events

    # spawn_agent should emit TOOL_CALL_REQUESTED
    spawn_requested = [
        e for e in events
        if e.type == EventType.TOOL_CALL_REQUESTED and e.payload.get("tool_name") == "spawn_agent"
    ]
    assert len(spawn_requested) == 1


def test_extract_exit_code_handles_nested_json_without_recursing_forever() -> None:
    output = json.dumps({"result": {"metadata": {"exit_code": 7}}})

    assert extract_exit_code(output) == 7


def test_codex_adapter_is_safe_to_reuse_across_concurrent_ingests(tmp_path) -> None:
    path_a = tmp_path / "session-a.jsonl"
    path_b = tmp_path / "session-b.jsonl"

    records_a = [
        {
            "timestamp": "2026-03-13T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "019c92d8-0250-7291-8585-6f69c1f1e981", "cwd": "/repo/a"},
        },
        {
            "timestamp": "2026-03-13T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "task a"},
        },
        {
            "timestamp": "2026-03-13T10:00:02Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "t1", "last_agent_message": "done a"},
        },
    ]
    records_b = [
        {
            "timestamp": "2026-03-13T11:00:00Z",
            "type": "session_meta",
            "payload": {"id": "019c92d8-0250-7291-8585-6f69c1f1e982", "cwd": "/repo/b"},
        },
        {
            "timestamp": "2026-03-13T11:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "task b"},
        },
        {
            "timestamp": "2026-03-13T11:00:02Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "t2", "last_agent_message": "done b"},
        },
    ]
    path_a.write_text("\n".join(json.dumps(record) for record in records_a), encoding="utf-8")
    path_b.write_text("\n".join(json.dumps(record) for record in records_b), encoding="utf-8")

    adapter = CodexAdapter()
    with ThreadPoolExecutor(max_workers=2) as executor:
        session_a, session_b = executor.map(adapter.ingest_file, [path_a, path_b])

    assert session_a.session_id != session_b.session_id

    # Check user request event IDs are set
    assert session_a.turns
    assert session_b.turns
