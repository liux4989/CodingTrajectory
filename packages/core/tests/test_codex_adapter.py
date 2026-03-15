from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

from coding_trajectory.ingestion.adapters.codex import CodexAdapter
from coding_trajectory.ingestion.common import extract_exit_code
from coding_trajectory.ingestion.models import EventType, StepTextItem, StepToolItem, ToolStatus
from coding_trajectory.trajectory import assemble_project_trajectories


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
    assert turn.event_ids

    # One step in the turn
    assert len(turn.steps) == 1
    step = turn.steps[0]
    assert any(isinstance(item, StepTextItem) and item.text == "Done" for item in step.items)
    failed_tool = next(item for item in step.items if isinstance(item, StepToolItem))
    assert failed_tool.tool_name == "exec_command"
    assert failed_tool.status == ToolStatus.FAILED


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


def test_codex_adapter_maps_parent_from_forked_from_id(tmp_path) -> None:
    parent_session_id = "019cc3a1-0526-7722-b2a7-9616e0c0097c"
    path = tmp_path / "child-session.jsonl"
    records = [
        {
            "timestamp": "2026-03-15T14:51:59.272Z",
            "type": "session_meta",
            "payload": {
                "id": "019cc3a2-4b7d-7fc1-a5b6-5a11d8d3c8c5",
                "forked_from_id": parent_session_id,
                "cwd": "/repo",
                "source": "cli",
            },
        },
        {
            "timestamp": "2026-03-15T14:52:00.272Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "continue"},
        },
        {
            "timestamp": "2026-03-15T14:52:01.272Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": "done"},
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")

    session = CodexAdapter().ingest_file(path)

    assert session.parent_session_id == UUID(parent_session_id)
    assert session.extensions is not None and session.extensions.codex is not None
    assert session.extensions.codex.forked_from_id == parent_session_id


def test_codex_adapter_maps_parent_from_thread_spawn_parent_id(tmp_path) -> None:
    parent_session_id = "019cb256-2ff7-72e1-9c02-758a401a3511"
    path = tmp_path / "spawned-session.jsonl"
    records = [
        {
            "timestamp": "2026-03-03T06:16:16.743Z",
            "type": "session_meta",
            "payload": {
                "id": "019cb257-1211-70e2-a008-d0d419f60cab",
                "cwd": "/repo",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": parent_session_id,
                            "depth": 1,
                            "agent_nickname": "Boole",
                            "agent_role": "explorer",
                        }
                    }
                },
            },
        },
        {
            "timestamp": "2026-03-03T06:16:17.743Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "work"},
        },
        {
            "timestamp": "2026-03-03T06:16:18.743Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": "done"},
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")

    session = CodexAdapter().ingest_file(path)

    assert session.parent_session_id == UUID(parent_session_id)
    assert session.extensions is not None and session.extensions.codex is not None
    assert session.extensions.codex.spawn_parent_thread_id == parent_session_id
    assert session.extensions.codex.spawn_depth == 1
    assert session.extensions.codex.agent_nickname == "Boole"
    assert session.extensions.codex.agent_role == "explorer"


def test_codex_adapter_captures_context_compacted_as_vendor_raw(tmp_path) -> None:
    path = tmp_path / "session-with-compaction.jsonl"
    records = [
        {
            "timestamp": "2026-03-15T09:29:00.000Z",
            "type": "session_meta",
            "payload": {"id": "019cf089-f9e9-7a23-a61e-b51f877784db", "cwd": "/repo"},
        },
        {
            "timestamp": "2026-03-15T09:29:10.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "continue"},
        },
        {
            "timestamp": "2026-03-15T09:29:51.609Z",
            "type": "event_msg",
            "payload": {"type": "context_compacted", "turn_id": "turn-1"},
        },
        {
            "timestamp": "2026-03-15T09:30:00.000Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": "done"},
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")

    session = CodexAdapter().ingest_file(path)

    compacted_events = [
        event
        for event in session.events
        if event.type == EventType.VENDOR_RAW and event.payload.get("raw_type") == "context_compacted"
    ]
    assert len(compacted_events) == 1
    assert compacted_events[0].payload.get("turn_id_raw") == "turn-1"

    assert len(session.turns) == 1
    assert len(session.turns[0].steps) == 1


def test_codex_parent_linked_sessions_stay_in_one_trajectory_component(tmp_path) -> None:
    root_id = "019cc3a1-0526-7722-b2a7-9616e0c0097c"
    child_id = "019cc3a2-4b7d-7fc1-a5b6-5a11d8d3c8c5"
    grandchild_id = "019cc3a3-4b7d-7fc1-a5b6-5a11d8d3c8c6"

    def _records(session_id: str, *, parent_id: str | None) -> list[dict[str, object]]:
        payload = {"id": session_id, "cwd": "/repo"}
        if parent_id is not None:
            payload["forked_from_id"] = parent_id
        return [
            {
                "timestamp": "2026-03-15T14:51:59.272Z",
                "type": "session_meta",
                "payload": payload,
            },
            {
                "timestamp": "2026-03-15T14:52:00.272Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "continue"},
            },
            {
                "timestamp": "2026-03-15T14:52:01.272Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": "done"},
            },
        ]

    path_root = tmp_path / "root.jsonl"
    path_child = tmp_path / "child.jsonl"
    path_grandchild = tmp_path / "grandchild.jsonl"
    path_root.write_text("\n".join(json.dumps(record) for record in _records(root_id, parent_id=None)), encoding="utf-8")
    path_child.write_text("\n".join(json.dumps(record) for record in _records(child_id, parent_id=root_id)), encoding="utf-8")
    path_grandchild.write_text(
        "\n".join(json.dumps(record) for record in _records(grandchild_id, parent_id=child_id)),
        encoding="utf-8",
    )

    adapter = CodexAdapter()
    sessions = [
        adapter.ingest_file(path_grandchild),
        adapter.ingest_file(path_root),
        adapter.ingest_file(path_child),
    ]

    trajectories = assemble_project_trajectories("test-proj", sessions)

    assert len(trajectories) == 1
    assert {session.session_id for session in trajectories[0].sessions} == {
        UUID(root_id),
        UUID(child_id),
        UUID(grandchild_id),
    }


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
