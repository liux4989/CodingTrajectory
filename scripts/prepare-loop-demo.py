"""Prepare explicitly synthetic local evidence; never reads the user's logs."""

import argparse
import json
import runpy
from pathlib import Path

CODEX_SESSION = "019faa00-0000-7000-8000-0000000000b0"
CODEX_TURNS = {
    "small": "019faa00-0000-7000-8000-0000000000b1",
    "large": "019faa00-0000-7000-8000-0000000000b2",
    "no_usage": "019faa00-0000-7000-8000-0000000000b3",
}


def _codex_row(second: int, payload_type: str, **payload) -> dict:
    minute, sec = divmod(second, 60)
    return {
        "timestamp": f"2026-09-12T08:{minute:02d}:{sec:02d}.000Z",
        "type": payload_type,
        "payload": payload,
    }


def _token_count(second: int, last: dict, total: dict) -> dict:
    return _codex_row(
        second,
        "event_msg",
        type="token_count",
        info={
            "total_token_usage": total,
            "last_token_usage": last,
            "model_context_window": 258400,
        },
    )


def codex_budget_journal() -> list[dict]:
    """Three synthetic turns: small usage, large usage, and no usage evidence."""
    rows = [
        _codex_row(
            0,
            "session_meta",
            id=CODEX_SESSION,
            cwd="/project/codex-budget-demo",
            timestamp="2026-09-12T07:59:00.000Z",
            model_provider="openai",
        ),
        # Turn 1: small, comfortably inside any sane budget.
        _codex_row(1, "event_msg", type="task_started", turn_id=CODEX_TURNS["small"]),
        _codex_row(
            2,
            "turn_context",
            turn_id=CODEX_TURNS["small"],
            model="gpt-5.5",
            effort="low",
        ),
        _codex_row(
            3, "event_msg", type="user_message", message="Synthetic small turn."
        ),
        _token_count(
            20,
            {
                "input_tokens": 2000,
                "cached_input_tokens": 0,
                "output_tokens": 200,
                "reasoning_output_tokens": 0,
                "total_tokens": 2200,
            },
            {
                "input_tokens": 2000,
                "cached_input_tokens": 0,
                "output_tokens": 200,
                "reasoning_output_tokens": 0,
                "total_tokens": 2200,
            },
        ),
        _codex_row(
            21,
            "event_msg",
            type="task_complete",
            turn_id=CODEX_TURNS["small"],
            last_agent_message="Synthetic small turn done.",
            duration_ms=19000,
        ),
        # Turn 2: two usage observations, clearly above a 50k budget.
        _codex_row(30, "event_msg", type="task_started", turn_id=CODEX_TURNS["large"]),
        _codex_row(
            31,
            "turn_context",
            turn_id=CODEX_TURNS["large"],
            model="gpt-5.5",
            effort="low",
        ),
        _codex_row(
            32, "event_msg", type="user_message", message="Synthetic large turn."
        ),
        _token_count(
            50,
            {
                "input_tokens": 60000,
                "cached_input_tokens": 30000,
                "output_tokens": 3000,
                "reasoning_output_tokens": 1000,
                "total_tokens": 64000,
            },
            {
                "input_tokens": 62000,
                "cached_input_tokens": 30000,
                "output_tokens": 3200,
                "reasoning_output_tokens": 1000,
                "total_tokens": 66200,
            },
        ),
        _token_count(
            55,
            {
                "input_tokens": 80000,
                "cached_input_tokens": 40000,
                "output_tokens": 5000,
                "reasoning_output_tokens": 2000,
                "total_tokens": 87000,
            },
            {
                "input_tokens": 144000,
                "cached_input_tokens": 70000,
                "output_tokens": 8400,
                "reasoning_output_tokens": 3000,
                "total_tokens": 155400,
            },
        ),
        _codex_row(
            56,
            "event_msg",
            type="task_complete",
            turn_id=CODEX_TURNS["large"],
            last_agent_message="Synthetic large turn done.",
            duration_ms=25000,
        ),
        # Turn 3: completes without any provider usage observation.
        _codex_row(
            60, "event_msg", type="task_started", turn_id=CODEX_TURNS["no_usage"]
        ),
        _codex_row(
            61,
            "turn_context",
            turn_id=CODEX_TURNS["no_usage"],
            model="gpt-5.5",
            effort="low",
        ),
        _codex_row(
            62,
            "event_msg",
            type="user_message",
            message="Synthetic turn without usage evidence.",
        ),
        _codex_row(
            63,
            "event_msg",
            type="task_complete",
            turn_id=CODEX_TURNS["no_usage"],
            last_agent_message="Synthetic no-usage turn done.",
            duration_ms=1000,
        ),
    ]
    return rows


def prepare(root: Path, *, track: list[Path] | None = None) -> Path:
    fixture = runpy.run_path(str(Path(__file__).with_name("validate-amp-live.py")))
    logs = root / ".coding-trajectory" / "amp" / "sessions"
    logs.mkdir(parents=True, exist_ok=True)
    for name, parent in [("PARENT", True), ("CHILD", False)]:
        records = fixture["journal"](fixture[name], parent=parent)
        text = "\n".join(json.dumps(row) for row in records)
        text = text.replace(
            "PRIVATE task", "Synthetic example: verify command failure handling."
        )
        text = text.replace("PRIVATE output", "Synthetic command exited with code 23.")
        text = text.replace(
            "PRIVATE final",
            "Synthetic result: the command failed with exit code 23; no change was applied.",
        )
        (logs / f"{name.lower()}.jsonl").write_text(text + "\n")

    # Synthetic Codex evidence exercises Monitor pass/breach/unavailable outcomes.
    codex_root = root / ".codex" / "sessions"
    codex_root.mkdir(parents=True, exist_ok=True)
    journal = codex_budget_journal()
    (codex_root / "loop-budget-demo.jsonl").write_text(
        "\n".join(json.dumps(row) for row in journal) + "\n"
    )
    # Codex project-scoped discovery is gated on config.toml tracking the
    # server's working directory (frozen Core behavior). Track the demo root so
    # a server run from this directory can scope by project name.
    tracked = [str(path) for path in (track or [root])]
    # TOML: one table header per tracked path.
    config = "".join(
        f'[projects."{path}"]\ntrust_level = "trusted"\n' for path in tracked
    )
    (root / ".codex" / "config.toml").write_text(config)
    return logs


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    print(prepare(directory, track=[directory]))
    print(
        "Synthetic Codex Monitor evidence written. To scope watches by the "
        "'codex-budget-demo' project, run the Loop server from this directory."
    )
