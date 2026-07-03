from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

try:
    from .codex_app_server import CodexAppServerClient, CodexAppServerManager
except ImportError:
    from codex_app_server import CodexAppServerClient, CodexAppServerManager


class AgentTurnResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    generated_at: str
    app_server_thread_id: str
    app_server_turn_id: str | None = None
    response_text: str


def run_agent_turn(
    *,
    prompt: str,
    thread_id: str | None = None,
    output_schema: dict[str, Any] | None = None,
    ephemeral: bool = True,
    app_server: CodexAppServerManager | None = None,
) -> AgentTurnResult:
    prompt = _clean_text(prompt)
    thread_id = _clean_text(thread_id) or None
    if not prompt:
        raise ValueError("prompt is required")
    app_server_client = app_server or CodexAppServerClient()
    app_result = app_server_client.run_turn(
        cwd=_repo_root(),
        user_text=prompt,
        output_schema=output_schema,
        thread_id=thread_id,
        ephemeral=ephemeral,
    )
    return AgentTurnResult(
        generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        app_server_thread_id=app_result.thread_id,
        app_server_turn_id=app_result.turn_id,
        response_text=app_result.text.strip(),
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _clean_text(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()
