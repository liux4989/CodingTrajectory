from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

try:
    from .codex_app_server import CodexAppServerClient, CodexAppServerResult, PiRpcClient
    from .session_analysis import AnalysisProvider
except ImportError:
    from codex_app_server import CodexAppServerClient, CodexAppServerResult, PiRpcClient
    from session_analysis import AnalysisProvider


AgentTaskSource = Literal["codex_app_server_skill", "pi_rpc_skill"]


class AgentTaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    task_goal: str
    generated_at: str
    source: AgentTaskSource
    provider: AnalysisProvider
    app_server_thread_id: str
    app_server_turn_id: str | None = None
    response_text: str


class AgentTaskRunner(Protocol):
    def run_turn(
        self,
        *,
        cwd: Path,
        user_text: str,
        output_schema: dict[str, Any] | None = None,
    ) -> CodexAppServerResult:
        ...


def run_agent_task(
    *,
    task_goal: str,
    task_context: str,
    provider: AnalysisProvider = "codex",
) -> AgentTaskResult:
    task_goal = _clean_text(task_goal)
    task_context = _clean_text(task_context)
    if not task_goal:
        raise ValueError("task_goal is required")
    if not task_context:
        raise ValueError("task_context is required")
    provider = _normalize_provider(provider)
    app_result = _agent_task_runner(provider).run_turn(
        cwd=_repo_root(),
        user_text=_task_request_text(task_goal, task_context),
    )
    return AgentTaskResult(
        task_goal=task_goal,
        generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        source=_task_source(provider),
        provider=provider,
        app_server_thread_id=app_result.thread_id,
        app_server_turn_id=app_result.turn_id,
        response_text=app_result.text.strip(),
    )


def _agent_task_runner(provider: AnalysisProvider) -> AgentTaskRunner:
    if provider == "pi":
        return PiRpcClient()
    return CodexAppServerClient()


def _task_source(provider: AnalysisProvider) -> AgentTaskSource:
    return "pi_rpc_skill" if provider == "pi" else "codex_app_server_skill"


def _normalize_provider(value: str) -> AnalysisProvider:
    normalized = value.strip().lower().replace("_", "-")
    if normalized in {"codex", "codex-app-server", "codex_app_server"}:
        return "codex"
    if normalized in {"pi", "pi-rpc", "pi_rpc"}:
        return "pi"
    raise ValueError("unknown agent task provider; expected codex or pi")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


_TASK_INSTRUCTIONS = """\
# Role
You run a general dashboard agent task.

# Task
- `task_goal`: what the dashboard feature wants accomplished.
- `task_context`: the plain-text context supplied by that feature.

# Rules
Use the supplied context as the evidence boundary. If the goal asks for fixes, \
include likely causes, concrete next actions, and any missing evidence needed to \
proceed. Return plain text only."""


def _task_request_text(task_goal: str, task_context: str) -> str:
    packet = {"task_goal": task_goal, "task_context": task_context}
    return (
        f"{_TASK_INSTRUCTIONS}\n\n"
        "<task>\n"
        f"{json.dumps(packet, ensure_ascii=False, separators=(',', ':'))}\n"
        "</task>"
    )


def _clean_text(value: str) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()
