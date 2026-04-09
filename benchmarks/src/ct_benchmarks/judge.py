"""LLM judge: prompt templates, JSON schemas, and response parsing."""
from __future__ import annotations
from .models import AgentOutput, CompactJudgeScore, DynamicJudgeScore, JudgeScore, ScoringCriterion, TaskType, TestCase


def _judge_schema(model: type) -> dict:
    """Derive a JSON schema from a score model, excluding fields the judge shouldn't fill."""
    schema = model.model_json_schema()
    for field in ("case_id",):
        schema.get("properties", {}).pop(field, None)
        if field in schema.get("required", []):
            schema["required"].remove(field)
    return schema


# ---------------------------------------------------------------------------
# Judge system prompts (one per task type) — used when no per-case criteria
# ---------------------------------------------------------------------------

_JUDGE_INTROS: dict[TaskType, str] = {
    TaskType.SESSION_COMPACT: (
        "You are an expert evaluator of compact continuation summaries produced from coding agent sessions."
    ),
    TaskType.ERROR_ROOT_CAUSE: "You are an expert evaluator of coding agent log analysis.",
    TaskType.SUBAGENT_DELEGATION: "You are an expert evaluator of coding agent log analysis.",
    TaskType.TOOL_FAILURE_REPORT: "You are an expert evaluator of coding agent log analysis.",
}

_JUDGE_PROMPTS: dict[TaskType, str] = {
    TaskType.SESSION_COMPACT: """\
You are an expert evaluator of compact continuation summaries produced from coding agent sessions.
Score the following summary on six dimensions (0-5 each).

Evaluation criteria:
- state_preservation (0-5): Does the next agent still know the actual task? \
The summary must carry forward the original objective so a fresh agent can understand what it is building/fixing.
- constraint_preservation (0-5): Does it retain the non-obvious requirements? \
Edge-case rules, user preferences, performance targets, and compatibility constraints that are easy to drop.
- decision_preservation (0-5): Does it keep prior commitments and architectural choices? \
Framework picks, API contracts, naming conventions, and rejected alternatives that should not be revisited.
- verification_fidelity (0-5): Does it distinguish "done" from "tested"? \
Clearly marks which steps are implemented-but-unverified vs. fully tested and passing.
- resume_quality (0-5): Can the next turn act immediately? \
The summary provides enough context (file paths, current branch, error state) to start coding without re-reading logs.
- compression_ratio (0-5): Is it much shorter than the original session? \
Rewards aggressive compression while penalising loss of essential information (balance with other criteria).""",

    TaskType.ERROR_ROOT_CAUSE: """\
You are an expert evaluator of coding agent log analysis.
Score the following error root cause analysis on four dimensions (0-5 each).

Evaluation criteria:
- completeness: Are all error-retry loops in the session identified? Is the location, retry count, and resolution reported for each?
- accuracy: Is the root cause diagnosis correct (not just restating the error message)? Are retry counts and resolutions factually accurate per the logs?
- structure: Is the analysis well-organized with clear per-error breakdowns and an impact summary?
- insight: Does it distinguish symptoms from root causes? Does it assess the agent's error recovery strategy quality?""",

    TaskType.SUBAGENT_DELEGATION: """\
You are an expert evaluator of coding agent log analysis.
Score the following subagent delegation analysis on four dimensions (0-5 each).

Evaluation criteria:
- completeness: Are all subagent delegations identified? Is each delegation's task, return value, and parent usage reported?
- accuracy: Are parent/child relationships, task assignments, and return values correct per the logs?
- structure: Is the delegation graph clearly presented with data flow direction?
- insight: Does it identify context loss across handoffs, delegation failures, or inefficient delegation patterns?""",

    TaskType.TOOL_FAILURE_REPORT: """\
You are an expert evaluator of coding agent log analysis.
Score the following tool failure report on four dimensions (0-5 each).

Evaluation criteria:
- completeness: Are all distinct tool failures captured? Are retries correctly grouped rather than listed as separate failures?
- accuracy: Are tool names, error messages, and recovery outcomes correct per the logs? Are lifecycle events correctly excluded?
- structure: Is each failure clearly reported with tool name, input summary, error, recovery, and classification?
- insight: Does it correctly classify failures (transient/permanent/fatal) and assess the agent's recovery strategy?""",
}


# ---------------------------------------------------------------------------
# JSON schemas (used with claude --json-schema) — TaskType defaults
# ---------------------------------------------------------------------------

JUDGE_SCHEMAS: dict[TaskType, dict] = {
    TaskType.SESSION_COMPACT: _judge_schema(CompactJudgeScore),
    TaskType.ERROR_ROOT_CAUSE: _judge_schema(JudgeScore),
    TaskType.SUBAGENT_DELEGATION: _judge_schema(JudgeScore),
    TaskType.TOOL_FAILURE_REPORT: _judge_schema(JudgeScore),
}


# ---------------------------------------------------------------------------
# Score model mapping — TaskType defaults
# ---------------------------------------------------------------------------

_SCORE_MODEL: dict[TaskType, type[JudgeScore | CompactJudgeScore]] = {
    TaskType.SESSION_COMPACT: CompactJudgeScore,
    TaskType.ERROR_ROOT_CAUSE: JudgeScore,
    TaskType.SUBAGENT_DELEGATION: JudgeScore,
    TaskType.TOOL_FAILURE_REPORT: JudgeScore,
}


# ---------------------------------------------------------------------------
# Per-case dynamic helpers
# ---------------------------------------------------------------------------

def _build_dynamic_prompt(task_type: TaskType, criteria: list[ScoringCriterion]) -> str:
    """Build a judge prompt from per-case scoring criteria."""
    intro = _JUDGE_INTROS[task_type]
    criterion_lines = "\n".join(
        f"- {c.name} (0-{c.max_score}): {c.description}"
        for c in criteria
    )
    return (
        f"{intro}\n"
        f"Score the following on {len(criteria)} dimension(s) (0-5 each).\n\n"
        f"Evaluation criteria:\n{criterion_lines}"
    )


def _build_dynamic_schema(criteria: list[ScoringCriterion]) -> dict:
    """Build a JSON schema from per-case scoring criteria."""
    properties: dict = {
        c.name: {"type": "number", "minimum": 0, "maximum": c.max_score}
        for c in criteria
    }
    properties["reasoning"] = {"type": "string"}
    return {
        "type": "object",
        "properties": properties,
        "required": [c.name for c in criteria],
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_judge_prompt(test_case: TestCase) -> str:
    """Return the judge system prompt for a test case.

    Uses per-case criteria when present; falls back to the TaskType default.
    """
    if test_case.scoring_criteria:
        return _build_dynamic_prompt(test_case.task_type, test_case.scoring_criteria)
    return _JUDGE_PROMPTS[test_case.task_type]


def get_judge_schema(test_case: TestCase) -> dict:
    """Return the JSON schema for a test case.

    Uses per-case criteria when present; falls back to the TaskType default.
    """
    if test_case.scoring_criteria:
        return _build_dynamic_schema(test_case.scoring_criteria)
    return JUDGE_SCHEMAS[test_case.task_type]


def build_judge_input(output: AgentOutput, reference_answer: str | None = None) -> str:
    """Return the agent output as judge input, optionally with a reference answer."""
    if reference_answer:
        return f"Reference answer:\n{reference_answer}\n\nAgent output:\n{output.output}"
    return output.output


def parse_judge_response(
    case_id: str,
    data: dict,
    test_case: TestCase,
) -> JudgeScore | CompactJudgeScore | DynamicJudgeScore:
    """Build a score model from the validated judge dict."""
    if test_case.scoring_criteria:
        scores = {c.name: data[c.name] for c in test_case.scoring_criteria}
        return DynamicJudgeScore(case_id=case_id, scores=scores, reasoning=data.get("reasoning", ""))
    model = _SCORE_MODEL[test_case.task_type]
    return model(case_id=case_id, **data)
