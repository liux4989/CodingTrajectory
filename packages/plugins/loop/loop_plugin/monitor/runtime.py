"""Monitor strategy execution over local Core evidence.

Dry-run evaluates bounded historical evidence through frozen Core methods.
Refresh evaluates newly observed sessions discovered through the Core
`living.sessions` change feed. Both paths are idempotent for the same watch
revision, input scope, and observed evidence; changed evidence supersedes the
previous evaluation instead of rewriting it.

There is no remote fallback: every Core call uses the local in-process runtime
with the ``local`` connection profile.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from uuid import uuid4

from coding_trajectory.runtime import ServiceRuntime

from loop_plugin.models import CanonicalReference
from loop_plugin.monitor.models import (
    DryRunResult,
    Evaluation,
    EvaluationSummary,
    EvaluatorSpec,
    Finding,
    FindingStatusEvent,
    RefreshResult,
    Watch,
)
from loop_plugin.monitor.store import MonitorStore
from loop_plugin.monitor.strategies import evaluate_turn, fingerprint_turn_evidence


def _now() -> datetime:
    return datetime.now(UTC)


def _idempotency_key(
    *, watch: Watch, trigger: str, session_id: str, turn_id: str, fingerprint: str
) -> str:
    payload = {
        "watch_id": str(watch.watch_id),
        "config_revision": watch.config_revision,
        "trigger": trigger,
        "session_id": session_id,
        "turn_id": turn_id,
        "fingerprint": fingerprint,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _finding_dedupe_key(*, watch: Watch, session_id: str, turn_id: str) -> str:
    payload = {
        "watch_id": str(watch.watch_id),
        "config_revision": watch.config_revision,
        "session_id": session_id,
        "turn_id": turn_id,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class CoreFacade:
    """One local Core runtime per Monitor run; no remote fallback exists."""

    def __init__(self):
        self._runtime = ServiceRuntime(
            global_scope=True,
            current_dir=Path.cwd(),
        )

    def __enter__(self) -> Self:
        self._runtime.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self._runtime.__exit__(*exc)

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        reply = self._runtime.execute({"method": method, "params": params})
        if not reply.get("ok"):
            error = reply.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise MonitorCoreError(method, str(message))
        return reply["result"]

    def view_hash(self) -> str | None:
        return ((self._runtime.transport_metadata() or {}).get("identity") or {}).get(
            "view_manifest_sha256"
        )


class MonitorCoreError(RuntimeError):
    def __init__(self, method: str, message: str):
        super().__init__(f"Core {method} failed: {message}")
        self.method = method
        self.message = message


def _turn_rows(usage: dict[str, Any]) -> list[dict[str, Any]]:
    turns = usage.get("turns")
    return [turn for turn in turns if isinstance(turn, dict)] if turns else []


def _request_counts(ledger: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    requests = ledger.get("requests")
    for request in requests if isinstance(requests, list) else []:
        if isinstance(request, dict) and isinstance(request.get("turn_id"), str):
            counts[request["turn_id"]] = counts.get(request["turn_id"], 0) + 1
    return counts


def _evaluate_session_turns(
    *,
    core: CoreFacade,
    store: MonitorStore,
    watch: Watch,
    session_id: str,
    trigger: str,
    summary: EvaluationSummary,
    emit_findings: bool,
) -> tuple[list[Evaluation], list[Finding]]:
    """Evaluate every turn Core reports for one session graph, idempotently."""
    started = _now()
    written: list[Evaluation] = []
    findings: list[Finding] = []
    usage = core.call("session.usage", {"session_id": session_id})
    view_hash = core.view_hash()
    ledger = core.call(
        "session.request_usage",
        {"session_id": session_id, "view_manifest_sha256": view_hash},
    )
    counts = _request_counts(ledger)
    measurement_coverage = usage.get("measurement_coverage")
    warnings = usage.get("warnings") or []
    summary.sessions_examined += 1
    for turn in _turn_rows(usage):
        turn_id = turn.get("turn_id")
        turn_session = turn.get("session_id") or usage.get("session_id")
        if not isinstance(turn_id, str) or not isinstance(turn_session, str):
            continue
        runtime = turn.get("runtime") if isinstance(turn.get("runtime"), dict) else {}
        turn_usage = turn.get("usage")
        request_count = counts.get(turn_id, 0)
        turn_ended = bool(runtime.get("ended_at"))
        fingerprint = fingerprint_turn_evidence(
            turn_usage=turn_usage if isinstance(turn_usage, dict) else None,
            request_count=request_count,
            turn_ended=turn_ended,
        )
        key = _idempotency_key(
            watch=watch,
            trigger=trigger,
            session_id=turn_session,
            turn_id=turn_id,
            fingerprint=fingerprint,
        )
        condition, state, result, reason, coverage = evaluate_turn(
            config=watch.config,
            turn_usage=turn_usage if isinstance(turn_usage, dict) else None,
            request_count=request_count,
            turn_ended=turn_ended,
            measurement_coverage=measurement_coverage,
        )
        if warnings:
            coverage["warnings"] = warnings
        evaluation = Evaluation(
            evaluation_id=uuid4(),
            idempotency_key=key,
            watch_id=watch.watch_id,
            strategy_id=watch.strategy_id,
            strategy_version=watch.strategy_version,
            config_revision=watch.config_revision,
            trigger=trigger,  # type: ignore[arg-type]
            reference=CanonicalReference(
                session_id=turn_session, turn_id=turn_id, view_manifest_sha256=view_hash
            ),
            state=state,  # type: ignore[arg-type]
            result=result,  # type: ignore[arg-type]
            condition=condition,
            coverage=coverage,
            reason=reason,
            evaluator=EvaluatorSpec(),
            evidence_fingerprint=fingerprint,
            started_at=started,
            completed_at=_now(),
        )
        previous = store.latest_evaluation(
            watch_id=watch.watch_id,
            config_revision=watch.config_revision,
            trigger=trigger,
            session_id=turn_session,
            turn_id=turn_id,
        )
        if previous is not None and previous.idempotency_key == key:
            summary.skipped_unchanged += 1
            continue
        if not store.insert_evaluation(evaluation):
            summary.skipped_unchanged += 1
            continue
        if previous is not None:
            store.mark_superseded(previous.evaluation_id, evaluation.evaluation_id)
        summary.turns_evaluated += 1
        if state == "completed" and result == "pass":
            summary.passed += 1
        elif state == "completed" and result == "breach":
            summary.breached += 1
        elif state == "unavailable":
            summary.unavailable += 1
        elif state == "pending":
            summary.pending += 1
        written.append(evaluation)
        if result == "breach":
            finding = _finding_for(watch=watch, evaluation=evaluation)
            if emit_findings and watch.config.emit_findings:
                created = store.insert_finding(finding)
                if not created:
                    # A finding already exists for this watch revision and turn
                    # (any lifecycle status); link it without resurrecting a
                    # human's triage decision.
                    existing = store.finding_for_dedupe(finding.dedupe_key)
                    finding = existing if existing is not None else finding
                store.attach_finding(evaluation.evaluation_id, finding.finding_id)
                evaluation = evaluation.model_copy(
                    update={"finding_id": finding.finding_id}
                )
                written[-1] = evaluation
                if created:
                    findings.append(finding)
            else:
                findings.append(finding)  # prospective preview only
    return written, findings


def _finding_for(*, watch: Watch, evaluation: Evaluation) -> Finding:
    now = _now()
    reference = evaluation.reference
    return Finding(
        finding_id=uuid4(),
        dedupe_key=_finding_dedupe_key(
            watch=watch,
            session_id=reference.session_id,
            turn_id=reference.turn_id or "",
        ),
        watch_id=watch.watch_id,
        evaluation_id=evaluation.evaluation_id,
        strategy_id=watch.strategy_id,
        strategy_version=watch.strategy_version,
        config_revision=watch.config_revision,
        severity=watch.config.severity,
        title=(f"Turn token budget exceeded: {evaluation.condition.summary}")[:256],
        condition=evaluation.condition,
        reference=reference,
        created_at=now,
        updated_at=now,
        status_history=[FindingStatusEvent(status="open", at=now)],
    )


def resolve_scope_sessions(core: CoreFacade, watch: Watch) -> tuple[list[str], int]:
    """Resolve a watch scope to graph entrypoint IDs via Core inventory.

    Returns (entrypoint_ids, total_in_inventory) over the pinned inventory pages.
    """
    if watch.scope.session_id:
        return [watch.scope.session_id], 1
    params = (
        {"project_name": watch.scope.project_name} if watch.scope.project_name else {}
    )
    items = []
    while True:
        result = core.call("project.sessions", params)
        items.extend(result.get("items") or [])
        if not result.get("next_cursor"):
            break
        params["cursor"] = result["next_cursor"]
    entrypoints = [
        item["root_session_id"]
        for item in items
        if isinstance(item, dict) and isinstance(item.get("root_session_id"), str)
    ]
    return entrypoints, len(entrypoints)


def dry_run(store: MonitorStore, watch: Watch, *, max_sessions: int) -> DryRunResult:
    """Bounded historical evaluation. Labeled; never persists findings."""
    summary = EvaluationSummary()
    evaluations: list[Evaluation] = []
    prospective: list[Finding] = []
    notes: list[str] = []
    with CoreFacade() as core:
        entrypoints, total = resolve_scope_sessions(core, watch)
        selected = entrypoints[:max_sessions]
        truncated = len(entrypoints) > len(selected)
        if truncated:
            notes.append(
                f"Scope inventory has {total} session(s); dry-run evaluated the "
                f"first {len(selected)}. Narrow the scope or increase the dry-run limit."
            )
        for entrypoint in selected:
            written, previews = _evaluate_session_turns(
                core=core,
                store=store,
                watch=watch,
                session_id=entrypoint,
                trigger="historical_dry_run",
                summary=summary,
                emit_findings=False,
            )
            evaluations.extend(written)
            prospective.extend(previews)
    if not summary.sessions_examined:
        notes.append("No sessions matched the configured scope.")
    notes.append(
        "Dry-run evaluations are labeled historical_dry_run and never emit "
        "live findings; the preview below shows what enabling would flag."
    )
    return DryRunResult(
        watch=watch,
        evaluations=evaluations,
        summary=summary,
        prospective_findings=prospective,
        scope_sessions_total=total,
        truncated=truncated,
        notes=notes,
    )


def refresh(store: MonitorStore, watch: Watch, *, max_sessions: int) -> RefreshResult:
    """Evaluate newly observed sessions from the local living.sessions feed."""
    summary = EvaluationSummary()
    evaluations: list[Evaluation] = []
    findings: list[Finding] = []
    notes: list[str] = []
    state = watch.refresh
    after = state.cursor
    through = state.watermark if after else None
    remaining = False
    processed = 0
    with CoreFacade() as core:
        member_ids: set[str] | None = None
        if watch.scope.project_name:
            try:
                entrypoints, _total = resolve_scope_sessions(core, watch)
            except MonitorCoreError as exc:
                entrypoints = []
                notes.append(f"Project scope resolved to no sessions: {exc.message}")
            member_ids = set(entrypoints)
        while True:
            page = core.call(
                "living.sessions",
                {
                    "limit": 200,
                    **({"after": after} if after else {}),
                    **({"through": through} if through else {}),
                },
            )
            watermark = page.get("through")
            changes = page.get("changes") or []
            for change in changes:
                if processed >= max_sessions:
                    remaining = True
                    break
                resource = change.get("resource") or {}
                session_id = resource.get("session_id")
                root_id = resource.get("root_session_id")
                if member_ids is not None:
                    # Project scope: the change belongs to an in-scope graph.
                    # Evaluate the graph entrypoint so member turns are covered.
                    in_scope = root_id in member_ids or session_id in member_ids
                    target = root_id if root_id in member_ids else session_id
                else:
                    # Session scope: the scoped session (or a graph rooted at
                    # it) changed. Evaluate the scoped session itself.
                    in_scope = (
                        session_id == watch.scope.session_id
                        or root_id == watch.scope.session_id
                    )
                    target = watch.scope.session_id
                if change.get("operation") == "upsert" and in_scope and target:
                    processed += 1
                    try:
                        written, emitted = _evaluate_session_turns(
                            core=core,
                            store=store,
                            watch=watch,
                            session_id=target,
                            trigger="manual_refresh",
                            summary=summary,
                            emit_findings=watch.enabled,
                        )
                        evaluations.extend(written)
                        if watch.enabled:
                            findings.extend(emitted)
                    except MonitorCoreError as exc:
                        summary.errored += 1
                        notes.append(str(exc))
                after = change.get("cursor") or after
            if remaining:
                break
            if not page.get("has_more"):
                after = None
                through = watermark
                break
            after = page.get("next_cursor") or after
            through = watermark
        watch.refresh = state.model_copy(
            update={
                "cursor": after,
                "watermark": through,
                "last_run_at": _now(),
                "caught_up": not remaining,
            }
        )
        watch.updated_at = _now()
        store.save_watch(watch)
    if not processed:
        notes.append("No in-scope session changes since the last refresh.")
    if remaining:
        notes.append(
            f"More changed sessions remain after this bounded run "
            f"(max_sessions={max_sessions}); refresh again to continue."
        )
    return RefreshResult(
        watch=watch,
        evaluations=evaluations,
        findings=findings,
        summary=summary,
        remaining=remaining,
        notes=notes,
    )
