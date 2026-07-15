from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

try:
    from .codex_app_server import CodexAppServerClient, CodexAppServerManager
except ImportError:
    from codex_app_server import CodexAppServerClient, CodexAppServerManager


CtJson = Callable[[list[str]], Any]
ScopeType = Literal["turn", "session"]
TaskCategory = Literal[
    "repository_engineering",
    "terminal_workflow",
    "repository_understanding",
    "mixed",
]
CriterionMechanism = Literal["semantic", "executable", "both", "human_optional"]
CriterionState = Literal["pass", "partial", "fail", "unknown", "not_applicable"]
ExecutableState = Literal["pass", "fail", "error", "timeout", "not_run"]
Resolution = Literal[
    "verified_resolved",
    "judged_resolved",
    "partial",
    "unresolved",
    "unverified",
    "not_applicable",
]

SCHEMA_VERSION = 1
RUBRIC_VERSION = "session-evaluation-rubric-v1"
EVALUATOR_VERSION = "session-evaluation-lite-v1"
EVALUATOR_MODEL = "gpt-5.4"
EVALUATOR_EFFORT = "low"
MAX_EVIDENCE_CHARS = 80_000
MAX_SELECTED_TURNS = 20
MAX_SELECTED_ITEMS = 160
MAX_VALIDATIONS = 4


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EligibilityResult(StrictModel):
    status: Literal["evaluable", "not_applicable"]
    confidence: float = Field(ge=0, le=1)
    reason: str


class CategoryResult(StrictModel):
    primary: TaskCategory
    secondary: list[TaskCategory]
    confidence: float = Field(ge=0, le=1)
    reason: str


class DifficultyEstimate(StrictModel):
    level: Literal["easy", "medium", "hard", "very_hard"]
    confidence: float = Field(ge=0, le=1)
    reason: str
    factors: list[str]


class CheckoutState(StrictModel):
    project_path: str | None = None
    repository_root: str | None = None
    expected_revision: str | None = None
    current_revision: str | None = None
    clean: bool | None = None
    matches_expected_revision: bool | None = None
    reason: str


class EvidenceRecord(StrictModel):
    evidence_id: str
    kind: Literal[
        "request",
        "turn_summary",
        "agent_message",
        "tool_summary",
        "validation",
        "artifact",
        "repository_instruction",
        "checkout",
    ]
    source_ref: str
    content: str
    turn_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvaluationInput(StrictModel):
    schema_version: Literal[1] = 1
    scope_type: ScopeType
    scope_id: str
    session_id: str
    project_path: str | None = None
    selected_turn_ids: list[str]
    omitted_turn_count: int = 0
    checkout: CheckoutState
    evidence: list[EvidenceRecord]


class RubricCriterion(StrictModel):
    criterion_id: str
    statement: str
    required: bool
    weight: int = Field(ge=1, le=5)
    mechanism: CriterionMechanism
    evidence_requirements: list[str]
    prohibitions: list[str]
    observable_postcondition: str | None


class TurnContribution(StrictModel):
    turn_id: str
    contribution: Literal["critical", "supporting", "exploratory", "superseded"]
    reason: str


class ValidationSpecification(StrictModel):
    validation_id: str
    argv: list[str] = Field(min_length=1)
    cwd: str
    timeout_seconds: int = Field(ge=1, le=300)
    expected_exit_codes: list[int]
    side_effect: Literal["read_only", "local_build"]
    network_required: bool
    supports_criteria: list[str] = Field(min_length=1)
    source_evidence_ids: list[str] = Field(min_length=1)
    postcondition: str


class RubricCompilation(StrictModel):
    schema_version: Literal[1]
    title: str
    eligibility_agrees: bool
    eligibility_reason: str
    category: CategoryResult
    difficulty: DifficultyEstimate
    criteria: list[RubricCriterion] = Field(min_length=1, max_length=8)
    turn_contributions: list[TurnContribution]
    proposed_validations: list[ValidationSpecification]


class FrozenRubric(StrictModel):
    schema_version: Literal[1] = 1
    rubric_version: str
    revision: int = 1
    origin: Literal["retrospective", "prospective"]
    provenance_confidence: float = Field(ge=0, le=1)
    frozen_at: str
    criteria: list[RubricCriterion]


class SemanticCriterionResult(StrictModel):
    criterion_id: str
    result: Literal["pass", "partial", "fail", "unknown"]
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str]
    reason: str


class SemanticEvaluation(StrictModel):
    schema_version: Literal[1]
    criterion_results: list[SemanticCriterionResult]
    contradictions: list[str]
    summary: str


class ExecutableRunResult(StrictModel):
    validation_id: str
    status: ExecutableState
    argv: list[str]
    cwd: str
    started_at: str | None = None
    ended_at: str | None = None
    exit_code: int | None = None
    output_head: str = ""
    output_tail: str = ""
    output_sha256: str | None = None
    supports_criteria: list[str]
    reason: str


class AggregatedCriterionResult(StrictModel):
    criterion_id: str
    required: bool
    weight: int
    mechanism: CriterionMechanism
    result: CriterionState
    semantic_result: SemanticCriterionResult | None = None
    executable_results: list[ExecutableRunResult] = Field(default_factory=list)
    reason: str


class EvaluationAggregate(StrictModel):
    resolution: Resolution
    rubric_score: float = Field(ge=0, le=1)
    evidence_coverage: float = Field(ge=0, le=1)
    criteria: list[AggregatedCriterionResult]
    reason: str


class AppServerInvocation(StrictModel):
    thread_id: str
    turn_id: str | None = None


class EvaluationIdentity(StrictModel):
    evaluation_id: str
    scope_type: ScopeType
    scope_id: str
    source_fingerprint: str
    rubric_version: str
    evaluator_version: str
    created_at: str


class EvaluationArtifact(StrictModel):
    schema_version: Literal[1] = 1
    identity: EvaluationIdentity
    eligibility: EligibilityResult
    title: str
    category: CategoryResult | None = None
    difficulty: DifficultyEstimate | None = None
    rubric: FrozenRubric | None = None
    input: EvaluationInput
    turn_contributions: list[TurnContribution] = Field(default_factory=list)
    validation_plan: list[ValidationSpecification] = Field(default_factory=list)
    semantic_evaluation: SemanticEvaluation | None = None
    executable_results: list[ExecutableRunResult] = Field(default_factory=list)
    aggregate: EvaluationAggregate
    compiler_invocation: AppServerInvocation | None = None
    evaluator_invocation: AppServerInvocation | None = None
    evaluator_model: str
    evaluator_effort: str


class EvaluationIndex(StrictModel):
    schema_version: Literal[1] = 1
    scope_type: ScopeType
    scope_id: str
    evaluation_ids: list[str]
    updated_at: str


class EvaluationInputBuilder:
    def __init__(self, *, ct_json: CtJson) -> None:
        self._ct_json = ct_json

    def build(self, *, scope_type: ScopeType, scope_id: str) -> EvaluationInput:
        entry_key = "turn_id" if scope_type == "turn" else "session_id"
        overview = self._call("session.overview", {entry_key: scope_id})
        if not isinstance(overview, dict):
            raise RuntimeError("session.overview returned an invalid payload")
        session_id = _clean_text(
            overview.get("root_session_id") or overview.get("id")
        )
        if not session_id:
            raise ValueError(f"unknown {scope_type}_id")

        all_turns = _overview_turns(overview)
        if scope_type == "turn":
            turns = [turn for turn in all_turns if turn["id"] == scope_id]
            if not turns:
                raise ValueError("turn is not present in the resolved session graph")
            omitted_turn_count = 0
        else:
            turns, omitted_turn_count = _bounded_turns(all_turns)
        selected_item_ids, item_to_turn = _selected_item_ids(turns)
        items = self._call(
            "session.items",
            {"item_ids": selected_item_ids[:MAX_SELECTED_ITEMS]},
        )
        if not isinstance(items, list):
            raise RuntimeError("session.items returned an invalid payload")

        project_path = _project_path(overview, turns)
        records = _evidence_records(
            turns=turns,
            items=[item for item in items if isinstance(item, dict)],
            item_to_turn=item_to_turn,
            project_path=project_path,
        )
        checkout = _checkout_state(project_path, records)
        records.append(
            EvidenceRecord(
                evidence_id="checkout-001",
                kind="checkout",
                source_ref="current recorded project checkout",
                content=json.dumps(checkout.model_dump(mode="json"), sort_keys=True),
            )
        )
        records = _cap_evidence(records)
        return EvaluationInput(
            scope_type=scope_type,
            scope_id=scope_id,
            session_id=session_id,
            project_path=project_path,
            selected_turn_ids=[turn["id"] for turn in turns],
            omitted_turn_count=omitted_turn_count,
            checkout=checkout,
            evidence=records,
        )

    def _call(self, method: str, params: dict[str, Any]) -> Any:
        payload = self._ct_json(
            [
                "api",
                "call",
                method,
                "--global-scope",
                "--params",
                json.dumps(params, separators=(",", ":")),
            ]
        )
        if not isinstance(payload, dict):
            raise RuntimeError(f"{method} returned an invalid response envelope")
        if not payload.get("ok"):
            error = payload.get("error") or {}
            raise ValueError(str(error.get("message") or f"{method} failed"))
        return payload.get("result")


def _overview_turns(overview: dict[str, Any]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for session in overview.get("sessions") or []:
        if not isinstance(session, dict):
            continue
        for turn in session.get("turns") or []:
            if not isinstance(turn, dict):
                continue
            turn_id = _clean_text(turn.get("turn_id") or turn.get("id"))
            if not turn_id:
                continue
            turns.append(
                {
                    **turn,
                    "id": turn_id,
                    "session_id": _clean_text(
                        session.get("session_id") or session.get("id")
                    ),
                    "cwd": _clean_text(session.get("cwd")),
                    "session_status": _clean_text(session.get("status")),
                    "relationship": session.get("relationship") or {},
                }
            )
    return turns


def _bounded_turns(turns: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    if len(turns) <= MAX_SELECTED_TURNS:
        return turns, 0
    selected = [turns[0], *turns[-(MAX_SELECTED_TURNS - 1) :]]
    return selected, len(turns) - len(selected)


def _selected_item_ids(
    turns: list[dict[str, Any]],
) -> tuple[list[str], dict[str, str]]:
    selected: list[str] = []
    item_to_turn: dict[str, str] = {}
    for turn in turns:
        turn_id = turn["id"]
        refs = turn.get("refs") or {}
        raw_item_ids = (
            refs.get("item_ids")
            if isinstance(refs, dict) and refs.get("item_ids") is not None
            else turn.get("items")
        )
        item_ids = [
            str(item_id)
            for item_id in raw_item_ids or []
            if _clean_text(item_id)
        ]
        activity_tool_ids = [
            str(item_id)
            for activity in turn.get("activity") or []
            if isinstance(activity, dict)
            for item_id in activity.get("item_ids") or []
            if _clean_text(item_id)
        ]
        candidates = [*item_ids[:2], *activity_tool_ids[-10:], *item_ids[-8:]]
        for item_id in candidates:
            item_to_turn[item_id] = turn_id
            if item_id not in selected:
                selected.append(item_id)
    return selected[:MAX_SELECTED_ITEMS], item_to_turn


def _project_path(
    overview: dict[str, Any], turns: list[dict[str, Any]]
) -> str | None:
    for turn in reversed(turns):
        if turn.get("cwd"):
            return str(Path(turn["cwd"]).expanduser())
    for session in overview.get("sessions") or []:
        if isinstance(session, dict) and _clean_text(session.get("cwd")):
            return str(Path(str(session["cwd"])).expanduser())
    return None


def _evidence_records(
    *,
    turns: list[dict[str, Any]],
    items: list[dict[str, Any]],
    item_to_turn: dict[str, str],
    project_path: str | None,
) -> list[EvidenceRecord]:
    counters: Counter[str] = Counter()
    records: list[EvidenceRecord] = []
    item_by_id = {
        str(item.get("item_id")): item for item in items if item.get("item_id")
    }

    for turn in turns:
        turn_id = turn["id"]
        request = turn.get("user_request") or turn.get("request") or {}
        request_text = _clean_text(
            (
                request.get("content") or request.get("text")
                if isinstance(request, dict)
                else request
            )
        )
        if request_text:
            records.append(
                _record(
                    counters,
                    kind="request",
                    source_ref=f"session.overview turn {turn_id} request",
                    content=_truncate(request_text, 4_000),
                    turn_id=turn_id,
                    metadata={
                        "source": request.get("source")
                        if isinstance(request, dict)
                        else None
                    },
                )
            )
        summary = {
            "status": turn.get("status"),
            "session_status": turn.get("session_status"),
            "relationship": turn.get("relationship"),
            "activity": turn.get("activity") or [],
        }
        records.append(
            _record(
                counters,
                kind="turn_summary",
                source_ref=f"session.overview turn {turn_id}",
                content=_truncate(
                    json.dumps(summary, ensure_ascii=False, sort_keys=True), 5_000
                ),
                turn_id=turn_id,
            )
        )

        turn_items = [
            item_by_id[item_id]
            for item_id, mapped_turn_id in item_to_turn.items()
            if mapped_turn_id == turn_id and item_id in item_by_id
        ]
        agent_messages = [
            item for item in turn_items if item.get("kind") == "agent_message"
        ]
        for item in agent_messages[-2:]:
            text = _item_text(item)
            if not text:
                continue
            records.append(
                _record(
                    counters,
                    kind="agent_message",
                    source_ref=f"session.items {item.get('item_id')}",
                    content=_truncate(text, 6_000),
                    turn_id=turn_id,
                    metadata={"item_id": item.get("item_id")},
                )
            )

        tools = [item for item in turn_items if item.get("kind") != "agent_message"]
        tool_counts = Counter(
            _clean_text((item.get("shape") or {}).get("tool_name")) or item.get("kind")
            for item in tools
        )
        if tool_counts:
            records.append(
                _record(
                    counters,
                    kind="tool_summary",
                    source_ref=f"sampled session.items for turn {turn_id}",
                    content=json.dumps(dict(tool_counts), sort_keys=True),
                    turn_id=turn_id,
                    metadata={"sampled_item_count": len(tools)},
                )
            )
        for item in tools:
            detail = _tool_detail(item)
            if not detail or not _material_tool_detail(detail):
                continue
            records.append(
                _record(
                    counters,
                    kind=(
                        "validation" if _looks_like_validation(detail) else "artifact"
                    ),
                    source_ref=f"session.items {item.get('item_id')}",
                    content=_truncate(detail, 4_000),
                    turn_id=turn_id,
                    metadata={"item_id": item.get("item_id")},
                )
            )

    for instruction_path in _applicable_instruction_paths(project_path):
        try:
            instruction = instruction_path.read_text(errors="replace")
        except OSError:
            continue
        records.append(
            _record(
                counters,
                kind="repository_instruction",
                source_ref=str(instruction_path),
                content=_truncate(instruction, 12_000),
            )
        )
    return records


def _record(
    counters: Counter[str],
    *,
    kind: Any,
    source_ref: str,
    content: str,
    turn_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> EvidenceRecord:
    counters[kind] += 1
    return EvidenceRecord(
        evidence_id=f"{kind}-{counters[kind]:03d}",
        kind=kind,
        source_ref=source_ref,
        content=content,
        turn_id=turn_id,
        metadata=metadata or {},
    )


def _item_text(item: dict[str, Any]) -> str:
    shape = item.get("shape") or {}
    texts = shape.get("texts") if isinstance(shape, dict) else None
    if isinstance(texts, list):
        return "\n".join(str(text) for text in texts if _clean_text(text)).strip()
    return ""


def _tool_detail(item: dict[str, Any]) -> str:
    shape = item.get("shape") or {}
    if not isinstance(shape, dict):
        return ""
    relevant = {
        key: value
        for key, value in shape.items()
        if key
        in {
            "tool_name",
            "tool_input",
            "tool_output",
            "command",
            "exit_code",
            "output",
            "path",
            "operation",
        }
    }
    return json.dumps(relevant, ensure_ascii=False, sort_keys=True)


def _material_tool_detail(detail: str) -> bool:
    lower = detail.lower()
    return _looks_like_validation(detail) or any(
        token in lower
        for token in [
            "apply_patch",
            "file_change",
            "git commit",
            "git status",
            "git diff",
            "error",
            "failed",
        ]
    )


def _looks_like_validation(value: str) -> bool:
    lower = value.lower()
    return any(
        token in lower
        for token in [
            " run build",
            " ruff check",
            "compileall",
            "diff --check",
            " validate",
            "validation",
            "typecheck",
            " type-check",
            " lint",
        ]
    )


def _applicable_instruction_paths(project_path: str | None) -> list[Path]:
    if not project_path:
        return []
    path = Path(project_path)
    if not path.is_dir():
        return []
    repository_root = _git_output(path, ["rev-parse", "--show-toplevel"])
    stop = Path(repository_root) if repository_root else path
    candidates: list[Path] = []
    current = path
    while True:
        candidate = current / "AGENTS.md"
        if candidate.is_file():
            candidates.append(candidate)
        if current == stop or current.parent == current:
            break
        current = current.parent
    return list(reversed(candidates))


def _checkout_state(
    project_path: str | None, records: list[EvidenceRecord]
) -> CheckoutState:
    if not project_path or not Path(project_path).is_dir():
        return CheckoutState(
            project_path=project_path,
            reason="recorded project path is unavailable",
        )
    root_text = _git_output(Path(project_path), ["rev-parse", "--show-toplevel"])
    if not root_text:
        return CheckoutState(
            project_path=project_path,
            reason="recorded project path is not a Git checkout",
        )
    root = Path(root_text)
    current_revision = _git_output(root, ["rev-parse", "HEAD"])
    status = _git_output(root, ["status", "--porcelain"], preserve_empty=True)
    clean = status == ""
    expected_revision = _expected_revision(records)
    matches = None
    if expected_revision and current_revision:
        matches = current_revision.startswith(expected_revision) and clean
    if not expected_revision:
        reason = "historical session does not expose a final checkout revision"
    elif matches:
        reason = "current clean checkout matches the recorded final revision"
    elif not clean:
        reason = "current checkout has uncommitted changes"
    else:
        reason = "current checkout revision differs from the recorded final revision"
    return CheckoutState(
        project_path=project_path,
        repository_root=str(root),
        expected_revision=expected_revision,
        current_revision=current_revision,
        clean=clean,
        matches_expected_revision=matches,
        reason=reason,
    )


def _expected_revision(records: list[EvidenceRecord]) -> str | None:
    patterns = [
        r"(?im)^commit:\s*`?([0-9a-f]{7,40})`?",
        r"(?im)^commit\s+([0-9a-f]{7,40})\b",
        r"(?im)\b([0-9a-f]{7,40})\s+(?:feat|fix|docs|refactor|chore)[(:]",
    ]
    for record in reversed(records):
        if record.kind != "agent_message":
            continue
        for pattern in patterns:
            match = re.search(pattern, record.content)
            if match:
                return match.group(1)
    return None


def _cap_evidence(records: list[EvidenceRecord]) -> list[EvidenceRecord]:
    kept: list[EvidenceRecord] = []
    total = 0
    for record in records:
        size = len(record.content)
        if total + size > MAX_EVIDENCE_CHARS:
            continue
        kept.append(record)
        total += size
    return kept


class EvaluationEligibility:
    _LOW_VALUE = re.compile(
        r"^(?:ok(?:ay)?|thanks?|status\??|wait|continue waiting|what is the status)[.! ]*$",
        re.IGNORECASE,
    )

    def classify(self, evaluation_input: EvaluationInput) -> EligibilityResult:
        requests = [
            record.content
            for record in evaluation_input.evidence
            if record.kind == "request"
        ]
        if not requests:
            return EligibilityResult(
                status="not_applicable",
                confidence=0.98,
                reason="no observable user request is present in the selected scope",
            )
        substantive = any(
            record.kind
            in {
                "tool_summary",
                "validation",
                "artifact",
                "agent_message",
            }
            for record in evaluation_input.evidence
        )
        if all(self._LOW_VALUE.fullmatch(request.strip()) for request in requests):
            if not substantive:
                return EligibilityResult(
                    status="not_applicable",
                    confidence=0.94,
                    reason="the scope contains only a status, wait, or acknowledgement exchange",
                )
        return EligibilityResult(
            status="evaluable",
            confidence=0.9 if substantive else 0.72,
            reason="the scope has an observable requested outcome and substantive agent work",
        )


class RubricCompiler:
    def __init__(
        self,
        *,
        app_server: CodexAppServerManager | None = None,
        model: str = EVALUATOR_MODEL,
        effort: str = EVALUATOR_EFFORT,
    ) -> None:
        self._app_server = app_server
        self._model = model
        self._effort = effort

    def compile(
        self,
        *,
        evaluation_input: EvaluationInput,
        eligibility: EligibilityResult,
        cwd: Path,
    ) -> tuple[RubricCompilation, AppServerInvocation]:
        client = self._app_server or CodexAppServerClient()
        result = client.run_turn(
            cwd=cwd,
            user_text=_compiler_prompt(evaluation_input, eligibility),
            output_schema=RubricCompilation.model_json_schema(),
            ephemeral=True,
            model=self._model,
            effort=self._effort,
        )
        compilation = RubricCompilation.model_validate(result.parse_json())
        _validate_compilation(compilation, evaluation_input)
        return compilation, AppServerInvocation(
            thread_id=result.thread_id,
            turn_id=result.turn_id,
        )


class SemanticEvaluator:
    def __init__(
        self,
        *,
        app_server: CodexAppServerManager | None = None,
        model: str = EVALUATOR_MODEL,
        effort: str = EVALUATOR_EFFORT,
    ) -> None:
        self._app_server = app_server
        self._model = model
        self._effort = effort

    def evaluate(
        self,
        *,
        evaluation_input: EvaluationInput,
        rubric: FrozenRubric,
        cwd: Path,
    ) -> tuple[SemanticEvaluation, AppServerInvocation]:
        client = self._app_server or CodexAppServerClient()
        result = client.run_turn(
            cwd=cwd,
            user_text=_semantic_prompt(evaluation_input, rubric),
            output_schema=SemanticEvaluation.model_json_schema(),
            ephemeral=True,
            model=self._model,
            effort=self._effort,
        )
        evaluation = SemanticEvaluation.model_validate(result.parse_json())
        _validate_semantic_evaluation(evaluation, rubric, evaluation_input)
        return evaluation, AppServerInvocation(
            thread_id=result.thread_id,
            turn_id=result.turn_id,
        )


class ValidationPlanBuilder:
    def build(
        self,
        *,
        proposals: list[ValidationSpecification],
        rubric: FrozenRubric,
        evaluation_input: EvaluationInput,
    ) -> list[ValidationSpecification]:
        if not evaluation_input.project_path:
            return []
        project_path = Path(evaluation_input.project_path).resolve()
        evidence_ids = {record.evidence_id for record in evaluation_input.evidence}
        criteria = {criterion.criterion_id: criterion for criterion in rubric.criteria}
        accepted: list[ValidationSpecification] = []
        seen: set[tuple[str, ...]] = set()
        for proposal in proposals:
            if len(accepted) >= MAX_VALIDATIONS:
                break
            if proposal.network_required or not _safe_validation_argv(proposal.argv):
                continue
            if not set(proposal.source_evidence_ids).issubset(evidence_ids):
                continue
            if not proposal.supports_criteria:
                continue
            supported = [
                criterion_id
                for criterion_id in proposal.supports_criteria
                if criterion_id in criteria
                and criteria[criterion_id].mechanism in {"executable", "both"}
            ]
            if not supported:
                continue
            cwd = (project_path / proposal.cwd).resolve()
            try:
                cwd.relative_to(project_path)
            except ValueError:
                continue
            if not cwd.is_dir():
                continue
            command_key = tuple([str(cwd), *proposal.argv])
            if command_key in seen:
                continue
            seen.add(command_key)
            accepted.append(
                proposal.model_copy(
                    update={
                        "cwd": str(cwd),
                        "supports_criteria": supported,
                    }
                )
            )
        return accepted


class LiteExecutableRunner:
    def run(
        self,
        *,
        plan: list[ValidationSpecification],
        checkout: CheckoutState,
    ) -> list[ExecutableRunResult]:
        if not plan:
            return []
        if checkout.matches_expected_revision is not True:
            return [
                ExecutableRunResult(
                    validation_id=spec.validation_id,
                    status="not_run",
                    argv=spec.argv,
                    cwd=spec.cwd,
                    supports_criteria=spec.supports_criteria,
                    reason=f"source checkout unavailable: {checkout.reason}",
                )
                for spec in plan
            ]
        return [self._run_one(spec) for spec in plan]

    def _run_one(self, spec: ValidationSpecification) -> ExecutableRunResult:
        started_at = _now()
        try:
            completed = subprocess.run(
                spec.argv,
                cwd=spec.cwd,
                env=_runner_environment(),
                check=False,
                text=True,
                capture_output=True,
                timeout=spec.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            output = _completed_output(exc.stdout, exc.stderr)
            return _run_result(
                spec,
                status="timeout",
                started_at=started_at,
                output=output,
                reason=f"validation exceeded {spec.timeout_seconds} seconds",
            )
        except OSError as exc:
            return _run_result(
                spec,
                status="error",
                started_at=started_at,
                output="",
                reason=str(exc),
            )
        output = _completed_output(completed.stdout, completed.stderr)
        status: ExecutableState = (
            "pass" if completed.returncode in spec.expected_exit_codes else "fail"
        )
        return _run_result(
            spec,
            status=status,
            started_at=started_at,
            output=output,
            exit_code=completed.returncode,
            reason=(
                f"observable postcondition passed: {spec.postcondition}"
                if status == "pass"
                else f"command did not establish postcondition: {spec.postcondition}"
            ),
        )


class EvaluationAggregator:
    _ACHIEVEMENT = {
        "pass": 1.0,
        "partial": 0.5,
        "fail": 0.0,
        "unknown": 0.0,
        "not_applicable": 0.0,
    }

    def aggregate(
        self,
        *,
        eligibility: EligibilityResult,
        rubric: FrozenRubric | None,
        semantic: SemanticEvaluation | None,
        executable: list[ExecutableRunResult],
    ) -> EvaluationAggregate:
        if eligibility.status == "not_applicable" or rubric is None:
            return EvaluationAggregate(
                resolution="not_applicable",
                rubric_score=0,
                evidence_coverage=0,
                criteria=[],
                reason=eligibility.reason,
            )
        semantic_by_id = {
            result.criterion_id: result
            for result in (semantic.criterion_results if semantic else [])
        }
        executable_by_id: dict[str, list[ExecutableRunResult]] = defaultdict(list)
        for run in executable:
            for criterion_id in run.supports_criteria:
                executable_by_id[criterion_id].append(run)

        criterion_results = [
            _aggregate_criterion(
                criterion,
                semantic_by_id.get(criterion.criterion_id),
                executable_by_id.get(criterion.criterion_id, []),
            )
            for criterion in rubric.criteria
        ]
        applicable = [
            result
            for result in criterion_results
            if result.mechanism != "human_optional"
        ]
        total_weight = sum(result.weight for result in applicable)
        achieved = sum(
            result.weight * self._ACHIEVEMENT[result.result] for result in applicable
        )
        covered = sum(
            result.weight
            for result in applicable
            if result.result not in {"unknown", "not_applicable"}
        )
        required = [result for result in applicable if result.required]
        if any(result.result == "fail" for result in required):
            resolution: Resolution = "unresolved"
            reason = "at least one required criterion failed"
        elif required and all(result.result == "pass" for result in required):
            required_executable = [
                result
                for result in required
                if result.mechanism in {"executable", "both"}
            ]
            executable_complete = all(
                result.executable_results
                and all(run.status == "pass" for run in result.executable_results)
                for result in required_executable
            )
            resolution = (
                "verified_resolved" if executable_complete else "judged_resolved"
            )
            reason = (
                "all required criteria passed with required executable evidence"
                if executable_complete
                else "all required criteria passed semantically but executable proof is unavailable"
            )
        elif any(result.result == "partial" for result in required):
            resolution = "partial"
            reason = "required criteria show useful but incomplete achievement"
        elif any(result.result == "pass" for result in required):
            resolution = "partial"
            reason = "some required criteria passed while other required evidence is unknown"
        else:
            resolution = "unverified"
            reason = "required evidence is insufficient for a resolution decision"
        return EvaluationAggregate(
            resolution=resolution,
            rubric_score=round(achieved / total_weight, 4) if total_weight else 0,
            evidence_coverage=round(covered / total_weight, 4) if total_weight else 0,
            criteria=criterion_results,
            reason=reason,
        )


class EvaluationStore:
    def __init__(self, root: Path | None = None) -> None:
        configured = os.environ.get("CT_EVALUATION_ROOT")
        self.root = (
            root
            or (Path(configured).expanduser() if configured else None)
            or Path("~/.coding-trajectory/evaluations/v1").expanduser()
        )

    def get(self, evaluation_id: str) -> EvaluationArtifact | None:
        path = self.artifact_path(evaluation_id)
        if not path.is_file():
            return None
        return EvaluationArtifact.model_validate_json(path.read_text())

    def find(
        self,
        *,
        scope_type: ScopeType,
        scope_id: str,
    ) -> list[EvaluationArtifact]:
        index = self._read_index(scope_type, scope_id)
        if index is None:
            return []
        return [
            artifact
            for evaluation_id in index.evaluation_ids
            if (artifact := self.get(evaluation_id)) is not None
        ]

    def save(self, artifact: EvaluationArtifact) -> Path:
        path = self.artifact_path(artifact.identity.evaluation_id)
        data = artifact.model_dump_json(indent=2)
        if path.exists():
            existing = EvaluationArtifact.model_validate_json(path.read_text())
            if existing != artifact:
                raise RuntimeError(
                    f"immutable evaluation artifact already exists: {path}"
                )
            return path
        _atomic_write(path, data + "\n")
        existing_index = self._read_index(
            artifact.identity.scope_type, artifact.identity.scope_id
        )
        evaluation_ids = list(existing_index.evaluation_ids) if existing_index else []
        if artifact.identity.evaluation_id not in evaluation_ids:
            evaluation_ids.append(artifact.identity.evaluation_id)
        index = EvaluationIndex(
            scope_type=artifact.identity.scope_type,
            scope_id=artifact.identity.scope_id,
            evaluation_ids=evaluation_ids,
            updated_at=_now(),
        )
        _atomic_write(
            self._index_path(artifact.identity.scope_type, artifact.identity.scope_id),
            index.model_dump_json(indent=2) + "\n",
        )
        return path

    def artifact_path(self, evaluation_id: str) -> Path:
        if not re.fullmatch(r"eval_[0-9a-f]{24}", evaluation_id):
            raise ValueError("invalid evaluation_id")
        return self.root / "artifacts" / f"{evaluation_id}.json"

    def _read_index(
        self, scope_type: ScopeType, scope_id: str
    ) -> EvaluationIndex | None:
        path = self._index_path(scope_type, scope_id)
        if not path.is_file():
            return None
        return EvaluationIndex.model_validate_json(path.read_text())

    def _index_path(self, scope_type: ScopeType, scope_id: str) -> Path:
        safe_scope_id = re.sub(r"[^A-Za-z0-9_.-]", "_", scope_id)
        return self.root / "index" / scope_type / f"{safe_scope_id}.json"


class EvaluationService:
    def __init__(
        self,
        *,
        ct_json: CtJson,
        app_server: CodexAppServerManager | None = None,
        store: EvaluationStore | None = None,
        model: str = EVALUATOR_MODEL,
        effort: str = EVALUATOR_EFFORT,
    ) -> None:
        self._input_builder = EvaluationInputBuilder(ct_json=ct_json)
        self._eligibility = EvaluationEligibility()
        self._compiler = RubricCompiler(
            app_server=app_server, model=model, effort=effort
        )
        self._semantic = SemanticEvaluator(
            app_server=app_server, model=model, effort=effort
        )
        self._plan_builder = ValidationPlanBuilder()
        self._runner = LiteExecutableRunner()
        self._aggregator = EvaluationAggregator()
        self.store = store or EvaluationStore()
        self.model = model
        self.effort = effort

    def evaluate(
        self, *, scope_type: ScopeType, scope_id: str
    ) -> EvaluationArtifact:
        evaluation_input = self._input_builder.build(
            scope_type=scope_type, scope_id=scope_id
        )
        source_fingerprint = _fingerprint(evaluation_input)
        identity = _identity(
            scope_type=scope_type,
            scope_id=scope_id,
            source_fingerprint=source_fingerprint,
        )
        existing = self.store.get(identity.evaluation_id)
        if existing is not None:
            return existing

        eligibility = self._eligibility.classify(evaluation_input)
        if eligibility.status == "not_applicable":
            aggregate = self._aggregator.aggregate(
                eligibility=eligibility,
                rubric=None,
                semantic=None,
                executable=[],
            )
            artifact = EvaluationArtifact(
                identity=identity,
                eligibility=eligibility,
                title="Not applicable",
                input=evaluation_input,
                aggregate=aggregate,
                evaluator_model=self.model,
                evaluator_effort=self.effort,
            )
            self.store.save(artifact)
            return artifact

        cwd = _evaluation_cwd(evaluation_input)
        compilation, compiler_invocation = self._compiler.compile(
            evaluation_input=evaluation_input,
            eligibility=eligibility,
            cwd=cwd,
        )
        rubric = FrozenRubric(
            rubric_version=RUBRIC_VERSION,
            origin="retrospective",
            provenance_confidence=0.7,
            frozen_at=_now(),
            criteria=compilation.criteria,
        )
        semantic, evaluator_invocation = self._semantic.evaluate(
            evaluation_input=evaluation_input,
            rubric=rubric,
            cwd=cwd,
        )
        validation_plan = self._plan_builder.build(
            proposals=compilation.proposed_validations,
            rubric=rubric,
            evaluation_input=evaluation_input,
        )
        executable_results = self._runner.run(
            plan=validation_plan,
            checkout=evaluation_input.checkout,
        )
        aggregate = self._aggregator.aggregate(
            eligibility=eligibility,
            rubric=rubric,
            semantic=semantic,
            executable=executable_results,
        )
        artifact = EvaluationArtifact(
            identity=identity,
            eligibility=eligibility,
            title=compilation.title,
            category=compilation.category,
            difficulty=compilation.difficulty,
            rubric=rubric,
            input=evaluation_input,
            turn_contributions=compilation.turn_contributions,
            validation_plan=validation_plan,
            semantic_evaluation=semantic,
            executable_results=executable_results,
            aggregate=aggregate,
            compiler_invocation=compiler_invocation,
            evaluator_invocation=evaluator_invocation,
            evaluator_model=self.model,
            evaluator_effort=self.effort,
        )
        self.store.save(artifact)
        return artifact


def _compiler_prompt(
    evaluation_input: EvaluationInput, eligibility: EligibilityResult
) -> str:
    return "\n".join(
        [
            "You are compiling a retrospective coding-session evaluation rubric.",
            "Use only the bounded evidence package below. Do not inspect the checkout, call tools, or infer missing facts.",
            "Classify the requested outcome, not the techniques used. A turn cannot use mixed as its primary category.",
            "Produce 2 to 6 concrete criteria. Freeze requirements from the requests and material follow-ups before judging outcomes.",
            "Use executable or both only for a named observable postcondition supported by recorded validation or repository instructions.",
            "Proposed validations must be argument arrays, network-free, read-only or local builds, and cite evidence IDs that contain the command authority.",
            "For session scope, include every selected turn exactly once as critical, supporting, exploratory, or superseded.",
            "Return JSON matching the supplied schema.",
            "",
            "Eligibility classifier:",
            eligibility.model_dump_json(indent=2),
            "",
            "Bounded evidence package:",
            evaluation_input.model_dump_json(indent=2),
        ]
    )


def _semantic_prompt(
    evaluation_input: EvaluationInput, rubric: FrozenRubric
) -> str:
    criteria = [
        criterion
        for criterion in rubric.criteria
        if criterion.mechanism in {"semantic", "both"}
    ]
    return "\n".join(
        [
            "You are the independent semantic judge for a coding-session evaluation.",
            "Judge only the frozen rubric and bounded observable evidence below. Do not inspect the checkout or call tools.",
            "Return one result for every semantic or both criterion and no result for executable-only or human-optional criteria.",
            "Every pass or fail must cite at least one valid evidence_id. Use unknown when evidence is insufficient.",
            "Treat final-answer claims as claims, not proof. Identify contradictions explicitly. Do not choose the final session resolution.",
            "Return JSON matching the supplied schema.",
            "",
            "Frozen semantic criteria:",
            json.dumps(
                [criterion.model_dump(mode="json") for criterion in criteria],
                ensure_ascii=False,
                indent=2,
            ),
            "",
            "Bounded evidence package:",
            evaluation_input.model_dump_json(indent=2),
        ]
    )


def _validate_compilation(
    compilation: RubricCompilation, evaluation_input: EvaluationInput
) -> None:
    criterion_ids = [criterion.criterion_id for criterion in compilation.criteria]
    if len(criterion_ids) != len(set(criterion_ids)):
        raise ValueError("rubric compiler returned duplicate criterion IDs")
    if evaluation_input.scope_type == "turn" and compilation.category.primary == "mixed":
        raise ValueError("turn evaluation cannot use mixed as its primary category")
    selected_turn_ids = set(evaluation_input.selected_turn_ids)
    contribution_ids = {
        contribution.turn_id for contribution in compilation.turn_contributions
    }
    if evaluation_input.scope_type == "session":
        if contribution_ids != selected_turn_ids:
            raise ValueError(
                "session rubric must classify every selected turn contribution exactly once"
            )
    elif compilation.turn_contributions:
        if contribution_ids != selected_turn_ids:
            raise ValueError("turn contribution must reference the selected turn")


def _validate_semantic_evaluation(
    semantic: SemanticEvaluation,
    rubric: FrozenRubric,
    evaluation_input: EvaluationInput,
) -> None:
    expected = {
        criterion.criterion_id
        for criterion in rubric.criteria
        if criterion.mechanism in {"semantic", "both"}
    }
    returned = [result.criterion_id for result in semantic.criterion_results]
    if len(returned) != len(set(returned)) or set(returned) != expected:
        raise ValueError(
            "semantic evaluator must return exactly one result for each semantic criterion"
        )
    evidence_ids = {record.evidence_id for record in evaluation_input.evidence}
    for result in semantic.criterion_results:
        if not set(result.evidence_ids).issubset(evidence_ids):
            raise ValueError(
                f"semantic result references unknown evidence: {result.criterion_id}"
            )
        if result.result in {"pass", "fail"} and not result.evidence_ids:
            raise ValueError(
                f"semantic {result.result} requires evidence: {result.criterion_id}"
            )


def _safe_validation_argv(argv: list[str]) -> bool:
    if not argv or any(
        not arg
        or "\n" in arg
        or arg in {"&&", "||", ";", "|", ">", ">>", "<"}
        for arg in argv
    ):
        return False
    command = tuple(argv)
    if command[:3] == ("git", "diff", "--check"):
        return True
    if command[:3] == ("uv", "run", "ruff") and "check" in command[3:5]:
        return True
    if command[:5] == ("uv", "run", "python", "-m", "compileall"):
        return True
    if command[:3] == ("uv", "run", "ct") and "validate" in command[3:]:
        return True
    if command[:3] == ("uv", "run", "python") and len(command) >= 4:
        return command[3] == "scripts/validate-metrics-baselines.py"
    if command[0] in {"bun", "npm", "pnpm"} and len(command) >= 3:
        return command[1] == "run" and command[2] in {
            "build",
            "check",
            "lint",
            "typecheck",
            "type-check",
        }
    if len(command) == 1 and command[0].endswith(
        "scripts/check-metrics-quality-gate.sh"
    ):
        return True
    return False


def _run_result(
    spec: ValidationSpecification,
    *,
    status: ExecutableState,
    started_at: str,
    output: str,
    reason: str,
    exit_code: int | None = None,
) -> ExecutableRunResult:
    return ExecutableRunResult(
        validation_id=spec.validation_id,
        status=status,
        argv=spec.argv,
        cwd=spec.cwd,
        started_at=started_at,
        ended_at=_now(),
        exit_code=exit_code,
        output_head=_truncate(output[:2_000], 2_000),
        output_tail=_truncate(output[-2_000:], 2_000),
        output_sha256=hashlib.sha256(output.encode()).hexdigest(),
        supports_criteria=spec.supports_criteria,
        reason=reason,
    )


def _completed_output(stdout: Any, stderr: Any) -> str:
    parts: list[str] = []
    for value in [stdout, stderr]:
        if isinstance(value, bytes):
            value = value.decode(errors="replace")
        if isinstance(value, str) and value:
            parts.append(value)
    return "\n".join(parts)


def _runner_environment() -> dict[str, str]:
    allowed = ["PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TERM"]
    return {key: os.environ[key] for key in allowed if key in os.environ}


def _aggregate_criterion(
    criterion: RubricCriterion,
    semantic: SemanticCriterionResult | None,
    executable: list[ExecutableRunResult],
) -> AggregatedCriterionResult:
    if criterion.mechanism == "human_optional":
        return AggregatedCriterionResult(
            criterion_id=criterion.criterion_id,
            required=criterion.required,
            weight=criterion.weight,
            mechanism=criterion.mechanism,
            result="not_applicable",
            reason="human-optional criteria do not block automatic resolution",
        )
    if any(run.status == "fail" for run in executable):
        result: CriterionState = "fail"
        reason = "an executable check failed the declared postcondition"
    elif criterion.mechanism == "executable":
        if executable and all(run.status == "pass" for run in executable):
            result = "pass"
            reason = "all linked executable postconditions passed"
        else:
            result = "unknown"
            reason = "required executable evidence is unavailable"
    elif semantic is None:
        result = "unknown"
        reason = "semantic evidence is unavailable"
    elif semantic.result == "fail":
        result = "fail"
        reason = semantic.reason
    elif criterion.mechanism == "both":
        if semantic.result in {"partial", "unknown"}:
            result = semantic.result
            reason = semantic.reason
        elif executable and all(run.status == "pass" for run in executable):
            result = "pass"
            reason = "semantic judgment and executable postcondition both passed"
        else:
            result = "pass"
            reason = "semantic judgment passed; executable proof is unavailable"
    else:
        result = semantic.result
        reason = semantic.reason
    return AggregatedCriterionResult(
        criterion_id=criterion.criterion_id,
        required=criterion.required,
        weight=criterion.weight,
        mechanism=criterion.mechanism,
        result=result,
        semantic_result=semantic,
        executable_results=executable,
        reason=reason,
    )


def _identity(
    *, scope_type: ScopeType, scope_id: str, source_fingerprint: str
) -> EvaluationIdentity:
    stable = "\n".join(
        [
            scope_type,
            scope_id,
            source_fingerprint,
            RUBRIC_VERSION,
            EVALUATOR_VERSION,
            EVALUATOR_MODEL,
            EVALUATOR_EFFORT,
        ]
    )
    evaluation_id = f"eval_{hashlib.sha256(stable.encode()).hexdigest()[:24]}"
    return EvaluationIdentity(
        evaluation_id=evaluation_id,
        scope_type=scope_type,
        scope_id=scope_id,
        source_fingerprint=source_fingerprint,
        rubric_version=RUBRIC_VERSION,
        evaluator_version=EVALUATOR_VERSION,
        created_at=_now(),
    )


def _fingerprint(evaluation_input: EvaluationInput) -> str:
    payload = evaluation_input.model_dump(mode="json")
    normalized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(normalized.encode()).hexdigest()


def _evaluation_cwd(evaluation_input: EvaluationInput) -> Path:
    if evaluation_input.project_path:
        project_path = Path(evaluation_input.project_path)
        if project_path.is_dir():
            return project_path
    return _repo_root()


def _atomic_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(value)
    temporary.replace(path)


def _git_output(
    cwd: Path, argv: list[str], *, preserve_empty: bool = False
) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *argv],
            cwd=cwd,
            check=False,
            text=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    if preserve_empty:
        return value
    return value or None


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(limit - 1, 0)].rstrip() + "…"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _default_ct_json(args: list[str]) -> Any:
    ct = os.environ.get("CT_COMMAND") or shutil.which("ct")
    if not ct:
        raise RuntimeError("ct executable not found; set CT_COMMAND to the ct command path")
    completed = subprocess.run(
        [*shlex.split(ct), *args],
        cwd=_repo_root(),
        check=False,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.strip()
            or completed.stdout.strip()
            or "ct command failed"
        )
    return json.loads(completed.stdout)


def render_evaluation(artifact: EvaluationArtifact, *, store: EvaluationStore) -> str:
    category = artifact.category.primary if artifact.category else "not_applicable"
    lines = [
        f"Evaluation {artifact.identity.evaluation_id}",
        f"Scope: {artifact.identity.scope_type} {artifact.identity.scope_id}",
        f"Title: {artifact.title}",
        f"Category: {category}",
        f"Resolution: {artifact.aggregate.resolution}",
        f"Rubric score: {artifact.aggregate.rubric_score:.1%}",
        f"Evidence coverage: {artifact.aggregate.evidence_coverage:.1%}",
    ]
    if artifact.aggregate.criteria:
        lines.append("Criteria:")
        lines.extend(
            f"  {criterion.result.upper():7} {criterion.criterion_id}: {criterion.reason}"
            for criterion in artifact.aggregate.criteria
        )
    lines.append(f"Artifact: {store.artifact_path(artifact.identity.evaluation_id)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ct plugin dashboard session evaluate",
        description="Run the lightweight evaluation for a canonical session or turn.",
    )
    parser.add_argument("session_id", help="Canonical session ID to evaluate.")
    parser.add_argument(
        "--turn",
        dest="turn_id",
        default=None,
        help="Evaluate one turn from the resolved session graph.",
    )
    parser.add_argument(
        "--output", "-o", choices=("text", "json"), default="text"
    )
    parser.add_argument("--store-root", default=None)
    args = parser.parse_args(argv)

    store = EvaluationStore(
        Path(args.store_root).expanduser() if args.store_root else None
    )
    manager = CodexAppServerManager(cwd=_repo_root())
    try:
        service = EvaluationService(
            ct_json=_default_ct_json,
            app_server=manager,
            store=store,
        )
        scope_type: ScopeType = "turn" if args.turn_id else "session"
        scope_id = args.turn_id or args.session_id
        artifact = service.evaluate(scope_type=scope_type, scope_id=scope_id)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        manager.close()
    if args.output == "json":
        print(artifact.model_dump_json(indent=2))
    else:
        print(render_evaluation(artifact, store=store))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
