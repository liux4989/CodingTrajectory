"""Dashboard job service: agent-driven session analysis.

Read routes are served by the incremental runtime (see
``incremental_runtime.py``); this module only owns the long-running
session-analysis jobs and their polling handles.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

try:
    from .codex_app_server import CodexAppServerManager, close_active_app_servers
    from . import session_analysis as session_analysis_mod
    from .jobs import JobRunner, JobStore
except ImportError:
    from codex_app_server import CodexAppServerManager, close_active_app_servers
    import session_analysis as session_analysis_mod
    from jobs import JobRunner, JobStore


class DashboardDataService:
    def __init__(self) -> None:
        self._jobs = JobStore()
        self._runner = JobRunner(self._jobs)
        self._app_server = CodexAppServerManager(cwd=_repo_root())

    def shutdown(self) -> None:
        self._runner.shutdown(wait=False)
        self._app_server.close()
        close_active_app_servers()

    def session_analysis(self, body: dict[str, Any]) -> dict[str, Any]:
        session_id = body.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id is required")
        refresh = bool(body.get("refresh"))
        operation_key = _session_analysis_operation_key(session_id.strip(), refresh)
        if refresh:
            job_id = self._runner.submit(
                "session-analysis",
                session_analysis_mod.build_analysis,
                session_id.strip(),
                ct_json=_ct_json,
                app_server=self._app_server,
            )
            reused = False
        else:
            job_id, created = self._runner.submit_once(
                operation_key,
                "session-analysis",
                session_analysis_mod.build_analysis,
                session_id.strip(),
                ct_json=_ct_json,
                app_server=self._app_server,
            )
            reused = not created
        return {
            "status": "pending",
            "job_id": job_id,
            "operation_key": operation_key,
            "reused": reused,
        }

    def job_status(self, job_id: str) -> dict[str, Any]:
        record = self._jobs.get(job_id)
        if record is None:
            raise ValueError("unknown job_id")
        return record.public()


def _ct_json(args: list[str]) -> dict[str, Any]:
    return _run_ct_json(args, timeout_seconds=120)


def _run_ct_json(args: list[str], *, timeout_seconds: int) -> dict[str, Any]:
    ct = os.environ.get("CT_COMMAND") or shutil.which("ct")
    if not ct:
        raise RuntimeError(
            "ct executable not found; set CT_COMMAND to the ct command path"
        )
    command = [*shlex.split(ct), *args]
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            cwd=_repo_root(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"ct command timed out after {timeout_seconds}s: {' '.join(command)}"
        ) from exc
    if completed.returncode != 0:
        message = (
            completed.stderr.strip() or completed.stdout.strip() or "ct command failed"
        )
        raise RuntimeError(message)
    return json.loads(completed.stdout)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _session_analysis_operation_key(session_id: str, refresh: bool) -> str:
    refresh_key = "refresh" if refresh else "cached"
    return f"session-analysis:v5:{refresh_key}:{session_id}"
