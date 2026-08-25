"""Resumable backfill scheduling and budgets for historical backcasts."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from coding_trajectory.estimation.ledger import ForecastLedger
from coding_trajectory.estimation.task import (
    SessionGraphIndexCache,
    TaskExclusion,
    candidate_for_turn,
    turn_episode_exclusion,
)
from coding_trajectory.query import DocumentStore

# Bounded retries for transport/provider failures; schema and eligibility
# failures stay inspectable as permanent failures instead of being retried.
MAX_ATTEMPTS = 2

ForecastOne = Callable[[UUID], dict[str, Any]]


def run_backfill(
    params: dict[str, Any],
    *,
    store: DocumentStore,
    ledger: ForecastLedger,
    forecast_one: ForecastOne,
    spec: dict[str, Any],
) -> dict[str, Any]:
    """Run one resumable backcast job to completion in the foreground.

    Restart safety comes from the ledger: re-running with the same ``job_id``
    skips already processed candidates, and forecasts already succeeded under
    the same idempotency key are never regenerated.
    """

    return _run_backfill(
        params,
        store=store,
        ledger=ledger,
        forecast_one=forecast_one,
        spec=spec,
    )


def _run_backfill(
    params: dict[str, Any],
    *,
    store: DocumentStore,
    ledger: ForecastLedger,
    forecast_one: ForecastOne,
    spec: dict[str, Any],
) -> dict[str, Any]:
    with ledger.transaction():
        ledger_jobs = ledger.jobs()
        resume_job_id = params.get("job_id")
        if resume_job_id:
            job = ledger_jobs.get(resume_job_id)
            if job is None:
                raise ValueError(f"backfill job not found: {resume_job_id}")
            if not _same_backfill_spec(job.get("spec") or {}, spec):
                raise ValueError(
                    "backfill resume parameters do not match the original job spec"
                )
            job_id = resume_job_id
            processed = set(job.get("processed_turn_ids") or [])
            counts = _resume_counts(job)
        else:
            job_id = uuid4().hex
            processed = set()
            counts = _fresh_counts()
            ledger.append_job_event(
                "job_created",
                {
                    "job_id": job_id,
                    "created_at": _utcnow(),
                    "spec": spec,
                },
            )

    max_forecasts = int(params.get("max_forecasts") or 25)
    concurrency = int(params.get("concurrency") or 4)
    inventory = _candidate_inventory(store)

    stop_reason = "inventory_exhausted"
    status = "completed"
    indexes = SessionGraphIndexCache(store)
    remaining = iter(inventory)
    in_flight: dict[Future[str], UUID] = {}
    inventory_exhausted = False

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        while True:
            while (
                not inventory_exhausted
                and len(in_flight) < concurrency
                and counts["succeeded"] + len(in_flight) < max_forecasts
            ):
                try:
                    turn_id = next(remaining)
                except StopIteration:
                    inventory_exhausted = True
                    break
                if str(turn_id) in processed:
                    continue
                eligibility = _eligibility_outcome(store, turn_id, indexes=indexes)
                if eligibility != "eligible":
                    _record_candidate(
                        ledger, job_id=job_id, turn_id=turn_id, outcome=eligibility
                    )
                    _count(counts, eligibility)
                    processed.add(str(turn_id))
                    continue
                future = executor.submit(
                    _forecast_eligible_candidate,
                    forecast_one=forecast_one,
                    turn_id=turn_id,
                )
                in_flight[future] = turn_id

            if not in_flight:
                if counts["succeeded"] >= max_forecasts:
                    status = "stopped"
                    stop_reason = "budget_exhausted:max_forecasts"
                break

            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            future = next(iter(done))
            turn_id = in_flight.pop(future)
            outcome = future.result()
            _record_candidate(ledger, job_id=job_id, turn_id=turn_id, outcome=outcome)
            _count(counts, outcome)
            processed.add(str(turn_id))

    ledger.append_job_event(
        "job_finished",
        {
            "job_id": job_id,
            "status": status,
            "finished_at": _utcnow(),
            "stop_reason": stop_reason,
        },
    )
    return _job_response(ledger, job_id)


def backfill_status(ledger: ForecastLedger, job_id: str) -> dict[str, Any]:
    job = ledger.find_job(job_id)
    if job is None:
        raise ValueError(f"backfill job not found: {job_id}")
    return _job_response(ledger, job_id)


def _candidate_inventory(store: DocumentStore) -> list[UUID]:
    """Deterministic candidate inventory over every canonical turn."""

    turns = sorted(
        store.turns.values(),
        key=lambda turn: (turn.started_at, str(turn.turn_id)),
    )
    return [turn.turn_id for turn in turns]


def _eligibility_outcome(
    store: DocumentStore,
    *,
    turn_id: UUID,
    indexes: SessionGraphIndexCache,
) -> str:
    candidate = candidate_for_turn(store, turn_id, indexes=indexes)
    if isinstance(candidate, TaskExclusion):
        return f"excluded:{candidate.reason}"
    episode_exclusion = turn_episode_exclusion(candidate)
    if episode_exclusion is not None:
        return f"excluded:{episode_exclusion.reason}"
    return "eligible"


def _forecast_eligible_candidate(*, forecast_one: ForecastOne, turn_id: UUID) -> str:
    result = forecast_one(turn_id)
    outcome = str(result.get("outcome") or "permanent_failed")
    attempts = 1
    while outcome == "retryable_failed" and attempts < MAX_ATTEMPTS:
        result = forecast_one(turn_id)
        outcome = str(result.get("outcome") or "permanent_failed")
        attempts += 1
    return outcome


def _record_candidate(
    ledger: ForecastLedger,
    *,
    job_id: str,
    turn_id: UUID,
    outcome: str,
) -> None:
    ledger.append_job_event(
        "candidate_processed",
        {"job_id": job_id, "turn_id": str(turn_id), "outcome": outcome},
    )


def _count(counts: dict[str, Any], outcome: str) -> None:
    if outcome.startswith("excluded:"):
        reason = outcome.split(":", 1)[1]
        excluded = counts.setdefault("excluded", {})
        excluded[reason] = excluded.get(reason, 0) + 1
        return
    counts[outcome] = counts.get(outcome, 0) + 1


def _fresh_counts() -> dict[str, Any]:
    return {
        "eligible": 0,
        "succeeded": 0,
        "skipped_existing": 0,
        "retryable_failed": 0,
        "permanent_failed": 0,
        "excluded": {},
    }


def _same_backfill_spec(existing: dict[str, Any], requested: dict[str, Any]) -> bool:
    """Allow scheduler parallelism to change when resuming a durable job.

    Concurrency affects only how many independent stateless estimator turns are
    in flight. It is not part of the retrieval or estimator cohort that gives
    forecast artifacts their meaning, and older job receipts predate this
    setting entirely.
    """

    existing_identity = dict(existing)
    requested_identity = dict(requested)
    existing_identity.pop("concurrency", None)
    requested_identity.pop("concurrency", None)
    return existing_identity == requested_identity


def _resume_counts(job: dict[str, Any]) -> dict[str, Any]:
    counts = _fresh_counts()
    prior = job.get("counts") or {}
    for key in (
        "eligible",
        "succeeded",
        "skipped_existing",
        "retryable_failed",
        "permanent_failed",
    ):
        counts[key] = int(prior.get(key) or 0)
    counts["excluded"] = dict(prior.get("excluded") or {})
    return counts


def _job_response(ledger: ForecastLedger, job_id: str) -> dict[str, Any]:
    job = ledger.find_job(job_id)
    if job is None:
        raise ValueError(f"backfill job not found: {job_id}")
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "created_at": job["created_at"],
        "finished_at": job["finished_at"],
        "spec": job["spec"],
        "counts": job["counts"],
        "stop_reason": job["stop_reason"],
    }


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()
