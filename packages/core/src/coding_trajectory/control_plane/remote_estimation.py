"""Supabase-backed authority and worker for durable remote estimation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from coding_trajectory.control_plane.remote import (
    RemoteControlPlaneError,
    SupabaseHistoricalRepository,
    SupabaseRpcClient,
)
from coding_trajectory.estimation.calibration import compute_calibration
from coding_trajectory.estimation.comparison import join_actual
from coding_trajectory.estimation.forecast import idempotency_key
from coding_trajectory.estimation.provider import (
    ESTIMATOR_PROMPT_VERSION,
    ESTIMATOR_SCHEMA_VERSION,
    EstimateError,
    EstimateResult,
    EstimateTransportError,
    build_prompt,
    prompt_fingerprint,
    validate_estimate,
)
from coding_trajectory.estimation.retrieval import (
    RETRIEVAL_POLICY_VERSION,
    select_examples,
)
from coding_trajectory.estimation.task import (
    TASK_SNAPSHOT_VERSION,
    TargetConfig,
    TaskExclusion,
    assign_forecast_kind,
    build_task_snapshot,
    candidate_for_turn,
    project_target_config,
    snapshot_fingerprint,
    task_fingerprint,
    turn_episode_exclusion,
)
from coding_trajectory.ingestion.common import format_datetime, parse_iso_timestamp
from coding_trajectory.query import DocumentStore


class EstimationClaim(BaseModel):
    """Credential-free estimator work leased from the remote authority."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    job_id: UUID
    attempt_number: int = Field(gt=0)
    worker_id: str
    prompt: str
    model: str | None = None
    effort: str | None = None


class RemoteEstimatorExecutor(Protocol):
    """The only provider-specific operation required by the central worker."""

    def estimate(
        self, *, prompt: str, model: str | None, effort: str | None
    ) -> EstimateResult: ...


class RemoteEstimationRepository:
    """Typed persistence boundary over the estimation RPC contract."""

    def __init__(self, *, client: SupabaseRpcClient, workspace_id: UUID) -> None:
        self._client = client
        self.workspace_id = workspace_id

    def predict(
        self, *, snapshot_sequence: int, plan: dict[str, Any]
    ) -> dict[str, Any]:
        return self._call(
            "ct_estimate_predict",
            {"snapshot_sequence": snapshot_sequence, "plan": plan},
        )

    def bind(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._call("ct_estimate_bind", request)

    def compare(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._call("ct_estimate_compare", request)

    def get(self, prediction_id: str, *, snapshot_sequence: int) -> dict[str, Any]:
        return self._call(
            "ct_estimate_get",
            {
                "prediction_id": prediction_id,
                "snapshot_sequence": snapshot_sequence,
            },
        )

    def list(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._call("ct_estimate_list", request)

    def calibration_records(self, request: dict[str, Any]) -> list[dict[str, Any]]:
        response = self._call("ct_estimate_calibration", request)
        records = response.get("records")
        if not isinstance(records, list):
            raise RemoteControlPlaneError("calibration RPC returned no record list")
        return records

    def start_backfill(
        self,
        *,
        snapshot_sequence: int,
        spec: dict[str, Any],
        plans: list[dict[str, Any]],
        job_id: str | None,
        excluded: dict[str, int],
    ) -> dict[str, Any]:
        return self._call(
            "ct_estimate_backfill_start",
            {
                "snapshot_sequence": snapshot_sequence,
                "spec": spec,
                "plans": plans,
                "job_id": job_id,
                "excluded": excluded,
            },
        )

    def backfill_status(self, job_id: str) -> dict[str, Any]:
        return self._call("ct_estimate_backfill_status", {"job_id": job_id})

    def _call(self, name: str, request: dict[str, Any]) -> dict[str, Any]:
        return self._client.call(
            name, {"workspace_id": str(self.workspace_id), **request}
        )


class RemoteEstimationAuthority:
    """Snapshot-aware application handler for every ``estimate.*`` method.

    Planning and calibration deliberately reuse the embedded estimator's pure
    functions. Only provider execution is deferred to a service-role worker.
    A newly queued prediction returns the contract-shaped ``forecast_pending``
    retryable failure; repeating the same call returns the durable forecast as
    soon as its worker completes it.
    """

    def __init__(
        self,
        *,
        client: SupabaseRpcClient,
        workspace_id: UUID,
        snapshot_sequence: int | None = None,
        estimator_provider: str = "codex-app-server",
    ) -> None:
        self._client = client
        self._repository = RemoteEstimationRepository(
            client=client, workspace_id=workspace_id
        )
        self.workspace_id = workspace_id
        self.snapshot_sequence = snapshot_sequence
        self.estimator_provider = estimator_provider

    def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "estimate.predict":
            return self._predict(params)
        if method == "estimate.bind":
            return self._bind(params)
        if method == "estimate.get":
            return self._get(params)
        if method == "estimate.list":
            return self._list(params)
        if method == "estimate.calibration":
            return self._calibration(params)
        if method == "estimate.backfill.start":
            return self._backfill_start(params)
        if method == "estimate.backfill.status":
            return self._repository.backfill_status(params["job_id"])
        raise KeyError(f"no remote estimate handler registered for {method}")

    def _snapshot(self) -> tuple[DocumentStore, int]:
        repository = SupabaseHistoricalRepository(
            client=self._client,
            workspace_id=self.workspace_id,
            snapshot_sequence=self.snapshot_sequence,
        )
        store, _ = repository.store_for("estimate", {})
        sequence = repository.snapshot_sequence
        if sequence is None:
            raise RemoteControlPlaneError("historical snapshot has no sequence")
        return store, sequence

    def _predict(self, params: dict[str, Any]) -> dict[str, Any]:
        store, sequence = self._snapshot()
        outcome = self._plan(store, params)
        if "failure" in outcome:
            return {
                "forecast": None,
                "failure": outcome["failure"],
                "reused_existing": False,
            }
        return self._repository.predict(
            snapshot_sequence=sequence, plan=outcome["plan"]
        )

    def _bind(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            UUID(params["prediction_id"])
            turn_id = UUID(params["turn_id"])
        except ValueError:
            return {
                "forecast": None,
                "failure": _not_applicable(
                    "forecast_not_found", params["prediction_id"]
                ),
            }
        _, sequence = self._snapshot()
        current = self._repository.get(
            params["prediction_id"], snapshot_sequence=sequence
        ).get("forecast")
        if current is None:
            return {
                "forecast": None,
                "failure": _not_applicable(
                    "forecast_not_found", params["prediction_id"]
                ),
            }
        if current.get("forecast_kind") != "prospective_unbound":
            return {
                "forecast": current,
                "failure": _not_applicable("not_unbound", current.get("forecast_kind")),
            }
        if current.get("bound_at"):
            return {
                "forecast": current,
                "failure": _not_applicable("already_bound", current.get("turn_id")),
            }

        store, sequence = self._snapshot()
        candidate = candidate_for_turn(store, turn_id)
        if isinstance(candidate, TaskExclusion):
            return {
                "forecast": current,
                "failure": _not_applicable(candidate.reason, candidate.detail),
            }
        issued_at = parse_iso_timestamp(current.get("issued_at"))
        if issued_at is None or issued_at >= candidate.target_execution_started_at:
            return {
                "forecast": current,
                "failure": {
                    "state": "permanent_failed",
                    "reason": "binding_window_missed"
                    if issued_at
                    else "missing_issued_at",
                    "detail": params["prediction_id"],
                },
            }
        if task_fingerprint(candidate.request_text) != current.get("task_fingerprint"):
            return {
                "forecast": current,
                "failure": _permanent("task_fingerprint_mismatch", params["turn_id"]),
            }
        observed = project_target_config(candidate).as_dict()
        for key, expected in (current.get("target") or {}).items():
            if expected is not None and observed.get(key) != expected:
                return {
                    "forecast": current,
                    "failure": _permanent(
                        "target_config_mismatch",
                        f"{key}: declared {expected!r} != observed {observed.get(key)!r}",
                    ),
                }

        comparison = join_actual(store, turn_id=candidate.turn.turn_id, source_paths=[])
        request = {
            "prediction_id": params["prediction_id"],
            "snapshot_sequence": sequence,
            "binding": {
                "bound_at": format_datetime(datetime.now(UTC)),
                "turn_id": str(candidate.turn.turn_id),
                "root_session_id": str(candidate.root_session_id),
                "session_id": str(candidate.session.session_id),
                "task_available_at": format_datetime(candidate.task_available_at),
                "target_execution_started_at": format_datetime(
                    candidate.target_execution_started_at
                ),
            },
            "comparison": comparison
            if comparison.get("exclusion") != "missing_terminal_time"
            else None,
        }
        return self._repository.bind(request)

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            UUID(params["prediction_id"])
        except ValueError:
            return {"forecast": None}
        _, sequence = self._snapshot()
        response = self._repository.get(
            params["prediction_id"], snapshot_sequence=sequence
        )
        forecast = response.get("forecast")
        return {"forecast": self._refresh_one(forecast) if forecast else None}

    def _list(self, params: dict[str, Any]) -> dict[str, Any]:
        _, sequence = self._snapshot()
        response = self._repository.list({**params, "snapshot_sequence": sequence})
        return {
            "items": [self._refresh_one(item) for item in response.get("items", [])]
        }

    def _calibration(self, params: dict[str, Any]) -> dict[str, Any]:
        _, sequence = self._snapshot()
        records = [
            self._refresh_one(item)
            for item in self._repository.calibration_records(
                {**params, "snapshot_sequence": sequence}
            )
        ]
        filters = {
            key: value
            for key, value in params.items()
            if key != "include_buckets" and value is not None
        }
        return compute_calibration(
            records,
            filters=filters,
            include_buckets=bool(params.get("include_buckets", True)),
        )

    def _refresh_one(self, forecast: dict[str, Any]) -> dict[str, Any]:
        if not forecast.get("turn_id") or forecast.get("status") == "compared":
            return forecast
        try:
            store, sequence = self._snapshot()
            comparison = join_actual(
                store, turn_id=UUID(forecast["turn_id"]), source_paths=[]
            )
            if comparison.get("exclusion") == "missing_terminal_time":
                return forecast
            response = self._repository.compare(
                {
                    "prediction_id": forecast["prediction_id"],
                    "snapshot_sequence": sequence,
                    "comparison": comparison,
                }
            )
            return response.get("forecast") or forecast
        except (ValueError, RemoteControlPlaneError):
            return forecast

    def _backfill_start(self, params: dict[str, Any]) -> dict[str, Any]:
        store, sequence = self._snapshot()
        estimator = self._estimator(params)
        spec = {
            "project_name": params.get("project_name"),
            "since_days": params.get("since_days"),
            "agent_vendor": params.get("agent_vendor"),
            "max_forecasts": int(params.get("max_forecasts") or 25),
            "max_examples": int(params.get("max_examples", 8)),
            "concurrency": int(params.get("concurrency") or 4),
            "estimator": estimator,
            "retrieval_policy_version": RETRIEVAL_POLICY_VERSION,
        }
        plans: list[dict[str, Any]] = []
        excluded: dict[str, int] = {}
        cutoff = (
            datetime.now(UTC) - timedelta(days=params["since_days"])
            if params.get("since_days")
            else None
        )
        turns = sorted(
            store.turns.values(), key=lambda item: (item.started_at, str(item.turn_id))
        )
        for turn in turns:
            candidate = candidate_for_turn(store, turn.turn_id)
            reason: str | None = None
            if isinstance(candidate, TaskExclusion):
                reason = candidate.reason
            elif (
                (cutoff and candidate.task_available_at < cutoff)
                or (
                    params.get("project_name")
                    and candidate.project_name != params["project_name"]
                )
                or (
                    params.get("agent_vendor")
                    and project_target_config(candidate).agent_vendor
                    != params["agent_vendor"]
                )
            ):
                continue
            else:
                episode_exclusion = turn_episode_exclusion(candidate)
                reason = episode_exclusion.reason if episode_exclusion else None
            if reason:
                excluded[reason] = excluded.get(reason, 0) + 1
                continue
            outcome = self._plan(
                store, {**params, "turn_id": str(turn.turn_id), "task_text": None}
            )
            if "plan" in outcome:
                plans.append(outcome["plan"])
            if len(plans) >= spec["max_forecasts"]:
                break
        return self._repository.start_backfill(
            snapshot_sequence=sequence,
            spec=spec,
            plans=plans,
            job_id=params.get("job_id"),
            excluded=excluded,
        )

    def _plan(self, store: DocumentStore, params: dict[str, Any]) -> dict[str, Any]:
        issued_at = datetime.now(UTC)
        estimator = self._estimator(params)
        turn_id: UUID | None = None
        if params.get("turn_id"):
            turn_id = UUID(params["turn_id"])
            candidate = candidate_for_turn(store, turn_id)
            if isinstance(candidate, TaskExclusion):
                return {"failure": _not_applicable(candidate.reason, candidate.detail)}
            target = project_target_config(candidate)
            snapshot = build_task_snapshot(candidate, target=target)
            fingerprint = task_fingerprint(candidate.request_text)
            kind = assign_forecast_kind(
                turn_bound=True,
                task_available_at=candidate.task_available_at,
                target_execution_started_at=candidate.target_execution_started_at,
                issued_at=issued_at,
            )
            data_cutoff_at = (
                min(issued_at, candidate.task_available_at)
                if kind == "historical_backcast"
                else issued_at
            )
            excluded_sessions = candidate.graph_session_ids
            project_name = candidate.project_name
            identity = {
                "turn_id": str(turn_id),
                "root_session_id": str(candidate.root_session_id),
                "session_id": str(candidate.session.session_id),
                "task_available_at": format_datetime(candidate.task_available_at),
                "target_execution_started_at": format_datetime(
                    candidate.target_execution_started_at
                ),
                "session_title": snapshot.get("session_title"),
            }
            comparison = join_actual(store, turn_id=turn_id, source_paths=[])
        else:
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
                "request": {
                    "type": "message",
                    "source": "human_user",
                    "text": task_text,
                },
                "project_name": params.get("project_name"),
                "session_title": None,
                "task_class": None,
                "prior_turns": [],
                "target": target.as_dict(),
            }
            fingerprint = task_fingerprint(task_text)
            kind = "prospective_unbound"
            data_cutoff_at = issued_at
            excluded_sessions = frozenset()
            project_name = params.get("project_name")
            identity = {
                "turn_id": None,
                "root_session_id": None,
                "session_id": None,
                "task_available_at": None,
                "target_execution_started_at": None,
                "session_title": None,
            }
            comparison = None

        retrieval = select_examples(
            store,
            data_cutoff_at=data_cutoff_at,
            exclude_turn_id=turn_id,
            exclude_session_ids=excluded_sessions,
            exclude_task_fingerprint=fingerprint,
            target_project_name=project_name,
            target=target,
            k=int(params.get("max_examples", 8)),
        )
        retrieval["data_cutoff_at"] = format_datetime(retrieval["data_cutoff_at"])
        prompt = build_prompt(snapshot=snapshot, examples=retrieval["examples"])
        key = idempotency_key(
            turn_id=str(turn_id) if turn_id else None,
            task_fingerprint=fingerprint,
            snapshot_fingerprint=snapshot_fingerprint(snapshot),
            estimator=estimator,
            retrieval_policy_version=RETRIEVAL_POLICY_VERSION,
        )
        issued_iso = format_datetime(issued_at)
        record = {
            "prediction_id": uuid4().hex,
            "idempotency_key": key,
            "forecast_kind": kind,
            **identity,
            "task_fingerprint": fingerprint,
            "issued_at": issued_iso,
            "data_cutoff_at": format_datetime(data_cutoff_at),
            "bound_at": None,
            "project_name": project_name,
            "task_class": None,
            "task_snapshot": snapshot,
            "target": target.as_dict(),
            "estimator": estimator,
            "prompt_fingerprint": prompt_fingerprint(prompt),
            "retrieval": retrieval,
            "p50_minutes": None,
            "p80_minutes": None,
            "comparison": None,
            "created_at": issued_iso,
        }
        return {
            "plan": {
                "idempotency_key": key,
                "record": record,
                "prompt": prompt,
                "comparison": comparison
                if comparison and comparison.get("exclusion") != "missing_terminal_time"
                else None,
            }
        }

    def _estimator(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "provider": self.estimator_provider,
            "model": params.get("estimator_model"),
            "effort": params.get("estimator_effort"),
            "prompt_version": ESTIMATOR_PROMPT_VERSION,
            "schema_version": ESTIMATOR_SCHEMA_VERSION,
        }


class RemoteEstimationWorker:
    """Lease, execute, and atomically finish one estimator job at a time."""

    def __init__(
        self,
        *,
        client: SupabaseRpcClient,
        worker_id: str,
        executor: RemoteEstimatorExecutor,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be empty")
        self._client = client
        self.worker_id = worker_id
        self.executor = executor

    def run_once(self, *, lease_seconds: int = 300) -> bool:
        raw = self._client.call(
            "ct_estimator_claim",
            {"worker_id": self.worker_id, "lease_seconds": lease_seconds},
        )
        if not raw:
            return False
        claim = EstimationClaim.model_validate(raw)
        try:
            result = self.executor.estimate(
                prompt=claim.prompt, model=claim.model, effort=claim.effort
            )
            p50, p80 = validate_estimate(
                {"p50_minutes": result.p50_minutes, "p80_minutes": result.p80_minutes}
            )
            self._client.call(
                "ct_estimator_complete",
                {
                    "workspace_id": str(claim.workspace_id),
                    "job_id": str(claim.job_id),
                    "attempt_number": claim.attempt_number,
                    "worker_id": self.worker_id,
                    "p50_minutes": p50,
                    "p80_minutes": p80,
                },
            )
        except Exception as exc:
            permanent = isinstance(
                exc, (EstimateError, ValueError, TypeError)
            ) and not isinstance(exc, EstimateTransportError)
            self._client.call(
                "ct_estimator_fail",
                {
                    "workspace_id": str(claim.workspace_id),
                    "job_id": str(claim.job_id),
                    "attempt_number": claim.attempt_number,
                    "worker_id": self.worker_id,
                    "error": str(exc)[:1000],
                    "permanent": permanent,
                    "retry_seconds": min(3600, 2 ** min(10, claim.attempt_number)),
                },
            )
            raise
        return True


def _not_applicable(reason: str, detail: Any) -> dict[str, Any]:
    return {
        "state": "not_applicable",
        "reason": reason,
        "detail": None if detail is None else str(detail),
    }


def _permanent(reason: str, detail: Any) -> dict[str, Any]:
    return {
        "state": "permanent_failed",
        "reason": reason,
        "detail": None if detail is None else str(detail),
    }
