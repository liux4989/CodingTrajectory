"""Codex app-server estimator provider adapter."""

from __future__ import annotations

import json
import os
from pathlib import Path

from coding_trajectory.app_server import CodexAppServerClient
from coding_trajectory.estimation.provider import (
    OUTPUT_SCHEMA,
    EstimateResult,
    EstimateSchemaError,
    EstimateTransportError,
    validate_estimate,
)


def default_estimator_workdir() -> Path:
    """Neutral service working directory; never the target checkout."""

    override = os.environ.get("CT_ESTIMATION_WORKDIR")
    directory = (
        Path(override).expanduser()
        if override
        else Path.home() / ".coding-trajectory" / "estimation"
    )
    directory.mkdir(parents=True, exist_ok=True)
    return directory


class CodexAppServerEstimator:
    """One stateless, ephemeral estimator turn per forecast.

    The thread runs in a neutral working directory under a read-only sandbox
    with approvals disabled. The app-server protocol used here has no switch
    to disable tools outright, so the prompt forbids tool use; the read-only
    sandbox plus the neutral cwd keep the estimator away from target checkout
    writes, and post-task repository state is not part of the prompt.
    """

    provider_name = "codex_app_server"

    def __init__(
        self,
        *,
        workdir: Path | None = None,
        timeout_seconds: float = 300,
    ) -> None:
        self.workdir = workdir or default_estimator_workdir()
        self.timeout_seconds = timeout_seconds

    def estimate(
        self,
        *,
        prompt: str,
        model: str | None,
        effort: str | None,
    ) -> EstimateResult:
        client = CodexAppServerClient(timeout_seconds=self.timeout_seconds)
        try:
            result = client.run_turn(
                cwd=self.workdir,
                user_text=prompt,
                output_schema=OUTPUT_SCHEMA,
                ephemeral=True,
                model=model,
                effort=effort,
                service_name="coding-trajectory-estimation",
            )
        except RuntimeError as exc:
            raise EstimateTransportError(str(exc)) from exc
        p50, p80 = validate_estimate(_parse_response_json(result.text))
        return EstimateResult(p50_minutes=p50, p80_minutes=p80, raw_text=result.text)


def _parse_response_json(text: str) -> object:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = [line for line in lines if not line.strip().startswith("```")]
        stripped = "\n".join(lines).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise EstimateSchemaError(
            f"estimator response is not valid JSON: {exc}"
        ) from exc
