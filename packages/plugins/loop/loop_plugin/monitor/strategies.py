"""First-party Loop strategy catalog and deterministic evaluators.

Strategies read Core-owned native measurements through frozen Core methods and
own only threshold/breach interpretation. They never compute token formulas,
never read transcript content, and never alter canonical records.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from loop_plugin.monitor.models import (
    MEASURES,
    SEVERITIES,
    STRATEGY_ID,
    STRATEGY_VERSION,
    ConditionExplanation,
    EvaluatorSpec,
    TokenBudgetConfig,
)

# Frozen Core methods this strategy consumes, with minimum method versions.
REQUIRED_CORE_METHODS: dict[str, int] = {
    "project.list": 3,
    "project.sessions": 3,
    "session.usage": 3,
    "session.request_usage": 3,
    "living.sessions": 2,
}


class StrategyPermission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    detail: str


class StrategyManifest(BaseModel):
    """Versioned strategy definition shown to humans before configuration."""

    model_config = ConfigDict(extra="forbid")

    strategy_id: str
    version: str
    name: str
    description: str
    author: str
    evaluator: EvaluatorSpec
    scope_types: list[Literal["project", "session"]]
    trigger: dict[str, str]
    input_window: str
    measures: dict[str, str]
    severities: list[str]
    finding_policy: str
    permissions: list[StrategyPermission]
    required_core_methods: dict[str, int]
    config_schema: dict[str, str] = Field(default_factory=dict)


STRATEGY_MANIFEST = StrategyManifest(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    name="Turn token budget",
    description=(
        "Flags turns whose Core-owned native token measurement exceeds a "
        "configured budget. Token count is not currency cost: the threshold "
        "applies to the exact native measure selected in the configuration."
    ),
    author="loop",
    evaluator=EvaluatorSpec(),
    scope_types=["project", "session"],
    trigger={
        "historical_dry_run": "Bounded evaluation over existing local Core evidence.",
        "manual_refresh": (
            "Evaluation of newly observed sessions discovered through the local "
            "Core living.sessions change feed. Loop polls on human request; it "
            "does not claim a live push guarantee."
        ),
    },
    input_window="One canonical turn per evaluation.",
    measures=dict(MEASURES),
    severities=list(SEVERITIES),
    finding_policy=(
        "An evaluation is written for every eligible turn, including passes, "
        "unavailable evidence, and errors. Only configured breaches emit a "
        "finding when emit_findings is enabled."
    ),
    permissions=[
        StrategyPermission(
            id="core_metrics_read",
            label="Read local Core usage measurements",
            detail=(
                "Reads session.usage turn token accounting and the "
                "session.request_usage provider-request ledger on this host. "
                "No transcript, prompt, response, or tool content is read."
            ),
        ),
        StrategyPermission(
            id="core_inventory_read",
            label="Read local Core session inventory",
            detail=(
                "Reads project.sessions membership and the living.sessions "
                "change feed to learn which sessions need evaluation."
            ),
        ),
        StrategyPermission(
            id="content_read",
            label="Transcript content access",
            detail="Not requested. This strategy never reads message content.",
        ),
        StrategyPermission(
            id="external_egress",
            label="External data egress",
            detail="Not requested. All evidence stays on this host.",
        ),
        StrategyPermission(
            id="notifications",
            label="Notifications",
            detail="Not requested. Findings appear only in this product.",
        ),
        StrategyPermission(
            id="enforcement",
            label="Enforcement actions",
            detail="Not requested. Monitor observes and advises only.",
        ),
    ],
    required_core_methods=dict(REQUIRED_CORE_METHODS),
    config_schema={
        "measure": "One native per-turn token measure from session.usage.",
        "threshold_tokens": "Maximum allowed value; a turn breaches when observed > threshold.",
        "severity": "Severity assigned to emitted findings.",
        "emit_findings": "Whether breach evaluations emit findings.",
    },
)


def catalog() -> list[StrategyManifest]:
    """The first-party strategy catalog. Versioned manifests, not records."""
    return [STRATEGY_MANIFEST]


def fingerprint_turn_evidence(
    *, turn_usage: dict[str, Any] | None, request_count: int, turn_ended: bool
) -> str:
    """Hash the exact Core evidence slice an evaluation depends on.

    The fingerprint contains measured values and coverage facts only. A changed
    fingerprint means newer evidence supersedes the previous evaluation rather
    than rewriting it.
    """
    payload = {
        "usage": turn_usage,
        "request_count": request_count,
        "turn_ended": turn_ended,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def evaluate_turn(
    *,
    config: TokenBudgetConfig,
    turn_usage: dict[str, Any] | None,
    request_count: int,
    turn_ended: bool,
    measurement_coverage: dict[str, Any] | None,
) -> tuple[ConditionExplanation, str, str | None, str | None, dict[str, Any]]:
    """Evaluate one turn against the configured token budget.

    Returns ``(condition, state, result, reason, coverage)`` where state is one
    of completed/unavailable/pending and result is pass/breach/None. Missing or
    partial evidence is never treated as a zero measurement.
    """
    coverage: dict[str, Any] = {
        "usage_source": None,
        "request_ledger_observations": request_count,
        "turn_ended": turn_ended,
    }
    if measurement_coverage:
        coverage["measurement_coverage"] = measurement_coverage

    threshold = config.threshold_tokens
    measure = config.measure

    if (
        not isinstance(turn_usage, dict)
        or turn_usage.get("availability") == "unavailable"
    ):
        source = turn_usage.get("source") if isinstance(turn_usage, dict) else None
        coverage["usage_source"] = source
        reason = "Core reports usage evidence unavailable for this turn" + (
            f" (source: {source})." if source else "."
        )
        condition = ConditionExplanation(
            measure=measure,
            observed=None,
            threshold_tokens=threshold,
            outcome="unavailable",
            summary=f"{measure} unavailable — {reason}",
        )
        return condition, "unavailable", None, reason, coverage

    observed = turn_usage.get(measure)
    coverage["usage_source"] = "reported"
    if observed is None:
        reason = f"Core did not report the {measure} measure for this turn."
        condition = ConditionExplanation(
            measure=measure,
            observed=None,
            threshold_tokens=threshold,
            outcome="unavailable",
            summary=f"{measure} unavailable — {reason}",
        )
        return condition, "unavailable", None, reason, coverage

    if not turn_ended:
        reason = "Turn is still in progress; usage evidence is not final."
        condition = ConditionExplanation(
            measure=measure,
            observed=int(observed),
            threshold_tokens=threshold,
            outcome="pending",
            summary=(
                f"{measure} observed {int(observed):,} so far; evaluation is "
                "pending until the turn completes."
            ),
        )
        return condition, "pending", None, reason, coverage

    if request_count == 0 and not any(
        isinstance(value, (int, float)) and value > 0 for value in turn_usage.values()
    ):
        # Core reports an all-zero payload when no usage observations exist.
        # Zero is a placeholder here, not a measurement.
        reason = (
            "No provider usage observations are retained for this turn; "
            "the all-zero payload is not a measurement."
        )
        condition = ConditionExplanation(
            measure=measure,
            observed=None,
            threshold_tokens=threshold,
            outcome="unavailable",
            summary=f"{measure} unavailable — {reason}",
        )
        return condition, "unavailable", None, reason, coverage

    observed = int(observed)
    if observed <= threshold:
        condition = ConditionExplanation(
            measure=measure,
            observed=observed,
            threshold_tokens=threshold,
            outcome="pass",
            summary=f"{measure} {observed:,} ≤ {threshold:,} token budget.",
        )
        return condition, "completed", "pass", None, coverage

    condition = ConditionExplanation(
        measure=measure,
        observed=observed,
        threshold_tokens=threshold,
        outcome="breach",
        summary=(
            f"{measure} {observed:,} exceeds the {threshold:,} token budget "
            f"by {observed - threshold:,}."
        ),
    )
    return condition, "completed", "breach", None, coverage
