from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

from coding_trajectory_cli.cli import EXIT_USAGE, main


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


def test_trajectory_list_defaults_to_enrichment_for_current_project(tmp_path: Path, capsys, monkeypatch) -> None:
    setup_project_logs(tmp_path, monkeypatch)

    exit_code = main(["trajectory", "list"])

    assert exit_code == 0
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert len(output) == 2
    assert all("enrichment" in item for item in output)
    assert all(item["enrichment"]["derived"]["multi_agent_mode"] == "single_session" for item in output)
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
    assert Counter(item["enrichment"]["derived"]["multi_agent_mode"] for item in output) == {
        "single_session": 3,
    }


def test_session_get_defaults_to_canonical_payload(tmp_path: Path, capsys, monkeypatch) -> None:
    setup_project_logs(tmp_path, monkeypatch)

    exit_code = main(["session", "get", "019c92d8-0250-7291-8585-6f69c1f1e981"])

    assert exit_code == 0
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["session_id"] == "019c92d8-0250-7291-8585-6f69c1f1e981"
    assert "turn_ids" in output
    assert "turns" not in output
    assert "Discovered coding-agent logs:" not in captured.err


def test_session_get_pretty_is_rejected_without_enrichment_endpoint(tmp_path: Path, capsys, monkeypatch) -> None:
    setup_project_logs(tmp_path, monkeypatch)

    exit_code = main(["session", "get", "019c92d8-0250-7291-8585-6f69c1f1e981", "--view", "pretty"])

    assert exit_code == EXIT_USAGE
    assert "pretty view is not supported for session" in capsys.readouterr().err


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
    assert output["session_id"] == "019c92d8-0250-7291-8585-6f69c1f1e983"


def test_trajectory_get_defaults_to_enrichment_payload(tmp_path: Path, capsys, monkeypatch) -> None:
    setup_project_logs(tmp_path, monkeypatch)

    main(["session", "get", "019c92d8-0250-7291-8585-6f69c1f1e981"])
    session_output = json.loads(capsys.readouterr().out)
    trajectory_id = session_output["trajectory_id"]

    exit_code = main(["trajectory", "get", trajectory_id])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["trajectory_id"] == trajectory_id
    assert output["enrichment"]["derived"]["multi_agent_mode"] == "single_session"
    assert "project_identifier" not in output


def test_trajectory_get_fields_can_select_enrichment_keys(tmp_path: Path, capsys, monkeypatch) -> None:
    setup_project_logs(tmp_path, monkeypatch)

    main(["session", "get", "019c92d8-0250-7291-8585-6f69c1f1e981"])
    session_output = json.loads(capsys.readouterr().out)
    trajectory_id = session_output["trajectory_id"]

    exit_code = main(["trajectory", "get", trajectory_id, "--fields", "trajectory_id,enrichment"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["trajectory_id"] == trajectory_id
    assert "enrichment" in output


def test_session_list_can_filter_by_trajectory_in_raw_mode(tmp_path: Path, capsys, monkeypatch) -> None:
    setup_project_logs(tmp_path, monkeypatch)

    main(["session", "get", "019c92d8-0250-7291-8585-6f69c1f1e981"])
    session_output = json.loads(capsys.readouterr().out)
    trajectory_id = session_output["trajectory_id"]

    exit_code = main(["session", "list", "--trajectory-id", trajectory_id])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert len(output) == 1
    assert output[0]["session_id"] == "019c92d8-0250-7291-8585-6f69c1f1e981"
    assert "turn_ids" in output[0]


def test_session_list_pretty_is_rejected_without_enrichment_endpoint(tmp_path: Path, capsys, monkeypatch) -> None:
    setup_project_logs(tmp_path, monkeypatch)

    exit_code = main(["session", "list", "--view", "pretty"])

    assert exit_code == EXIT_USAGE
    assert "pretty view is not supported for session list" in capsys.readouterr().err


def test_trajectory_get_raw_includes_canonical_graph_fields(tmp_path: Path, capsys, monkeypatch) -> None:
    setup_project_logs(tmp_path, monkeypatch)

    main(["session", "get", "019c92d8-0250-7291-8585-6f69c1f1e981"])
    session_output = json.loads(capsys.readouterr().out)
    trajectory_id = session_output["trajectory_id"]

    exit_code = main(["trajectory", "get", trajectory_id, "--json"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["trajectory_id"] == trajectory_id
    assert output["summary"]["session_count"] == 1
    assert len(output["session_ids"]) == 1
    assert output["edges"] == []


def test_turn_get_raw(tmp_path: Path, capsys, monkeypatch) -> None:
    setup_project_logs(tmp_path, monkeypatch)

    main(["session", "get", "019c92d8-0250-7291-8585-6f69c1f1e981"])
    session = json.loads(capsys.readouterr().out)
    turn_id = session["turn_ids"][0]

    exit_code = main(["turn", "get", turn_id, "--json"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["turn_id"] == turn_id
    assert len(output["step_ids"]) >= 1
