from __future__ import annotations

import json
import os
import queue
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class CodexAppServerResult:
    thread_id: str
    turn_id: str | None
    text: str


class CodexAppServerClient:
    def __init__(self, *, timeout_seconds: float = 180) -> None:
        self.timeout_seconds = timeout_seconds
        self._next_id = 1

    def run_skill_turn(
        self,
        *,
        cwd: Path,
        skill_name: str,
        skill_path: Path,
        user_text: str,
        output_schema: dict[str, Any],
    ) -> CodexAppServerResult:
        command = _app_server_command()
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        if proc.stdin is None or proc.stdout is None or proc.stderr is None:
            raise RuntimeError("failed to open codex app-server pipes")
        messages: queue.Queue[dict[str, Any]] = queue.Queue()
        stderr_lines: queue.Queue[str] = queue.Queue()
        stdout_thread = threading.Thread(
            target=_read_jsonl,
            args=(proc.stdout, messages),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_read_stderr,
            args=(proc.stderr, stderr_lines),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        try:
            self._send(
                proc,
                {
                    "method": "initialize",
                    "id": self._request_id(),
                    "params": {
                        "clientInfo": {
                            "name": "coding_trajectory_dashboard",
                            "title": "CodingTrajectory Dashboard",
                            "version": "0.1.0",
                        },
                        "capabilities": {
                            "optOutNotificationMethods": [
                                "item/agentMessage/delta",
                                "item/reasoning/summaryTextDelta",
                                "item/reasoning/summaryPartAdded",
                            ]
                        },
                    },
                },
            )
            self._wait_response(messages, 1, proc, stderr_lines)
            self._send(proc, {"method": "initialized", "params": {}})
            thread_request_id = self._request_id()
            self._send(
                proc,
                {
                    "method": "thread/start",
                    "id": thread_request_id,
                    "params": {
                        "cwd": str(cwd),
                        "ephemeral": True,
                        "approvalPolicy": "never",
                        "sandbox": "read-only",
                        "serviceName": "coding-trajectory-dashboard",
                    },
                },
            )
            thread_result = self._wait_response(messages, thread_request_id, proc, stderr_lines)
            thread_id = str((thread_result.get("thread") or {}).get("id") or "")
            if not thread_id:
                raise RuntimeError("codex app-server thread/start returned no thread id")
            turn_request_id = self._request_id()
            self._send(
                proc,
                {
                    "method": "turn/start",
                    "id": turn_request_id,
                    "params": {
                        "threadId": thread_id,
                        "cwd": str(cwd),
                        "approvalPolicy": "never",
                        "outputSchema": output_schema,
                        "input": [
                            {"type": "text", "text": user_text},
                            {
                                "type": "skill",
                                "name": skill_name,
                                "path": str(skill_path),
                            },
                        ],
                    },
                },
            )
            turn_result = self._wait_response(messages, turn_request_id, proc, stderr_lines)
            turn_id = str((turn_result.get("turn") or {}).get("id") or "") or None
            text = self._collect_turn_text(messages, proc, stderr_lines, thread_id, turn_id)
            return CodexAppServerResult(thread_id=thread_id, turn_id=turn_id, text=text)
        finally:
            _terminate(proc)

    def _request_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id

    def _send(self, proc: subprocess.Popen[str], message: dict[str, Any]) -> None:
        if proc.stdin is None:
            raise RuntimeError("codex app-server stdin is closed")
        proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        proc.stdin.flush()

    def _wait_response(
        self,
        messages: queue.Queue[dict[str, Any]],
        request_id: int,
        proc: subprocess.Popen[str],
        stderr_lines: queue.Queue[str],
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            _raise_if_exited(proc, stderr_lines)
            try:
                message = messages.get(timeout=0.2)
            except queue.Empty:
                continue
            if message.get("id") != request_id:
                continue
            if message.get("error"):
                error = message["error"]
                raise RuntimeError(str(error.get("message") or error))
            result = message.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(f"codex app-server response {request_id} omitted result")
            return result
        raise RuntimeError(f"codex app-server timed out waiting for response {request_id}")

    def _collect_turn_text(
        self,
        messages: queue.Queue[dict[str, Any]],
        proc: subprocess.Popen[str],
        stderr_lines: queue.Queue[str],
        thread_id: str,
        turn_id: str | None,
    ) -> str:
        deadline = time.monotonic() + self.timeout_seconds
        agent_messages: list[str] = []
        completed_status: str | None = None
        while time.monotonic() < deadline:
            _raise_if_exited(proc, stderr_lines)
            try:
                message = messages.get(timeout=0.2)
            except queue.Empty:
                continue
            method = message.get("method")
            params = message.get("params") or {}
            if not isinstance(params, dict):
                continue
            if method == "item/completed" and params.get("threadId") == thread_id:
                item = params.get("item") or {}
                if isinstance(item, dict) and item.get("type") == "agentMessage":
                    text = item.get("text")
                    phase = item.get("phase")
                    if isinstance(text, str) and (phase in {None, "final_answer"}):
                        agent_messages.append(text)
            elif method == "turn/completed" and params.get("threadId") == thread_id:
                turn = params.get("turn") or {}
                if isinstance(turn, dict):
                    if turn_id and turn.get("id") != turn_id:
                        continue
                    completed_status = str(turn.get("status") or "")
                    error = turn.get("error")
                    if completed_status not in {"completed", "success"}:
                        raise RuntimeError(f"codex app-server turn {completed_status}: {error}")
                    break
        else:
            raise RuntimeError("codex app-server timed out waiting for turn completion")
        if completed_status is None:
            raise RuntimeError("codex app-server turn did not complete")
        text = "\n".join(part for part in agent_messages if part.strip()).strip()
        if not text:
            raise RuntimeError("codex app-server returned no final agent message")
        return text


def _app_server_command() -> list[str]:
    raw = os.environ.get("CODEX_APP_SERVER_COMMAND")
    if raw:
        return shlex.split(raw)
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("codex executable not found; set CODEX_APP_SERVER_COMMAND")
    return [codex, "app-server", "--stdio"]


def _read_jsonl(stream: Any, messages: queue.Queue[dict[str, Any]]) -> None:
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict):
            messages.put(message)


def _read_stderr(stream: Any, stderr_lines: queue.Queue[str]) -> None:
    for line in stream:
        if line.strip():
            stderr_lines.put(line.strip())


def _raise_if_exited(proc: subprocess.Popen[str], stderr_lines: queue.Queue[str]) -> None:
    if proc.poll() is None:
        return
    lines: list[str] = []
    while not stderr_lines.empty():
        lines.append(stderr_lines.get_nowait())
    suffix = f": {' '.join(lines[-6:])}" if lines else ""
    raise RuntimeError(f"codex app-server exited with code {proc.returncode}{suffix}")


def _terminate(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
