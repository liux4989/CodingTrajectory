"""Run a benchmark test case via `claude -p` (one-shot)."""
from __future__ import annotations
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from .models import AgentOutput, TestCase

_OUTPUT_FILE_INSTRUCTION = """
---
Write your complete final analysis/output to this file: {output_file}
Use the Write tool to save it. Do not just print it — the file is how your result is collected.
"""


def run_agent(test_case: TestCase, timeout: int = 600, interactive: bool = False) -> AgentOutput:
    """Invoke `claude -p` for a test case and return the captured AgentOutput.

    Default (non-interactive): uses `claude --print` with JSON output, fully captured.

    interactive=True: launches `claude` (no --print) as a visible terminal process.
    The prompt is piped via stdin; stdout/stderr go to the terminal so you can watch
    the agent work live. The agent is instructed to write its output to a temp file,
    which is read back after the session ends.
    """
    start = time.monotonic()

    if interactive:
        output_file = tempfile.mktemp(suffix=".txt", prefix="ct_bench_")
        prompt = (
            test_case.prompt
            + _OUTPUT_FILE_INSTRUCTION.format(output_file=output_file)
        )
        # No --print: full interactive REPL, visible in terminal.
        # Orchestrator blocks on subprocess.run() until the user quits the session.
        cmd = [
            "claude",
            "--dangerously-skip-permissions",
            prompt,  # positional arg keeps stdin as TTY → UI appears immediately
        ]
        subprocess.run(cmd)  # no input= → stdin inherits TTY
        duration = time.monotonic() - start

        output_path = Path(output_file)
        if not output_path.exists():
            raise RuntimeError(
                f"Agent did not write output file: {output_file} "
                f"(case_id={test_case.case_id})"
            )
        output_text = output_path.read_text(encoding="utf-8")
        os.unlink(output_path)
        return AgentOutput(
            case_id=test_case.case_id,
            task_type=test_case.task_type,
            tool_variant=test_case.tool_variant,
            output=output_text,
            duration_seconds=round(duration, 2),
        )

    # --- non-interactive (default) ---
    cmd = [
        "claude",
        "--print", test_case.prompt,
        "--output-format", "json",
        "--dangerously-skip-permissions",
    ]
    proc_obj = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=None, text=True)
    try:
        stdout_data, _ = proc_obj.communicate(timeout=timeout)
    except KeyboardInterrupt:
        proc_obj.terminate()
        try:
            proc_obj.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc_obj.kill()
        raise
    except subprocess.TimeoutExpired:
        proc_obj.kill()
        proc_obj.wait()
        raise

    class _ProcResult:
        returncode = proc_obj.returncode
        stdout = stdout_data
        stderr = ""

    proc = _ProcResult()
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
