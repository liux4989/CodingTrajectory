from __future__ import annotations

import json

from coding_trajectory_cli import cli


class _FakeRpcClient:
    def __init__(self, responses: dict[tuple[str, str], object], *, global_scope: bool = False):
        self._responses = responses
        self.global_scope = global_scope

    def call(self, method: str, params: dict) -> object:
        key = (method, json.dumps(params, sort_keys=True))
        return self._responses[key]

    def __enter__(self) -> _FakeRpcClient:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        return None


def test_event_overview_command_calls_rpc(monkeypatch, capsys) -> None:
    responses = {
        ("event.overview", json.dumps({"event_id": "e1"}, sort_keys=True)): {
            "event": {
                "event_id": "e1",
                "type": "tool.call.requested",
                "category": "tool_call",
            }
        }
    }
    monkeypatch.setattr(cli, "RpcClient", lambda global_scope=False: _FakeRpcClient(responses, global_scope=global_scope))

    exit_code = cli.main(["event", "overview", "e1"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"]["category"] == "tool_call"


def test_session_overview_command_calls_rpc(monkeypatch, capsys) -> None:
    responses = {
        ("session.overview", json.dumps({"session_id": "s1"}, sort_keys=True)): {
            "session": {"session_id": "s1", "trajectory_id": "t1"},
            "turns": [{"turn_id": "turn-1", "user_request": "analyze logs"}],
        }
    }
    monkeypatch.setattr(cli, "RpcClient", lambda global_scope=False: _FakeRpcClient(responses, global_scope=global_scope))

    exit_code = cli.main(["session", "overview", "s1"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session"]["session_id"] == "s1"
    assert payload["turns"][0]["turn_id"] == "turn-1"


def test_trajectory_overview_command_calls_rpc(monkeypatch, capsys) -> None:
    responses = {
        ("trajectory.overview", json.dumps({"trajectory_id": "t1"}, sort_keys=True)): {
            "trajectory": {"trajectory_id": "t1"},
            "operations": [{"edge_type": "spawned_subagent"}],
            "tree": [{"session": {"session_id": "s1"}, "children": []}],
            "notes": [],
        }
    }
    monkeypatch.setattr(cli, "RpcClient", lambda global_scope=False: _FakeRpcClient(responses, global_scope=global_scope))

    exit_code = cli.main(["trajectory", "overview", "t1"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["operations"][0]["edge_type"] == "spawned_subagent"
    assert payload["tree"][0]["session"]["session_id"] == "s1"
