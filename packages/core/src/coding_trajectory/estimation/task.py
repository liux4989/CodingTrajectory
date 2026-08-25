"""Pre-execution task candidates, eligibility, and target-configuration projection."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from coding_trajectory.analysis.request_lineage import effective_user_request
from coding_trajectory.analysis.session_stats import session_title
from coding_trajectory.ingestion.common import canonical_json
from coding_trajectory.ingestion.indexes import (
    SessionGraphIndex,
    build_session_graph_index,
)
from coding_trajectory.ingestion.models import (
    EventType,
    Session,
    Turn,
    TurnStatus,
)
from coding_trajectory.query import DocumentStore

TASK_SNAPSHOT_VERSION = "ct.estimation.task_snapshot.v1"

_PRIOR_TURN_LIMIT = 5


class SessionGraphIndexCache:
    """Memoizes one session-graph index per root for corpus-scale scans."""

    def __init__(self, store: DocumentStore) -> None:
        self._store = store
        self._indexes: dict[UUID, SessionGraphIndex] = {}

    def index_for(self, root_session_id: UUID) -> SessionGraphIndex | None:
        if root_session_id not in self._indexes:
            session_graph = self._store.session_graphs.get(root_session_id)
            if session_graph is None:
                return None
            self._indexes[root_session_id] = build_session_graph_index(session_graph)
        return self._indexes[root_session_id]


@dataclass(frozen=True, slots=True)
class TaskCandidate:
    """One eligible canonical turn episode with its pre-execution evidence."""

    turn: Turn
    session: Session
    root_session_id: UUID
    graph_session_ids: frozenset[UUID]
    project_name: str | None
    request_type: str
    request_source: str
    request_text: str
    task_available_at: datetime
    target_execution_started_at: datetime


@dataclass(frozen=True, slots=True)
class TaskExclusion:
    reason: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class TargetConfig:
    """Observed or declared target execution configuration.

    Fields that are not observed in immutable source evidence stay ``None``
    (``unknown``); the estimator must not guess them.
    """

    agent_vendor: str | None = None
    harness_name: str | None = None
    harness_version: str | None = None
    model: str | None = None
    effort: str | None = None
    execution_policy_fingerprint: str | None = None
    approval_policy: str | None = None
    sandbox_mode: str | None = None
    permission_mode: str | None = None
    multi_agent_mode: str | None = None
    spawn_depth: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "agent_vendor": self.agent_vendor,
                "harness_name": self.harness_name,
                "harness_version": self.harness_version,
                "model": self.model,
                "effort": self.effort,
                "execution_policy_fingerprint": self.execution_policy_fingerprint,
                "approval_policy": self.approval_policy,
                "sandbox_mode": self.sandbox_mode,
                "permission_mode": self.permission_mode,
                "multi_agent_mode": self.multi_agent_mode,
                "spawn_depth": self.spawn_depth,
            }.items()
            if value is not None
        }


def normalize_request_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).lower()


def task_fingerprint(request_text: str) -> str:
    return hashlib.sha256(
        canonical_json({"request": normalize_request_text(request_text)}).encode(
            "utf-8"
        )
    ).hexdigest()


def candidate_for_turn(
    store: DocumentStore,
    turn_id: UUID,
    *,
    indexes: SessionGraphIndexCache | None = None,
) -> TaskCandidate | TaskExclusion:
    turn = store.turns.get(turn_id)
    if turn is None:
        return TaskExclusion("turn_not_found", str(turn_id))
    session = store.sessions.get(turn.session_id)
    if session is None:
        return TaskExclusion("session_not_found", str(turn.session_id))
    root_session_id = store.session_to_root.get(session.session_id)
    if root_session_id is None:
        return TaskExclusion("root_session_not_found", str(session.session_id))
    session_graph = store.session_graphs.get(root_session_id)
    if session_graph is None:
        return TaskExclusion("session_graph_not_found", str(root_session_id))

    if indexes is not None:
        index = indexes.index_for(root_session_id)
        if index is None:
            return TaskExclusion("session_graph_not_found", str(root_session_id))
    else:
        index = build_session_graph_index(session_graph)
    request = effective_user_request(index, turn, session=session)
    if request is None or not request.get("content"):
        return TaskExclusion("no_user_request", str(turn_id))

    request_event = (
        store.events.get(turn.user_request_event_id)
        if turn.user_request_event_id is not None
        else None
    )
    task_available_at = (
        request_event.timestamp if request_event is not None else turn.started_at
    )
    item_starts = [item.started_at for item in turn.items if item.started_at]
    target_execution_started_at = min(item_starts) if item_starts else turn.started_at

    return TaskCandidate(
        turn=turn,
        session=session,
        root_session_id=root_session_id,
        graph_session_ids=frozenset(item.session_id for item in session_graph.sessions),
        project_name=session_graph.project_identifier,
        request_type=request.get("type") or "message",
        request_source=request.get("source") or "unknown",
        request_text=request["content"],
        task_available_at=task_available_at,
        target_execution_started_at=target_execution_started_at,
    )


def turn_episode_exclusion(candidate: TaskCandidate) -> TaskExclusion | None:
    """Exclusion reasons that keep a turn out of actual-duration statistics."""

    if candidate.turn.status == TurnStatus.INTERRUPTED:
        return TaskExclusion("interrupted", str(candidate.turn.turn_id))
    if candidate.turn.ended_at is None:
        return TaskExclusion("missing_terminal_time", str(candidate.turn.turn_id))
    seconds = (candidate.turn.ended_at - candidate.turn.started_at).total_seconds()
    if seconds <= 0:
        return TaskExclusion("zero_duration", str(candidate.turn.turn_id))
    return None


def assign_forecast_kind(
    *,
    turn_bound: bool,
    task_available_at: datetime | None,
    target_execution_started_at: datetime | None,
    issued_at: datetime,
) -> str:
    """Assign the forecast kind from observed timing; caller labels are not trusted."""

    if not turn_bound:
        return "prospective_unbound"
    if (
        task_available_at is not None
        and target_execution_started_at is not None
        and task_available_at <= issued_at < target_execution_started_at
    ):
        return "prospective"
    return "historical_backcast"


def build_task_snapshot(
    candidate: TaskCandidate,
    *,
    target: TargetConfig,
) -> dict[str, Any]:
    """Pre-execution evidence only; never the turn's own items or outcome."""

    prior_turns: list[dict[str, Any]] = []
    session_turns = sorted(
        (
            turn
            for turn in candidate.session.turns
            if turn.sequence < candidate.turn.sequence
        ),
        key=lambda turn: turn.sequence,
    )
    for turn in session_turns[-_PRIOR_TURN_LIMIT:]:
        duration_seconds = (
            round((turn.ended_at - turn.started_at).total_seconds())
            if turn.ended_at is not None
            else None
        )
        prior_turns.append(
            {
                "sequence": turn.sequence,
                "status": turn.status.value,
                "duration_seconds": duration_seconds,
            }
        )

    return {
        "snapshot_version": TASK_SNAPSHOT_VERSION,
        "request": {
            "type": candidate.request_type,
            "source": candidate.request_source,
            "text": candidate.request_text,
        },
        "project_name": candidate.project_name,
        "session_title": session_title(candidate.session),
        "task_class": None,
        "prior_turns": prior_turns,
        "target": target.as_dict(),
    }


def snapshot_fingerprint(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()


def project_target_config(candidate: TaskCandidate) -> TargetConfig:
    """Project the observed target harness configuration from canonical evidence."""

    session = candidate.session
    vendor = session.vendor.value if session.vendor else None
    extensions = session.extensions
    approval_policy = None
    sandbox_mode = None
    permission_mode = None
    multi_agent_mode = None
    spawn_depth = None
    if extensions is not None and extensions.codex is not None:
        approval_policy = extensions.codex.approval_policy
        sandbox_mode = extensions.codex.sandbox_mode
        multi_agent_mode = extensions.codex.multi_agent_mode
        spawn_depth = extensions.codex.spawn_depth
    if extensions is not None and extensions.claude_code is not None:
        permission_mode = extensions.claude_code.permission_mode
        if spawn_depth is None:
            spawn_depth = extensions.claude_code.spawn_depth

    policy_fields = {
        "approval_policy": approval_policy,
        "sandbox_mode": sandbox_mode,
        "permission_mode": permission_mode,
        "multi_agent_mode": multi_agent_mode,
        "spawn_depth": spawn_depth,
    }
    known_policy = {
        key: value for key, value in policy_fields.items() if value is not None
    }
    policy_fingerprint = (
        hashlib.sha256(canonical_json(known_policy).encode("utf-8")).hexdigest()
        if known_policy
        else None
    )

    return TargetConfig(
        agent_vendor=vendor,
        harness_name=vendor,
        # Harness version is not part of canonical evidence; stays unknown.
        harness_version=None,
        model=_dominant_turn_model(candidate),
        effort=_turn_effort(candidate),
        execution_policy_fingerprint=policy_fingerprint,
        approval_policy=approval_policy,
        sandbox_mode=sandbox_mode,
        permission_mode=permission_mode,
        multi_agent_mode=multi_agent_mode,
        spawn_depth=spawn_depth,
    )


def _dominant_turn_model(candidate: TaskCandidate) -> str | None:
    event_ids = set(candidate.turn.event_ids)
    counts: dict[str, int] = {}
    for event in candidate.session.events:
        if event.event_id not in event_ids:
            continue
        if event.type != EventType.LLM_RESPONSE:
            continue
        model = event.payload.get("model") or event.payload.get("model_version")
        if isinstance(model, str) and model:
            counts[model] = counts.get(model, 0) + 1
    if not counts:
        return None
    return max(sorted(counts), key=lambda model: counts[model])


def _turn_effort(candidate: TaskCandidate) -> str | None:
    """Observed reasoning effort for the turn, when the vendor recorded one."""

    turn_id_raw = str(candidate.turn.turn_id)
    for observation in candidate.session.runtime_observations:
        if observation.kind != "effort_changed":
            continue
        if observation.turn_id_raw and observation.turn_id_raw != turn_id_raw:
            continue
        if (
            observation.timestamp <= candidate.turn.started_at
            or observation.turn_id_raw == turn_id_raw
        ) and observation.effort_to:
            return observation.effort_to
    return None
