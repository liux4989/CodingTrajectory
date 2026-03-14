from __future__ import annotations

from collections import Counter
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from coding_trajectory_cli.cli import EXIT_NOT_FOUND, build_session_preview_context, enrich_preview_payload, main
from coding_trajectory.ingestion.models import Event, EventType, Vendor


def write_codex_log(
    path: Path,
    *,
    session_id: str,
    cwd: Path,
    message: str,
    include_tool: bool = False,
) -> None:
    records: list[dict[str, object]] = [
        {
            "timestamp": "2026-03-13T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": str(cwd)},
        },
        {
            "timestamp": "2026-03-13T10:00:01Z",
            "type": "turn_context",
            "payload": {
                "turn_id": f"turn-{session_id[-4:]}",
                "approval_policy": "never",
                "sandbox_policy": {"type": "danger-full-access"},
            },
        },
        {
            "timestamp": "2026-03-13T10:00:02Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": message},
        },
    ]

    if include_tool:
        records.extend(
            [
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
            ]
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")


def setup_project_logs(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    project_dir = tmp_path / "CodingTrajectory"
    project_dir.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project_dir)

    write_codex_log(
        home / ".codex" / "sessions" / "session-a.jsonl",
        session_id="019c92d8-0250-7291-8585-6f69c1f1e981",
        cwd=project_dir,
        message="fix the bug",
        include_tool=True,
    )
    write_codex_log(
        home / ".codex" / "sessions" / "session-b.jsonl",
        session_id="019c92d8-0250-7291-8585-6f69c1f1e982",
        cwd=project_dir,
        message="investigate the regression",
    )
    return home, project_dir


def test_trajectory_list_defaults_to_current_project(tmp_path: Path, capsys, monkeypatch) -> None:
    setup_project_logs(tmp_path, monkeypatch)

    exit_code = main(["trajectory", "list"])

    assert exit_code == 0
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert len(output) == 2
    assert all(item["project"] == "coding-trajectory" for item in output)
    assert all(item["session_count"] == 1 for item in output)
    assert all(item["multi_agent_mode"] == "in_session" for item in output)
    assert "Discovered coding-agent logs:" in captured.err


def test_trajectory_list_global_includes_other_projects(tmp_path: Path, capsys, monkeypatch) -> None:
    home, project_dir = setup_project_logs(tmp_path, monkeypatch)
    other_dir = tmp_path / "OtherProject"
    other_dir.mkdir()

    write_codex_log(
        home / ".codex" / "sessions" / "session-c.jsonl",
        session_id="019c92d8-0250-7291-8585-6f69c1f1e983",
        cwd=other_dir,
        message="task c",
    )

    exit_code = main(["trajectory", "list", "-g"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert Counter(item["project"] for item in output) == {
        "coding-trajectory": 2,
        "other-project": 1,
    }


def test_session_get_uses_current_project_discovery(tmp_path: Path, capsys, monkeypatch) -> None:
    setup_project_logs(tmp_path, monkeypatch)

    exit_code = main(["session", "get", "019c92d8-0250-7291-8585-6f69c1f1e981"])

    assert exit_code == 0
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["id"] == "019c92d8-0250-7291-8585-6f69c1f1e981"
    assert output["status"] == "completed"
    assert [item["kind"] for item in output["timeline"]] == ["event", "turn"]
    assert output["timeline"][0]["type"] == "session.started"
    assert output["timeline"][1]["preview"] == "fix the bug"
    assert output["timeline"][1]["event_count"] == 3
    assert "events" not in output["timeline"][1]
    assert output["timeline_count"] == 2
    assert "Discovered coding-agent logs:" not in captured.err


def test_session_preview_context_backfills_model_from_request_id() -> None:
    session_id = uuid4()
    timestamp = datetime(2026, 3, 13, 10, 0, tzinfo=timezone.utc)
    events = [
        Event(
            session_id=session_id,
            timestamp=timestamp,
            type=EventType.LLM_REQUEST_COMPLETED,
            vendor_source=Vendor.CLAUDE_CODE,
            actor="assistant",
            payload={"request_id": "req-1", "model": "claude-sonnet-4"},
        ),
        Event(
            session_id=session_id,
            timestamp=timestamp,
            type=EventType.LLM_STREAM_EVENT,
            vendor_source=Vendor.CLAUDE_CODE,
            actor="assistant",
            payload={"request_id": "req-1"},
        ),
    ]

    tool_name_by_call_id, model_by_request_id = build_session_preview_context(events)

    assert tool_name_by_call_id == {}
    assert model_by_request_id == {"req-1": "claude-sonnet-4"}
    assert enrich_preview_payload(
        {"request_id": "req-1"},
        tool_name_by_call_id=tool_name_by_call_id,
        model_by_request_id=model_by_request_id,
    ) == {
        "request_id": "req-1",
        "model": "claude-sonnet-4",
    }


def test_session_get_global_can_resolve_other_project_id(tmp_path: Path, capsys, monkeypatch) -> None:
    home, _ = setup_project_logs(tmp_path, monkeypatch)
    other_dir = tmp_path / "OtherProject"
    other_dir.mkdir()

    write_codex_log(
        home / ".codex" / "sessions" / "session-c.jsonl",
        session_id="019c92d8-0250-7291-8585-6f69c1f1e983",
        cwd=other_dir,
        message="task c",
    )

    exit_code = main(["session", "get", "019c92d8-0250-7291-8585-6f69c1f1e983", "-g"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["id"] == "019c92d8-0250-7291-8585-6f69c1f1e983"


def test_session_get_raw_matches_session_detail_shape(tmp_path: Path, capsys, monkeypatch) -> None:
    setup_project_logs(tmp_path, monkeypatch)

    exit_code = main(["session", "get", "019c92d8-0250-7291-8585-6f69c1f1e981", "--json"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["session_id"] == "019c92d8-0250-7291-8585-6f69c1f1e981"
    assert [item["kind"] for item in output["timeline"]] == ["event", "turn"]
    assert "events" not in output
    assert "turns" not in output


def test_trajectory_get_fields(tmp_path: Path, capsys, monkeypatch) -> None:
    setup_project_logs(tmp_path, monkeypatch)

    session_code = main(["session", "get", "019c92d8-0250-7291-8585-6f69c1f1e981"])
    assert session_code == 0
    session_output = json.loads(capsys.readouterr().out)
    trajectory_id = session_output["trajectory"]

    exit_code = main(["trajectory", "get", trajectory_id, "--fields", "id,session_count,multi_agent_mode"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["id"] == trajectory_id
    assert output["session_count"] == 1
    assert output["multi_agent_mode"] == "in_session"


def test_session_list_can_filter_by_trajectory(tmp_path: Path, capsys, monkeypatch) -> None:
    setup_project_logs(tmp_path, monkeypatch)

    main(["session", "get", "019c92d8-0250-7291-8585-6f69c1f1e981"])
    session_output = json.loads(capsys.readouterr().out)
    trajectory_id = session_output["trajectory"]

    exit_code = main(["session", "list", "--trajectory-id", trajectory_id])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert len(output) == 1
    assert output[0]["id"] == "019c92d8-0250-7291-8585-6f69c1f1e981"
    assert "timeline" not in output[0]
    assert output[0]["timeline_count"] == 2


def test_trajectory_get_raw_includes_graph_fields(tmp_path: Path, capsys, monkeypatch) -> None:
    setup_project_logs(tmp_path, monkeypatch)

    main(["session", "get", "019c92d8-0250-7291-8585-6f69c1f1e981"])
    session_output = json.loads(capsys.readouterr().out)
    trajectory_id = session_output["trajectory"]

    exit_code = main(["trajectory", "get", trajectory_id, "--json"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["trajectory_id"] == trajectory_id
    assert output["multi_agent_mode"] == "in_session"
    assert output["summary"]["session_count"] == 1
    assert len(output["session_refs"]) == 1
    assert output["edges"] == []
    assert len(output["sections"]) >= 1
    assert len(output["inference_notes"]) >= 1


def test_turn_get_raw(tmp_path: Path, capsys, monkeypatch) -> None:
    setup_project_logs(tmp_path, monkeypatch)

    main(["session", "get", "019c92d8-0250-7291-8585-6f69c1f1e981"])
    session = json.loads(capsys.readouterr().out)
    turn_id = session["timeline"][1]["id"]

    exit_code = main(["turn", "get", turn_id, "--json"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["turn_id"] == turn_id
    assert len(output["event_ids"]) >= 1


def test_turn_get_pretty_includes_event_ids(tmp_path: Path, capsys, monkeypatch) -> None:
    setup_project_logs(tmp_path, monkeypatch)

    main(["session", "get", "019c92d8-0250-7291-8585-6f69c1f1e981"])
    session = json.loads(capsys.readouterr().out)
    turn_id = session["timeline"][1]["id"]

    exit_code = main(["turn", "get", turn_id, "--view", "pretty"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["id"] == turn_id
    assert len(output["event_ids"]) >= 1


def test_event_get_pretty(tmp_path: Path, capsys, monkeypatch) -> None:
    setup_project_logs(tmp_path, monkeypatch)

    main(["session", "get", "019c92d8-0250-7291-8585-6f69c1f1e981"])
    session = json.loads(capsys.readouterr().out)
    turn_id = session["timeline"][1]["id"]
    main(["turn", "get", turn_id, "--json"])
    turn = json.loads(capsys.readouterr().out)
    event_id = turn["event_ids"][1]

    exit_code = main(["event", "get", event_id, "--view", "pretty"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["id"] == event_id
    assert output["type"] == "tool.call.requested"
    assert output["payload_preview"]["tool_name"] == "exec_command"


def test_missing_resource_returns_not_found(tmp_path: Path, capsys, monkeypatch) -> None:
    setup_project_logs(tmp_path, monkeypatch)

    exit_code = main(["event", "get", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"])

    assert exit_code == EXIT_NOT_FOUND
    assert "event not found" in capsys.readouterr().err
