"""Forecast pipeline: eligibility, retrieval, estimate, record, compare."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from coding_trajectory.estimation.comparison import join_actual
from coding_trajectory.estimation.ledger import ForecastLedger
from coding_trajectory.estimation.provider import (
    ESTIMATOR_PROMPT_VERSION,
    ESTIMATOR_SCHEMA_VERSION,
    EstimateError,
    EstimateSchemaError,
    EstimateTransportError,
    EstimatorProvider,
    build_prompt,
    prompt_fingerprint,
)
from coding_trajectory.estimation.retrieval import (
    RETRIEVAL_POLICY_VERSION,
    select_examples,
)
from coding_trajectory.estimation.store_resolution import full_store
from coding_trajectory.estimation.task import (
    TASK_SNAPSHOT_VERSION,
    TargetConfig,
    TaskCandidate,
    TaskExclusion,
    assign_forecast_kind,
    build_task_snapshot,
    candidate_for_turn,
    project_target_config,
    snapshot_fingerprint,
    task_fingerprint,
)
from coding_trajectory.ingestion.common import format_datetime


def run_forecast_for_turn(
    store: Any,
    *,
    ledger: ForecastLedger,
    provider: EstimatorProvider,
    estimator: dict[str, Any],
    turn_id: UUID,
    max_examples: int,
    cache: Any,
) -> dict[str, Any]:
    """Single-turn pipeline: eligibility, retrieval, estimate, record, compare.

    The provider call deliberately runs outside a ledger transaction.  Backfill
    workers may use independent app-server connections concurrently; only the
    durable read/check/write boundary is serialized below.
    """
    candidate = candidate_for_turn(store, turn_id)
    if isinstance(candidate, TaskExclusion):
        return {
            "outcome": "permanent_failed",
            "failure": {
                "state": "not_applicable",
                "reason": candidate.reason,
                "detail": candidate.detail,
            },
        }

    issued_at = datetime.now(UTC)
    kind = assign_forecast_kind(
        turn_bound=True,
        task_available_at=candidate.task_available_at,
        target_execution_started_at=candidate.target_execution_started_at,
        issued_at=issued_at,
    )
    target = project_target_config(candidate)
    snapshot = build_task_snapshot(candidate, target=target)
    fingerprint = task_fingerprint(candidate.request_text)
    # A backcast may only retrieve evidence available when the target request
    # became available; a prospective forecast cuts off at issue time.
    data_cutoff_at = (
        min(issued_at, candidate.task_available_at)
        if kind == "historical_backcast"
        else issued_at
    )

    key = idempotency_key(
        turn_id=str(turn_id),
        task_fingerprint=fingerprint,
        snapshot_fingerprint=snapshot_fingerprint(snapshot),
        estimator=estimator,
        retrieval_policy_version=RETRIEVAL_POLICY_VERSION,
    )
    existing = ledger.find_by_idempotency_key(key)
    if existing is not None:
        existing_comparison = existing.get("comparison")
        if existing_comparison is None or (
            existing_comparison.get("exclusion") == "missing_terminal_time"
        ):
            comparison = join_actual(
                store,
                turn_id=turn_id,
                source_paths=source_paths(cache, candidate),
            )
            if append_terminal_comparison(
                ledger,
                existing["prediction_id"],
                comparison,
            ):
                existing = ledger.find_forecast(existing["prediction_id"]) or existing
        return {
            "outcome": "skipped_existing",
            "record": existing,
            "reused_existing": True,
        }

    retrieval = select_examples(
        store,
        data_cutoff_at=data_cutoff_at,
        exclude_turn_id=turn_id,
        exclude_session_ids=candidate.graph_session_ids,
        exclude_task_fingerprint=fingerprint,
        target_project_name=candidate.project_name,
        target=target,
        k=max_examples,
    )
    retrieval["data_cutoff_at"] = format_datetime(retrieval["data_cutoff_at"])

    prompt = build_prompt(snapshot=snapshot, examples=retrieval["examples"])
    try:
        result = provider.estimate(
            prompt=prompt,
            model=estimator.get("model"),
            effort=estimator.get("effort"),
        )
    except EstimateError as exc:
        return record_attempt_failure(
            ledger,
            idempotency_key=key,
            turn_id=str(turn_id),
            estimator=estimator,
            exc=exc,
        )

    issued_iso = format_datetime(issued_at)
    record = {
        "prediction_id": uuid4().hex,
        "idempotency_key": key,
        "forecast_kind": kind,
        "turn_id": str(turn_id),
        "root_session_id": str(candidate.root_session_id),
        "session_id": str(candidate.session.session_id),
        "task_fingerprint": fingerprint,
        "task_available_at": format_datetime(candidate.task_available_at),
        "target_execution_started_at": format_datetime(
            candidate.target_execution_started_at
        ),
        "issued_at": issued_iso,
        "data_cutoff_at": format_datetime(data_cutoff_at),
        "project_name": candidate.project_name,
        "task_class": None,
        "session_title": snapshot.get("session_title"),
        "task_snapshot": snapshot,
        "target": target.as_dict(),
        "estimator": estimator,
        "prompt_fingerprint": prompt_fingerprint(prompt),
        "retrieval": retrieval,
        "p50_minutes": result.p50_minutes,
        "p80_minutes": result.p80_minutes,
        "created_at": issued_iso,
    }
    # A second worker can form the same deterministic plan while this worker is
    # waiting on the estimator.  Re-check and write under the ledger lock so we
    # never retain two forecast artifacts for one idempotency key.
    with ledger.transaction():
        existing = ledger.find_by_idempotency_key(key)
        if existing is not None:
            return {
                "outcome": "skipped_existing",
                "record": existing,
                "reused_existing": True,
            }
        ledger.append_forecast(record)
        comparison = join_actual(
            store,
            turn_id=turn_id,
            source_paths=source_paths(cache, candidate),
        )
        append_terminal_comparison(ledger, record["prediction_id"], comparison)
        persisted = ledger.find_forecast(record["prediction_id"])

    return {"outcome": "succeeded", "record": persisted}


def predict_unbound(
    params: dict[str, Any],
    *,
    ledger: ForecastLedger,
    provider: EstimatorProvider,
    estimator: dict[str, Any],
    current_dir: Path,
    cache: Any,
) -> dict[str, Any]:
    """Forecast from task text before a target turn exists (prospective_unbound)."""

    with ledger.transaction():
        task_text = str(params["task_text"]).strip()
        target = TargetConfig(
            agent_vendor=params.get("target_agent_vendor"),
            harness_name=params.get("target_harness_name"),
            harness_version=params.get("target_harness_version"),
            model=params.get("target_model"),
            effort=params.get("target_effort"),
            execution_policy_fingerprint=params.get(
                "target_execution_policy_fingerprint"
            ),
        )
        snapshot = {
            "snapshot_version": TASK_SNAPSHOT_VERSION,
            "request": {"type": "message", "source": "human_user", "text": task_text},
            "project_name": params.get("project_name"),
            "session_title": None,
            "task_class": None,
            "prior_turns": [],
            "target": target.as_dict(),
        }
        fingerprint = task_fingerprint(task_text)
        issued_at = datetime.now(UTC)
        key = idempotency_key(
            turn_id=None,
            task_fingerprint=fingerprint,
            snapshot_fingerprint=snapshot_fingerprint(snapshot),
            estimator=estimator,
            retrieval_policy_version=RETRIEVAL_POLICY_VERSION,
        )
        existing = ledger.find_by_idempotency_key(key)
        if existing is not None:
            return {"forecast": existing, "failure": None, "reused_existing": True}

        store = full_store({}, current_dir=current_dir, cache=cache)
        retrieval = select_examples(
            store,
            data_cutoff_at=issued_at,
            exclude_turn_id=None,
            exclude_session_ids=frozenset(),
            exclude_task_fingerprint=fingerprint,
            target_project_name=params.get("project_name"),
            target=target,
            k=int(params.get("max_examples") or 8),
        )
        retrieval["data_cutoff_at"] = format_datetime(retrieval["data_cutoff_at"])

        prompt = build_prompt(snapshot=snapshot, examples=retrieval["examples"])
        try:
            result = provider.estimate(
                prompt=prompt,
                model=estimator.get("model"),
                effort=estimator.get("effort"),
            )
        except EstimateError as exc:
            failure = failure_payload(exc)
            ledger.append_attempt_failure(
                {
                    "idempotency_key": key,
                    "turn_id": None,
                    "task_fingerprint": fingerprint,
                    "estimator": estimator,
                    **failure,
                }
            )
            return {"forecast": None, "failure": failure, "reused_existing": False}

        issued_iso = format_datetime(issued_at)
        record = {
            "prediction_id": uuid4().hex,
            "idempotency_key": key,
            "forecast_kind": "prospective_unbound",
            "turn_id": None,
            "root_session_id": None,
            "session_id": None,
            "task_fingerprint": fingerprint,
            "task_available_at": None,
            "target_execution_started_at": None,
            "issued_at": issued_iso,
            "data_cutoff_at": issued_iso,
            "project_name": params.get("project_name"),
            "task_class": None,
            "session_title": None,
            "task_snapshot": snapshot,
            "target": target.as_dict(),
            "estimator": estimator,
            "prompt_fingerprint": prompt_fingerprint(prompt),
            "retrieval": retrieval,
            "p50_minutes": result.p50_minutes,
            "p80_minutes": result.p80_minutes,
            "created_at": issued_iso,
        }
        ledger.append_forecast(record)
        return {
            "forecast": ledger.find_forecast(record["prediction_id"]),
            "failure": None,
            "reused_existing": False,
        }


def estimator_config(
    provider: EstimatorProvider, model: str | None, effort: str | None
) -> dict[str, Any]:
    return {
        "provider": provider.provider_name,
        "model": model,
        "effort": effort,
        "prompt_version": ESTIMATOR_PROMPT_VERSION,
        "schema_version": ESTIMATOR_SCHEMA_VERSION,
    }


def idempotency_key(
    *,
    turn_id: str | None,
    task_fingerprint: str,
    snapshot_fingerprint: str,
    estimator: dict[str, Any],
    retrieval_policy_version: str,
) -> str:
    from coding_trajectory.ingestion.common import canonical_json

    return hashlib.sha256(
        canonical_json(
            {
                "turn_id": turn_id,
                "task_fingerprint": task_fingerprint,
                "snapshot_fingerprint": snapshot_fingerprint,
                "estimator": estimator,
                "retrieval_policy_version": retrieval_policy_version,
            }
        ).encode("utf-8")
    ).hexdigest()


def failure_payload(exc: EstimateError) -> dict[str, Any]:
    state = (
        "retryable_failed"
        if isinstance(exc, EstimateTransportError)
        else "permanent_failed"
    )
    reason = (
        "provider_transport"
        if isinstance(exc, EstimateTransportError)
        else "schema_violation"
        if isinstance(exc, EstimateSchemaError)
        else "provider_error"
    )
    return {"state": state, "reason": reason, "detail": str(exc)}


def record_attempt_failure(
    ledger: ForecastLedger,
    *,
    idempotency_key: str,
    turn_id: str | None,
    estimator: dict[str, Any],
    exc: EstimateError,
) -> dict[str, Any]:
    failure = failure_payload(exc)
    ledger.append_attempt_failure(
        {
            "idempotency_key": idempotency_key,
            "turn_id": turn_id,
            "estimator": estimator,
            **failure,
        }
    )
    return {"outcome": failure["state"], "failure": failure}


def append_terminal_comparison(
    ledger: ForecastLedger,
    prediction_id: str,
    comparison: dict[str, Any],
) -> bool:
    if comparison.get("exclusion") == "missing_terminal_time":
        return False
    ledger.append_comparison(prediction_id, comparison)
    return True


def source_paths(cache: Any, candidate: TaskCandidate) -> list[Path]:
    if cache is None:
        return []
    return [
        Path(path)
        for path in cache.paths_for_session_graph(str(candidate.root_session_id))
    ]
