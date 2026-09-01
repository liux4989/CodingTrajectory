"""Contracts for the estimate.* agent temporality forecasts and calibration."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from coding_trajectory.contracts.base import ContractModel, RequestModel

ForecastKind = Literal[
    "prospective",
    "prospective_unbound",
    "historical_backcast",
    "runtime_advisory",
]


class EstimatePredictRequest(RequestModel):
    """One forecast from a historical ``turn_id`` or pre-execution task text."""

    turn_id: str | None = None
    task_text: str | None = None
    project_name: str | None = None
    target_agent_vendor: str | None = None
    target_harness_name: str | None = None
    target_harness_version: str | None = None
    target_model: str | None = None
    target_effort: str | None = None
    target_execution_policy_fingerprint: str | None = None
    estimator_model: str | None = None
    estimator_effort: str | None = None
    max_examples: int = Field(default=8, ge=0, le=32)

    @model_validator(mode="after")
    def require_task_source(self) -> EstimatePredictRequest:
        if bool(self.turn_id) == bool(self.task_text):
            raise ValueError("exactly one of turn_id or task_text is required")
        return self


class EstimateBindRequest(RequestModel):
    prediction_id: str
    turn_id: str


class EstimateGetRequest(RequestModel):
    prediction_id: str


class EstimateListRequest(RequestModel):
    forecast_kind: ForecastKind | None = None
    project_name: str | None = None
    target_harness_name: str | None = None
    status: Literal["unbound", "uncompared", "compared"] | None = None
    limit: int = Field(default=50, ge=1, le=200)


class EstimateCalibrationRequest(RequestModel):
    forecast_kind: ForecastKind | None = None
    project_name: str | None = None
    target_harness_name: str | None = None
    target_model: str | None = None
    estimator_model: str | None = None
    prompt_version: str | None = None
    retrieval_policy_version: str | None = None
    include_buckets: bool = True


class EstimateBackfillStartRequest(RequestModel):
    project_name: str | None = None
    since_days: int | None = Field(default=None, ge=1)
    agent_vendor: str | None = None
    max_forecasts: int = Field(default=25, ge=1, le=1000)
    max_examples: int = Field(default=8, ge=0, le=32)
    concurrency: int = Field(default=4, ge=1, le=8)
    estimator_model: str | None = None
    estimator_effort: str | None = None
    job_id: str | None = None


class EstimateBackfillStatusRequest(RequestModel):
    job_id: str


class EstimateTargetConfig(ContractModel):
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


class EstimateRetrievalExample(ContractModel):
    turn_id: str
    root_session_id: str
    actual_minutes: float
    project_name: str | None = None
    harness_name: str | None = None
    model: str | None = None


class EstimateRetrievalSnapshot(ContractModel):
    policy_version: str
    corpus_fingerprint: str
    data_cutoff_at: datetime
    examples: list[EstimateRetrievalExample] = Field(default_factory=list)


class EstimateEstimatorConfig(ContractModel):
    provider: str
    model: str | None = None
    effort: str | None = None
    prompt_version: str
    schema_version: str


class EstimateComparison(ContractModel):
    compared_at: datetime
    actual_execution_seconds: int | None = None
    duration_bucket: str | None = None
    outcome: str = "unknown"
    exclusion: str | None = None
    source_fingerprint: str | None = None


class EstimateForecastRecord(ContractModel):
    prediction_id: str
    idempotency_key: str
    forecast_kind: ForecastKind
    role: Literal["primary", "diagnostic"]
    status: Literal["unbound", "uncompared", "compared"]
    turn_id: str | None = None
    root_session_id: str | None = None
    session_id: str | None = None
    task_fingerprint: str
    task_available_at: datetime | None = None
    target_execution_started_at: datetime | None = None
    issued_at: datetime
    data_cutoff_at: datetime
    bound_at: datetime | None = None
    project_name: str | None = None
    task_class: str | None = None
    session_title: str | None = None
    task_snapshot: dict[str, Any] = Field(default_factory=dict)
    target: EstimateTargetConfig = Field(default_factory=EstimateTargetConfig)
    estimator: EstimateEstimatorConfig
    prompt_fingerprint: str | None = None
    retrieval: EstimateRetrievalSnapshot | None = None
    p50_minutes: float | None = None
    p80_minutes: float | None = None
    comparison: EstimateComparison | None = None
    created_at: datetime


class EstimateFailure(ContractModel):
    state: Literal["retryable_failed", "permanent_failed", "not_applicable"]
    reason: str
    detail: str | None = None


class EstimatePredictResponse(ContractModel):
    forecast: EstimateForecastRecord | None = None
    failure: EstimateFailure | None = None
    reused_existing: bool = False


class EstimateBindResponse(ContractModel):
    forecast: EstimateForecastRecord | None = None
    failure: EstimateFailure | None = None


class EstimateGetResponse(ContractModel):
    forecast: EstimateForecastRecord | None = None


class EstimateListResponse(ContractModel):
    items: list[EstimateForecastRecord] = Field(default_factory=list)


class EstimateCalibrationCohort(ContractModel):
    cohort: dict[str, Any] = Field(default_factory=dict)
    eligible_count: int = 0
    primary_count: int = 0
    exclusions: dict[str, int] = Field(default_factory=dict)
    statistics: dict[str, Any] = Field(default_factory=dict)
    buckets: list[dict[str, Any]] = Field(default_factory=list)


class EstimateCalibrationResponse(ContractModel):
    policy: dict[str, Any] = Field(default_factory=dict)
    cohorts: list[EstimateCalibrationCohort] = Field(default_factory=list)


class EstimateBackfillJob(ContractModel):
    job_id: str
    status: Literal["running", "completed", "stopped", "failed"]
    created_at: datetime
    finished_at: datetime | None = None
    spec: dict[str, Any] = Field(default_factory=dict)
    counts: dict[str, Any] = Field(default_factory=dict)
    stop_reason: str | None = None


class EstimateBackfillStartResponse(ContractModel):
    job: EstimateBackfillJob


class EstimateBackfillStatusResponse(ContractModel):
    job: EstimateBackfillJob
