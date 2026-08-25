"""Deterministic point-in-time reference selection for the estimator."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from uuid import UUID

from coding_trajectory.estimation.task import (
    SessionGraphIndexCache,
    TargetConfig,
    TaskExclusion,
    candidate_for_turn,
    project_target_config,
    task_fingerprint,
    turn_episode_exclusion,
)
from coding_trajectory.ingestion.common import canonical_json
from coding_trajectory.query import DocumentStore

RETRIEVAL_POLICY_VERSION = "ct.estimation.retrieval.v1"


def select_examples(
    store: DocumentStore,
    *,
    data_cutoff_at: datetime,
    exclude_turn_id: UUID | None,
    exclude_session_ids: frozenset[UUID] | set[UUID],
    exclude_task_fingerprint: str | None,
    target_project_name: str | None,
    target: TargetConfig,
    k: int,
) -> dict[str, Any]:
    """Select at most ``k`` reference examples whose terminal evidence existed
    at or before ``data_cutoff_at``.

    The fallback hierarchy (same project, then same harness, then same model)
    is an ordering policy, never permission to use future examples. The target
    turn, every session in its graph, and any record sharing the target task
    fingerprint are always excluded.
    """

    corpus: list[dict[str, Any]] = []
    indexes = SessionGraphIndexCache(store)
    for session_graph in store.session_graphs.values():
        for session in session_graph.sessions:
            if session.session_id in exclude_session_ids:
                continue
            for turn in session.turns:
                if exclude_turn_id is not None and turn.turn_id == exclude_turn_id:
                    continue
                if turn.ended_at is None or turn.ended_at > data_cutoff_at:
                    continue
                candidate = candidate_for_turn(store, turn.turn_id, indexes=indexes)
                if isinstance(candidate, TaskExclusion):
                    continue
                if turn_episode_exclusion(candidate) is not None:
                    continue
                fingerprint = task_fingerprint(candidate.request_text)
                if (
                    exclude_task_fingerprint is not None
                    and fingerprint == exclude_task_fingerprint
                ):
                    continue
                minutes = (turn.ended_at - turn.started_at).total_seconds() / 60.0
                corpus.append(
                    {
                        "turn_id": str(turn.turn_id),
                        "root_session_id": str(session_graph.root_session_id),
                        "actual_minutes": round(minutes, 3),
                        "ended_at": turn.ended_at,
                        "project_name": session_graph.project_identifier,
                        "harness_name": session.vendor.value
                        if session.vendor
                        else None,
                        "model": project_target_config(candidate).model,
                        "request_preview": candidate.request_text[:200],
                    }
                )

    corpus_fingerprint = hashlib.sha256(
        canonical_json(
            sorted(
                f"{item['turn_id']}:{item['ended_at'].isoformat()}:{item['actual_minutes']}"
                for item in corpus
            )
        ).encode("utf-8")
    ).hexdigest()

    def rank(item: dict[str, Any]) -> tuple[Any, ...]:
        return (
            item["project_name"] is not None
            and item["project_name"] == target_project_name,
            item["harness_name"] is not None
            and item["harness_name"] == target.harness_name,
            item["model"] is not None and item["model"] == target.model,
            item["ended_at"],
            item["turn_id"],
        )

    ordered = sorted(corpus, key=rank, reverse=True)
    examples = [
        {
            "turn_id": item["turn_id"],
            "root_session_id": item["root_session_id"],
            "actual_minutes": item["actual_minutes"],
            "project_name": item["project_name"],
            "harness_name": item["harness_name"],
            "model": item["model"],
        }
        for item in ordered[: max(k, 0)]
    ]
    return {
        "policy_version": RETRIEVAL_POLICY_VERSION,
        "corpus_fingerprint": corpus_fingerprint,
        "data_cutoff_at": data_cutoff_at,
        "examples": examples,
    }
