#!/usr/bin/env python3
"""Standalone benchmark for comparing raw-log access against CLI tool access.

This script is fully isolated from the product CLI/core layers.
It parses vendor JSONL logs directly to build a reference, then runs
two Codex agents with different access methods:
  - raw_file: reads the JSONL log file directly
  - cli_tool: uses the `codingtrajectory` CLI to explore the session
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


SUPPORTED_VENDORS = ("claude_code", "codex_cli", "gemini_cli", "amp")

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "number"},
        "reasoning": {"type": "string"},
    },
    "required": ["score", "reasoning"],
    "additionalProperties": False,
}

_TASK_PREAMBLE = "\n".join(
    [
        "You are analyzing a coding session log.",
        "Summarize the coding session task instructions.",
        "Focus on the explicit user request, requested metrics, requested workflow, and constraints.",
        "Ignore runtime recording noise, tool chatter, and local command bookkeeping.",
        "Return JSON with one key: summary.",
    ]
)


@dataclass(slots=True)
class BenchmarkCase:
    access_mode: str
    label: str
    prompt: str
    sandbox: str
    sandbox_permissions: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PromptRecord:
    prompt_id: str
    text: str
    timestamp: str | None = None


# ---------------------------------------------------------------------------
# Prompt text normalisation (benchmark-local, no core dependency)
# ---------------------------------------------------------------------------

def normalize_instruction_text(text: str | None) -> str | None:
    if text is None:
        return None
    compact = " ".join(text.split())
    lowered = compact.casefold()
    if not compact:
        return None
    if "<local-command-caveat>" in lowered:
        return None
    if "<command-name>" in lowered or "<command-message>" in lowered:
        return None
    if lowered == "[request interrupted by user]":
        return None
    if lowered.startswith("this session is being continued from a previous conversation"):
        return None
    if lowered.startswith("# agents.md instructions"):
        return None
    if lowered.startswith("<environment_context>"):
        return None
    if lowered.startswith("## skills"):
        return None
    if compact in {"/clear", "clear"}:
        return None
    return compact


def _extract_text_from_content(content: Any) -> str | None:
    """Extract user-facing text from a message content field (str or list of blocks)."""
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        texts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        joined = " ".join(t for t in texts if t).strip()
        return joined or None
    return None


# ---------------------------------------------------------------------------
# JSONL parsing (benchmark-local, no core dependency)
# ---------------------------------------------------------------------------

def extract_prompt_records(raw_log: Path, vendor: str) -> tuple[str, list[PromptRecord], int]:
    """Parse a vendor JSONL log and extract user prompt records directly."""
    session_id = raw_log.stem
    prompt_records: list[PromptRecord] = []
    event_count = 0
    with raw_log.open(encoding="utf-8", errors="ignore") as handle:
        for index, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_count += 1

            if vendor == "codex_cli":
                if record.get("type") == "session_meta":
                    session_id = record.get("payload", {}).get("id") or session_id
                if record.get("type") != "event_msg":
                    continue
                payload = record.get("payload") or {}
                if payload.get("type") != "user_message":
                    continue
                text = normalize_instruction_text(payload.get("message"))
                if text is None:
                    continue
                prompt_records.append(
                    PromptRecord(
                        prompt_id=f"raw-{index}",
                        text=text,
                        timestamp=record.get("timestamp"),
                    )
                )
                continue

            # claude_code, gemini_cli, amp
            if record.get("type") == "user":
                if record.get("isMeta"):
                    continue
                content = record.get("message", {}).get("content")
                text = normalize_instruction_text(_extract_text_from_content(content))
                if text is None:
                    continue
                prompt_records.append(
                    PromptRecord(
                        prompt_id=record.get("uuid") or f"raw-{index}",
                        text=text,
                        timestamp=record.get("timestamp"),
                    )
                )

    deduped: list[PromptRecord] = []
    seen: set[str] = set()
    for prompt in prompt_records:
        key = prompt.text.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(prompt)
    return str(session_id), deduped, event_count


# ---------------------------------------------------------------------------
# Reference answer (built from benchmark-local parsing, not from core)
# ---------------------------------------------------------------------------

def build_reference(prompts: list[PromptRecord]) -> dict[str, Any]:
    cleaned = [normalize_instruction_text(prompt.text) for prompt in prompts]
    cleaned = [item for item in cleaned if item]
    if not cleaned:
        summary = "No explicit user task instructions were detected."
    elif len(cleaned) == 1:
        summary = cleaned[0]
    else:
        summary = "Session task instructions, in order:\n" + "\n".join(
            f"{index}. {instruction}" for index, instruction in enumerate(cleaned, 1)
        )
    return {"task_instructions": cleaned, "summary": summary}


# ---------------------------------------------------------------------------
# Case builders — same task, different access methods
# ---------------------------------------------------------------------------

def _build_raw_file_prompt(raw_log: Path) -> str:
    return "\n".join(
        [
            _TASK_PREAMBLE,
            "",
            "Access method: Read the raw log file directly.",
            f"File path: {raw_log}",
        ]
    )


def _build_cli_tool_prompt(cli_bin: str, raw_log: Path) -> str:
    return "\n".join(
        [
            _TASK_PREAMBLE,
            "",
            "Access method: Use the `codingtrajectory` CLI tool via shell commands.",
            "Do NOT read raw log files directly. Only use the CLI tool.",
            "",
            "Workflow:",
            f"1. Run: {cli_bin} --log-file {raw_log} --pretty list",
            "   This returns all available trajectories with session IDs.",
            f"2. Pick a trajectory and run: {cli_bin} --log-file {raw_log} --pretty session overview <session_id>",
            "   This returns session metadata, turns with user_request text, and step summaries.",
            f"3. If you need more detail on a specific event: {cli_bin} --log-file {raw_log} --pretty event get <event_id>",
            "",
            "Use the information retrieved from the CLI to build your summary.",
        ]
    )


def _find_cli_bin() -> str:
    """Locate the codingtrajectory binary."""
    venv_bin = ROOT / ".venv" / "bin" / "codingtrajectory"
    if venv_bin.exists():
        return str(venv_bin)
    which = shutil.which("codingtrajectory")
    if which:
        return which
    print("error: codingtrajectory CLI not found. Install with: uv pip install -e packages/cli", file=sys.stderr)
    sys.exit(1)


def build_cases(raw_log: Path) -> list[BenchmarkCase]:
    cli_bin = _find_cli_bin()
    return [
        BenchmarkCase(
            access_mode="raw_file",
            label="Direct raw log file access",
            prompt=_build_raw_file_prompt(raw_log),
            sandbox="read-only",
            sandbox_permissions=["disk-full-read-access"],
        ),
        BenchmarkCase(
            access_mode="cli_tool",
            label="CLI tool progressive drill-down",
            prompt=_build_cli_tool_prompt(cli_bin, raw_log),
            sandbox="read-only",
            sandbox_permissions=["disk-full-read-access"],
        ),
    ]


# ---------------------------------------------------------------------------
# Judge prompt (blind — no access_mode)
# ---------------------------------------------------------------------------

def build_judge_prompt(reference: dict[str, Any], candidate_summary: str) -> str:
    return "\n".join(
        [
            "Evaluate whether the candidate summary correctly captures the coding session task instructions.",
            "Score from 0.0 to 1.0.",
            "Penalize missing requirements, noisy tangents, and incorrect claims.",
            "Use only the reference text and the candidate summary provided here.",
            "Return JSON with keys: score and reasoning.",
            "Reference:",
            json.dumps(reference, indent=2, sort_keys=True),
            "Candidate summary:",
            candidate_summary,
        ]
    )


# ---------------------------------------------------------------------------
# Codex executor
# ---------------------------------------------------------------------------

def run_codex_json(
    prompt: str,
    schema: dict[str, Any],
    *,
    model: str | None,
    cwd: Path,
    sandbox: str,
    sandbox_permissions: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, int | None]]:
    with tempfile.TemporaryDirectory(prefix="session-task-benchmark-") as temp_dir:
        temp_path = Path(temp_dir)
        schema_path = temp_path / "schema.json"
        output_path = temp_path / "output.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")

        cmd = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--json",
            "--sandbox",
            sandbox,
            "--cd",
            str(cwd),
            "--output-schema",
            str(schema_path),
            "-o",
            str(output_path),
            "-",
        ]
        if sandbox_permissions:
            cmd.extend(["-c", f'sandbox_permissions={json.dumps(sandbox_permissions)}'])
        if model:
            cmd.extend(["--model", model])

        completed = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            capture_output=True,
            check=False,
        )

        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "codex exec failed")

        payload = json.loads(output_path.read_text(encoding="utf-8"))
        # Codex turn.completed usage is cumulative across the entire session,
        # so the last turn.completed carries the total for all turns.
        usage: dict[str, int | None] = {
            "input_tokens": None,
            "output_tokens": None,
            "cached_input_tokens": None,
        }
        for line in completed.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            if event.get("type") == "turn.completed":
                raw_usage = event.get("usage") or {}
                usage = {
                    "input_tokens": raw_usage.get("input_tokens"),
                    "output_tokens": raw_usage.get("output_tokens"),
                    "cached_input_tokens": raw_usage.get("cached_input_tokens"),
                }
        return payload, usage


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

def benchmark_single_file(raw_log: Path, vendor: str, *, model: str | None, include_context: bool) -> dict[str, Any]:
    session_id, prompts, event_count = extract_prompt_records(raw_log, vendor)
    reference = build_reference(prompts)
    cases = build_cases(raw_log)

    report_cases: list[dict[str, Any]] = []
    for case in cases:
        summary_payload, summary_usage = run_codex_json(
            case.prompt,
            SUMMARY_SCHEMA,
            model=model,
            cwd=ROOT,
            sandbox=case.sandbox,
            sandbox_permissions=case.sandbox_permissions,
        )
        summary = summary_payload["summary"].strip()

        judge_prompt = build_judge_prompt(reference, summary)
        judge_payload, judge_usage = run_codex_json(
            judge_prompt,
            JUDGE_SCHEMA,
            model=model,
            cwd=ROOT,
            sandbox="read-only",
            sandbox_permissions=["disk-full-read-access"],
        )
        score = clamp_score(judge_payload["score"])

        summary_total_tokens = total_tokens(summary_usage)
        token_efficiency = round((score * 1000.0) / summary_total_tokens, 6) if summary_total_tokens else None

        case_report = {
            "access_mode": case.access_mode,
            "label": case.label,
            "summary": summary,
            "summary_usage": {**summary_usage, "total_tokens": summary_total_tokens},
            "judge": {
                "score": score,
                "reasoning": judge_payload["reasoning"].strip(),
                "usage": {**judge_usage, "total_tokens": total_tokens(judge_usage)},
            },
            "token_efficiency": {
                "score_per_1k_total_tokens": token_efficiency,
            },
        }
        if include_context:
            case_report["summary_prompt"] = case.prompt
            case_report["judge_prompt"] = judge_prompt
        report_cases.append(case_report)

    return {
        "scenario": "session_task_summary",
        "input": {
            "raw_log": str(raw_log),
            "vendor": vendor,
            "model": model,
        },
        "session": {
            "session_id": session_id,
            "turn_count": len(prompts),
            "event_count": event_count,
        },
        "reference": reference,
        "cases": report_cases,
    }


def total_tokens(usage: dict[str, int | None]) -> int | None:
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if input_tokens is None or output_tokens is None:
        return None
    return input_tokens + output_tokens


def clamp_score(value: Any) -> float:
    score = float(value)
    if score > 1.0 and score <= 100.0:
        score = score / 100.0
    return max(0.0, min(1.0, score))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Standalone benchmark for one raw session log.")
    parser.add_argument("--raw-log", required=True, help="Path to one vendor raw log file.")
    parser.add_argument(
        "--vendor",
        required=True,
        choices=list(SUPPORTED_VENDORS),
        help="Vendor format of the raw log.",
    )
    parser.add_argument("--model", help="Optional model override for codex exec.")
    parser.add_argument("--include-context", action="store_true", help="Include prompts and contexts in the output.")
    parser.add_argument("--output", help="Optional path for writing the JSON report.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = benchmark_single_file(
        Path(args.raw_log),
        args.vendor,
        model=args.model,
        include_context=args.include_context,
    )
    rendered = json.dumps(report, indent=2)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
