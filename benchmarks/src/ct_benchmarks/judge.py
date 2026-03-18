"""LLM judge for automated evaluation of agent outputs."""
from __future__ import annotations
import json
from .models import AgentOutput, JudgeScore, TaskType, TestCase


# ---------------------------------------------------------------------------
# Base judge framing (shared across all task types)
# ---------------------------------------------------------------------------

_JUDGE_BASE = """\
You are an expert evaluator of coding agent log analysis.
Score the following analysis output on four dimensions (0-5 each).

{rubric}

Respond with JSON: {{"completeness": N, "accuracy": N, "structure": N, "insight": N, "reasoning": "..."}}
"""

# ---------------------------------------------------------------------------
# Task-specific rubric definitions
# ---------------------------------------------------------------------------

_TASK_RUBRICS: dict[TaskType, str] = {
    TaskType.SESSION_SUMMARY: """\
Evaluation criteria for a **session summary**:
- completeness: Does it cover user goal, agent approach, tool sequence, and final outcome?
- accuracy: Are the described events, tool calls, and results factually correct per the logs?
- structure: Is there a clear narrative arc (goal → approach → execution → outcome)?
- insight: Does it surface non-obvious patterns such as retries, pivots, or wasted effort?""",

    TaskType.SESSION_CONNECTION: """\
Evaluation criteria for a **session connection map**:
- completeness: Are all sessions identified with their parent/child, subagent, or handoff relationships?
- accuracy: Are session IDs, relationships, and role assignments correct per the logs?
- structure: Is the topology clearly presented (e.g. tree, diagram, or structured list)?
- insight: Does it explain *why* sessions were spawned and what role each plays in the overall task?""",

    TaskType.EFFORT_DISTRIBUTION: """\
Evaluation criteria for an **effort distribution analysis**:
- completeness: Are all four categories covered (planning, executing, bug fixing, interactive refinement)?
- accuracy: Are the counts, percentages, and category assignments correct per the logs?
- structure: Are results presented with clear counts/percentages and supporting evidence?
- insight: Does it identify imbalances, bottlenecks, or unusual distribution patterns?""",
}


def _get_judge_system_prompt(task_type: TaskType) -> str:
    """Return the judge system prompt with task-specific rubric."""
    rubric = _TASK_RUBRICS[task_type]
    return _JUDGE_BASE.format(rubric=rubric)


def build_judge_prompt(test_case: TestCase, output: AgentOutput) -> str:
    """Build the prompt to send to the LLM judge."""
    parts = [
        f"Task type: {test_case.task_type.value}",
        f"Tool variant: {test_case.tool_variant.value}",
        f"Task prompt:\n{test_case.prompt}",
        f"\nAgent output:\n{output.output}",
    ]
    if test_case.reference_answer:
        parts.append(f"\nReference answer:\n{test_case.reference_answer}")
    return "\n".join(parts)


def parse_judge_response(case_id: str, raw_response: str) -> JudgeScore:
    """Parse the LLM judge's JSON response into a JudgeScore."""
    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    data = json.loads(cleaned)
    return JudgeScore(
        case_id=case_id,
        completeness=data["completeness"],
        accuracy=data["accuracy"],
        structure=data["structure"],
        insight=data["insight"],
        reasoning=data.get("reasoning", ""),
    )
