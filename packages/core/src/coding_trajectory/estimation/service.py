"""Service handlers for the ``estimate.*`` methods."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from coding_trajectory.estimation.calibration import compute_calibration
from coding_trajectory.estimation.codex import CodexAppServerEstimator
from coding_trajectory.estimation.comparison import join_actual
from coding_trajectory.estimation.forecast import (
    append_terminal_comparison,
    estimator_config,
    predict_unbound,
    run_forecast_for_turn,
    source_paths,
)
from coding_trajectory.estimation.jobs import backfill_status, run_backfill
from coding_trajectory.estimation.ledger import ForecastLedger
from coding_trajectory.estimation.retrieval import RETRIEVAL_POLICY_VERSION
from coding_trajectory.estimation.store_resolution import full_store, store_for_turn
from coding_trajectory.estimation.task import (
    TaskCandidate,
    TaskExclusion,
    candidate_for_turn,
    project_target_config,
    task_fingerprint,
)
from coding_trajectory.ingestion.common import format_datetime, parse_iso_timestamp


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
    estimator = estimator_config(
        provider, params.get("estimator_model"), params.get("estimator_effort")
    )
    if params.get("turn_id"):
        # Retrieval needs the global point-in-time corpus, not just the
        # target graph's store; full discovery also resolves the candidate.
        store = full_store(params, current_dir=current_dir, cache=cache)
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

    return predict_unbound(
        params,
        ledger=ledger,
        provider=provider,
        estimator=estimator,
        current_dir=current_dir,
        cache=cache,
    )


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

        store = store_for_turn(
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
            source_paths=source_paths(cache, candidate),
        )
        append_terminal_comparison(ledger, record["prediction_id"], comparison)
        return {
            "forecast": ledger.find_forecast(record["prediction_id"]),
            "failure": None,
        }


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
    store = full_store(params, current_dir=current_dir, cache=cache)
    provider = CodexAppServerEstimator()
    estimator = estimator_config(
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
            store = full_store({}, current_dir=current_dir, cache=cache)
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
            paths = (
                source_paths(cache, candidate)
                if isinstance(candidate, TaskCandidate)
                else []
            )
            comparison = join_actual(
                store,
                turn_id=turn_id,
                source_paths=paths,
            )
            changed = (
                append_terminal_comparison(
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
