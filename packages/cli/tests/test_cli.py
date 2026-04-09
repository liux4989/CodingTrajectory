from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from coding_trajectory_cli import cli


def _patch_dispatch(monkeypatch, responses: dict[str, object]) -> None:
    """Patch resolve_store and dispatch so the CLI calls the service layer with canned responses."""
    fake_store = MagicMock()
    fake_cache = MagicMock()
    fake_cache.save = MagicMock()

    monkeypatch.setattr(cli, "resolve_store", lambda params, **kw: (fake_store, ""))
    monkeypatch.setattr(cli, "IndexCache", MagicMock(load=MagicMock(return_value=fake_cache)))

    def fake_dispatch(method, params, **kw):
        key = (method, json.dumps(params, sort_keys=True))
        return responses[key]

    monkeypatch.setattr(cli, "dispatch", fake_dispatch)


def test_project_list_calls_rpc(monkeypatch, capsys) -> None:
    responses = {
        ("project.list", json.dumps({}, sort_keys=True)): {
            "items": ["my-app", "other-project"]
        }
    }
    _patch_dispatch(monkeypatch, responses)

    exit_code = cli.main(["project", "list"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["items"] == ["my-app", "other-project"]


def test_project_trajectories_lists_trajectories(monkeypatch, capsys) -> None:
    responses = {
        ("trajectory.list", json.dumps({"project_name": "my-app"}, sort_keys=True)): {
            "items": [{"trajectory_id": "t1"}]
        }
    }
    _patch_dispatch(monkeypatch, responses)

    exit_code = cli.main(["project", "trajectories", "my-app"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["items"][0]["trajectory_id"] == "t1"


def test_trajectory_overview_command_calls_rpc(monkeypatch, capsys) -> None:
    responses = {
        ("trajectory.overview", json.dumps({"trajectory_id": "t1"}, sort_keys=True)): {
            "trajectory_id": "t1",
            "sessions": [
                {
                    "session_id": "s1",
                    "connection": {"role": "main"},
                    "turns": [
                        {
                            "turn_id": "turn-1",
                            "user_request": "analyze the schema design",
                            "steps": [{"step_id": "step-1", "type": "tool_call"}],
                        }
                    ],
                }
            ],
        }
    }
    _patch_dispatch(monkeypatch, responses)

    exit_code = cli.main(["trajectory", "overview", "t1"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["trajectory_id"] == "t1"
    assert payload["sessions"][0]["session_id"] == "s1"
    assert payload["sessions"][0]["turns"][0]["steps"][0]["type"] == "tool_call"


def test_step_detail_command_calls_rpc(monkeypatch, capsys) -> None:
    responses = {
        ("step.details", json.dumps({"step_ids": ["step-1"]}, sort_keys=True)): [
            {
                "step_id": "step-1",
                "type": "tool_call",
                "operations": ["Read"],
                "shape": {
                    "tool_name": "Read",
                    "tool_input": {"file_path": "/src/foo.py"},
                    "tool_output": {"content": "..."},
                },
                "event_ids": ["e1", "e2"],
            }
        ]
    }
    _patch_dispatch(monkeypatch, responses)

    exit_code = cli.main(["step", "detail", "step-1"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert payload[0]["step_id"] == "step-1"
    assert payload[0]["type"] == "tool_call"
    assert payload[0]["shape"]["tool_name"] == "Read"
