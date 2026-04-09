"""Run the LLM judge via `claude -p` (one-shot, no tools needed)."""
from __future__ import annotations
import json
import subprocess

from .judge import get_judge_prompt, get_judge_schema, build_judge_input, parse_judge_response
from .models import AgentOutput, CompactJudgeScore, DynamicJudgeScore, JudgeScore, TestCase


def run_judge(test_case: TestCase, output: AgentOutput, timeout: int = 120) -> JudgeScore | CompactJudgeScore | DynamicJudgeScore:
    """Invoke `claude -p` as an LLM judge and return a structured score."""
    prompt = f"{get_judge_prompt(test_case)}\n\n{build_judge_input(output, test_case.reference_answer)}"
    schema = get_judge_schema(test_case)

    cmd = [
        "claude",
        "--print", prompt,
        "--output-format", "json",
        "--json-schema", json.dumps(schema),
        "--allowedTools", "",
    ]

    proc_obj = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout_data, stderr_data = proc_obj.communicate(timeout=timeout)
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

    if proc_obj.returncode != 0:
        raise RuntimeError(f"judge subprocess failed: {stderr_data}")

    if not stdout_data or not stdout_data.strip():
        raise RuntimeError("judge subprocess returned empty output")

    # With --json-schema, claude puts validated output in "structured_output"
    outer = json.loads(stdout_data)
    result = outer.get("structured_output")
    if result is None:
        raise RuntimeError(f"judge response missing structured_output: {stdout_data[:200]}")

    return parse_judge_response(output.case_id, result, test_case=test_case)
