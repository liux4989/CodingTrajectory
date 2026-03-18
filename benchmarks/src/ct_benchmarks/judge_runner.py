"""Run the LLM judge via `claude -p` (one-shot, no tools needed)."""
from __future__ import annotations
import json
import subprocess

from .judge import _get_judge_system_prompt, build_judge_prompt, parse_judge_response
from .models import AgentOutput, JudgeScore, TestCase


def run_judge(test_case: TestCase, output: AgentOutput, timeout: int = 120) -> JudgeScore:
    """Invoke `claude -p` as an LLM judge and return a structured JudgeScore."""
    system_prompt = _get_judge_system_prompt(test_case.task_type)
    prompt = f"{system_prompt}\n\n{build_judge_prompt(test_case, output)}"

    cmd = [
        "claude",
        "--print", prompt,
        "--output-format", "json",
        "--allowedTools", "",  # no tools — pure text completion
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    if proc.returncode != 0:
        raise RuntimeError(f"judge subprocess failed: {proc.stderr}")

    data = json.loads(proc.stdout)
    raw = data.get("result", proc.stdout)
    return parse_judge_response(output.case_id, raw)
