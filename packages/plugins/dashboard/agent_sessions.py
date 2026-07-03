from __future__ import annotations

import datetime as dt
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

try:
    from .codex_app_server import CodexAppServerSession
except ImportError:
    from codex_app_server import CodexAppServerSession


class AgentTurnResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    generated_at: str
    agent_session_id: str
    app_server_turn_id: str | None = None
    response_text: str


@dataclass(slots=True)
class AgentSessionRecord:
    id: str
    route_scope: str | None
    created_at: float
    last_used_at: float
    session: CodexAppServerSession
    lock: threading.Lock = field(default_factory=threading.Lock)
    recent_job_ids: deque[str] = field(default_factory=lambda: deque(maxlen=12))
    active_job_id: str | None = None


class AgentSessionStore:
    def __init__(self, *, ttl_seconds: float = 1800, cwd: Path) -> None:
        self._ttl_seconds = ttl_seconds
        self._cwd = cwd
        self._sessions: dict[str, AgentSessionRecord] = {}
        self._lock = threading.Lock()

    def create(
        self,
        *,
        route_scope: str | None = None,
        ephemeral: bool = False,
    ) -> dict[str, Any]:
        self._evict_expired()
        agent_session_id = uuid.uuid4().hex
        session = CodexAppServerSession(cwd=self._cwd)
        try:
            session.start_thread(ephemeral=ephemeral)
        except Exception:
            session.close()
            raise
        now = time.monotonic()
        record = AgentSessionRecord(
            id=agent_session_id,
            route_scope=route_scope,
            created_at=now,
            last_used_at=now,
            session=session,
        )
        with self._lock:
            self._sessions[agent_session_id] = record
        return self.public(agent_session_id)

    def public(self, agent_session_id: str) -> dict[str, Any]:
        record = self._get(agent_session_id)
        return {
            "agent_session_id": record.id,
            "route_scope": record.route_scope,
            "created_at": _iso_from_monotonic_age(record.created_at),
            "last_used_at": _iso_from_monotonic_age(record.last_used_at),
            "active_job_id": record.active_job_id,
            "recent_job_ids": list(record.recent_job_ids),
        }

    def note_job_started(self, agent_session_id: str, job_id: str) -> None:
        record = self._get(agent_session_id)
        with self._lock:
            record.active_job_id = job_id
            record.recent_job_ids.append(job_id)
            record.last_used_at = time.monotonic()

    def run_turn(
        self,
        *,
        agent_session_id: str,
        prompt: str,
        output_schema: dict[str, Any] | None = None,
    ) -> AgentTurnResult:
        record = self._get(agent_session_id)
        with record.lock:
            with self._lock:
                record.last_used_at = time.monotonic()
            try:
                app_result = record.session.run_turn(
                    user_text=prompt,
                    output_schema=output_schema,
                )
            finally:
                with self._lock:
                    record.active_job_id = None
                    record.last_used_at = time.monotonic()
            return AgentTurnResult(
                generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                agent_session_id=agent_session_id,
                app_server_turn_id=app_result.turn_id,
                response_text=app_result.text.strip(),
            )

    def close(self, agent_session_id: str) -> None:
        with self._lock:
            record = self._sessions.pop(agent_session_id, None)
        if record:
            record.session.close()

    def shutdown(self) -> None:
        with self._lock:
            records = list(self._sessions.values())
            self._sessions.clear()
        for record in records:
            record.session.close()

    def _get(self, agent_session_id: str) -> AgentSessionRecord:
        self._evict_expired()
        with self._lock:
            record = self._sessions.get(agent_session_id)
        if record is None:
            raise ValueError("agent_session_not_found")
        return record

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired: list[AgentSessionRecord] = []
        with self._lock:
            for agent_session_id, record in list(self._sessions.items()):
                if now - record.last_used_at > self._ttl_seconds:
                    expired.append(record)
                    del self._sessions[agent_session_id]
        for record in expired:
            record.session.close()


def _iso_from_monotonic_age(value: float) -> str:
    age = max(time.monotonic() - value, 0.0)
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age)).isoformat()
