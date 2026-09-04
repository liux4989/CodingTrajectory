"""Benchmark data models."""
from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4
from pydantic import BaseModel, Field


class ToolVariant(str, Enum):
    """How the agent accesses coding log data.

    Two tiers of tool guidance, from most structured to most autonomous:
    - ct_cli:         5 purpose-built ct commands with return schemas and strategy hints
    - tool_assisted:  generic Read/Grep tools with parameter schemas and strategy hints
    """
    CT_CLI = "ct_cli"                # progressive disclosure via ct commands
    TOOL_ASSISTED = "tool_assisted"  # specific tool schemas (Read/Grep) with guidance


class TaskType(str, Enum):
    """What kind of analysis the agent performs."""
    SESSION_COMPACT = "session_compact"               # baseline: compact continuation summary
    ERROR_ROOT_CAUSE = "error_root_cause"             # progressive disclosure: locate & diagnose error loops
    SUBAGENT_DELEGATION = "subagent_delegation"       # contextual connection: trace delegations across sessions
    TOOL_FAILURE_REPORT = "tool_failure_report"       # noisy removal: extract tool failures from noisy logs


class ScoringCriterion(BaseModel):
    """A single scoring dimension for a test case."""
    name: str           # e.g. "accuracy"
    description: str    # e.g. "Is the answer factually correct?"
    max_score: int = 5


class TestCase(BaseModel):
    """A single benchmark test case."""
    case_id: str = Field(default_factory=lambda: uuid4().hex[:8])
    task_type: TaskType
    tool_variant: ToolVariant
    log_file: str                              # path to the coding agent log file
    prompt: str                                # the task prompt for the agent
    reference_answer: str | None = None        # optional ground truth for LLM judge
    scoring_criteria: list[ScoringCriterion] = Field(default_factory=list)  # per-case rubric; falls back to TaskType default when empty
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentOutput(BaseModel):
    """Result from a single agent run."""
    case_id: str
    task_type: TaskType
    tool_variant: ToolVariant
    output: str                                # the agent's analysis text
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)  # record of tools used
    token_usage: dict[str, int] = Field(default_factory=dict)
    duration_seconds: float = 0.0
    timestamp: datetime = Field(default_factory=datetime.now)


class JudgeScore(BaseModel):
    """LLM judge evaluation of an agent output (default rubric)."""
    case_id: str
    completeness: float = Field(ge=0, le=5)     # covers all key points
    accuracy: float = Field(ge=0, le=5)         # factually correct
    structure: float = Field(ge=0, le=5)        # well-organized
    insight: float = Field(ge=0, le=5)          # depth of analysis
    reasoning: str = ""                         # judge's reasoning

    def average(self) -> float:
        return (self.completeness + self.accuracy + self.structure + self.insight) / 4


class CompactJudgeScore(BaseModel):
    """LLM judge evaluation specialised for compact continuation summaries."""
    case_id: str
    state_preservation: float = Field(ge=0, le=5)       # does the next agent still know the actual task?
    constraint_preservation: float = Field(ge=0, le=5)  # does it retain non-obvious requirements?
    decision_preservation: float = Field(ge=0, le=5)    # does it keep prior commitments & architectural choices?
    verification_fidelity: float = Field(ge=0, le=5)    # does it distinguish "done" from "tested"?
    resume_quality: float = Field(ge=0, le=5)           # can the next turn act immediately?
    compression_ratio: float = Field(ge=0, le=5)        # is it much shorter than the original session?
    reasoning: str = ""                                  # judge's reasoning

    def average(self) -> float:
        return (
            self.state_preservation + self.constraint_preservation
            + self.decision_preservation + self.verification_fidelity
            + self.resume_quality + self.compression_ratio
        ) / 6


class DynamicJudgeScore(BaseModel):
    """LLM judge evaluation built from per-case scoring criteria."""
    case_id: str
    scores: dict[str, float]    # criterion name -> score
    reasoning: str = ""

    def average(self) -> float:
        if not self.scores:
            return 0.0
        return sum(self.scores.values()) / len(self.scores)


class ArenaComparison(BaseModel):
    """Human arena: side-by-side comparison of two variant outputs."""
    case_id: str
    task_type: TaskType
    output_a: AgentOutput                      # one variant
    output_b: AgentOutput                      # other variant
    winner: str | None = None                  # "a", "b", "tie", or None if not yet judged
    human_notes: str = ""


class BenchmarkRun(BaseModel):
    """A complete benchmark run."""
    run_id: str = Field(default_factory=lambda: uuid4().hex[:12])
    timestamp: datetime = Field(default_factory=datetime.now)
    test_cases: list[TestCase] = Field(default_factory=list)
    outputs: list[AgentOutput] = Field(default_factory=list)
    judge_scores: list[JudgeScore | CompactJudgeScore | DynamicJudgeScore] = Field(default_factory=list)
    arena_comparisons: list[ArenaComparison] = Field(default_factory=list)
