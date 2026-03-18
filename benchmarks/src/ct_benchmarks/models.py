"""Benchmark data models."""
from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4
from pydantic import BaseModel, Field


class ToolVariant(str, Enum):
    """How the agent accesses coding log data.

    Three tiers of tool guidance, from most structured to most autonomous:
    - ct_cli:         5 purpose-built ct commands with return schemas and strategy hints
    - tool_assisted:  generic Read/Grep tools with parameter schemas and strategy hints
    - read_file:      just the file path — agent decides its own tools and approach
    """
    CT_CLI = "ct_cli"                # progressive disclosure via ct commands
    TOOL_ASSISTED = "tool_assisted"  # specific tool schemas (Read/Grep) with guidance
    READ_FILE = "read_file"          # raw file path only, agent reasons freely


class TaskType(str, Enum):
    """What kind of analysis the agent performs."""
    SESSION_SUMMARY = "session_summary"             # general purpose summary
    SESSION_CONNECTION = "session_connection"         # session map & task status
    EFFORT_DISTRIBUTION = "effort_distribution"       # planning/executing/bugfix/refinement


class TestCase(BaseModel):
    """A single benchmark test case."""
    case_id: str = Field(default_factory=lambda: uuid4().hex[:8])
    task_type: TaskType
    tool_variant: ToolVariant
    log_file: str                              # path to the coding agent log file
    prompt: str                                # the task prompt for the agent
    reference_answer: str | None = None        # optional ground truth for LLM judge
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
    """LLM judge evaluation of an agent output."""
    case_id: str
    completeness: float = Field(ge=0, le=5)     # covers all key points
    accuracy: float = Field(ge=0, le=5)         # factually correct
    structure: float = Field(ge=0, le=5)        # well-organized
    insight: float = Field(ge=0, le=5)          # depth of analysis
    reasoning: str = ""                         # judge's reasoning


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
    judge_scores: list[JudgeScore] = Field(default_factory=list)
    arena_comparisons: list[ArenaComparison] = Field(default_factory=list)
