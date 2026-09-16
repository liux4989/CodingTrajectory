"""Loop Monitor product contracts (`ct.loop.v1`).

Monitor owns strategies, watches, evaluations, and findings. Every record is
reference-only: canonical session/turn identity, measured values, thresholds,
configuration, provenance, and lifecycle state. Transcript text, item bodies,
and event payloads are never persisted here — Core remains the evidence owner.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from loop_plugin.models import CanonicalReference

TRIGGERS = ("historical_dry_run", "manual_refresh")

# Native per-turn token measures owned by Core `session.usage` (glossary names).
# The strategy never computes these; it selects one and compares it with the
# configured threshold. Descriptions mirror docs/token-usage-glossary.md.
MEASURES: dict[str, str] = {
    "processed_tokens": "Unified processed-token total (prompt buckets + completion + reasoning).",
    "prompt_completion_tokens": "Prompt plus visible completion tokens.",
    "reported_total_tokens": "Provider/log-source total, preserved unchanged when reported.",
    "prompt_tokens": "Fresh prompt/input tokens for the turn.",
    "uncached_prompt_tokens": "Prompt tokens processed without a cache hit.",
    "cached_prompt_tokens": "Prompt tokens read from provider cache.",
    "cache_write_tokens": "Prompt tokens written into provider cache.",
    "completion_tokens": "Visible model output tokens.",
    "reasoning_tokens": "Model reasoning/thinking tokens when reported separately.",
}

SEVERITIES = ("info", "warning", "critical")

EVALUATOR_RULE = "ct.loop.turn_token_budget"
EVALUATOR_RULE_VERSION = "1.0.0"
STRATEGY_ID = "turn-token-budget"
STRATEGY_VERSION = "1.0.0"


class EvaluatorSpec(BaseModel):
    """Evaluator provenance recorded on every evaluation."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["deterministic"] = "deterministic"
    rule: str = EVALUATOR_RULE
    rule_version: str = EVALUATOR_RULE_VERSION


class TokenBudgetConfig(BaseModel):
    """Human-approved watch configuration for the turn token-budget strategy."""

    model_config = ConfigDict(extra="forbid")

    measure: Literal[
        "processed_tokens",
        "prompt_completion_tokens",
        "reported_total_tokens",
        "prompt_tokens",
        "uncached_prompt_tokens",
        "cached_prompt_tokens",
        "cache_write_tokens",
        "completion_tokens",
        "reasoning_tokens",
    ]
    threshold_tokens: int = Field(gt=0, le=100_000_000)
    severity: Literal["info", "warning", "critical"] = "warning"
    emit_findings: bool = True


class WatchScope(BaseModel):
    """Exactly one scope selector. Membership semantics stay Core-owned."""

    model_config = ConfigDict(extra="forbid")

    project_name: str | None = Field(default=None, min_length=1, max_length=256)
    session_id: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def _exactly_one(self) -> WatchScope:
        if bool(self.project_name) == bool(self.session_id):
            raise ValueError("scope needs exactly one of project_name or session_id")
        return self


class WatchRevision(BaseModel):
    """One effective configuration revision; historical results stay pinned."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)
    scope: WatchScope
    config: TokenBudgetConfig
    changed_at: datetime


class RefreshState(BaseModel):
    """Core `living.sessions` continuation position owned by one watch."""

    model_config = ConfigDict(extra="forbid")

    cursor: str | None = Field(default=None, max_length=4096)
    watermark: str | None = Field(default=None, max_length=4096)
    last_run_at: datetime | None = None
    caught_up: bool = False


class Watch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    watch_id: UUID
    strategy_id: str = STRATEGY_ID
    strategy_version: str = STRATEGY_VERSION
    name: str = Field(min_length=1, max_length=160)
    scope: WatchScope
    config: TokenBudgetConfig
    config_revision: int = Field(default=1, ge=1)
    enabled: bool = False
    created_at: datetime
    updated_at: datetime
    revisions: list[WatchRevision] = Field(default_factory=list, max_length=50)
    refresh: RefreshState = Field(default_factory=RefreshState)


class ConditionExplanation(BaseModel):
    """Explicit condition record: measure, observed value, threshold, outcome."""

    model_config = ConfigDict(extra="forbid")

    measure: str
    observed: int | None
    threshold_tokens: int
    comparator: Literal["<="] = "<="
    outcome: Literal["pass", "breach", "unavailable", "pending", "error"]
    summary: str = Field(min_length=1, max_length=512)


class Evaluation(BaseModel):
    """Every attempted strategy execution over one canonical turn."""

    model_config = ConfigDict(extra="forbid")

    evaluation_id: UUID
    idempotency_key: str = Field(min_length=16, max_length=128)
    watch_id: UUID
    strategy_id: str
    strategy_version: str
    config_revision: int = Field(ge=1)
    trigger: Literal["historical_dry_run", "manual_refresh"]
    reference: CanonicalReference
    state: Literal["completed", "unavailable", "pending", "errored"]
    result: Literal["pass", "breach"] | None
    condition: ConditionExplanation
    coverage: dict = Field(default_factory=dict)
    reason: str | None = Field(default=None, max_length=512)
    evaluator: EvaluatorSpec
    evidence_fingerprint: str = Field(min_length=16, max_length=128)
    superseded_by: UUID | None = None
    finding_id: UUID | None = None
    started_at: datetime
    completed_at: datetime


class FindingStatusEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["open", "acknowledged", "resolved", "dismissed"]
    at: datetime


class Finding(BaseModel):
    """Actionable work item emitted by a breach evaluation."""

    model_config = ConfigDict(extra="forbid")

    finding_id: UUID
    dedupe_key: str = Field(min_length=16, max_length=128)
    watch_id: UUID
    evaluation_id: UUID
    strategy_id: str
    strategy_version: str
    config_revision: int = Field(ge=1)
    severity: Literal["info", "warning", "critical"]
    status: Literal["open", "acknowledged", "resolved", "dismissed"] = "open"
    title: str = Field(min_length=1, max_length=256)
    condition: ConditionExplanation
    reference: CanonicalReference
    created_at: datetime
    updated_at: datetime
    status_history: list[FindingStatusEvent] = Field(
        default_factory=list, max_length=100
    )


class EvaluationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sessions_examined: int = Field(default=0, ge=0)
    turns_evaluated: int = Field(default=0, ge=0)
    passed: int = Field(default=0, ge=0)
    breached: int = Field(default=0, ge=0)
    unavailable: int = Field(default=0, ge=0)
    pending: int = Field(default=0, ge=0)
    errored: int = Field(default=0, ge=0)
    skipped_unchanged: int = Field(default=0, ge=0)


class DryRunResult(BaseModel):
    """Labeled historical preview. Never persists findings."""

    model_config = ConfigDict(extra="forbid")

    watch: Watch
    evaluations: list[Evaluation]
    summary: EvaluationSummary
    prospective_findings: list[Finding]
    scope_sessions_total: int = Field(ge=0)
    truncated: bool
    notes: list[str] = Field(default_factory=list)


class RefreshResult(BaseModel):
    """One manual evaluation pass over newly observed Core evidence."""

    model_config = ConfigDict(extra="forbid")

    watch: Watch
    evaluations: list[Evaluation]
    findings: list[Finding]
    summary: EvaluationSummary
    remaining: bool
    notes: list[str] = Field(default_factory=list)


# --- API request bodies -------------------------------------------------


class WatchCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: str = STRATEGY_ID
    name: str = Field(min_length=1, max_length=160)
    scope: WatchScope
    config: TokenBudgetConfig


class WatchUpdateRequest(BaseModel):
    """Any scope/config change creates a new effective configuration revision."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=160)
    scope: WatchScope | None = None
    config: TokenBudgetConfig | None = None
    enabled: bool | None = None


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_sessions: int = Field(default=8, ge=1, le=50)


class FindingStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["open", "acknowledged", "resolved", "dismissed"]
