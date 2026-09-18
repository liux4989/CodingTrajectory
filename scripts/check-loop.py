"""Offline HTTP integration qualification over synthetic Core evidence, no unit tests."""

from __future__ import annotations

import json
import os
import runpy
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def main():
    assert not (ROOT / "packages/plugins/datahub/plugin.toml").exists()
    assert not (ROOT / "docs/datahub-design.md").exists()
    retired = "data" + "hub"
    candidates = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        text=True,
    ).splitlines()
    for name in candidates:
        path = ROOT / name
        if (
            path.is_file()
            and name.startswith(
                (
                    "packages/plugins/loop/",
                    ".github/",
                    ".agents/setup",
                    ".agents/resume",
                )
            )
            and path.suffix not in {".lock", ".png"}
        ):
            source = path.read_text()
            assert (
                "ct." + retired not in source
                and "/api/" + retired not in source
                and "plugins/" + retired not in source
            ), name
    with tempfile.TemporaryDirectory(prefix="ct-loop-check-") as tmp:
        home = Path(tmp)
        prepare = runpy.run_path(str(ROOT / "scripts/prepare-loop-demo.py"))["prepare"]
        logs = prepare(home, track=[Path.cwd()])
        env = {
            key: value for key, value in os.environ.items() if not key.startswith("CT_")
        }
        env.update(
            HOME=tmp,
            CT_AMP_LOG_DIR=str(logs),
            CT_CONNECTION_DIR=str(home / "connections"),
        )
        # Deliberately unusable remote settings must never be used as fallback.
        env.update(
            CT_QUERY_SOURCE="shared", CT_CLOUDFLARE_URL="https://invalid.invalid"
        )
        state = home / "views.sqlite3"
        monitor_state = home / "monitor.sqlite3"
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "loop_plugin.main",
                "web",
                "--port",
                "18765",
                "--state",
                str(state),
                "--monitor-state",
                str(monitor_state),
            ],
            env=env,
            stdout=subprocess.DEVNULL,
        )
        base = "http://127.0.0.1:18765"

        def call(path, data=None, headers=None):
            request = Request(
                base + path,
                data=json.dumps(data).encode() if data is not None else None,
                headers={"Content-Type": "application/json", **(headers or {})},
            )
            with urlopen(request, timeout=30) as response:
                return json.load(response)

        def core(method, params):
            reply = call("/api/core", {"method": method, "params": params})
            assert reply["ok"], reply
            assert reply["meta"]["source"] == "local"
            return reply["result"]

        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("Loop server exited before readiness")
                try:
                    assert call("/api/status")["protocol"] == "ct.loop.v1"
                    break
                except URLError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("Loop server did not become ready")
            # Browser preconnect sockets must not monopolize the HTTP accept loop.
            with socket.create_connection(("127.0.0.1", 18765), timeout=5):
                assert call("/api/status")["remote"] is False
            projects = core("project.list", {})
            project_id = next(
                key
                for key, value in projects["items"].items()
                if value["display_name"] == "amp-example"
            )
            assert projects["items"][project_id]["project_id"] == project_id
            sessions = core("project.sessions", {"project_id": project_id})["items"]
            assert (
                sessions
                == core("project.sessions", {"project_name": "AmpExample"})["items"]
            )
            assert sessions
            session_id = sessions[0]["root_session_id"]
            scope = {"session_id": session_id}
            brief = core("session.summary", scope)
            assert (
                brief["objective"]["text"]
                == "Synthetic example: verify command failure handling."
            )
            assert brief["coverage"]["retention"] == "preview"
            assert core("session.overview", {**scope, "limit": 1})["sessions"]
            first = core("session.items", {**scope, "limit": 1})
            second = core(
                "session.items", {**scope, "limit": 1, "cursor": first["next_cursor"]}
            )
            assert first["items"][0]["item_id"] != second["items"][0]["item_id"]
            all_items = core("session.items", scope)["items"]
            failed = next(item for item in all_items if item["status"] == "failed")
            assert failed["coverage"]["retention"] == "not_retained"
            assert (
                failed["output_evidence"]["processor"]
                == "ct.output_evidence.command.v1"
            )
            assert failed["output_evidence"]["exit_code"] == 23
            exact = core(
                "session.items",
                {**scope, "item_ids": [failed["item_id"]]},
            )["items"]
            assert exact == [failed]
            events = core(
                "session.events", {**scope, "event_ids": failed["event_ids"]}
            )["events"]
            assert events and all(event["session_id"] == session_id for event in events)
            first_event = core("session.events", {**scope, "limit": 1})
            next_event = core(
                "session.events",
                {**scope, "limit": 1, "cursor": first_event["next_cursor"]},
            )
            assert (
                first_event["events"][0]["event_id"]
                != next_event["events"][0]["event_id"]
            )
            reference = {
                "session_id": session_id,
                "turn_id": failed["turn_id"],
                "item_id": failed["item_id"],
            }
            view = {
                "id": "10000000-0000-4000-8000-000000000001",
                "title": "Command failure",
                "reference": reference,
            }
            saved = call("/api/investigations", view)["investigation"]
            assert saved["revision"] == "latest"
            assert call("/api/investigations")["items"] == [saved]
            view["title"] = "Reviewed command failure"
            call("/api/investigations", view)
            assert len(call("/api/investigations")["items"]) == 1
            with sqlite3.connect(state) as db:
                stored = db.execute("SELECT state FROM investigations").fetchone()[0]
                assert "Synthetic command" not in stored and "provenance" not in stored
            for headers in [
                {"Origin": "https://attacker.invalid"},
                {"Host": "attacker.invalid"},
                {"Sec-Fetch-Site": "cross-site"},
            ]:
                try:
                    call("/api/investigations", view, headers)
                except HTTPError as error:
                    assert error.code == 403
                else:
                    raise AssertionError("Cross-origin/host request accepted")
            for path, body, status in [
                ("/api/investigations", {**view, "transcript": "forbidden"}, 400),
                ("/api/core", {"method": "retired.method"}, 400),
                ("/api/retired", {}, 404),
            ]:
                try:
                    call(path, body)
                except HTTPError as error:
                    assert error.code == status
                else:
                    raise AssertionError("Invalid request accepted")
            missing = call(
                "/api/core",
                {"method": "session.items", "params": {"session_id": "does-not-exist"}},
            )
            assert missing["ok"] is False
            monitor(call, core, monitor_state, home)
            print(
                "Loop integration: PASS — local discovery, summary/overview, cursor continuation, exact item/event references, reference-only persistence, invalid scope, origin/host rejection, no remote fallback"
            )
            print(
                "Loop Monitor: PASS — strategy catalog, scope/config validation, asymmetric pass/breach/unavailable dry-run, idempotent reruns, revisioned config, supersession on changed evidence, finding lifecycle, exact references, reference-only persistence, no remote fallback"
            )
        finally:
            process.terminate()
            process.wait(timeout=10)


def monitor(call, core, monitor_state: Path, home: Path) -> None:
    """Qualify the deterministic turn-token-budget Monitor slice."""
    strategies = call("/api/monitor/strategies")["items"]
    assert [item["strategy_id"] for item in strategies] == ["turn-token-budget"]
    manifest = strategies[0]
    assert manifest["required_core_methods"] == {
        "project.list": 3,
        "project.sessions": 3,
        "session.usage": 3,
        "session.request_usage": 3,
        "living.sessions": 2,
    }
    assert manifest["evaluator"]["type"] == "deterministic"
    assert any(
        p["id"] == "content_read" and "Not requested" in p["detail"]
        for p in manifest["permissions"]
    )

    codex_usage = core(
        "session.usage", {"session_id": "019faa00-0000-7000-8000-0000000000b0"}
    )
    usage_by_turn = {turn["turn_id"]: turn for turn in codex_usage["turns"]}
    assert len(usage_by_turn) == 3

    def expect_error(path, body, status):
        try:
            call(path, body)
        except HTTPError as error:
            assert error.code == status, (path, error.code)
            return
        raise AssertionError(f"Invalid request accepted: {path}")

    # Invalid configurations and unknown objects are rejected.
    expect_error(
        "/api/monitor/watches",
        {
            "strategy_id": "turn-token-budget",
            "name": "bad",
            "scope": {"session_id": "019faa00-0000-7000-8000-0000000000b0"},
            "config": {"measure": "processed_tokens", "threshold_tokens": 0},
        },
        400,
    )
    expect_error(
        "/api/monitor/watches",
        {
            "strategy_id": "unknown-strategy",
            "name": "bad",
            "scope": {"session_id": "019faa00-0000-7000-8000-0000000000b0"},
            "config": {"measure": "processed_tokens", "threshold_tokens": 1},
        },
        400,
    )
    expect_error(
        "/api/monitor/watches",
        {
            "strategy_id": "turn-token-budget",
            "name": "bad",
            "scope": {},
            "config": {"measure": "processed_tokens", "threshold_tokens": 1},
        },
        400,
    )
    expect_error(
        "/api/monitor/watches/00000000-0000-4000-8000-0000000000ff/dry-run",
        {},
        404,
    )

    watch = call(
        "/api/monitor/watches",
        {
            "strategy_id": "turn-token-budget",
            "name": "Codex turn budget",
            "scope": {"session_id": "019faa00-0000-7000-8000-0000000000b0"},
            "config": {
                "measure": "processed_tokens",
                "threshold_tokens": 50000,
                "severity": "warning",
                "emit_findings": True,
            },
        },
    )["watch"]
    watch_id = watch["watch_id"]
    assert watch["enabled"] is False and watch["config_revision"] == 1

    # Historical dry-run: asymmetric outcomes with exact Core references.
    run = call(f"/api/monitor/watches/{watch_id}/dry-run", {"max_sessions": 5})["run"]
    assert run["summary"]["sessions_examined"] == 1
    assert run["summary"]["turns_evaluated"] == 3
    by_outcome = {}
    for evaluation in run["evaluations"]:
        assert evaluation["trigger"] == "historical_dry_run"
        assert evaluation["config_revision"] == 1
        assert evaluation["evaluator"]["rule"] == "ct.loop.turn_token_budget"
        reference = evaluation["reference"]
        assert reference["session_id"] == "019faa00-0000-7000-8000-0000000000b0"
        turn = usage_by_turn[reference["turn_id"]]
        condition = evaluation["condition"]
        if evaluation["result"] == "pass":
            assert evaluation["state"] == "completed"
            assert condition["observed"] == turn["usage"]["processed_tokens"]
            assert condition["observed"] <= condition["threshold_tokens"]
            by_outcome["pass"] = evaluation
        elif evaluation["result"] == "breach":
            assert condition["observed"] == turn["usage"]["processed_tokens"]
            assert condition["observed"] > condition["threshold_tokens"]
            by_outcome["breach"] = evaluation
        else:
            assert evaluation["state"] == "unavailable"
            assert condition["observed"] is None  # never unavailable-as-zero
            assert "No provider usage observations" in condition["summary"]
            by_outcome["unavailable"] = evaluation
    assert set(by_outcome) == {"pass", "breach", "unavailable"}
    assert len(run["prospective_findings"]) == 1
    assert call("/api/monitor/findings")["items"] == []  # dry-run stays labeled

    # Idempotent rerun: identical evidence and config writes nothing new.
    rerun = call(f"/api/monitor/watches/{watch_id}/dry-run", {})["run"]
    assert rerun["summary"]["skipped_unchanged"] == 3
    assert rerun["summary"]["turns_evaluated"] == 0
    assert rerun["evaluations"] == []

    # Disabled watches cannot run the live path; enabling is explicit.
    expect_error(f"/api/monitor/watches/{watch_id}/refresh", {}, 409)
    call(f"/api/monitor/watches/{watch_id}", {"enabled": True})
    live = call(f"/api/monitor/watches/{watch_id}/refresh", {})["run"]
    assert live["summary"]["turns_evaluated"] == 3
    assert len(live["findings"]) == 1
    finding = live["findings"][0]
    assert finding["status"] == "open" and finding["severity"] == "warning"
    assert finding["reference"] == by_outcome["breach"]["reference"]
    assert finding["condition"]["outcome"] == "breach"
    relive = call(f"/api/monitor/watches/{watch_id}/refresh", {})["run"]
    assert relive["summary"]["skipped_unchanged"] == 3
    assert relive["findings"] == [] and relive["remaining"] is False

    # Trigger partitions: dry-run and refresh records coexist per turn.
    all_live = call(
        f"/api/monitor/evaluations?watch_id={watch_id}&trigger=manual_refresh"
    )["items"]
    assert len(all_live) == 3
    breach_only = call(f"/api/monitor/evaluations?watch_id={watch_id}&result=breach")[
        "items"
    ]
    assert {item["trigger"] for item in breach_only} == {
        "historical_dry_run",
        "manual_refresh",
    }

    # Finding lifecycle, with durable history and no evidence mutation.
    fid = finding["finding_id"]
    for status in ("acknowledged", "resolved", "dismissed", "open"):
        updated = call(f"/api/monitor/findings/{fid}/status", {"status": status})[
            "finding"
        ]
        assert updated["status"] == status
    assert [event["status"] for event in updated["status_history"]] == [
        "open",
        "acknowledged",
        "resolved",
        "dismissed",
        "open",
    ]
    expect_error(f"/api/monitor/findings/{fid}/status", {"status": "deleted"}, 400)
    expect_error(
        "/api/monitor/findings/00000000-0000-4000-8000-0000000000ff/status",
        {"status": "open"},
        404,
    )
    open_findings = call("/api/monitor/findings?status=open")["items"]
    assert [item["finding_id"] for item in open_findings] == [fid]

    # Project scope over Amp evidence: every turn honestly unavailable.
    amp_watch = call(
        "/api/monitor/watches",
        {
            "strategy_id": "turn-token-budget",
            "name": "Amp turn budget",
            "scope": {"project_name": "amp-example"},
            "config": {
                "measure": "processed_tokens",
                "threshold_tokens": 50000,
                "severity": "info",
                "emit_findings": True,
            },
        },
    )["watch"]
    amp_run = call(f"/api/monitor/watches/{amp_watch['watch_id']}/dry-run", {})["run"]
    assert amp_run["summary"]["sessions_examined"] == 1
    assert amp_run["summary"]["unavailable"] == amp_run["summary"]["turns_evaluated"]
    assert amp_run["summary"]["passed"] == 0 and amp_run["summary"]["breached"] == 0
    assert all(
        evaluation["condition"]["observed"] is None
        for evaluation in amp_run["evaluations"]
    )

    # Project scope over the synthetic Codex project matches by inventory.
    codex_watch = call(
        "/api/monitor/watches",
        {
            "strategy_id": "turn-token-budget",
            "name": "Codex project budget",
            "scope": {"project_name": "codex-budget-demo"},
            "config": {
                "measure": "processed_tokens",
                "threshold_tokens": 50000,
                "severity": "critical",
                "emit_findings": True,
            },
        },
    )["watch"]
    codex_run = call(f"/api/monitor/watches/{codex_watch['watch_id']}/dry-run", {})[
        "run"
    ]
    assert codex_run["summary"]["passed"] == 1
    assert codex_run["summary"]["breached"] == 1
    assert codex_run["summary"]["unavailable"] == 1
    assert codex_run["scope_sessions_total"] == 1

    # A watch scoped to a missing session fails honestly instead of fabricating.
    missing_watch = call(
        "/api/monitor/watches",
        {
            "strategy_id": "turn-token-budget",
            "name": "Missing session",
            "scope": {"session_id": "00000000-0000-4000-8000-0000000000ff"},
            "config": {"measure": "processed_tokens", "threshold_tokens": 50000},
        },
    )["watch"]
    expect_error(f"/api/monitor/watches/{missing_watch['watch_id']}/dry-run", {}, 400)

    # A configuration change creates a new revision and re-observes the scope.
    revised = call(
        f"/api/monitor/watches/{watch_id}",
        {
            "config": {
                "measure": "processed_tokens",
                "threshold_tokens": 200000,
                "severity": "info",
                "emit_findings": True,
            }
        },
    )["watch"]
    assert revised["config_revision"] == 2 and len(revised["revisions"]) == 2
    assert revised["refresh"]["cursor"] is None
    rev2 = call(f"/api/monitor/watches/{watch_id}/refresh", {})["run"]
    assert rev2["summary"]["passed"] == 2 and rev2["summary"]["breached"] == 0
    assert rev2["findings"] == []
    assert call("/api/monitor/findings?status=open")["items"]  # old finding kept

    # Late-arriving usage evidence supersedes the unavailable evaluation.
    with open(home / ".codex" / "sessions" / "loop-budget-demo.jsonl", "a") as fh:
        fh.write(
            json.dumps(
                {
                    "timestamp": "2026-09-12T08:01:30.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 150000,
                                "cached_input_tokens": 70000,
                                "output_tokens": 9000,
                                "reasoning_output_tokens": 3000,
                                "total_tokens": 162000,
                            },
                            "last_token_usage": {
                                "input_tokens": 1000,
                                "cached_input_tokens": 0,
                                "output_tokens": 100,
                                "reasoning_output_tokens": 0,
                                "total_tokens": 1100,
                            },
                            "model_context_window": 258400,
                        },
                    },
                }
            )
            + "\n"
        )
    time.sleep(1.1)  # source mtime resolution for the living feed
    follow = call(f"/api/monitor/watches/{watch_id}/refresh", {})["run"]
    assert follow["summary"]["turns_evaluated"] == 1
    revived = follow["evaluations"][0]
    assert revived["state"] == "completed" and revived["config_revision"] == 2
    refetched = core(
        "session.usage", {"session_id": "019faa00-0000-7000-8000-0000000000b0"}
    )
    current_turn = {t["turn_id"]: t for t in refetched["turns"]}[
        revived["reference"]["turn_id"]
    ]
    assert revived["condition"]["observed"] == current_turn["usage"]["processed_tokens"]
    with sqlite3.connect(monitor_state) as db:
        superseded = db.execute(
            "SELECT COUNT(*) FROM evaluations WHERE superseded_by IS NOT NULL"
        ).fetchone()[0]
    assert superseded == 1

    # Monitor persistence stays reference-only: no transcript or event content.
    raw = monitor_state.read_bytes()
    for forbidden in (
        b"Synthetic small turn",
        b"Synthetic large turn",
        b"PRIVATE",
        b"user_message",
        b"last_agent_message",
    ):
        assert forbidden not in raw, forbidden
    with sqlite3.connect(monitor_state) as db:
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {"watches", "evaluations", "findings"} <= tables


if __name__ == "__main__":
    main()
