from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from coding_trajectory.ingestion.adapters.codex import CodexAdapter
from coding_trajectory.ingestion.models import SessionStatus, StepTextItem, TurnStatus


def _write_rollout(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / "rollout.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def _step_texts(path: Path) -> list[str]:
    session = CodexAdapter().ingest_file(path)
    return [
        item.text
        for step in session.turns[0].steps
        for item in step.items
        if isinstance(item, StepTextItem)
    ]


def _step_item_kinds(path: Path) -> list[list[str]]:
    session = CodexAdapter().ingest_file(path)
    return [[item.kind for item in step.items] for step in session.turns[0].steps]


def _session(path: Path):
    return CodexAdapter().ingest_file(path)


def test_codex_adapter_keeps_response_item_final_answer_and_ignores_task_complete_copy(tmp_path: Path) -> None:
    session_id = str(uuid4())
    final_text = (
        "Implemented the feature.\n\n"
        "<oai-mem-citation>\n"
        "<citation_entries>\n"
        "MEMORY.md:1-2|note=[example]\n"
        "</citation_entries>\n"
        "<rollout_ids>\n"
        "00000000-0000-0000-0000-000000000000\n"
        "</rollout_ids>\n"
        "</oai-mem-citation>"
    )
    records = [
        {"type": "session_meta", "payload": {"id": session_id, "cwd": "/tmp/project"}},
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "do it"},
        },
        {
            "timestamp": "2026-01-01T00:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": final_text}],
            },
        },
        {
            "timestamp": "2026-01-01T00:00:02Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": "Implemented the feature."},
        },
    ]

    assert _step_texts(_write_rollout(tmp_path, records)) == [final_text]


def test_codex_adapter_ignores_stale_task_complete_message_when_final_answer_exists(tmp_path: Path) -> None:
    session_id = str(uuid4())
    commentary = "The current split is exactly what you want to remove."
    final_text = "<proposed_plan>\n# Refactor Plan\n\nUse one canonical source artifact.\n</proposed_plan>"
    records = [
        {"type": "session_meta", "payload": {"id": session_id, "cwd": "/tmp/project"}},
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "plan it"},
        },
        {
            "timestamp": "2026-01-01T00:00:01Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "phase": "commentary", "message": commentary},
        },
        {
            "timestamp": "2026-01-01T00:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": commentary}],
            },
        },
        {
            "timestamp": "2026-01-01T00:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": final_text}],
            },
        },
        {
            "timestamp": "2026-01-01T00:00:03Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": commentary},
        },
    ]

    path = _write_rollout(tmp_path, records)

    assert _step_texts(path) == [commentary, final_text]
    assert _step_item_kinds(path) == [["text"], ["text"]]


def test_codex_adapter_uses_task_complete_message_when_no_final_answer_exists(tmp_path: Path) -> None:
    session_id = str(uuid4())
    commentary = "I’m checking the current implementation."
    fallback_final = "Use the existing source artifact and remove the academic artifact path."
    records = [
        {"type": "session_meta", "payload": {"id": session_id, "cwd": "/tmp/project"}},
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "what should we do"},
        },
        {
            "timestamp": "2026-01-01T00:00:01Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "phase": "commentary", "message": commentary},
        },
        {
            "timestamp": "2026-01-01T00:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": commentary}],
            },
        },
        {
            "timestamp": "2026-01-01T00:00:02Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": fallback_final},
        },
    ]

    assert _step_texts(_write_rollout(tmp_path, records)) == [commentary, fallback_final]


def test_codex_adapter_marks_superseded_turn_as_interrupted(tmp_path: Path) -> None:
    session_id = str(uuid4())
    first_commentary = "I’ll start this change."
    second_final = "Completed the revised request."
    records = [
        {"type": "session_meta", "payload": {"id": session_id, "cwd": "/tmp/project"}},
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "first request"},
        },
        {
            "timestamp": "2026-01-01T00:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": first_commentary}],
            },
        },
        {
            "timestamp": "2026-01-01T00:00:02Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "second request"},
        },
        {
            "timestamp": "2026-01-01T00:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": second_final}],
            },
        },
        {
            "timestamp": "2026-01-01T00:00:04Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": second_final},
        },
    ]

    session = _session(_write_rollout(tmp_path, records))

    assert session.status == SessionStatus.COMPLETED
    assert [turn.status for turn in session.turns] == [TurnStatus.INTERRUPTED, TurnStatus.COMPLETED]
    assert [
        item.text
        for turn in session.turns
        for step in turn.steps
        for item in step.items
        if isinstance(item, StepTextItem)
    ] == [first_commentary, second_final]


def test_codex_adapter_marks_open_recent_turn_as_running(tmp_path: Path) -> None:
    session_id = str(uuid4())
    commentary = "Still working on it."
    records = [
        {"type": "session_meta", "payload": {"id": session_id, "cwd": "/tmp/project"}},
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "do long work"},
        },
        {
            "timestamp": "2026-01-01T00:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": commentary}],
            },
        },
    ]

    session = _session(_write_rollout(tmp_path, records))

    assert session.status == SessionStatus.ACTIVE
    assert session.turns[0].status == TurnStatus.RUNNING
