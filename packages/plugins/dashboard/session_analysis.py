from __future__ import annotations

import datetime as dt
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict

try:
    from . import context_window as context_window_mod
    from .codex_app_server import CodexAppServerClient, CodexAppServerResult, PiRpcClient
except ImportError:
    import context_window as context_window_mod
    from codex_app_server import CodexAppServerClient, CodexAppServerResult, PiRpcClient


CtJson = Callable[[list[str]], dict[str, Any]]
AnalysisProvider = Literal["codex", "pi"]
AnalysisSource = Literal["codex_app_server_skill", "pi_rpc_skill"]
FindingKind = Literal[
    "justified_expensive_work",
    "avoidable_pattern",
    "optimal_pattern",
    "recommended_workflow",
]


class SessionPhase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    turn_ids: list[str]
    summary: str


class TaskStory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    initial_request: str | None
    follow_up_requests: list[str]
    phases: list[SessionPhase]
    touched_artifacts: list[str]
    outcomes: list[str]


class ContextCompositionEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str
    concept: str
    source_key: str
    label: str
    tokens: int
    percent: float | None
    confidence: str
    resident_estimated_cost_usd: float | None


class ExpensiveBilledItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    category: str
    summary: str
    billed_tokens: int
    billed_estimated_cost_usd: float


class UsageEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    billed_tokens: int
    billed_input_tokens: int
    billed_uncached_input_tokens: int
    billed_cached_tokens: int
    billed_cache_creation_tokens: int
    billed_output_tokens: int
    billed_reasoning_tokens: int
    resident_context_tokens: int | None
    context_window_tokens: int | None
    resident_context_percent: float | None
    high_billed_turns: list[dict[str, Any]]
    context_composition: list[ContextCompositionEntry] = []
    expensive_billed_items: list[ExpensiveBilledItem] = []


class ToolBucket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    judgment: Literal["good", "neutral", "risky"]
    calls: int
    failed_calls: int
    output_chars: int
    call_share: float
    output_share: float


class ToolExample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bucket: str
    tool: str
    output_chars: int
    failed: bool
    command: str
    timestamp: str | None = None


class ToolEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_requested_calls: int
    total_result_calls: int
    failed_result_calls: int
    output_chars: int
    buckets: list[ToolBucket]
    top_output_calls: list[ToolExample]


class AnalysisFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: FindingKind
    title: str
    body: str
    impact: str | None
    evidence: list[str]


class SessionAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[3] = 3
    session_id: str
    generated_at: str
    source: AnalysisSource
    provider: AnalysisProvider
    artifact_path: str | None = None
    app_server_thread_id: str
    app_server_turn_id: str | None = None
    task_story: TaskStory
    usage_evidence: UsageEvidence
    tool_evidence: ToolEvidence
    findings: list[AnalysisFinding]


class AgentReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_story: TaskStory
    findings: list[AnalysisFinding]


class AnalysisRunner(Protocol):
    def run_skill_turn(
        self,
        *,
        cwd: Path,
        skill_name: str,
        skill_path: Path,
        user_text: str,
        output_schema: dict[str, Any],
    ) -> CodexAppServerResult:
        ...


def build_or_load_analysis(
    session_id: str,
    *,
    ct_json: CtJson,
    refresh: bool = False,
    artifact_dir: Path | None = None,
    provider: AnalysisProvider = "codex",
) -> SessionAnalysis:
    provider = _normalize_provider(provider)
    artifact = _artifact_path(session_id, artifact_dir, provider=provider)
    if not refresh and artifact.is_file():
        return SessionAnalysis.model_validate_json(artifact.read_text(encoding="utf-8"))
    analysis = build_analysis(session_id, ct_json=ct_json, provider=provider)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    analysis = analysis.model_copy(update={"artifact_path": str(artifact)})
    artifact.write_text(analysis.model_dump_json(indent=2), encoding="utf-8")
    return analysis


def build_analysis(
    session_id: str,
    *,
    ct_json: CtJson,
    provider: AnalysisProvider = "codex",
) -> SessionAnalysis:
    provider = _normalize_provider(provider)
    overview = ct_json(["session", "overview", "--global-scope", "--output", "json", session_id])
    usage = ct_json(["session", "usage", "--global-scope", "--output", "json", session_id])
    stats = ct_json(["session", "stats", "--global-scope", "--output", "json", session_id])
    requested = ct_json([
        "session",
        "events",
        "--global-scope",
        "--output",
        "json",
        session_id,
        "--type",
        "tool.call.requested",
    ])
    succeeded = ct_json([
        "session",
        "events",
        "--global-scope",
        "--output",
        "json",
        session_id,
        "--type",
        "tool.call.succeeded",
    ])
    failed = ct_json([
        "session",
        "events",
        "--global-scope",
        "--output",
        "json",
        session_id,
        "--type",
        "tool.call.failed",
    ])
    task_story = _task_story(overview)
    usage_evidence = _usage_evidence(stats, usage)
    tool_evidence = _tool_evidence(requested, succeeded, failed)
    resolved_session_id = str(stats.get("id") or overview.get("id") or session_id)
    usage_evidence = _augment_usage_evidence(
        resolved_session_id,
        usage_evidence,
        stats=stats,
        ct_json=ct_json,
    )
    evidence_packet = _evidence_packet(
        session_id=resolved_session_id,
        task_story=task_story,
        usage_evidence=usage_evidence,
        tool_evidence=tool_evidence,
    )
    app_result = _analysis_runner(provider).run_skill_turn(
        cwd=_repo_root(),
        skill_name="coding-session-review",
        skill_path=_skill_path(),
        user_text=_analysis_request_text(evidence_packet),
        output_schema=AgentReviewOutput.model_json_schema(),
    )
    review = AgentReviewOutput.model_validate(_json_from_text(app_result.text))
    return SessionAnalysis(
        session_id=resolved_session_id,
        generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        source=_analysis_source(provider),
        provider=provider,
        app_server_thread_id=app_result.thread_id,
        app_server_turn_id=app_result.turn_id,
        task_story=review.task_story,
        usage_evidence=usage_evidence,
        tool_evidence=tool_evidence,
        findings=review.findings,
    )


def _analysis_runner(provider: AnalysisProvider) -> AnalysisRunner:
    if provider == "pi":
        return PiRpcClient()
    return CodexAppServerClient()


def _analysis_source(provider: AnalysisProvider) -> AnalysisSource:
    return "pi_rpc_skill" if provider == "pi" else "codex_app_server_skill"


def _normalize_provider(value: str) -> AnalysisProvider:
    normalized = value.strip().lower().replace("_", "-")
    if normalized in {"codex", "codex-app-server", "codex_app_server"}:
        return "codex"
    if normalized in {"pi", "pi-rpc", "pi_rpc"}:
        return "pi"
    raise ValueError("unknown analysis provider; expected codex or pi")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _skill_path() -> Path:
    return Path(__file__).resolve().parent / "skills" / "coding-session-review" / "SKILL.md"


def _evidence_packet(
    *,
    session_id: str,
    task_story: TaskStory,
    usage_evidence: UsageEvidence,
    tool_evidence: ToolEvidence,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "suggested_task_story": task_story.model_dump(mode="json"),
        "usage_evidence": usage_evidence.model_dump(mode="json"),
        "tool_evidence": tool_evidence.model_dump(mode="json"),
        "domain_review_hints": [
            "Judge tool cost against task necessity.",
            "Use usage_evidence.context_composition[].concept/source_key as the canonical context concept taxonomy.",
            "Keep resident context composition separate from billed token usage.",
            "Do not treat cloud/live state checks as automatically bad.",
            "Prefer missed routing artifacts, broad output, and repeated avoidable probes as prioritization signals.",
        ],
    }


def _analysis_request_text(evidence_packet: dict[str, Any]) -> str:
    return (
        "$coding-session-review Review this CodingTrajectory session and return only JSON matching "
        "the output schema. Use the evidence packet as the only source of facts.\n\n"
        f"{json.dumps(evidence_packet, ensure_ascii=False, separators=(',', ':'))}"
    )


def _json_from_text(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            return json.loads(stripped[start : end + 1])
        raise


def _artifact_path(session_id: str, artifact_dir: Path | None, *, provider: AnalysisProvider) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", session_id).strip("-") or "session"
    directory = artifact_dir or Path.home() / ".coding-trajectory" / "dashboard" / "session-analysis"
    return directory / f"{safe_id}.{provider}.v3.json"


def _task_story(overview: dict[str, Any]) -> TaskStory:
    turns = _overview_turns(overview)
    requests = [_request_text(turn.get("request")) for turn in turns]
    requests = [request for request in requests if request]
    artifacts = sorted(_touched_artifacts(turns))[:40]
    outcomes = [
        activity.get("text")
        for turn in turns
        for activity in turn.get("activity") or []
        if isinstance(activity, dict)
        and isinstance(activity.get("text"), str)
        and _looks_like_outcome(activity["text"])
    ]
    return TaskStory(
        initial_request=requests[0] if requests else None,
        follow_up_requests=requests[1:],
        phases=_session_phases(turns),
        touched_artifacts=artifacts,
        outcomes=[_one_line(item, 220) for item in outcomes[-6:]],
    )


def _overview_turns(overview: dict[str, Any]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for session in overview.get("sessions") or []:
        if isinstance(session, dict):
            for turn in session.get("turns") or []:
                if isinstance(turn, dict):
                    turns.append(turn)
    return turns


def _request_text(request: Any) -> str | None:
    if not isinstance(request, dict):
        return None
    value = request.get("text") or request.get("content") or request.get("summary")
    return _one_line(value, 220) if isinstance(value, str) and value.strip() else None


def _session_phases(turns: list[dict[str, Any]]) -> list[SessionPhase]:
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    for turn in turns:
        label = _phase_label(_request_text(turn.get("request")) or "")
        if groups and groups[-1][0] == label:
            groups[-1][1].append(turn)
        else:
            groups.append((label, [turn]))
    return [
        SessionPhase(
            label=label,
            turn_ids=[str(turn.get("id") or turn.get("turn_id") or "-") for turn in items],
            summary=_phase_summary(label, items),
        )
        for label, items in groups
    ]


def _phase_label(request: str) -> str:
    lower = request.lower()
    if lower in {"do it", "implement it", "fix it"} or "do it" == lower.strip():
        return "implementation"
    if any(term in lower for term in ["why", "review", "status", "still", "does", "can we"]):
        return "diagnosis"
    if any(term in lower for term in ["deploy", "docker", "ci", "push", "kill"]):
        return "operations"
    return "discussion"


def _phase_summary(label: str, turns: list[dict[str, Any]]) -> str:
    first = _request_text(turns[0].get("request")) or "session work"
    if len(turns) == 1:
        return first
    return f"{len(turns)} turns of {label}: {first}"


def _touched_artifacts(turns: list[dict[str, Any]]) -> set[str]:
    artifacts: set[str] = set()
    path_keys = ("path", "paths", "target", "targets")
    for turn in turns:
        for activity in turn.get("activity") or []:
            if not isinstance(activity, dict):
                continue
            for key in path_keys:
                value = activity.get(key)
                if isinstance(value, str):
                    artifacts.update(_extract_paths(value))
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, str):
                            artifacts.update(_extract_paths(item))
    return artifacts


def _extract_paths(text: str) -> set[str]:
    candidates = re.findall(
        r"(?:[\w.-]+/)+[\w.$@%+=:,~.-]+|\.github/workflows/[\w.-]+|README\.md|AGENTS\.md",
        text,
    )
    return {candidate.rstrip(".,)") for candidate in candidates if len(candidate) > 3}


def _looks_like_outcome(text: str) -> bool:
    lower = text.lower()
    return any(term in lower for term in ["implemented and committed", "done and committed", "current status", "killed them", "the short answer"])


def _usage_evidence(stats: dict[str, Any], usage: dict[str, Any]) -> UsageEvidence:
    billed = stats.get("billed_token_usage")
    if not isinstance(billed, dict):
        raise ValueError("session stats payload is missing billed_token_usage")
    context = stats.get("context") or stats.get("context_window") or {}
    model = stats.get("model") or {}
    turns = usage.get("turns") or []
    high_billed_turns = sorted(
        [
            {
                "turn_id": turn.get("id") or turn.get("turn_id"),
                "total_tokens": _int_value((turn.get("usage") or {}).get("total")),
                "input_tokens": _int_value((turn.get("usage") or {}).get("input")),
                "cached_tokens": _int_value((turn.get("usage") or {}).get("cached")),
            }
            for turn in turns
            if isinstance(turn, dict)
        ],
        key=lambda item: item["total_tokens"],
        reverse=True,
    )[:6]
    return UsageEvidence(
        billed_tokens=_usage_int(billed, "total"),
        billed_input_tokens=_usage_int(billed, "input"),
        billed_uncached_input_tokens=_usage_int(billed, "uncached_input"),
        billed_cached_tokens=_usage_int(billed, "cached"),
        billed_cache_creation_tokens=_usage_int(billed, "cache_creation"),
        billed_output_tokens=_usage_int(billed, "output"),
        billed_reasoning_tokens=_usage_int(billed, "reasoning"),
        resident_context_tokens=_optional_int(
            context.get("used") or context.get("used_tokens")
        ),
        context_window_tokens=_optional_int(model.get("context_window") or model.get("context_window_tokens")),
        resident_context_percent=_optional_float(
            context.get("pct") or context.get("used_percent")
        ),
        high_billed_turns=high_billed_turns,
    )


def _usage_int(usage: dict[str, Any], key: str) -> int:
    raw_key = {
        "input": "input_tokens",
        "uncached_input": "uncached_input_tokens",
        "cached": "cached_input_tokens",
        "cache_creation": "cache_creation_input_tokens",
        "output": "output_tokens",
        "reasoning": "reasoning_output_tokens",
        "total": "total_tokens",
    }[key]
    return _int_value(usage.get(raw_key) if raw_key in usage else usage.get(key))


def _composition_leaves(stats: dict[str, Any]) -> list[dict[str, Any]]:
    context = stats.get("context") or stats.get("context_window") or {}
    leaves = list(_category_leaves(context.get("categories") or []))
    return sorted(
        leaves,
        key=lambda item: (
            _int_value(item.get("tokens")),
            _int_value((item.get("allocated_usage") or {}).get("total_tokens")),
        ),
        reverse=True,
    )


def _category_leaves(categories: list[Any]) -> list[dict[str, Any]]:
    leaves: list[dict[str, Any]] = []
    for category in categories:
        if not isinstance(category, dict):
            continue
        children = category.get("children") or []
        if children:
            leaves.extend(_category_leaves(children))
        else:
            leaves.append(category)
    return leaves


def _composition_category(source_key: str) -> str:
    if source_key in {
        "base_system",
        "developer_instructions",
        "agents_md",
        "skills",
        "mcp",
        "memory",
    }:
        return "starting_context"
    if source_key in {"user_initial_request", "user_follow_up_requests"}:
        return "user_input"
    if source_key in {"context_readfile", "files"}:
        return "files"
    if source_key.startswith("output_") or source_key == "output":
        return "output"
    if source_key in {
        "assistant_messages",
        "code_changes",
        "coordination",
        "editfile",
        "writefile",
        "todolist",
        "subagenttask",
        "sessionhandoff",
        "agent",
    }:
        return "agent_work"
    return "unattributed"


def _composition_concept(source_key: str) -> str:
    if source_key in {"editfile", "writefile", "code_changes"}:
        return "code_change"
    if source_key in {"todolist", "subagenttask", "sessionhandoff", "coordination"}:
        return "coordination"
    if source_key in {"context_readfile", "files"}:
        return "read_context"
    if source_key.startswith("output_") or source_key == "output":
        return "command_output"
    if source_key in {"assistant_messages", "agent"}:
        return "assistant_message"
    if source_key in {"user_initial_request", "user_follow_up_requests"}:
        return "user_prompt"
    if source_key in {
        "base_system",
        "developer_instructions",
        "agents_md",
        "skills",
        "mcp",
        "memory",
        "starting_context",
    }:
        return "initial_context"
    return source_key


def _augment_usage_evidence(
    session_id: str,
    usage_evidence: UsageEvidence,
    *,
    stats: dict[str, Any],
    ct_json: CtJson,
) -> UsageEvidence:
    try:
        projection = context_window_mod.build_projection(session_id, ct_json=ct_json)
    except Exception:
        return usage_evidence
    projected_by_source_key = {
        category.source_key: category for category in projection.categories
    }
    composition = [
        ContextCompositionEntry(
            category=_composition_category(source_key),
            concept=_composition_concept(source_key),
            source_key=source_key,
            label=_one_line(str(raw_category.get("label") or source_key), 120),
            tokens=_int_value(raw_category.get("tokens")),
            percent=_optional_float(
                raw_category.get("percent") or raw_category.get("pct")
            ),
            confidence=str(raw_category.get("confidence") or "estimated_tokens"),
            resident_estimated_cost_usd=(
                projected.estimated_cost.value_usd
                if projected and projected.estimated_cost
                else None
            ),
        )
        for raw_category in _composition_leaves(stats)
        if (source_key := str(raw_category.get("key") or "")).strip()
        for projected in [projected_by_source_key.get(source_key)]
    ]
    expensive_items = [
        ExpensiveBilledItem(
            label=_one_line(item.label, 120),
            category=item.category,
            summary=_one_line(item.summary, 220),
            billed_tokens=item.allocated_usage.get("total_tokens", 0),
            billed_estimated_cost_usd=item.estimated_cost.value_usd,
        )
        for item in projection.expensive_items[:8]
    ]
    return usage_evidence.model_copy(
        update={
            "resident_context_tokens": (
                projection.used_tokens.value
                if projection.used_tokens
                else usage_evidence.resident_context_tokens
            ),
            "context_window_tokens": (
                projection.context_window_tokens.value
                if projection.context_window_tokens
                else usage_evidence.context_window_tokens
            ),
            "resident_context_percent": (
                projection.used_percent
                if projection.used_percent is not None
                else usage_evidence.resident_context_percent
            ),
            "context_composition": composition,
            "expensive_billed_items": expensive_items,
        }
    )


def _tool_evidence(
    requested_payload: dict[str, Any],
    succeeded_payload: dict[str, Any],
    failed_payload: dict[str, Any],
) -> ToolEvidence:
    requested = _matches(requested_payload)
    results = [*_matches(succeeded_payload), *_matches(failed_payload)]
    requested_by_id = {
        str(match.get("payload", {}).get("tool_call_id")): match.get("payload") or {}
        for match in requested
        if isinstance(match.get("payload"), dict)
    }
    examples: list[ToolExample] = []
    bucket_data: dict[str, dict[str, int]] = defaultdict(lambda: {"calls": 0, "failed": 0, "chars": 0})
    total_chars = 0
    for result in results:
        payload = result.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        request = requested_by_id.get(str(payload.get("tool_call_id")), {})
        tool = str(request.get("tool_name") or "unknown")
        command = _command_from_request(request)
        output_chars = _output_size(str(payload.get("output") or ""))
        failed = payload.get("status") == "failed"
        bucket = _classify_tool(command, tool)
        bucket_data[bucket]["calls"] += 1
        bucket_data[bucket]["failed"] += int(failed)
        bucket_data[bucket]["chars"] += output_chars
        total_chars += output_chars
        examples.append(
            ToolExample(
                bucket=bucket,
                tool=tool,
                output_chars=output_chars,
                failed=failed,
                command=_one_line(command, 220),
                timestamp=result.get("timestamp") if isinstance(result.get("timestamp"), str) else None,
            )
        )
    total_result_calls = sum(data["calls"] for data in bucket_data.values())
    buckets = [
        ToolBucket(
            key=key,
            label=_bucket_label(key),
            judgment=_bucket_judgment(key),
            calls=data["calls"],
            failed_calls=data["failed"],
            output_chars=data["chars"],
            call_share=_ratio(data["calls"], total_result_calls),
            output_share=_ratio(data["chars"], total_chars),
        )
        for key, data in sorted(bucket_data.items(), key=lambda item: item[1]["chars"], reverse=True)
    ]
    return ToolEvidence(
        total_requested_calls=len(requested),
        total_result_calls=total_result_calls,
        failed_result_calls=sum(data["failed"] for data in bucket_data.values()),
        output_chars=total_chars,
        buckets=buckets,
        top_output_calls=sorted(examples, key=lambda item: item.output_chars, reverse=True)[:12],
    )


def _matches(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in payload.get("matches") or [] if isinstance(item, dict)]


def _command_from_request(request: dict[str, Any]) -> str:
    value = request.get("input")
    if isinstance(value, dict):
        cmd = value.get("cmd")
        if isinstance(cmd, str):
            return cmd
    if isinstance(value, str):
        return value
    return ""


def _classify_tool(command: str, tool: str) -> str:
    lower = command.lower()
    if tool == "apply_patch":
        return "edits"
    if tool == "reasoning":
        return "reasoning_items"
    if tool != "exec_command":
        return "other_tool"
    if "curl -fssl" in lower and "espn.com/soccer/" in lower and "| rg" in lower:
        return "raw_html_scrape"
    if lower.startswith("rg ") or lower.startswith("rg -n") or " rg -n " in lower:
        if (
            re.search(r"\s\.(?:$|\s)", lower)
            or "src aws packages readme" in lower
            or "docs" in lower
            or "/memories/" in lower
            or "world cup readiness|readiness" in lower
            or "source-evidence|research|aws smoke" in lower
            or "limit|limit|default_event_limit" in lower
        ):
            return "broad_search"
        return "targeted_search"
    if lower.startswith("sed ") or lower.startswith("nl ") or lower.startswith("cat "):
        return "file_read_shell"
    if any(term in lower for term in ["git status", "git diff", "git log"]):
        return "git_inspection"
    if any(
        term in lower
        for term in [
            "aws batch",
            " aws iam ",
            " aws sts ",
            "wrangler d1",
            "tt research",
            "curl -fss https://trailtrading-research-api",
        ]
    ):
        return "cloud_state_check"
    if any(term in lower for term in ["py_compile", "bun run check", "diff --check", "ruby -e"]):
        return "validation"
    if any(term in lower for term in ["git add", "git commit"]):
        return "git_write"
    return "other_exec"


def _bucket_label(key: str) -> str:
    return {
        "broad_search": "Broad searches",
        "raw_html_scrape": "Raw HTML scraping",
        "file_read_shell": "Large file reads",
        "cloud_state_check": "Cloud state checks",
        "git_inspection": "Git inspection",
        "targeted_search": "Targeted searches",
        "validation": "Validation",
        "edits": "Edits",
        "git_write": "Git writes",
        "reasoning_items": "Reasoning items",
        "other_exec": "Other shell commands",
        "other_tool": "Other tools",
    }.get(key, key.replace("_", " ").title())


def _bucket_judgment(key: str) -> Literal["good", "neutral", "risky"]:
    if key in {"targeted_search", "validation", "edits", "git_write"}:
        return "good"
    if key in {"broad_search", "raw_html_scrape", "file_read_shell"}:
        return "risky"
    return "neutral"


def _output_size(output: str) -> int:
    match = re.search(r"\[(\d[\d,]*) chars\]", output)
    if match:
        return int(match.group(1).replace(",", ""))
    return len(output)


def _ratio(value: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round((value / total) * 100, 1)


def _int_value(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int | float):
        return int(value)
    return 0


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    return None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _one_line(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"
