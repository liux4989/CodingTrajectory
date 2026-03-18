"""FSM orchestrator: log file → agent runs → judge → arena → saved results."""
from __future__ import annotations
import argparse
import sys
from enum import Enum, auto

from .agent import run_agent
from .arena import build_arena_pair
from .judge_runner import run_judge
from .models import BenchmarkRun, TaskType, TestCase
from .runner import generate_test_cases, save_run, summarize_run


class State(Enum):
    LOAD = auto()
    RUN_AGENT = auto()
    JUDGE = auto()
    ARENA = auto()
    SAVE = auto()
    DONE = auto()


class BenchmarkFSM:
    def __init__(
        self,
        log_file: str,
        task_case: str | None = None,
        output_dir: str = "results",
    ) -> None:
        self.log_file = log_file
        self.task_case = task_case
        self.output_dir = output_dir
        self.state = State.LOAD
        self.run = BenchmarkRun()
        self._pending: list[TestCase] = []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_task_types(self) -> list[TaskType] | None:
        if self.task_case is None:
            return None
        try:
            return [TaskType(self.task_case)]
        except ValueError:
            valid = [t.value for t in TaskType]
            raise ValueError(
                f"Unknown --task-case '{self.task_case}'. Valid values: {valid}"
            )

    def _log(self, state: str, msg: str) -> None:
        print(f"[{state}] {msg}", flush=True)

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def _do_load(self) -> None:
        task_types = self._resolve_task_types()
        self._pending = generate_test_cases([self.log_file], task_types)
        self.run.test_cases = list(self._pending)
        self._log("LOAD", f"Generated {len(self._pending)} test cases")

    def _do_run_agent(self) -> None:
        for case in self._pending:
            self._log(
                "RUN_AGENT",
                f"{case.task_type.value} / {case.tool_variant.value} ({case.case_id})",
            )
            output = run_agent(case)
            self.run.outputs.append(output)
        self._log("RUN_AGENT", f"Collected {len(self.run.outputs)} outputs")

    def _do_judge(self) -> None:
        case_by_id = {c.case_id: c for c in self.run.test_cases}
        for output in self.run.outputs:
            self._log("JUDGE", f"Scoring {output.case_id}")
            score = run_judge(case_by_id[output.case_id], output)
            self.run.judge_scores.append(score)
        self._log("JUDGE", f"Scored {len(self.run.judge_scores)} outputs")

    def _do_arena(self) -> None:
        for task_type in TaskType:
            pair = build_arena_pair(task_type, self.run.outputs)
            if pair is not None:
                self.run.arena_comparisons.append(pair)
        self._log("ARENA", f"Built {len(self.run.arena_comparisons)} comparison pairs")

    def _do_save(self) -> None:
        path = save_run(self.run, self.output_dir)
        self._log("SAVE", f"Run saved → {path}")
        summary = summarize_run(self.run)
        self._log("DONE", f"Summary: {summary}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    _TRANSITIONS = {
        State.LOAD: (State.RUN_AGENT, "_do_load"),
        State.RUN_AGENT: (State.JUDGE, "_do_run_agent"),
        State.JUDGE: (State.ARENA, "_do_judge"),
        State.ARENA: (State.SAVE, "_do_arena"),
        State.SAVE: (State.DONE, "_do_save"),
    }

    def step(self) -> State:
        """Execute the current state and advance to the next."""
        if self.state == State.DONE:
            return State.DONE
        next_state, method = self._TRANSITIONS[self.state]
        getattr(self, method)()
        self.state = next_state
        return self.state

    def run_all(self) -> BenchmarkRun:
        """Run all states to completion."""
        while self.state != State.DONE:
            self.step()
        return self.run


# ------------------------------------------------------------------
# CLI entrypoint
# ------------------------------------------------------------------

_VALID_TASKS = [t.value for t in TaskType]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ct-bench",
        description="Run coding trajectory benchmarks against a JSONL log file.",
    )
    parser.add_argument("logfile", help="Path to the JSONL coding agent log file.")
    parser.add_argument(
        "--task-case",
        dest="task_case",
        default=None,
        metavar="TASK",
        help=f"Run only a specific task type. Choices: {_VALID_TASKS}",
    )
    parser.add_argument(
        "--output-dir",
        dest="output_dir",
        default="results",
        help="Directory to write benchmark results (default: results).",
    )
    args = parser.parse_args(argv)

    try:
        fsm = BenchmarkFSM(
            log_file=args.logfile,
            task_case=args.task_case,
            output_dir=args.output_dir,
        )
        fsm.run_all()
    except (ValueError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
