"""Estimator provider interface, prompt assembly, and output validation."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Protocol

from coding_trajectory.ingestion.common import canonical_json

ESTIMATOR_PROMPT_VERSION = "ct.estimation.prompt.v1"
ESTIMATOR_SCHEMA_VERSION = "ct.estimation.schema.v1"

# Declared operational bounds for one canonical turn episode, in minutes.
MIN_FORECAST_MINUTES = 1 / 60
MAX_FORECAST_MINUTES = 7 * 24 * 60

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "p50_minutes": {"type": "number"},
        "p80_minutes": {"type": "number"},
    },
    "required": ["p50_minutes", "p80_minutes"],
    "additionalProperties": False,
}


class EstimateError(RuntimeError):
    """Base class for estimator failures recorded as terminal attempt states."""


class EstimateTransportError(EstimateError):
    """Retryable provider/transport failure (process exit, timeout, RPC error)."""


class EstimateSchemaError(EstimateError):
    """Permanent failure: the structured response violates the output schema."""


@dataclass(frozen=True, slots=True)
class EstimateResult:
    p50_minutes: float
    p80_minutes: float
    raw_text: str


class EstimatorProvider(Protocol):
    """Bounded semantic inference turn; CT owns everything around it."""

    provider_name: str

    def estimate(
        self,
        *,
        prompt: str,
        model: str | None,
        effort: str | None,
    ) -> EstimateResult: ...


def build_prompt(
    *,
    snapshot: dict[str, Any],
    examples: list[dict[str, Any]],
) -> str:
    """Assemble the exact normalized estimator prompt payload.

    The prompt is deterministic given the snapshot and the ordered retrieval
    examples so its content fingerprint is stable evidence.
    """

    example_lines = []
    for index, example in enumerate(examples, start=1):
        attributes = []
        if example.get("project_name"):
            attributes.append(f"project={example['project_name']}")
        if example.get("harness_name"):
            attributes.append(f"harness={example['harness_name']}")
        if example.get("model"):
            attributes.append(f"model={example['model']}")
        suffix = f" ({', '.join(attributes)})" if attributes else ""
        example_lines.append(
            f"{index}. actual={example['actual_minutes']} minutes{suffix}"
        )
    examples_block = "\n".join(example_lines) or "(no comparable past episodes)"

    return (
        "You are estimating the wall-clock duration of one coding-agent turn "
        "episode: one user request and the agent work it causes until the turn "
        "ends. Estimate calendar minutes, not compute time.\n"
        "Do not use tools. Do not inspect the filesystem. Base the estimate "
        "only on this message.\n\n"
        "=== TASK (pre-execution evidence) ===\n"
        f"{canonical_json(snapshot)}\n\n"
        "=== COMPARABLE PAST EPISODES (observed outcomes) ===\n"
        f"{examples_block}\n\n"
        "Respond with JSON only: p50_minutes is your median estimate, "
        "p80_minutes is an 80th-percentile upper estimate. Both must be "
        "positive numbers of minutes with p80_minutes >= p50_minutes."
    )


def prompt_fingerprint(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def validate_estimate(payload: Any) -> tuple[float, float]:
    """Validate the structured response against the declared schema and bounds."""

    if not isinstance(payload, dict):
        raise EstimateSchemaError("estimator response is not an object")
    extra = set(payload) - {"p50_minutes", "p80_minutes"}
    if extra:
        raise EstimateSchemaError(
            f"estimator response has extra fields: {sorted(extra)}"
        )
    values: list[float] = []
    for key in ("p50_minutes", "p80_minutes"):
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EstimateSchemaError(f"{key} must be a number")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise EstimateSchemaError(f"{key} must be finite")
        if not (MIN_FORECAST_MINUTES <= numeric <= MAX_FORECAST_MINUTES):
            raise EstimateSchemaError(
                f"{key}={numeric} outside operational bounds "
                f"[{MIN_FORECAST_MINUTES}, {MAX_FORECAST_MINUTES}] minutes"
            )
        values.append(numeric)
    p50, p80 = values
    if p80 < p50:
        raise EstimateSchemaError("p80_minutes must be >= p50_minutes")
    return p50, p80
