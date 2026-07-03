from __future__ import annotations

import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import threading
import time
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ACTIVE_SESSIONS: weakref.WeakSet[CodexAppServerSession]
_ACTIVE_SESSIONS_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class CodexAppServerResult:
    thread_id: str
    turn_id: str | None
    text: str

    def parse_json(self) -> Any:
        """Parse the agent reply as JSON for structured-output turns.

        Invokers that run a turn with an ``output_schema`` call this and then
        validate the value with their own model; plain-text turns use ``text``.
        """
        stripped = self.text.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
            stripped = re.sub(r"\s*```$", "", stripped)
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            start = stripped.find("{")
            end = stripped.rfind("}")
            if start >= 0 and end > start:
                return json.loads(stripped[start : end + 1])
            raise


class CodexAppServerClient:
    def __init__(self, *, timeout_seconds: float = 180) -> None:
        self.timeout_seconds = timeout_seconds

    def run_turn(
        self,
        *,
        cwd: Path,
        user_text: str,
        output_schema: dict[str, Any] | None = None,
        thread_id: str | None = None,
        ephemeral: bool = False,
        model: str | None = None,
        effort: str | None = None,
    ) -> CodexAppServerResult:
        session = CodexAppServerSession(cwd=cwd, timeout_seconds=self.timeout_seconds)
        try:
            if thread_id is None:
                session.start_thread(ephemeral=ephemeral, model=model)
            else:
                session.attach_thread(thread_id)
            return session.run_turn(
                user_text=user_text,
                output_schema=output_schema,
                model=model,
                effort=effort,
            )
        finally:
            session.close()


class CodexAppServerManager:
    """Owns one app-server subprocess for the dashboard server lifetime.

    The app-server JSON-RPC stream is bidirectional and turn notifications are
    global to the connection, so this manager serializes turns on one lock.
    Dashboard-level job concurrency still works for non-agent projections.
    """

    def __init__(self, *, cwd: Path, timeout_seconds: float = 180) -> None:
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._session: CodexAppServerSession | None = None
        self._closed = False

    def start_thread(self, *, cwd: Path | None = None, ephemeral: bool = True) -> str:
        with self._lock:
            return self._session_for_locked(cwd or self.cwd).start_thread(
                cwd=cwd or self.cwd,
                ephemeral=ephemeral,
            )

    def run_turn(
        self,
        *,
        cwd: Path,
        user_text: str,
        output_schema: dict[str, Any] | None = None,
        thread_id: str | None = None,
        ephemeral: bool = True,
        model: str | None = None,
        effort: str | None = None,
    ) -> CodexAppServerResult:
        with self._lock:
            session = self._session_for_locked(cwd)
            if thread_id is None:
                session.start_thread(cwd=cwd, ephemeral=ephemeral, model=model)
            else:
                session.attach_thread(thread_id)
            return session.run_turn(
                cwd=cwd,
                user_text=user_text,
                output_schema=output_schema,
                model=model,
                effort=effort,
            )

    def delete_thread(self, thread_id: str) -> None:
        with self._lock:
            self._session_for_locked(self.cwd).delete_thread(thread_id)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            session = self._session
            self._session = None
        if session is not None:
            session.close()

    def _session_for_locked(self, cwd: Path) -> CodexAppServerSession:
        if self._closed:
            raise RuntimeError("codex app-server manager is closed")
        if self._session is None:
            self._session = CodexAppServerSession(
                cwd=cwd,
                timeout_seconds=self.timeout_seconds,
            )
        return self._session


class CodexAppServerSession:
    def __init__(self, *, cwd: Path, timeout_seconds: float = 180) -> None:
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        self.thread_id: str | None = None
        self._next_id = 1
        self._close_lock = threading.Lock()
        self._closed = False
        self._proc = subprocess.Popen(
            _app_server_command(),
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        if (
            self._proc.stdin is None
            or self._proc.stdout is None
            or self._proc.stderr is None
        ):
            self.close()
            raise RuntimeError("failed to open codex app-server pipes")
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stderr_lines: queue.Queue[str] = queue.Queue()
        self._stdout_thread = threading.Thread(
            target=_read_jsonl,
            args=(self._proc.stdout, self._messages),
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=_read_stderr,
            args=(self._proc.stderr, self._stderr_lines),
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()
        _register_session(self)
        try:
            self._initialize()
        except Exception:
            self.close()
            raise

    def start_thread(
        self,
        *,
        cwd: Path | None = None,
        ephemeral: bool = False,
        model: str | None = None,
    ) -> str:
        cwd = cwd or self.cwd
        params: dict[str, Any] = {
            "cwd": str(cwd),
            "ephemeral": ephemeral,
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "serviceName": "coding-trajectory-dashboard",
        }
        if model:
            params["model"] = model
        thread_request_id = self._request_id()
        self._send(
            {
                "method": "thread/start",
                "id": thread_request_id,
                "params": params,
            }
        )
        thread_result = self._wait_response(thread_request_id)
        thread_id = str((thread_result.get("thread") or {}).get("id") or "").strip()
        if not thread_id:
            raise RuntimeError("codex app-server requires a thread id")
        self.thread_id = thread_id
        return thread_id

    def attach_thread(self, thread_id: str | None) -> None:
        thread_id = (thread_id or "").strip()
        if not thread_id:
            raise RuntimeError("codex app-server requires a thread id")
        self.thread_id = thread_id

    def run_turn(
        self,
        *,
        cwd: Path | None = None,
        user_text: str,
        output_schema: dict[str, Any] | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> CodexAppServerResult:
        if not self.thread_id:
            raise RuntimeError("codex app-server requires a thread id")
        cwd = cwd or self.cwd
        turn_request_id = self._request_id()
        turn_params: dict[str, Any] = {
            "threadId": self.thread_id,
            "cwd": str(cwd),
            "approvalPolicy": "never",
            "input": [
                {"type": "text", "text": user_text},
            ],
        }
        if output_schema is not None:
            turn_params["outputSchema"] = output_schema
        if model:
            turn_params["model"] = model
        if effort:
            turn_params["effort"] = effort
        self._send(
            {
                "method": "turn/start",
                "id": turn_request_id,
                "params": turn_params,
            }
        )
        turn_result = self._wait_response(turn_request_id)
        turn_id = str((turn_result.get("turn") or {}).get("id") or "") or None
        text = self._collect_turn_text(self.thread_id, turn_id)
        return CodexAppServerResult(
            thread_id=self.thread_id, turn_id=turn_id, text=text
        )

    def delete_thread(self, thread_id: str | None) -> None:
        thread_id = (thread_id or "").strip()
        if not thread_id:
            raise RuntimeError("codex app-server requires a thread id")
        request_id = self._request_id()
        self._send(
            {
                "method": "thread/delete",
                "id": request_id,
                "params": {"threadId": thread_id},
            }
        )
        self._wait_response(request_id)
        if self.thread_id == thread_id:
            self.thread_id = None

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        _unregister_session(self)
        _terminate(self._proc)
        _join_reader_thread(self._stdout_thread)
        _join_reader_thread(self._stderr_thread)

    def _initialize(self) -> None:
        request_id = self._request_id()
        self._send(
            {
                "method": "initialize",
                "id": request_id,
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
            }
        )
        self._wait_response(request_id)
        self._send({"method": "initialized", "params": {}})

    def _request_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id

    def _send(self, message: dict[str, Any]) -> None:
        if self._proc.stdin is None:
            raise RuntimeError("codex app-server stdin is closed")
        self._proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self._proc.stdin.flush()

    def _wait_response(
        self,
        request_id: int,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            _raise_if_exited(self._proc, self._stderr_lines)
            try:
                message = self._messages.get(timeout=0.2)
            except queue.Empty:
                continue
            if message.get("id") != request_id:
                continue
            if message.get("error"):
                error = message["error"]
                raise RuntimeError(str(error.get("message") or error))
            result = message.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(
                    f"codex app-server response {request_id} omitted result"
                )
            return result
        raise RuntimeError(
            f"codex app-server timed out waiting for response {request_id}"
        )

    def _collect_turn_text(
        self,
        thread_id: str,
        turn_id: str | None,
    ) -> str:
        deadline = time.monotonic() + self.timeout_seconds
        agent_messages: list[str] = []
        completed_status: str | None = None
        while time.monotonic() < deadline:
            _raise_if_exited(self._proc, self._stderr_lines)
            try:
                message = self._messages.get(timeout=0.2)
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
                        raise RuntimeError(
                            f"codex app-server turn {completed_status}: {error}"
                        )
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


def close_active_app_servers() -> None:
    """Terminate every app-server process still owned by this Python process."""
    with _ACTIVE_SESSIONS_LOCK:
        sessions = list(_ACTIVE_SESSIONS)
    for session in sessions:
        session.close()


def _register_session(session: CodexAppServerSession) -> None:
    with _ACTIVE_SESSIONS_LOCK:
        _ACTIVE_SESSIONS.add(session)


def _unregister_session(session: CodexAppServerSession) -> None:
    with _ACTIVE_SESSIONS_LOCK:
        _ACTIVE_SESSIONS.discard(session)


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


def _raise_if_exited(
    proc: subprocess.Popen[str], stderr_lines: queue.Queue[str]
) -> None:
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
    if proc.stdin is not None:
        try:
            proc.stdin.close()
        except OSError:
            pass
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _join_reader_thread(thread: threading.Thread) -> None:
    if thread is threading.current_thread():
        return
    thread.join(timeout=1)


_ACTIVE_SESSIONS = weakref.WeakSet()
