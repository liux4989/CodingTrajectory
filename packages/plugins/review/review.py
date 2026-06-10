from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    value: float
    unit: str
    evidence: str


@dataclass(frozen=True)
class Finding:
    key: str
    severity: str
    scope: str
    title: str
    evidence: str
    recommendation: str
    metrics: list[str]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ct plugin review",
        description="Review coding sessions for improvement opportunities.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    session_parser = subparsers.add_parser(
        "session",
        prog="ct plugin review session",
        description="Analyze one session using ct overview, stats, and usage evidence.",
    )
    session_parser.add_argument("session_id", help="Session ID to review.")
    session_parser.add_argument(
        "--global-scope",
        action="store_true",
        help="Search all known log files when resolving the session.",
    )
    session_parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Select stdout format.",
    )
    session_parser.add_argument(
        "--judge",
        choices=("app-server", "deterministic"),
        default="app-server",
        help="Select the finding/recommendation judge. Defaults to Codex app-server.",
    )
    session_parser.add_argument(
        "--app-server-cmd",
        default=os.environ.get("CODEX_APP_SERVER_CMD", "codex app-server"),
        help="Command used to start the Codex app-server judge.",
    )
    session_parser.add_argument(
        "--model",
        default=os.environ.get("CT_REVIEW_JUDGE_MODEL", ""),
        help="Optional model override for the app-server judge.",
    )
    session_parser.add_argument(
        "--effort",
        default=os.environ.get("CT_REVIEW_JUDGE_EFFORT", "low"),
        help="Optional reasoning effort for the app-server judge.",
    )
    session_parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("CT_REVIEW_JUDGE_TIMEOUT", "180")),
        help="Seconds to wait for the app-server judge.",
    )
    args = parser.parse_args(argv)

    if args.command == "session":
        payload = analyze_session(
            args.session_id,
            global_scope=args.global_scope,
            judge=args.judge,
            app_server_cmd=args.app_server_cmd,
            model=args.model.strip() or None,
            effort=args.effort.strip() or None,
            timeout=args.timeout,
        )
        if args.format == "json":
            print(json.dumps(payload, separators=(",", ":")))
        else:
            print(render_text(payload))
        return 0

    parser.error(f"Unsupported command: {args.command}")
    return 2


def analyze_session(
    session_id: str,
    *,
    global_scope: bool,
    judge: str,
    app_server_cmd: str,
    model: str | None,
    effort: str | None,
    timeout: float,
) -> dict[str, Any]:
    overview = _ct_json(["session", "overview", session_id, "--output", "json"], global_scope=global_scope)
    stats = _ct_json(
        ["session", "stats", session_id, "--output", "json"],
        global_scope=global_scope,
    )
    usage = _ct_json(
        ["session", "usage", session_id, "--output", "json"],
        global_scope=global_scope,
    )

    metrics = _metrics(overview, stats)
    evidence = _evidence_packet(session_id, metrics, overview, stats, usage)
    judge_result = _judge_findings(
        judge,
        evidence,
        app_server_cmd=app_server_cmd,
        model=model,
        effort=effort,
        timeout=timeout,
    )
    findings = judge_result["findings"]
    recommendations = judge_result["recommendations"]
    return {
        "plugin": "review",
        "kind": "session",
        "session_id": session_id,
        "judge": judge_result["judge"],
        "sources": {
            "overview": "ct session overview --output json",
            "stats": "ct session stats --output json",
            "usage": "ct session usage --output json",
        },
        "summary": _summary(metrics, findings, overview, stats, usage),
        "metrics": [metric.__dict__ for metric in metrics],
        "findings": findings,
        "recommendations": recommendations,
        "warnings": _warnings(stats, usage),
    }


def _metrics(overview: dict[str, Any], stats: dict[str, Any]) -> list[Metric]:
    categories = _category_index(stats)
    runtime = stats.get("runtime") or {}
    messages = stats.get("messages") or {}
    total_context = _num(_dig(stats, ["context", "used"])) or _num(_dig(stats, ["context_window", "used_tokens"]))

    context_gathering_tokens = sum(
        _category_tokens(categories, key)
        for key in ("context_readfile", "context_searchtext", "context_listfiles")
    )
    tool_output_tokens = _category_tokens(categories, "output")
    agent_work_tokens = _category_tokens(categories, "agent_work")
    tool_calls = _num(runtime.get("tools") if "tools" in runtime else runtime.get("tool_calls"))
    failed_tools = _num(runtime.get("failed_tools") if "failed_tools" in runtime else runtime.get("failed_tool_calls"))
    search_count = _activity_count(overview, "SearchText")
    read_count = _activity_count(overview, "ReadFile")
    list_count = _activity_count(overview, "ListFiles")
    run_count = _activity_count(overview, "RunCommand")

    return [
        Metric(
            "context_gathering_tokens",
            "Context gathering",
            context_gathering_tokens,
            "tokens",
            "Sum of Files read, Search results, and File listings in session stats.",
        ),
        Metric(
            "context_gathering_context_pct",
            "Context gathering share",
            _pct(context_gathering_tokens, total_context),
            "pct",
            "Context gathering tokens divided by current context used.",
        ),
        Metric(
            "tool_output_tokens",
            "Tool output",
            tool_output_tokens,
            "tokens",
            "Output bucket in session stats.",
        ),
        Metric(
            "tool_output_context_pct",
            "Tool output share",
            _pct(tool_output_tokens, total_context),
            "pct",
            "Tool output tokens divided by current context used.",
        ),
        Metric(
            "tool_output_agent_work_pct",
            "Tool output share of agent work",
            _pct(tool_output_tokens, agent_work_tokens),
            "pct",
            "Tool output tokens divided by agent work tokens.",
        ),
        Metric(
            "failed_tool_rate",
            "Failed tool rate",
            _pct(failed_tools, tool_calls),
            "pct",
            "Failed tool calls divided by total tool calls.",
        ),
        Metric("search_calls", "Search calls", search_count, "count", "SearchText activities in overview."),
        Metric("read_calls", "Read calls", read_count, "count", "ReadFile activities in overview."),
        Metric("list_calls", "List calls", list_count, "count", "ListFiles activities in overview."),
        Metric("run_calls", "Run commands", run_count, "count", "RunCommand activities in overview."),
        Metric(
            "tool_outputs_messages",
            "Tool output messages",
            _num(messages.get("tools") if "tools" in messages else messages.get("tool_outputs")),
            "count",
            "Tool output message count in session stats.",
        ),
    ]


def _deterministic_findings(
    metrics: list[Metric],
    overview: dict[str, Any],
    stats: dict[str, Any],
    usage: dict[str, Any],
) -> list[dict[str, Any]]:
    by_key = {metric.key: metric for metric in metrics}
    findings: list[Finding] = []

    context_pct = by_key["context_gathering_context_pct"].value
    context_tokens = by_key["context_gathering_tokens"].value
    if context_pct >= 12 or context_tokens >= 25000:
        findings.append(Finding(
            "high_context_gathering_load",
            "high" if context_pct >= 18 or context_tokens >= 40000 else "medium",
            "environment",
            "Context gathering consumed a large part of the session window.",
            f"Context gathering used {_tokens(context_tokens)} ({context_pct:.1f}% of current context).",
            "Add or update repo maps, architecture notes, command recipes, and task-specific entrypoint docs so agents can start from known landmarks instead of surveying broad code areas.",
            ["context_gathering_tokens", "context_gathering_context_pct"],
        ))

    tool_output_pct = by_key["tool_output_context_pct"].value
    tool_output_agent_pct = by_key["tool_output_agent_work_pct"].value
    if tool_output_pct >= 6 or tool_output_agent_pct >= 25:
        findings.append(Finding(
            "tool_output_pressure",
            "high" if tool_output_pct >= 10 or tool_output_agent_pct >= 40 else "medium",
            "tooling",
            "Tool outputs took a material share of the working context.",
            f"Tool output used {_tokens(by_key['tool_output_tokens'].value)} ({tool_output_pct:.1f}% of context, {tool_output_agent_pct:.1f}% of agent-work tokens).",
            "Prefer compact machine-readable modes for inspection tools, cap default listings, and add summary/detail flags so agents do not have to ingest human-oriented reports by default.",
            ["tool_output_tokens", "tool_output_context_pct", "tool_output_agent_work_pct"],
        ))

    tool_output_pct = by_key["tool_output_context_pct"].value
    tool_output_agent_pct = by_key["tool_output_agent_work_pct"].value
    if tool_output_pct >= 12 or tool_output_agent_pct >= 45:
        findings.append(Finding(
            "tool_output_dominance",
            "high" if tool_output_pct >= 18 or tool_output_agent_pct >= 60 else "medium",
            "agent",
            "Tool output dominated the observed session context.",
            f"Tool output accounted for {tool_output_pct:.1f}% of context and {tool_output_agent_pct:.1f}% of agent-work tokens.",
            "Batch related reads/searches, narrow commands before running them, and prefer existing structured ct outputs over repeated broad shell inspection.",
            ["tool_output_context_pct", "tool_output_agent_work_pct"],
        ))

    search_calls = by_key["search_calls"].value
    read_calls = by_key["read_calls"].value
    list_calls = by_key["list_calls"].value
    if search_calls >= 8 or (search_calls >= 4 and read_calls >= 4 and list_calls >= 1):
        findings.append(Finding(
            "broad_context_survey",
            "medium",
            "environment",
            "The session shows a broad context survey pattern.",
            "Observed "
            f"{_count_phrase(search_calls, 'search activity', 'search activities')}, "
            f"{_count_phrase(read_calls, 'read activity', 'read activities')}, and "
            f"{_count_phrase(list_calls, 'file-listing activity', 'file-listing activities')}.",
            "Create a lightweight repo orientation surface: key packages, ownership boundaries, common validation commands, and where session/plugin contracts live.",
            ["search_calls", "read_calls", "list_calls"],
        ))

    failed_rate = by_key["failed_tool_rate"].value
    if failed_rate >= 5:
        findings.append(Finding(
            "failed_tool_calls",
            "medium" if failed_rate < 15 else "high",
            "agent",
            "Some tool calls failed and likely added avoidable turns.",
            f"Failed tool rate was {failed_rate:.1f}%.",
            "Prefer checking command help and file existence before uncommon invocations; when a command fails, adapt the next command rather than retrying a similar broad probe.",
            ["failed_tool_rate"],
        ))

    if not findings:
        findings.append(Finding(
            "no_major_pressure_detected",
            "info",
            "session",
            "No major efficiency pressure crossed the current thresholds.",
            "The measured context, usage, and tool profile did not show a strong outlier.",
            "Keep using the current workflow; review JSON metrics for smaller local tuning opportunities.",
            [],
        ))

    return [finding.__dict__ for finding in findings]


def _judge_findings(
    judge: str,
    evidence: dict[str, Any],
    *,
    app_server_cmd: str,
    model: str | None,
    effort: str | None,
    timeout: float,
) -> dict[str, Any]:
    if judge == "deterministic":
        findings = _deterministic_findings(
            [Metric(**item) for item in evidence["metrics"]],
            evidence["overview"],
            evidence["stats"],
            evidence["usage"],
        )
        return {
            "judge": {"type": "deterministic", "model": None, "token_usage": None},
            "findings": findings,
            "recommendations": _recommendations_from_findings(findings),
        }
    if judge != "app-server":
        raise SystemExit(f"unsupported judge: {judge}")
    return _app_server_judge_findings(
        evidence,
        app_server_cmd=app_server_cmd,
        model=model,
        effort=effort,
        timeout=timeout,
    )


def _app_server_judge_findings(
    evidence: dict[str, Any],
    *,
    app_server_cmd: str,
    model: str | None,
    effort: str | None,
    timeout: float,
) -> dict[str, Any]:
    command = shlex.split(app_server_cmd)
    if not command:
        raise SystemExit("--app-server-cmd must not be empty")
    prompt = _judge_prompt(evidence)
    try:
        with CodexAppServer(command) as rpc:
            thread_id = rpc.start_thread(Path.cwd())
            turn = rpc.start_turn(thread_id, prompt, timeout_seconds=timeout, model=model, effort=effort)
    except AppServerError as exc:
        raise SystemExit(f"Codex app-server judge failed: {exc}") from exc
    result = _parse_judge_json(turn.get("text") or "")
    findings = _normalize_findings(result.get("findings"))
    recommendations = _normalize_recommendations(result.get("recommendations"), findings)
    return {
        "judge": {
            "type": "app-server",
            "model": model,
            "effort": effort,
            "token_usage": turn.get("token_usage"),
            "summary": _one_line(result.get("summary"), 240),
        },
        "findings": findings,
        "recommendations": recommendations,
    }


def _evidence_packet(
    session_id: str,
    metrics: list[Metric],
    overview: dict[str, Any],
    stats: dict[str, Any],
    usage: dict[str, Any],
) -> dict[str, Any]:
    runtime = stats.get("runtime") or {}
    return {
        "session_id": session_id,
        "metrics": [metric.__dict__ for metric in metrics],
        "overview": _compact_overview(overview),
        "stats": {
            "runtime": runtime,
            "messages": stats.get("messages") or {},
            "context": stats.get("context") or stats.get("context_window") or {},
            "warnings": stats.get("warnings") or [],
        },
        "usage": {
            "total": usage.get("usage") or usage.get("total_usage") or {},
            "turns": [
                {
                    "id": turn.get("id") or turn.get("turn_id"),
                    "usage": turn.get("usage") or {},
                    "activity": turn.get("activity") or turn.get("activity_usage") or [],
                }
                for turn in usage.get("turns") or []
            ],
            "warnings": usage.get("warnings") or [],
        },
    }


def _compact_overview(overview: dict[str, Any]) -> dict[str, Any]:
    sessions: list[dict[str, Any]] = []
    for session in overview.get("sessions") or []:
        turns: list[dict[str, Any]] = []
        for turn in session.get("turns") or []:
            activities: list[dict[str, Any]] = []
            for activity in turn.get("activity") or []:
                if not isinstance(activity, dict):
                    continue
                compact = {
                    key: activity.get(key)
                    for key in ("tool", "count", "status", "cmd", "path", "query", "url", "paths", "queries", "targets")
                    if activity.get(key) is not None
                }
                if "text" in activity:
                    compact["text"] = _one_line(activity.get("text"), 220)
                if compact:
                    activities.append(compact)
            request = turn.get("request") or turn.get("user_request") or {}
            turns.append(
                {
                    "id": turn.get("id") or turn.get("turn_id"),
                    "status": turn.get("status"),
                    "request": _one_line(request.get("text") if isinstance(request, dict) else request, 260),
                    "activity": activities,
                }
            )
        sessions.append(
            {
                "id": session.get("id") or session.get("session_id"),
                "vendor": session.get("vendor"),
                "status": session.get("status"),
                "cwd": session.get("cwd"),
                "turns": turns,
            }
        )
    return {"id": overview.get("id") or overview.get("root_session_id"), "sessions": sessions}


def _judge_prompt(evidence: dict[str, Any]) -> str:
    schema = {
        "summary": "one paragraph explaining the session's biggest improvement opportunities",
        "findings": [
            {
                "key": "stable_snake_case_id",
                "severity": "high|medium|low|info",
                "scope": "agent|environment|tooling|session",
                "title": "short title",
                "evidence": "specific measured evidence from the packet",
                "recommendation": "actionable recommendation tied to this finding",
                "metrics": ["metric_key"],
            }
        ],
        "recommendations": [
            {
                "scope": "agent|environment|tooling|session",
                "recommendation": "actionable recommendation",
                "from_finding": "finding key",
                "severity": "high|medium|low|info",
            }
        ],
    }
    return "\n".join(
        [
            "You are an expert judge of coding-agent session trajectories.",
            "Analyze the evidence packet and identify what could be improved.",
            "Do not call tools, inspect files, or use external context; judge only from the supplied evidence packet.",
            "",
            "Use the measured data. Do not invent facts. Do not merely apply fixed thresholds.",
            "Separate responsibility by scope:",
            "- agent: the coding agent could use tools or plan work better",
            "- environment: the repo/project should provide better orientation or docs",
            "- tooling: tools should reduce noisy output or expose better machine-readable modes",
            "- session: session-level observation with no clear owner",
            "",
            "Prefer findings that are supported by multiple pieces of evidence. If a metric is high but expected for the task, say so or lower severity.",
            "Each recommendation must be tied to a finding and must be actionable.",
            "Return only JSON matching this shape:",
            json.dumps(schema, indent=2),
            "",
            "Evidence packet:",
            json.dumps(evidence, indent=2, ensure_ascii=True),
        ]
    )


def _parse_judge_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise AppServerError("judge returned empty output")
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise AppServerError(f"judge did not return JSON: {_one_line(stripped, 300)}")
        value = json.loads(stripped[start : end + 1])
    if not isinstance(value, dict):
        raise AppServerError("judge JSON must be an object")
    return value


def _normalize_findings(value: Any) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            continue
        key = _safe_key(item.get("key"), fallback=f"finding_{index}")
        severity = _enum(item.get("severity"), {"high", "medium", "low", "info"}, "info")
        scope = _enum(item.get("scope"), {"agent", "environment", "tooling", "session"}, "session")
        metrics = item.get("metrics")
        findings.append(
            {
                "key": key,
                "severity": severity,
                "scope": scope,
                "title": _one_line(item.get("title"), 140) or key.replace("_", " ").title(),
                "evidence": _one_line(item.get("evidence"), 500),
                "recommendation": _one_line(item.get("recommendation"), 500),
                "metrics": [str(metric) for metric in metrics] if isinstance(metrics, list) else [],
            }
        )
    if not findings:
        findings.append(
            {
                "key": "judge_returned_no_findings",
                "severity": "info",
                "scope": "session",
                "title": "Judge returned no findings.",
                "evidence": "The LLM judge did not identify a supported improvement opportunity.",
                "recommendation": "Inspect metrics manually or rerun with a different model if this looks suspicious.",
                "metrics": [],
            }
        )
    return findings


def _normalize_recommendations(value: Any, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            recommendations.append(
                {
                    "scope": _enum(item.get("scope"), {"agent", "environment", "tooling", "session"}, "session"),
                    "recommendation": _one_line(item.get("recommendation"), 500),
                    "from_finding": _safe_key(item.get("from_finding"), fallback=""),
                    "severity": _enum(item.get("severity"), {"high", "medium", "low", "info"}, "info"),
                }
            )
    if recommendations:
        return recommendations
    return _recommendations_from_findings(findings)


def _recommendations_from_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "scope": finding.get("scope") or "session",
            "recommendation": finding.get("recommendation") or "",
            "from_finding": finding.get("key") or "",
            "severity": finding.get("severity") or "info",
        }
        for finding in findings
        if finding.get("recommendation")
    ]


def _summary(
    metrics: list[Metric],
    findings: list[dict[str, Any]],
    overview: dict[str, Any],
    stats: dict[str, Any],
    usage: dict[str, Any],
) -> dict[str, Any]:
    severity_order = {"high": 4, "medium": 3, "low": 2, "info": 1}
    top = max(findings, key=lambda item: severity_order.get(str(item.get("severity")), 0))
    runtime = stats.get("runtime") or {}
    return {
        "top_severity": top.get("severity"),
        "finding_count": len([item for item in findings if item.get("severity") != "info"]),
        "turns": runtime.get("turns"),
        "tool_calls": runtime.get("tools") if "tools" in runtime else runtime.get("tool_calls"),
        "session_cost": _num((usage.get("usage") or usage.get("total_usage") or {}).get("cost")),
        "primary_recommendation": top.get("recommendation"),
    }


def render_text(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        f"# Session Review `{payload.get('session_id') or '-'}`",
        "",
        f"Findings: {summary.get('finding_count', 0)}  "
        f"Top severity: {summary.get('top_severity') or '-'}  "
        f"Turns: {summary.get('turns') or 0}  "
        f"Tool calls: {summary.get('tool_calls') or 0}",
        "",
        "## Findings",
    ]
    for finding in payload.get("findings") or []:
        lines.extend([
            "",
            f"- [{finding.get('severity')}] {finding.get('title')}",
            f"  scope: {finding.get('scope')}",
            f"  evidence: {finding.get('evidence')}",
            f"  recommendation: {finding.get('recommendation')}",
        ])

    lines.extend(["", "## Key Metrics", "", "```"])
    for metric in payload.get("metrics") or []:
        value = _format_metric_value(metric.get("value"), metric.get("unit"))
        lines.append(f"{metric.get('label') or metric.get('key'):<34} {value:>12}")
    lines.append("```")

    warnings = payload.get("warnings") or []
    if warnings:
        lines.append("")
        for warning in warnings:
            lines.append(f"> {warning}")
    return "\n".join(lines).rstrip()


def _ct_json(args: list[str], *, global_scope: bool) -> dict[str, Any]:
    ct = os.environ.get("CT_COMMAND") or shutil.which("ct")
    if not ct:
        raise SystemExit("ct executable not found; set CT_COMMAND to the ct command path")
    command = [*shlex.split(ct), *args]
    if global_scope:
        command.append("--global-scope")
    try:
        completed = subprocess.run(command, check=False, text=True, capture_output=True, timeout=90)
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(f"ct command timed out: {' '.join(command)}") from exc
    if completed.returncode != 0:
        sys.stderr.write(completed.stderr or completed.stdout)
        raise SystemExit(completed.returncode)
    return json.loads(completed.stdout)


class AppServerError(RuntimeError):
    pass


class CodexAppServer:
    def __init__(self, command: list[str]) -> None:
        self.command = command
        self._next_id = 1
        self._process: subprocess.Popen[str] | None = None
        self._stdout_lines: queue.Queue[str | None] = queue.Queue()
        self._stdout_thread: threading.Thread | None = None

    def __enter__(self) -> "CodexAppServer":
        resolved = _resolve_command(self.command)
        try:
            self._process = subprocess.Popen(
                resolved,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise AppServerError(str(exc)) from exc
        assert self._process.stdout is not None
        self._stdout_thread = threading.Thread(target=self._read_stdout_lines, daemon=True)
        self._stdout_thread.start()
        self.call(
            "initialize",
            {
                "clientInfo": {
                    "name": "ct-review-plugin",
                    "title": "ct review plugin",
                    "version": "0.1.0",
                },
                "capabilities": {
                    "optOutNotificationMethods": [
                        "thread/started",
                        "thread/closed",
                        "turn/started",
                        "turn/diff/updated",
                        "turn/plan/updated",
                        "item/started",
                        "item/completed",
                        "item/reasoning/summaryTextDelta",
                        "item/reasoning/textDelta",
                    ],
                },
            },
        )
        self.notify("initialized", {})
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._write_message({"id": request_id, "method": method, "params": params})
        return self._wait_for_response(request_id)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write_message({"method": method, "params": params})

    def start_thread(self, cwd: Path) -> str:
        result = self.call("thread/start", {"cwd": str(cwd), "ephemeral": True})
        return _thread_id_from(result)

    def start_turn(
        self,
        thread_id: str,
        input_text: str,
        *,
        timeout_seconds: float,
        model: str | None,
        effort: str | None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": input_text}],
        }
        if model:
            params["model"] = model
        if effort:
            params["effort"] = effort
        started = self.call("turn/start", params)
        turn_id = _nested_get(started, ("turn", "id"))
        if not isinstance(turn_id, str) or not turn_id:
            raise AppServerError(f"turn/start response did not include a turn id: {started}")

        deltas: list[str] = []
        token_usage: dict[str, Any] | None = None
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            message = self._read_message(
                min(5.0, max(0.0, deadline - time.monotonic())),
                allow_timeout=True,
            )
            if message is None or "id" in message:
                continue
            method = message.get("method")
            msg_params = message.get("params")
            if not isinstance(msg_params, dict):
                continue
            if method == "item/agentMessage/delta" and msg_params.get("turnId") == turn_id:
                delta = _extract_text_delta(msg_params)
                if delta:
                    deltas.append(delta)
                continue
            if method == "thread/tokenUsage/updated" and msg_params.get("threadId") == thread_id:
                token_usage = msg_params
                continue
            if (
                method == "turn/completed"
                and msg_params.get("threadId") == thread_id
                and _nested_get(msg_params, ("turn", "id")) == turn_id
            ):
                return {"text": "".join(deltas), "turn": msg_params.get("turn"), "token_usage": token_usage}
        partial = _one_line("".join(deltas), 300)
        detail = f" Partial output: {partial}" if partial else ""
        raise AppServerError(f"Timed out waiting for turn/completed: {turn_id}.{detail}")

    def close(self) -> None:
        if self._process is None:
            return
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
        if self._stdout_thread is not None:
            self._stdout_thread.join(timeout=1)
            self._stdout_thread = None
        self._process = None

    def _write_message(self, message: dict[str, Any]) -> None:
        process = self._require_process()
        assert process.stdin is not None
        process.stdin.write(json.dumps(message, ensure_ascii=True) + "\n")
        process.stdin.flush()

    def _wait_for_response(self, request_id: int) -> dict[str, Any]:
        deadline = time.monotonic() + 30
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerError(f"timed out waiting for app-server response id {request_id}")
            payload = self._read_message(remaining)
            if payload is None or payload.get("id") != request_id:
                continue
            if "error" in payload:
                raise AppServerError(_format_rpc_error(payload["error"]))
            result = payload.get("result")
            return result if isinstance(result, dict) else {"value": result}

    def _read_message(self, timeout_seconds: float, *, allow_timeout: bool = False) -> dict[str, Any] | None:
        process = self._require_process()
        try:
            line = self._stdout_lines.get(timeout=timeout_seconds)
        except queue.Empty:
            if allow_timeout:
                return None
            raise AppServerError("timed out waiting for app-server output")
        if line is None or line == "":
            stderr = ""
            if process.poll() is not None and process.stderr is not None:
                stderr = process.stderr.read().strip()
            detail = f": {stderr}" if stderr else ""
            raise AppServerError(f"app-server exited before replying{detail}")
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AppServerError(f"app-server returned invalid JSON: {_one_line(line, 200)}") from exc
        return payload if isinstance(payload, dict) else {}

    def _read_stdout_lines(self) -> None:
        process = self._require_process()
        assert process.stdout is not None
        try:
            for line in process.stdout:
                self._stdout_lines.put(line)
        finally:
            self._stdout_lines.put(None)

    def _require_process(self) -> subprocess.Popen[str]:
        if self._process is None:
            raise AppServerError("app-server transport is not open")
        return self._process


def _category_index(stats: dict[str, Any]) -> dict[str, dict[str, Any]]:
    root_categories = _dig(stats, ["context", "categories"])
    if not isinstance(root_categories, list):
        root_categories = _dig(stats, ["context_window", "categories"])
    index: dict[str, dict[str, Any]] = {}

    def visit(category: dict[str, Any]) -> None:
        key = str(category.get("key") or category.get("label") or "")
        if key:
            index[key] = category
        label = str(category.get("label") or "")
        if label:
            index[label] = category
        for child in category.get("children") or []:
            if isinstance(child, dict):
                visit(child)

    for category in root_categories or []:
        if isinstance(category, dict):
            visit(category)
    return index


def _category_tokens(categories: dict[str, dict[str, Any]], key: str) -> float:
    category = categories.get(key)
    if not category:
        return 0
    return _num(category.get("tokens"))


def _activity_count(overview: dict[str, Any], tool: str) -> float:
    count = 0.0
    for session in overview.get("sessions") or []:
        for turn in session.get("turns") or []:
            for activity in turn.get("activity") or []:
                if not isinstance(activity, dict) or activity.get("tool") != tool:
                    continue
                count += _num(activity.get("count")) or 1
    return count


def _warnings(*payloads: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for payload in payloads:
        for warning in payload.get("warnings") or []:
            text = " ".join(str(warning).split())
            if text and text not in warnings:
                warnings.append(text)
    return warnings


def _resolve_command(command: list[str]) -> list[str]:
    if not command:
        raise AppServerError("empty app-server command")
    first = Path(command[0]).expanduser()
    if first.is_absolute() or os.sep in command[0] or (os.altsep and os.altsep in command[0]):
        resolved = first.resolve(strict=False)
        if not resolved.exists():
            raise AppServerError(f"app-server command not found: {command[0]}")
        return [str(resolved), *command[1:]]
    resolved = shutil.which(command[0])
    if not resolved:
        raise AppServerError(f"app-server command not found: {command[0]}")
    return [resolved, *command[1:]]


def _thread_id_from(payload: dict[str, Any]) -> str:
    for value in (
        payload.get("threadId"),
        payload.get("thread_id"),
        _nested_get(payload, ("thread", "id")),
    ):
        if isinstance(value, str) and value.strip():
            return value
    raise AppServerError(f"app-server response did not include a thread id: {payload}")


def _nested_get(value: Any, path: tuple[str, ...]) -> Any:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _extract_text_delta(params: dict[str, Any]) -> str:
    delta = params.get("delta")
    if isinstance(delta, str):
        return delta
    text = params.get("text")
    if isinstance(text, str):
        return text
    encoded = params.get("deltaBase64")
    if isinstance(encoded, str):
        try:
            return base64.b64decode(encoded).decode("utf-8")
        except Exception:
            return ""
    return ""


def _format_rpc_error(error: Any) -> str:
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "unknown JSON-RPC error")
    return str(error)


def _safe_key(value: Any, *, fallback: str) -> str:
    text = str(value or "").strip().lower()
    chars = [ch if ch.isalnum() else "_" for ch in text]
    key = "_".join("".join(chars).split("_"))
    return key or fallback


def _enum(value: Any, allowed: set[str], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def _dig(payload: dict[str, Any], path: list[str]) -> Any:
    current: Any = payload
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _num(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0


def _pct(part: float, total: float) -> float:
    return round((part / total) * 100, 1) if total else 0


def _tokens(value: float) -> str:
    return f"{int(round(value)):,} tokens"


def _one_line(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: max(0, limit - 3)].rstrip() + "..."


def _format_metric_value(value: Any, unit: Any) -> str:
    number = _num(value)
    if unit == "pct":
        return f"{number:.1f}%"
    if unit == "tokens":
        return f"{int(round(number)):,}"
    if unit == "count":
        return f"{int(round(number))}"
    return str(value)


def _count_phrase(value: float, singular: str, plural: str) -> str:
    count = int(round(value))
    return f"{count} {singular if count == 1 else plural}"


if __name__ == "__main__":
    raise SystemExit(main())
