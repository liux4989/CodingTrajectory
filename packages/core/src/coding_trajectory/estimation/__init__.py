"""Agent temporality estimation: forecasts, actuals, and calibration.

CT owns eligibility, retrieval, prompt assembly, schema validation, attempt
state, comparison, and aggregation. The estimator provider owns only the
bounded semantic inference turn. Estimator output is durable derived
evidence, never canonical CT data.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from coding_trajectory.estimation.calibration import compute_calibration
from coding_trajectory.estimation.codex import CodexAppServerEstimator
from coding_trajectory.estimation.comparison import join_actual
from coding_trajectory.estimation.jobs import backfill_status, run_backfill
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
from coding_trajectory.ingestion.common import format_datetime, parse_iso_timestamp

__all__ = [
    "ForecastLedger",
    "serve_estimate",
]


def serve_estimate(
    method: str,
    params: dict[str, Any],
    *,
    global_scope: bool,
    current_dir: Path,
    cache: Any,
) -> Any:
    """Dispatch one ``estimate.*`` service method."""

    ledger = ForecastLedger()
    if method == "estimate.predict":
        return _predict(
            params,
            ledger=ledger,
            global_scope=global_scope,
            current_dir=current_dir,
            cache=cache,
        )
    if method == "estimate.bind":
        return _bind(
            params,
            ledger=ledger,
            global_scope=global_scope,
            current_dir=current_dir,
            cache=cache,
        )
    if method == "estimate.get":
        record = ledger.find_forecast(params["prediction_id"])
        refreshed = _refresh_pending_comparisons(
            [record] if record is not None else [],
            ledger=ledger,
            current_dir=current_dir,
            cache=cache,
        )
        return {"forecast": refreshed[0] if refreshed else None}
    if method == "estimate.list":
        return _list(
            params,
            ledger=ledger,
            current_dir=current_dir,
            cache=cache,
        )
    if method == "estimate.calibration":
        return _calibration(
            params,
            ledger=ledger,
            current_dir=current_dir,
            cache=cache,
        )
    if method == "estimate.backfill.start":
        return _backfill_start(
            params,
            ledger=ledger,
            current_dir=current_dir,
            cache=cache,
        )
    if method == "estimate.backfill.status":
        return {"job": backfill_status(ledger, params["job_id"])}
    raise KeyError(f"no estimate handler registered for {method}")


# ---------------------------------------------------------------------------
# estimate.predict
# ---------------------------------------------------------------------------


def _predict(
    params: dict[str, Any],
    *,
    ledger: ForecastLedger,
    global_scope: bool,
    current_dir: Path,
    cache: Any,
) -> dict[str, Any]:
    provider = CodexAppServerEstimator()
    estimator = _estimator_config(
        provider, params.get("estimator_model"), params.get("estimator_effort")
    )
    if params.get("turn_id"):
        # Retrieval needs the global point-in-time corpus, not just the
        # target graph's store; full discovery also resolves the candidate.
        store = _full_store(params, current_dir=current_dir, cache=cache)
        outcome = run_forecast_for_turn(
            store,
            ledger=ledger,
            provider=provider,
            estimator=estimator,
            turn_id=UUID(params["turn_id"]),
            max_examples=int(params.get("max_examples") or 8),
            cache=cache,
        )
        return {
            "forecast": outcome.get("record"),
            "failure": outcome.get("failure"),
            "reused_existing": outcome.get("reused_existing", False),
        }

    return _predict_unbound(
        params,
        ledger=ledger,
        provider=provider,
        estimator=estimator,
        current_dir=current_dir,
        cache=cache,
    )


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

    return _run_forecast_for_turn(
        store,
        ledger=ledger,
        provider=provider,
        estimator=estimator,
        turn_id=turn_id,
        max_examples=max_examples,
        cache=cache,
    )


def _run_forecast_for_turn(
    store: Any,
    *,
    ledger: ForecastLedger,
    provider: EstimatorProvider,
    estimator: dict[str, Any],
    turn_id: UUID,
    max_examples: int,
    cache: Any,
) -> dict[str, Any]:
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

    key = _idempotency_key(
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
                source_paths=_source_paths(cache, candidate),
            )
            if _append_terminal_comparison(
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
        return _record_attempt_failure(
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
            source_paths=_source_paths(cache, candidate),
        )
        _append_terminal_comparison(ledger, record["prediction_id"], comparison)
        persisted = ledger.find_forecast(record["prediction_id"])

    return {"outcome": "succeeded", "record": persisted}


def _predict_unbound(
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
        return _predict_unbound_locked(
            params,
            ledger=ledger,
            provider=provider,
            estimator=estimator,
            current_dir=current_dir,
            cache=cache,
        )


def _predict_unbound_locked(
    params: dict[str, Any],
    *,
    ledger: ForecastLedger,
    provider: EstimatorProvider,
    estimator: dict[str, Any],
    current_dir: Path,
    cache: Any,
) -> dict[str, Any]:
    task_text = str(params["task_text"]).strip()
    target = TargetConfig(
        agent_vendor=params.get("target_agent_vendor"),
        harness_name=params.get("target_harness_name"),
        harness_version=params.get("target_harness_version"),
        model=params.get("target_model"),
        effort=params.get("target_effort"),
        execution_policy_fingerprint=params.get("target_execution_policy_fingerprint"),
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
    key = _idempotency_key(
        turn_id=None,
        task_fingerprint=fingerprint,
        snapshot_fingerprint=snapshot_fingerprint(snapshot),
        estimator=estimator,
        retrieval_policy_version=RETRIEVAL_POLICY_VERSION,
    )
    existing = ledger.find_by_idempotency_key(key)
    if existing is not None:
        return {"forecast": existing, "failure": None, "reused_existing": True}

    store = _full_store({}, current_dir=current_dir, cache=cache)
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
        failure = _failure_payload(exc)
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


# ---------------------------------------------------------------------------
# estimate.bind
# ---------------------------------------------------------------------------


def _bind(
    params: dict[str, Any],
    *,
    ledger: ForecastLedger,
    global_scope: bool,
    current_dir: Path,
    cache: Any,
) -> dict[str, Any]:
    with ledger.transaction():
        return _bind_locked(
            params,
            ledger=ledger,
            global_scope=global_scope,
            current_dir=current_dir,
            cache=cache,
        )


def _bind_locked(
    params: dict[str, Any],
    *,
    ledger: ForecastLedger,
    global_scope: bool,
    current_dir: Path,
    cache: Any,
) -> dict[str, Any]:
    record = ledger.find_forecast(params["prediction_id"])
    if record is None:
        return {
            "forecast": None,
            "failure": {
                "state": "not_applicable",
                "reason": "forecast_not_found",
                "detail": params["prediction_id"],
            },
        }
    if record.get("forecast_kind") != "prospective_unbound":
        return {
            "forecast": record,
            "failure": {
                "state": "not_applicable",
                "reason": "not_unbound",
                "detail": record.get("forecast_kind"),
            },
        }
    if record.get("bound_at"):
        return {
            "forecast": record,
            "failure": {
                "state": "not_applicable",
                "reason": "already_bound",
                "detail": record.get("turn_id"),
            },
        }

    store = _store_for_turn(
        params["turn_id"],
        global_scope=global_scope,
        current_dir=current_dir,
        cache=cache,
    )
    turn_id = UUID(params["turn_id"])
    candidate = candidate_for_turn(store, turn_id)
    if isinstance(candidate, TaskExclusion):
        return {
            "forecast": record,
            "failure": {
                "state": "not_applicable",
                "reason": candidate.reason,
                "detail": candidate.detail,
            },
        }

    failure = _binding_window_check(record, candidate) or _binding_match_check(
        record, candidate
    )
    if failure is not None:
        return {"forecast": record, "failure": failure}

    bound_at = format_datetime(datetime.now(UTC))
    receipt = {
        "bound_at": bound_at,
        "turn_id": str(turn_id),
        "root_session_id": str(candidate.root_session_id),
        "session_id": str(candidate.session.session_id),
        "task_available_at": format_datetime(candidate.task_available_at),
        "target_execution_started_at": format_datetime(
            candidate.target_execution_started_at
        ),
    }
    ledger.append_binding(record["prediction_id"], receipt)

    comparison = join_actual(
        store,
        turn_id=turn_id,
        source_paths=_source_paths(cache, candidate),
    )
    _append_terminal_comparison(ledger, record["prediction_id"], comparison)
    return {"forecast": ledger.find_forecast(record["prediction_id"]), "failure": None}


def _binding_window_check(
    record: dict[str, Any], candidate: TaskCandidate
) -> dict[str, Any] | None:
    issued_at = parse_iso_timestamp(record.get("issued_at"))
    if issued_at is None:
        return {
            "state": "permanent_failed",
            "reason": "missing_issued_at",
            "detail": record.get("prediction_id"),
        }
    if issued_at >= candidate.target_execution_started_at:
        return {
            "state": "permanent_failed",
            "reason": "binding_window_missed",
            "detail": (
                f"issued_at={record.get('issued_at')} not before first target "
                "activity of the bound turn"
            ),
        }
    return None


def _binding_match_check(
    record: dict[str, Any], candidate: TaskCandidate
) -> dict[str, Any] | None:
    if task_fingerprint(candidate.request_text) != record.get("task_fingerprint"):
        return {
            "state": "permanent_failed",
            "reason": "task_fingerprint_mismatch",
            "detail": str(candidate.turn.turn_id),
        }
    observed = project_target_config(candidate).as_dict()
    declared = record.get("target") or {}
    for key, expected in declared.items():
        if expected is None:
            continue
        if observed.get(key) != expected:
            return {
                "state": "permanent_failed",
                "reason": "target_config_mismatch",
                "detail": f"{key}: declared {expected!r} != observed {observed.get(key)!r}",
            }
    return None


# ---------------------------------------------------------------------------
# estimate.list / estimate.calibration
# ---------------------------------------------------------------------------


def _list(
    params: dict[str, Any],
    *,
    ledger: ForecastLedger,
    current_dir: Path,
    cache: Any,
) -> dict[str, Any]:
    records = ledger.forecasts()
    if params.get("forecast_kind"):
        records = [
            item
            for item in records
            if item.get("forecast_kind") == params["forecast_kind"]
        ]
    if params.get("project_name"):
        records = [
            item
            for item in records
            if item.get("project_name") == params["project_name"]
        ]
    if params.get("target_harness_name"):
        records = [
            item
            for item in records
            if (item.get("target") or {}).get("harness_name")
            == params["target_harness_name"]
        ]
    records = _refresh_pending_comparisons(
        records,
        ledger=ledger,
        current_dir=current_dir,
        cache=cache,
    )
    if params.get("status"):
        records = [item for item in records if item.get("status") == params["status"]]
    records.sort(
        key=lambda item: (str(item.get("issued_at")), item["prediction_id"]),
        reverse=True,
    )
    return {"items": records[: int(params.get("limit") or 50)]}


def _calibration(
    params: dict[str, Any],
    *,
    ledger: ForecastLedger,
    current_dir: Path,
    cache: Any,
) -> dict[str, Any]:
    filters = {
        key: params.get(key)
        for key in (
            "forecast_kind",
            "project_name",
            "target_harness_name",
            "target_model",
            "estimator_model",
            "prompt_version",
            "retrieval_policy_version",
        )
        if params.get(key) is not None
    }
    records = _refresh_pending_comparisons(
        ledger.forecasts(),
        ledger=ledger,
        current_dir=current_dir,
        cache=cache,
    )
    return compute_calibration(
        records,
        filters=filters,
        include_buckets=bool(params.get("include_buckets", True)),
    )


# ---------------------------------------------------------------------------
# estimate.backfill.*
# ---------------------------------------------------------------------------


def _backfill_start(
    params: dict[str, Any],
    *,
    ledger: ForecastLedger,
    current_dir: Path,
    cache: Any,
) -> dict[str, Any]:
    store = _full_store(params, current_dir=current_dir, cache=cache)
    provider = CodexAppServerEstimator()
    estimator = _estimator_config(
        provider, params.get("estimator_model"), params.get("estimator_effort")
    )
    max_examples = int(params.get("max_examples") or 8)

    def forecast_one(turn_id: UUID) -> dict[str, Any]:
        return run_forecast_for_turn(
            store,
            ledger=ledger,
            provider=provider,
            estimator=estimator,
            turn_id=turn_id,
            max_examples=max_examples,
            cache=cache,
        )

    spec = {
        "project_name": params.get("project_name"),
        "since_days": params.get("since_days"),
        "agent_vendor": params.get("agent_vendor"),
        "max_forecasts": params.get("max_forecasts"),
        "max_examples": max_examples,
        "concurrency": int(params.get("concurrency") or 4),
        "estimator": estimator,
        "retrieval_policy_version": RETRIEVAL_POLICY_VERSION,
    }
    job = run_backfill(
        params,
        store=store,
        ledger=ledger,
        forecast_one=forecast_one,
        spec=spec,
    )
    return {"job": job}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _estimator_config(
    provider: EstimatorProvider, model: str | None, effort: str | None
) -> dict[str, Any]:
    return {
        "provider": provider.provider_name,
        "model": model,
        "effort": effort,
        "prompt_version": ESTIMATOR_PROMPT_VERSION,
        "schema_version": ESTIMATOR_SCHEMA_VERSION,
    }


def _idempotency_key(
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


def _failure_payload(exc: EstimateError) -> dict[str, Any]:
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


def _record_attempt_failure(
    ledger: ForecastLedger,
    *,
    idempotency_key: str,
    turn_id: str | None,
    estimator: dict[str, Any],
    exc: EstimateError,
) -> dict[str, Any]:
    failure = _failure_payload(exc)
    ledger.append_attempt_failure(
        {
            "idempotency_key": idempotency_key,
            "turn_id": turn_id,
            "estimator": estimator,
            **failure,
        }
    )
    return {"outcome": failure["state"], "failure": failure}


def _append_terminal_comparison(
    ledger: ForecastLedger,
    prediction_id: str,
    comparison: dict[str, Any],
) -> bool:
    if comparison.get("exclusion") == "missing_terminal_time":
        return False
    ledger.append_comparison(prediction_id, comparison)
    return True


def _refresh_pending_comparisons(
    records: list[dict[str, Any]],
    *,
    ledger: ForecastLedger,
    current_dir: Path,
    cache: Any,
) -> list[dict[str, Any]]:
    pending = [
        record
        for record in records
        if record.get("turn_id")
        and (
            record.get("comparison") is None
            or (record.get("comparison") or {}).get("exclusion")
            == "missing_terminal_time"
        )
    ]
    if not pending:
        return records

    with ledger.transaction():
        try:
            store = _full_store({}, current_dir=current_dir, cache=cache)
        except Exception:  # noqa: BLE001 - comparison refresh is best-effort
            return records
        changed = False
        for record in pending:
            latest = ledger.find_forecast(record["prediction_id"])
            latest_comparison = (latest or {}).get("comparison")
            if latest_comparison is not None and (
                latest_comparison.get("exclusion") != "missing_terminal_time"
            ):
                changed = True
                continue
            turn_id = UUID(record["turn_id"])
            candidate = candidate_for_turn(store, turn_id)
            source_paths = (
                _source_paths(cache, candidate)
                if isinstance(candidate, TaskCandidate)
                else []
            )
            comparison = join_actual(
                store,
                turn_id=turn_id,
                source_paths=source_paths,
            )
            changed = (
                _append_terminal_comparison(
                    ledger,
                    record["prediction_id"],
                    comparison,
                )
                or changed
            )
        if not changed:
            return records
        refreshed_by_id = {
            record["prediction_id"]: record for record in ledger.forecasts()
        }
        return [refreshed_by_id[record["prediction_id"]] for record in records]


def _store_for_turn(
    turn_id: str,
    *,
    global_scope: bool,
    current_dir: Path,
    cache: Any,
) -> Any:
    from coding_trajectory.service import resolve_store

    store, _ = resolve_store(
        {"turn_id": turn_id},
        global_scope=global_scope,
        current_dir=current_dir,
        cache=cache,
        include_descendants=True,
    )
    return store


def _full_store(params: dict[str, Any], *, current_dir: Path, cache: Any) -> Any:
    from coding_trajectory.service import resolve_store

    store, _ = resolve_store(
        {
            key: params[key]
            for key in ("project_name", "since_days", "agent_vendor")
            if params.get(key) is not None
        },
        global_scope=True,
        current_dir=current_dir,
        cache=cache,
    )
    return store


def _source_paths(cache: Any, candidate: TaskCandidate) -> list[Path]:
    if cache is None:
        return []
    return [
        Path(path)
        for path in cache.paths_for_session_graph(str(candidate.root_session_id))
    ]
