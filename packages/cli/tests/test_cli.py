from __future__ import annotations

import json

from coding_trajectory_cli import cli


class _FakeRpcClient:
    def __init__(self, responses: dict[tuple[str, str], object], *, global_scope: bool = False, log_file=None):
        self._responses = responses
        self.global_scope = global_scope

    def call(self, method: str, params: dict) -> object:
        key = (method, json.dumps(params, sort_keys=True))
        return self._responses[key]

    def __enter__(self) -> _FakeRpcClient:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        return None


def test_list_command_calls_rpc(monkeypatch, capsys) -> None:
    responses = {
        ("trajectory.list", json.dumps({}, sort_keys=True)): {
            "items": [{"trajectory_id": "t1", "session_ids": ["s1"]}]
        }
    }
    monkeypatch.setattr(cli, "RpcClient", lambda global_scope=False, log_file=None: _FakeRpcClient(responses))

    exit_code = cli.main(["list"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["items"][0]["trajectory_id"] == "t1"


def test_list_projects_command_calls_rpc(monkeypatch, capsys) -> None:
    responses = {
        ("project.list", json.dumps({}, sort_keys=True)): {
            "items": ["my-app", "other-project"]
        }
    }
    monkeypatch.setattr(cli, "RpcClient", lambda global_scope=False, log_file=None: _FakeRpcClient(responses))

    exit_code = cli.main(["project", "list"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["items"] == ["my-app", "other-project"]


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
    monkeypatch.setattr(cli, "RpcClient", lambda global_scope=False, log_file=None: _FakeRpcClient(responses))

    exit_code = cli.main(["trajectory", "overview", "t1"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["trajectory_id"] == "t1"
    assert payload["sessions"][0]["session_id"] == "s1"
    assert payload["sessions"][0]["turns"][0]["steps"][0]["type"] == "tool_call"


def test_step_details_command_calls_rpc(monkeypatch, capsys) -> None:
    responses = {
        ("step.details", json.dumps({"step_id": "step-1"}, sort_keys=True)): {
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
    }
    monkeypatch.setattr(cli, "RpcClient", lambda global_scope=False, log_file=None: _FakeRpcClient(responses))

    exit_code = cli.main(["step", "details", "step-1"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["step_id"] == "step-1"
    assert payload["type"] == "tool_call"
    assert payload["shape"]["tool_name"] == "Read"
