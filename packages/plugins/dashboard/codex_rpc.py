"""Minimal Codex ``app-server`` JSON-RPC client for the dashboard plugin.

Spawns ``codex app-server --stdio`` and issues the ``account/read`` call to
obtain the live account identity (email, plan type, auth mode). Ports the
functional core of multicodex's ``codex_process.py`` without the profile
reconciliation machinery the CLI needs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

try:
    from . import codex_auth
except ImportError:
    import codex_auth


APP_SERVER_TIMEOUT_SECONDS = 20
_AUTH_SAVE_LOCK = threading.Lock()


def _write_auth_json(codex_home: Path, profile_auth: dict) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    auth_path = codex_home / "auth.json"
    tmp = codex_home / f".auth.json.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(profile_auth, indent=2), encoding="utf-8")
    tmp.replace(auth_path)


def _ensure_editor_env(env: dict[str, str]) -> None:
    if env.get("VISUAL") or env.get("EDITOR"):
        return
    for command in ("code", "vim", "nano", "vi"):
        if shutil.which(command):
            env["EDITOR"] = "code -w" if command == "code" else command
            return


def _read_responses(
    process: subprocess.Popen,
    pending_ids: set[int],
    *,
    timeout: int = APP_SERVER_TIMEOUT_SECONDS,
) -> tuple[dict[int, dict], list[dict]]:
    if process.stdout is None:
        raise RuntimeError("Codex app-server stdout is unavailable")
    import time

    responses: dict[int, dict] = {}
    notifications: list[dict] = []
    deadline = time.monotonic() + timeout
    while pending_ids - responses.keys():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for Codex app-server")
        line = process.stdout.readline()
        if not line:
            raise RuntimeError("Codex app-server exited before replying")
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        message_id = message.get("id")
        if isinstance(message_id, int):
            responses[message_id] = message
        elif isinstance(message, dict):
            notifications.append(message)
    return responses, notifications


def _write_message(process: subprocess.Popen, message: dict) -> None:
    if process.stdin is None:
        raise RuntimeError("Codex app-server stdin is unavailable")
    process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    process.stdin.flush()


def account_snapshot(
    account: str,
    profile_auth: dict,
    *,
    codex_home: Path | None = None,
    persist_runtime_auth: bool = True,
) -> dict[str, Any]:
    """Spawn ``codex app-server`` and return the account/read result.

    The returned payload contains at least ``account`` (with ``email``,
    ``planType``, ``type``) and ``authMode`` when reported by the server.
    """
    codex_bin = shutil.which("codex")
    if codex_bin is None:
        raise RuntimeError("'codex' not found in PATH")

    own_codex_home = False
    if codex_home is None:
        codex_home = Path(tempfile.mkdtemp(prefix="ct-codex-home-"))
        own_codex_home = True
    _write_auth_json(codex_home, profile_auth)

    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    _ensure_editor_env(env)

    process = subprocess.Popen(
        [codex_bin, "app-server", "--stdio"],
        env=env,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=1,
    )
    try:
        _write_message(
            process,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "coding-trajectory-dashboard",
                        "title": "Coding Trajectory Dashboard",
                        "version": "0.1.0",
                    }
                },
            },
        )
        init_responses, _ = _read_responses(process, {1})
        if "error" in init_responses[1]:
            raise RuntimeError(
                init_responses[1]["error"].get("message", "app-server initialization failed")
            )
        _write_message(process, {"method": "initialized", "params": {}})
        _write_message(
            process,
            {"id": 2, "method": "account/read", "params": {"refreshToken": True}},
        )
        responses, notifications = _read_responses(process, {2})

        result: dict[str, Any] = {}
        response = responses[2]
        if "result" in response:
            result["account"] = response["result"].get("account")
        else:
            error = response.get("error") or {}
            result["error"] = error.get("message", "account/read failed")

        for notification in notifications:
            if notification.get("method") != "account/updated":
                continue
            params = notification.get("params")
            if isinstance(params, dict) and isinstance(params.get("authMode"), str):
                result["authMode"] = params["authMode"]
        return result
    finally:
        try:
            process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait()

        if persist_runtime_auth:
            runtime_auth_path = codex_home / "auth.json"
            try:
                runtime_auth = json.loads(runtime_auth_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                runtime_auth = None
            if isinstance(runtime_auth, dict) and runtime_auth != profile_auth:
                with _AUTH_SAVE_LOCK:
                    if account:
                        state = codex_auth.load_mxc_state()
                        if account in state.get("profiles", {}):
                            state["profiles"][account]["auth"] = runtime_auth
                            codex_auth.save_mxc_state(state)

        if own_codex_home:
            shutil.rmtree(codex_home, ignore_errors=True)


def extract_account_info(
    snapshot: dict[str, Any], profile_auth: dict | None
) -> dict[str, Any] | None:
    """Derive ``{email, planType, authMode}`` from an app-server snapshot."""
    response = snapshot.get("account")
    account = response.get("account") if isinstance(response, dict) else None
    info: dict[str, Any] = {}
    if isinstance(account, dict):
        email = account.get("email")
        plan_type = account.get("planType")
        account_type = account.get("type")
        if isinstance(email, str) and email:
            info["email"] = email
        if isinstance(plan_type, str) and plan_type:
            info["planType"] = plan_type
        if account_type in ("apiKey", "chatgpt"):
            info["authMode"] = account_type
    auth_mode = snapshot.get("authMode")
    if isinstance(auth_mode, str):
        info["authMode"] = auth_mode
    elif "authMode" not in info:
        info["authMode"] = codex_auth.auth_mode_from_profile(profile_auth)

    if "email" not in info:
        jwt_info = codex_auth.account_info_from_jwt(profile_auth)
        if jwt_info and "email" in jwt_info:
            info["email"] = jwt_info["email"]
        if jwt_info and "planType" not in info and "planType" in jwt_info:
            info["planType"] = jwt_info["planType"]
    return info or None
