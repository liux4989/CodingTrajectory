"""Run a benchmark test case via `claude -p` (one-shot)."""
from __future__ import annotations
import json
import subprocess
import time

from .models import AgentOutput, TestCase, ToolVariant


def run_agent(test_case: TestCase, timeout: int = 300) -> AgentOutput:
    """Invoke `claude -p` for a test case and return the captured AgentOutput."""
    # CT_CLI variant only needs Bash (to run ct commands).
    # READ_FILE variant needs Read (and Bash as fallback).
    if test_case.tool_variant == ToolVariant.CT_CLI:
        allowed_tools = "Bash"
    else:
        allowed_tools = "Bash,Read"

    cmd = [
        "claude",
        "--print", test_case.prompt,
        "--output-format", "json",
        "--allowedTools", allowed_tools,
    ]

    start = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    duration = time.monotonic() - start

    output_text = ""
    token_usage: dict[str, int] = {}

    if proc.returncode == 0 and proc.stdout:
        try:
            data = json.loads(proc.stdout)
            output_text = data.get("result", "")
            # cost_usd is a float; store as micro-dollars to keep int model field
            cost = data.get("cost_usd", 0.0)
            if cost:
                token_usage["cost_usd_micro"] = int(cost * 1_000_000)
            turns = data.get("num_turns")
            if turns is not None:
                token_usage["num_turns"] = turns
        except json.JSONDecodeError:
            output_text = proc.stdout
    else:
        output_text = proc.stderr or f"agent exited with code {proc.returncode}"

    return AgentOutput(
        case_id=test_case.case_id,
        task_type=test_case.task_type,
        tool_variant=test_case.tool_variant,
        output=output_text,
        token_usage=token_usage,
        duration_seconds=round(duration, 2),
    )
