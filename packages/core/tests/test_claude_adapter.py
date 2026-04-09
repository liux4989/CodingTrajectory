from __future__ import annotations

import json
from uuid import UUID

from coding_trajectory.ingestion.adapters.claude_code import ClaudeCodeAdapter
from coding_trajectory.ingestion.models import EventType, StepTextItem, StepToolItem, ToolStatus


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
    assert turn.event_ids

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
    assert any(isinstance(item, StepTextItem) and item.text == "Here is my answer" for item in step.items)


def test_claude_adapter_merges_tool_results_into_tool_items(tmp_path) -> None:
    path = tmp_path / "session.jsonl"
    records = [
        {
            "type": "user",
            "sessionId": "05e58bcb-fe0a-4324-b311-2568aa901c9c",
            "timestamp": "2026-03-13T10:00:00Z",
            "message": {"role": "user", "content": "ship it"},
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
                    {"type": "text", "text": "Checking"},
                    {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {"path": "a.py"}},
                ],
                "stop_reason": "tool_use",
            },
        },
        {
            "type": "user",
            "sessionId": "05e58bcb-fe0a-4324-b311-2568aa901c9c",
            "timestamp": "2026-03-13T10:00:02Z",
            "uuid": "u-2",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "ok"}],
            },
            "toolUseResult": {"result": "ok"},
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")

    session = ClaudeCodeAdapter().ingest_file(path)
    step = session.turns[0].steps[0]

    tool_item = next(item for item in step.items if isinstance(item, StepToolItem))
    assert tool_item.tool_name == "Read"
    assert tool_item.status == ToolStatus.COMPLETED
    assert tool_item.output == {"result": "ok"}


def test_claude_adapter_assigns_unique_session_id_to_subagent_files(tmp_path) -> None:
    project_dir = tmp_path / "project"
    root_session_id = "05e58bcb-fe0a-4324-b311-2568aa901c9c"
    path = project_dir / root_session_id / "subagents" / "agent-123.jsonl"
    path.parent.mkdir(parents=True)
    path.with_name("agent-123.meta.json").write_text(
        json.dumps({"agentType": "infra", "description": "router and auth context"}),
        encoding="utf-8",
    )
    records = [
        {
            "type": "user",
            "sessionId": root_session_id,
            "timestamp": "2026-03-13T10:00:00Z",
            "agentId": "agent-123",
            "isSidechain": True,
            "message": {"role": "user", "content": "do work"},
            "uuid": "u-1",
        },
        {
            "type": "assistant",
            "sessionId": root_session_id,
            "timestamp": "2026-03-13T10:00:01Z",
            "agentId": "agent-123",
            "isSidechain": True,
            "uuid": "a-1",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "working"}],
                "stop_reason": "end_turn",
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")

    session = ClaudeCodeAdapter().ingest_file(path)

    assert session.session_id != UUID(root_session_id)
    assert session.parent_session_id == UUID(root_session_id)
    assert session.extensions is not None
    assert session.extensions.claude_code is not None
    assert session.extensions.claude_code.agent_role == "infra"
    assert session.extensions.claude_code.description == "router and auth context"
    assert all(event.session_id == session.session_id for event in session.events)
    assert all(turn.session_id == session.session_id for turn in session.turns)
    assert all(step.session_id == session.session_id for turn in session.turns for step in turn.steps)


def test_claude_adapter_populates_turn_team_state_from_tool_outputs(tmp_path) -> None:
    path = tmp_path / "session.jsonl"
    records = [
        {
            "type": "user",
            "sessionId": "05e58bcb-fe0a-4324-b311-2568aa901c9c",
            "timestamp": "2026-03-13T10:00:00Z",
            "message": {"role": "user", "content": "create a team"},
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
                    {"type": "tool_use", "id": "tool-team", "name": "Agent", "input": {"prompt": "spawn infra"}},
                    {"type": "tool_use", "id": "tool-task", "name": "TaskCreate", "input": {"subject": "Setup auth"}},
                ],
                "stop_reason": "tool_use",
            },
        },
        {
            "type": "user",
            "sessionId": "05e58bcb-fe0a-4324-b311-2568aa901c9c",
            "timestamp": "2026-03-13T10:00:02Z",
            "uuid": "u-2",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tool-team", "content": "spawned"}],
            },
            "toolUseResult": {
                "status": "teammate_spawned",
                "teammate_id": "infra@alpha",
                "name": "infra",
                "team_name": "alpha",
                "agent_type": "general-purpose",
            },
        },
        {
            "type": "user",
            "sessionId": "05e58bcb-fe0a-4324-b311-2568aa901c9c",
            "timestamp": "2026-03-13T10:00:03Z",
            "uuid": "u-3",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tool-task", "content": "created"}],
            },
            "toolUseResult": {"task": {"id": "1", "subject": "Setup auth"}},
        },
        {
            "type": "assistant",
            "sessionId": "05e58bcb-fe0a-4324-b311-2568aa901c9c",
            "timestamp": "2026-03-13T10:00:04Z",
            "uuid": "a-2",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Team running"}],
                "stop_reason": "end_turn",
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")

    session = ClaudeCodeAdapter().ingest_file(path)

    turn = session.turns[0]
    assert turn.team_state is not None
    assert [member.member_id for member in turn.team_state.members] == ["infra@alpha"]
    assert turn.team_state.members[0].team_name == "alpha"
    assert [task.task_id for task in turn.team_state.tasks] == ["1"]
    assert turn.team_state.tasks[0].title == "Setup auth"
    assert turn.team_state.tasks[0].status == "created"


def test_claude_adapter_populates_turn_team_state_from_teammate_messages(tmp_path) -> None:
    path = tmp_path / "session.jsonl"
    teammate_payload = (
        '<teammate-message teammate_id="infra" color="blue" summary="Task 1 done, 2 and 3 unblocked">\n'
        "Task #1 complete.\n"
        "</teammate-message>"
    )
    records = [
        {
            "type": "user",
            "sessionId": "05e58bcb-fe0a-4324-b311-2568aa901c9c",
            "timestamp": "2026-03-13T10:00:00Z",
            "message": {"role": "user", "content": teammate_payload},
            "uuid": "u-1",
        },
        {
            "type": "assistant",
            "sessionId": "05e58bcb-fe0a-4324-b311-2568aa901c9c",
            "timestamp": "2026-03-13T10:00:01Z",
            "uuid": "a-1",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "received"}],
                "stop_reason": "end_turn",
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")

    session = ClaudeCodeAdapter().ingest_file(path)

    turn = session.turns[0]
    assert turn.team_state is not None
    assert [member.member_id for member in turn.team_state.members] == ["infra"]
    assert turn.team_state.members[0].summary == "Task 1 done, 2 and 3 unblocked"
    assert [task.task_id for task in turn.team_state.tasks] == ["1"]
    assert turn.team_state.tasks[0].status == "completed"
