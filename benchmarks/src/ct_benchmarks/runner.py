"""Benchmark runner: generate test cases, run agents, evaluate."""
from __future__ import annotations
import json
from pathlib import Path
from .models import (
    AgentOutput,
    ArenaComparison,
    BenchmarkRun,
    JudgeScore,
    TaskType,
    TestCase,
    ToolVariant,
)
from .prompts import build_prompt


def generate_test_cases(log_files: list[str], task_types: list[TaskType] | None = None) -> list[TestCase]:
    """Generate test cases: each log file × each task type × each tool variant."""
    if task_types is None:
        task_types = list(TaskType)

    cases: list[TestCase] = []
    for log_file in log_files:
        for task_type in task_types:
            for variant in ToolVariant:
                prompt = build_prompt(task_type, variant, log_file)
                cases.append(TestCase(
                    task_type=task_type,
                    tool_variant=variant,
                    log_file=log_file,
                    prompt=prompt,
                ))
    return cases


def save_run(run: BenchmarkRun, output_dir: str | Path) -> Path:
    """Save a benchmark run to JSON."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"run_{run.run_id}.json"
    path.write_text(
        run.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return path


def load_run(path: str | Path) -> BenchmarkRun:
    """Load a benchmark run from JSON."""
    return BenchmarkRun.model_validate_json(Path(path).read_text())


def summarize_run(run: BenchmarkRun) -> dict:
    """Produce a summary report of a benchmark run."""
    scores_by_variant: dict[str, list[float]] = {}
    for score in run.judge_scores:
        case = next((c for c in run.test_cases if c.case_id == score.case_id), None)
        if case is None:
            continue
        variant = case.tool_variant.value
        avg = (score.completeness + score.accuracy + score.structure + score.insight) / 4
        scores_by_variant.setdefault(variant, []).append(avg)

    arena_wins: dict[str, int] = {}
    for comp in run.arena_comparisons:
        if comp.winner == "a":
            v = comp.output_a.tool_variant.value
        elif comp.winner == "b":
            v = comp.output_b.tool_variant.value
        elif comp.winner == "tie":
            v = "tie"
        else:
            continue
        arena_wins[v] = arena_wins.get(v, 0) + 1

    return {
        "run_id": run.run_id,
        "total_cases": len(run.test_cases),
        "total_outputs": len(run.outputs),
        "judge_scores_by_variant": {
            k: round(sum(v) / len(v), 2) if v else 0
            for k, v in scores_by_variant.items()
        },
        "arena_wins": arena_wins,
    }
