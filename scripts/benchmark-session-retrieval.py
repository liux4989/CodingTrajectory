#!/usr/bin/env python3
"""Evaluate session summary and search on deterministic synthetic trajectories.

This benchmark intentionally constructs canonical ``SessionGraph`` models in
memory. It does not read provider logs or claim real-world retrieval quality.
It evaluates projection behavior, structural-ranking ablations, invariants,
response bounds, and warm-store execution cost.

Usage:
    uv run python scripts/benchmark-session-retrieval.py
    uv run python scripts/benchmark-session-retrieval.py --repeat 100
    uv run python scripts/benchmark-session-retrieval.py --no-write
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import re
import statistics
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages" / "core" / "src"))

from coding_trajectory.ingestion.models import (
    AgentMessageItem,
    CommandExecutionItem,
    Event,
    EventType,
    FileChangeItem,
    PlanItem,
    ReasoningItem,
    Session,
    SessionEdge,
    SessionGraph,
    ToolStatus,
    Turn,
    Vendor,
)
from coding_trajectory.query import DocumentStore
from coding_trajectory.service import IndexCache, dispatch

BENCHMARK_NAME = "session-retrieval-synthetic"
SCHEMA_VERSION = 1
DEFAULT_OUTPUT = (
    REPO_ROOT / "benchmarks" / "results" / "session-retrieval-synthetic-v1.json"
)
_TOKEN_RE = re.compile(r"[\w./:@+-]+", re.UNICODE)


class RelevanceJudgment(BaseModel):
    identity: str
    relevance: int = Field(ge=1, le=3)


class SearchCase(BaseModel):
    name: str
    query: str
    mode: Literal["text", "path"] = "text"
    judgments: list[RelevanceJudgment]


class SyntheticFixture(BaseModel):
    graph: SessionGraph
    root_session_id: UUID
    child_session_id: UUID
    first_turn_id: UUID
    second_turn_id: UUID
    ids: dict[str, UUID]
    search_cases: list[SearchCase]


def _uuid(value: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{value:012d}")


def _event(
    *, event_id: UUID, session_id: UUID, timestamp: datetime, text: str
) -> Event:
    return Event(
        event_id=event_id,
        session_id=session_id,
        timestamp=timestamp,
        type=EventType.USER_PROMPT_SUBMITTED,
        vendor_source=Vendor.PI,
        payload={"text": text},
    )


def build_synthetic_fixture() -> SyntheticFixture:
    """Build one canonical graph with deliberate ranking and state transitions."""

    root_session_id = _uuid(1)
    child_session_id = _uuid(2)
    first_turn_id = _uuid(10)
    second_turn_id = _uuid(11)
    child_turn_id = _uuid(12)
    first_request_id = _uuid(20)
    second_request_id = _uuid(21)
    child_request_id = _uuid(22)
    started_at = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

    ids = {
        "decision": _uuid(100),
        "ranking_chatter": _uuid(101),
        "ranking_failure": _uuid(102),
        "first_change": _uuid(103),
        "ranking_recovery": _uuid(104),
        "initial_plan": _uuid(105),
        "private_reasoning": _uuid(106),
        "failed_test": _uuid(110),
        "second_change": _uuid(111),
        "passed_test": _uuid(112),
        "unresolved_check": _uuid(113),
        "tail_output": _uuid(114),
        "self_retrieval": _uuid(115),
        "final_plan": _uuid(116),
        "final_decision": _uuid(117),
    }

    first_event = _event(
        event_id=first_request_id,
        session_id=root_session_id,
        timestamp=started_at,
        text=(
            "Implement the authentication cache, preserve API compatibility, "
            "and retain ranking-signal evidence for evaluation. first-scope"
        ),
    )
    second_event = _event(
        event_id=second_request_id,
        session_id=root_session_id,
        timestamp=started_at + timedelta(minutes=1),
        text="Continue and verify the authentication cache. second-scope",
    )
    child_event = _event(
        event_id=child_request_id,
        session_id=child_session_id,
        timestamp=started_at + timedelta(seconds=30),
        text="Investigate child-secret without widening the parent session.",
    )

    first_items = [
        AgentMessageItem(
            item_id=ids["decision"],
            session_id=root_session_id,
            turn_id=first_turn_id,
            sequence=0,
            started_at=started_at + timedelta(seconds=1),
            status=ToolStatus.COMPLETED.value,
            text="Decision: Keep the public authentication API compatible.",
        ),
        AgentMessageItem(
            item_id=ids["ranking_chatter"],
            session_id=root_session_id,
            turn_id=first_turn_id,
            sequence=1,
            started_at=started_at + timedelta(seconds=2),
            status=ToolStatus.COMPLETED.value,
            text="Narrative note mentioning ranking-signal without changing code.",
        ),
        CommandExecutionItem(
            item_id=ids["ranking_failure"],
            session_id=root_session_id,
            turn_id=first_turn_id,
            sequence=2,
            started_at=started_at + timedelta(seconds=3),
            completed_at=started_at + timedelta(seconds=4),
            status=ToolStatus.FAILED.value,
            command="python -c 'validate_auth_cache()'",
            exit_code=1,
            output="ranking-signal validation failed",
        ),
        FileChangeItem(
            item_id=ids["first_change"],
            session_id=root_session_id,
            turn_id=first_turn_id,
            sequence=3,
            started_at=started_at + timedelta(seconds=5),
            completed_at=started_at + timedelta(seconds=6),
            status=ToolStatus.COMPLETED.value,
            tool_name="apply_patch",
            path="src/auth/cache.py",
            operation="edit",
            input={
                "path": "src/auth/cache.py",
                "patch": "Add authentication cache with ranking-signal marker",
            },
            output="updated src/auth/cache.py",
        ),
        CommandExecutionItem(
            item_id=ids["ranking_recovery"],
            session_id=root_session_id,
            turn_id=first_turn_id,
            sequence=4,
            started_at=started_at + timedelta(seconds=7),
            completed_at=started_at + timedelta(seconds=8),
            status=ToolStatus.COMPLETED.value,
            command="python -c 'validate_auth_cache()'",
            exit_code=0,
            output="validation recovered",
        ),
        PlanItem(
            item_id=ids["initial_plan"],
            session_id=root_session_id,
            turn_id=first_turn_id,
            sequence=5,
            started_at=started_at + timedelta(seconds=9),
            completed_at=started_at + timedelta(seconds=9),
            status=ToolStatus.COMPLETED.value,
            tool_name="update_plan",
            input={
                "todos": [
                    {
                        "content": "Implement authentication cache",
                        "status": "completed",
                    },
                    {"content": "Run authentication tests", "status": "in_progress"},
                ]
            },
        ),
        ReasoningItem(
            item_id=ids["private_reasoning"],
            session_id=root_session_id,
            turn_id=first_turn_id,
            sequence=6,
            started_at=started_at + timedelta(seconds=10),
            text="private-sentinel must never become searchable evidence",
        ),
    ]

    long_output = "head evidence " + ("x" * 17_000) + " tail-sentinel"
    second_items = [
        CommandExecutionItem(
            item_id=ids["failed_test"],
            session_id=root_session_id,
            turn_id=second_turn_id,
            sequence=0,
            started_at=started_at + timedelta(minutes=1, seconds=1),
            completed_at=started_at + timedelta(minutes=1, seconds=2),
            status=ToolStatus.FAILED.value,
            command="uv run pytest tests/test_auth_cache.py",
            exit_code=1,
            output="authentication cache test failed",
        ),
        FileChangeItem(
            item_id=ids["second_change"],
            session_id=root_session_id,
            turn_id=second_turn_id,
            sequence=1,
            started_at=started_at + timedelta(minutes=1, seconds=3),
            completed_at=started_at + timedelta(minutes=1, seconds=4),
            status=ToolStatus.COMPLETED.value,
            tool_name="apply_patch",
            path="src/auth/cache.py",
            operation="edit",
            input={"path": "src/auth/cache.py", "patch": "Stabilize cache keys"},
            output="updated cache key handling",
        ),
        CommandExecutionItem(
            item_id=ids["passed_test"],
            session_id=root_session_id,
            turn_id=second_turn_id,
            sequence=2,
            started_at=started_at + timedelta(minutes=1, seconds=5),
            completed_at=started_at + timedelta(minutes=1, seconds=6),
            status=ToolStatus.COMPLETED.value,
            command="uv run pytest tests/test_auth_cache.py",
            exit_code=0,
            output="1 passed",
        ),
        CommandExecutionItem(
            item_id=ids["unresolved_check"],
            session_id=root_session_id,
            turn_id=second_turn_id,
            sequence=3,
            started_at=started_at + timedelta(minutes=1, seconds=7),
            completed_at=started_at + timedelta(minutes=1, seconds=8),
            status=ToolStatus.FAILED.value,
            command="uv run mypy src/auth",
            exit_code=1,
            output="failed-validation remains unresolved",
        ),
        CommandExecutionItem(
            item_id=ids["tail_output"],
            session_id=root_session_id,
            turn_id=second_turn_id,
            sequence=4,
            started_at=started_at + timedelta(minutes=1, seconds=9),
            completed_at=started_at + timedelta(minutes=1, seconds=10),
            status=ToolStatus.COMPLETED.value,
            command="python -c 'emit_large_output()'",
            exit_code=0,
            output=long_output,
        ),
        CommandExecutionItem(
            item_id=ids["self_retrieval"],
            session_id=root_session_id,
            turn_id=second_turn_id,
            sequence=5,
            started_at=started_at + timedelta(minutes=1, seconds=11),
            completed_at=started_at + timedelta(minutes=1, seconds=12),
            status=ToolStatus.COMPLETED.value,
            command="ct session search 00000000-0000-4000-8000-000000000001 self-sentinel",
            exit_code=0,
            output="self-sentinel retrieval output",
        ),
        PlanItem(
            item_id=ids["final_plan"],
            session_id=root_session_id,
            turn_id=second_turn_id,
            sequence=6,
            started_at=started_at + timedelta(minutes=1, seconds=13),
            completed_at=started_at + timedelta(minutes=1, seconds=13),
            status=ToolStatus.COMPLETED.value,
            tool_name="update_plan",
            input={
                "todos": [
                    {
                        "content": "Implement authentication cache",
                        "status": "completed",
                    },
                    {"content": "Run authentication tests", "status": "completed"},
                ]
            },
        ),
        AgentMessageItem(
            item_id=ids["final_decision"],
            session_id=root_session_id,
            turn_id=second_turn_id,
            sequence=7,
            started_at=started_at + timedelta(minutes=1, seconds=14),
            status=ToolStatus.COMPLETED.value,
            text="We will keep cache keys stable across compatible clients.",
        ),
    ]
    for offset in range(14):
        item_id = _uuid(200 + offset)
        ids[f"recent_{offset}"] = item_id
        second_items.append(
            AgentMessageItem(
                item_id=item_id,
                session_id=root_session_id,
                turn_id=second_turn_id,
                sequence=8 + offset,
                started_at=started_at + timedelta(minutes=1, seconds=15 + offset),
                status=ToolStatus.COMPLETED.value,
                text=f"Bounded recent activity note {offset}.",
            )
        )

    first_turn = Turn(
        turn_id=first_turn_id,
        session_id=root_session_id,
        sequence=0,
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=11),
        user_request_event_id=first_request_id,
        event_ids=[first_request_id],
        items=first_items,
    )
    second_turn = Turn(
        turn_id=second_turn_id,
        session_id=root_session_id,
        sequence=1,
        started_at=started_at + timedelta(minutes=1),
        ended_at=started_at + timedelta(minutes=2),
        user_request_event_id=second_request_id,
        event_ids=[second_request_id],
        items=second_items,
    )
    root_session = Session(
        session_id=root_session_id,
        vendor=Vendor.PI,
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=2),
        cwd="/synthetic/auth-project",
        events=[first_event, second_event],
        turns=[first_turn, second_turn],
    )

    child_turn = Turn(
        turn_id=child_turn_id,
        session_id=child_session_id,
        sequence=0,
        started_at=started_at + timedelta(seconds=30),
        ended_at=started_at + timedelta(seconds=31),
        user_request_event_id=child_request_id,
        event_ids=[child_request_id],
    )
    child_session = Session(
        session_id=child_session_id,
        vendor=Vendor.PI,
        parent_session_id=root_session_id,
        started_at=started_at + timedelta(seconds=30),
        ended_at=started_at + timedelta(seconds=31),
        events=[child_event],
        turns=[child_turn],
    )
    graph = SessionGraph(
        root_session_id=root_session_id,
        project_identifier="synthetic-auth-project",
        sessions=[root_session, child_session],
        edges=[
            SessionEdge(
                type="spawned_subagent",
                source_session_id=root_session_id,
                target_session_id=child_session_id,
                source_turn_id=first_turn_id,
            )
        ],
    )

    def item_identity(kind: str, key: str) -> str:
        return f"{kind}:item:{ids[key]}"

    def event_identity(kind: str, event_id: UUID) -> str:
        return f"{kind}:event:{event_id}"

    search_cases = [
        SearchCase(
            name="structural-ranking",
            query="ranking-signal",
            judgments=[
                RelevanceJudgment(
                    identity=item_identity("tool_result", "ranking_failure"),
                    relevance=3,
                ),
                RelevanceJudgment(
                    identity=item_identity("file_change", "first_change"),
                    relevance=2,
                ),
                RelevanceJudgment(
                    identity=event_identity("user_message", first_request_id),
                    relevance=1,
                ),
                RelevanceJudgment(
                    identity=item_identity("assistant_message", "ranking_chatter"),
                    relevance=1,
                ),
            ],
        ),
        SearchCase(
            name="path-search",
            query="src/auth/cache.py",
            mode="path",
            judgments=[
                RelevanceJudgment(
                    identity=item_identity("file_change", "first_change"), relevance=3
                ),
                RelevanceJudgment(
                    identity=item_identity("file_change", "second_change"), relevance=3
                ),
            ],
        ),
        SearchCase(
            name="tail-preservation",
            query="tail-sentinel",
            judgments=[
                RelevanceJudgment(
                    identity=item_identity("tool_result", "tail_output"), relevance=3
                )
            ],
        ),
        SearchCase(
            name="explicit-decision",
            query="API compatibility",
            judgments=[
                RelevanceJudgment(
                    identity=event_identity("user_message", first_request_id),
                    relevance=3,
                ),
                RelevanceJudgment(
                    identity=item_identity("assistant_message", "decision"),
                    relevance=2,
                ),
            ],
        ),
        SearchCase(
            name="unresolved-failure",
            query="failed-validation",
            judgments=[
                RelevanceJudgment(
                    identity=item_identity("tool_result", "unresolved_check"),
                    relevance=3,
                )
            ],
        ),
    ]
    return SyntheticFixture(
        graph=graph,
        root_session_id=root_session_id,
        child_session_id=child_session_id,
        first_turn_id=first_turn_id,
        second_turn_id=second_turn_id,
        ids=ids,
        search_cases=search_cases,
    )


def _dispatch(
    store: DocumentStore, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    return dispatch(
        method,
        params,
        store=store,
        global_scope=True,
        current_dir=REPO_ROOT,
        discovery_note="synthetic canonical benchmark",
        cache=IndexCache(),
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _match_identity(match: dict[str, Any]) -> str:
    references = match.get("references") or {}
    item_id = references.get("item_id")
    if item_id:
        return f"{match['kind']}:item:{item_id}"
    event_ids = references.get("event_ids") or []
    if event_ids:
        return f"{match['kind']}:event:{event_ids[0]}"
    return f"{match['kind']}:unresolved:{match.get('rank')}"


def _refs_resolve(store: DocumentStore, references: dict[str, Any]) -> bool:
    session_id = UUID(references["session_id"])
    if session_id not in store.sessions:
        return False
    turn_id = references.get("turn_id")
    if turn_id and UUID(turn_id) not in store.turns:
        return False
    item_id = references.get("item_id")
    if item_id and UUID(item_id) not in store.items:
        return False
    return all(
        UUID(event_id) in store.events for event_id in references.get("event_ids", [])
    )


def _summary_references(summary: dict[str, Any]) -> list[dict[str, Any]]:
    references = []
    objective = summary.get("objective")
    if isinstance(objective, dict) and isinstance(objective.get("references"), dict):
        references.append(objective["references"])
    for key in (
        "decisions",
        "changes",
        "verification",
        "unresolved",
        "next_actions",
        "recent_activity",
    ):
        for entry in summary.get(key) or []:
            if isinstance(entry, dict) and isinstance(entry.get("references"), dict):
                references.append(entry["references"])
    return references


def evaluate_summary(fixture: SyntheticFixture, store: DocumentStore) -> dict[str, Any]:
    params = {"session_id": str(fixture.root_session_id)}
    summary = _dispatch(store, "session.summary", params)
    repeated = _dispatch(store, "session.summary", params)
    decision_texts = {entry["text"] for entry in summary["decisions"]}
    verification = [
        (entry["label"], entry["status"]) for entry in summary["verification"]
    ]
    unresolved_ids = {
        entry["references"].get("item_id") for entry in summary["unresolved"]
    }
    expected_operations = next(
        entry["operations"]
        for entry in summary["changes"]
        if entry["path"] == "src/auth/cache.py"
    )
    first_turn_summary = _dispatch(
        store,
        "session.summary",
        {
            "session_id": str(fixture.root_session_id),
            "turn_id": str(fixture.first_turn_id),
        },
    )
    serialized = _canonical_json(summary)
    source_bytes = len(fixture.graph.model_dump_json().encode())
    checks = {
        "latest_material_objective": summary.get("objective", {}).get("text")
        == "Continue and verify the authentication cache. second-scope",
        "explicit_decision_preserved": (
            "Decision: Keep the public authentication API compatible." in decision_texts
        ),
        "later_decision_preserved": (
            "We will keep cache keys stable across compatible clients."
            in decision_texts
        ),
        "changes_merged_by_path": (
            len(summary["changes"]) == 1 and expected_operations == ["edit"]
        ),
        "failed_and_passed_verification_preserved": (
            ("uv run pytest tests/test_auth_cache.py", "failed") in verification
            and ("uv run pytest tests/test_auth_cache.py", "succeeded") in verification
        ),
        "corrected_failure_cleared": str(fixture.ids["ranking_failure"])
        not in unresolved_ids,
        "uncorrected_failure_retained": unresolved_ids
        == {str(fixture.ids["unresolved_check"])},
        "latest_plan_snapshot_wins": summary["next_actions"] == [],
        "turn_scope_selects_objective": first_turn_summary.get("objective", {}).get(
            "text"
        )
        == (
            "Implement the authentication cache, preserve API compatibility, "
            "and retain ranking-signal evidence for evaluation. first-scope"
        ),
        "turn_scope_selects_plan_snapshot": [
            entry["text"] for entry in first_turn_summary["next_actions"]
        ]
        == ["Run authentication tests"],
        "recent_activity_is_bounded": (
            len(summary["recent_activity"]) == 12
            and summary["truncation"]["recent_activity"]["truncated"] is True
        ),
        "private_reasoning_excluded": "private-sentinel" not in serialized,
        "self_retrieval_excluded": "self-sentinel" not in serialized,
        "all_evidence_resolves": all(
            _refs_resolve(store, references)
            for references in _summary_references(summary)
        ),
        "deterministic_response": summary == repeated,
    }
    passed = sum(checks.values())
    return {
        "checks": checks,
        "passed": passed,
        "total": len(checks),
        "score": round(passed / len(checks), 4),
        "response_bytes": len(serialized.encode()),
        "canonical_fixture_bytes": source_bytes,
        "compression_ratio": round(len(serialized.encode()) / source_bytes, 4),
        "truncation": summary["truncation"],
    }


def _tokens(value: str) -> list[str]:
    return [match.group(0).casefold() for match in _TOKEN_RE.finditer(value)]


def _lexical_baseline_score(match: dict[str, Any], query: str) -> int:
    text = f"{match.get('label') or ''} {match.get('snippet') or ''}".casefold()
    return sum(text.count(term) for term in dict.fromkeys(_tokens(query)))


def _rank_ablation(
    matches: list[dict[str, Any]], *, strategy: Literal["current", "lexical", "recent"]
) -> list[str]:
    if strategy == "current":
        ordered = matches
    elif strategy == "lexical":
        ordered = sorted(
            matches,
            key=lambda match: (
                -_lexical_baseline_score(match, str(match["_query"])),
                str(match["timestamp"]),
                _match_identity(match),
            ),
            reverse=False,
        )
    else:
        ordered = sorted(
            matches,
            key=lambda match: (str(match["timestamp"]), _match_identity(match)),
            reverse=True,
        )
    return [_match_identity(match) for match in ordered]


def _ranking_metrics(
    ranked: list[str], judgments: list[RelevanceJudgment], *, k: int = 10
) -> dict[str, float]:
    relevance = {judgment.identity: judgment.relevance for judgment in judgments}
    top = ranked[:k]
    found = {identity for identity in top if identity in relevance}
    relevant_count = len(relevance)
    recall = len(found) / relevant_count if relevant_count else 1.0
    denominator = min(k, len(top))
    precision = len(found) / denominator if denominator else 1.0
    reciprocal_rank = next(
        (
            1 / rank
            for rank, identity in enumerate(ranked, start=1)
            if identity in relevance
        ),
        0.0,
    )

    def dcg(values: list[int]) -> float:
        return sum(
            (2**value - 1) / math.log2(index + 2) for index, value in enumerate(values)
        )

    observed = [relevance.get(identity, 0) for identity in top]
    ideal = sorted(relevance.values(), reverse=True)[:k]
    ideal_dcg = dcg(ideal)
    return {
        "recall_at_10": round(recall, 4),
        "precision_at_returned_10": round(precision, 4),
        "mrr": round(reciprocal_rank, 4),
        "ndcg_at_10": round(dcg(observed) / ideal_dcg if ideal_dcg else 1.0, 4),
    }


def _mean_metrics(rows: list[dict[str, float]]) -> dict[str, float]:
    return {
        key: round(statistics.fmean(row[key] for row in rows), 4) for key in rows[0]
    }


def evaluate_search(fixture: SyntheticFixture, store: DocumentStore) -> dict[str, Any]:
    strategy_rows: dict[str, list[dict[str, float]]] = {
        "current_structural_lexical": [],
        "lexical_snippet_baseline": [],
        "recent_first_baseline": [],
    }
    case_results = []
    all_references_resolve = True
    for case in fixture.search_cases:
        response = _dispatch(
            store,
            "session.search",
            {
                "session_id": str(fixture.root_session_id),
                "query": case.query,
                "mode": case.mode,
                "limit": 50,
            },
        )
        matches = response["matches"]
        all_references_resolve = all_references_resolve and all(
            _refs_resolve(store, match["references"]) for match in matches
        )
        annotated = [{**match, "_query": case.query} for match in matches]
        ranked_by_strategy = {
            "current_structural_lexical": _rank_ablation(annotated, strategy="current"),
            "lexical_snippet_baseline": _rank_ablation(annotated, strategy="lexical"),
            "recent_first_baseline": _rank_ablation(annotated, strategy="recent"),
        }
        metrics_by_strategy = {}
        for strategy, ranked in ranked_by_strategy.items():
            metrics = _ranking_metrics(ranked, case.judgments)
            metrics_by_strategy[strategy] = metrics
            strategy_rows[strategy].append(metrics)
        case_results.append(
            {
                "name": case.name,
                "query": case.query,
                "mode": case.mode,
                "matching_documents": response["total"],
                "coverage": response["coverage"],
                "warnings": response["warnings"],
                "metrics": metrics_by_strategy,
                "current_order": ranked_by_strategy["current_structural_lexical"],
            }
        )

    private = _dispatch(
        store,
        "session.search",
        {"session_id": str(fixture.root_session_id), "query": "private-sentinel"},
    )
    self_retrieval = _dispatch(
        store,
        "session.search",
        {"session_id": str(fixture.root_session_id), "query": "self-sentinel"},
    )
    child_from_root = _dispatch(
        store,
        "session.search",
        {"session_id": str(fixture.root_session_id), "query": "child-secret"},
    )
    child_direct = _dispatch(
        store,
        "session.search",
        {"session_id": str(fixture.child_session_id), "query": "child-secret"},
    )
    wrong_turn = _dispatch(
        store,
        "session.search",
        {
            "session_id": str(fixture.root_session_id),
            "turn_id": str(fixture.second_turn_id),
            "query": "first-scope",
        },
    )
    right_turn = _dispatch(
        store,
        "session.search",
        {
            "session_id": str(fixture.root_session_id),
            "turn_id": str(fixture.first_turn_id),
            "query": "first-scope",
        },
    )
    bounded = _dispatch(
        store,
        "session.search",
        {
            "session_id": str(fixture.root_session_id),
            "query": "ranking-signal",
            "limit": 2,
        },
    )
    kind_filtered = _dispatch(
        store,
        "session.search",
        {
            "session_id": str(fixture.root_session_id),
            "query": "ranking-signal",
            "kinds": ["file_change"],
        },
    )
    deterministic = _dispatch(
        store,
        "session.search",
        {
            "session_id": str(fixture.root_session_id),
            "query": "ranking-signal",
            "limit": 50,
        },
    ) == _dispatch(
        store,
        "session.search",
        {
            "session_id": str(fixture.root_session_id),
            "query": "ranking-signal",
            "limit": 50,
        },
    )
    ranking_case = next(
        case for case in case_results if case["name"] == "structural-ranking"
    )
    tail_case = next(
        case for case in case_results if case["name"] == "tail-preservation"
    )
    expected_first = f"tool_result:item:{fixture.ids['ranking_failure']}"
    expected_second = f"file_change:item:{fixture.ids['first_change']}"
    invariants = {
        "all_evidence_resolves": all_references_resolve,
        "private_reasoning_excluded": private["total"] == 0,
        "self_retrieval_excluded": self_retrieval["total"] == 0,
        "child_scope_isolated": (
            child_from_root["total"] == 0 and child_direct["total"] == 1
        ),
        "turn_scope_isolated": wrong_turn["total"] == 0 and right_turn["total"] == 1,
        "limit_and_truncation_truthful": (
            len(bounded["matches"]) == 2
            and bounded["total"] == 4
            and bounded["truncated"] is True
        ),
        "kind_filter_honored": (
            kind_filtered["total"] == 1
            and kind_filtered["matches"][0]["kind"] == "file_change"
        ),
        "long_tail_match_preserved": tail_case["matching_documents"] == 1,
        "long_field_coverage_truthful": (
            tail_case["coverage"]["content_complete"] is False
            and bool(tail_case["warnings"])
        ),
        "structural_failure_precedes_mutation": ranking_case["current_order"][:2]
        == [expected_first, expected_second],
        "deterministic_response": deterministic,
    }
    aggregates = {
        strategy: _mean_metrics(rows) for strategy, rows in strategy_rows.items()
    }
    return {
        "cases": case_results,
        "aggregate": aggregates,
        "ablation_candidate_set": (
            "All rankers reorder the same documents matched by the current lexical matcher."
        ),
        "invariants": invariants,
        "invariants_passed": sum(invariants.values()),
        "invariants_total": len(invariants),
    }


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(math.ceil(percentile * len(ordered)) - 1, 0)
    return ordered[index]


def evaluate_performance(
    fixture: SyntheticFixture, store: DocumentStore, *, repeat: int
) -> dict[str, Any]:
    cases = {
        "session.summary": (
            "session.summary",
            {"session_id": str(fixture.root_session_id)},
        ),
        "session.search": (
            "session.search",
            {
                "session_id": str(fixture.root_session_id),
                "query": "ranking-signal",
                "limit": 20,
            },
        ),
    }
    results = {}
    for name, (method, params) in cases.items():
        runs_ms = []
        response = None
        for _ in range(repeat):
            started = time.perf_counter_ns()
            response = _dispatch(store, method, params)
            runs_ms.append((time.perf_counter_ns() - started) / 1_000_000)
        assert response is not None
        results[name] = {
            "repeat": repeat,
            "median_ms": round(statistics.median(runs_ms), 4),
            "p95_ms": round(_percentile(runs_ms, 0.95), 4),
            "min_ms": round(min(runs_ms), 4),
            "max_ms": round(max(runs_ms), 4),
            "response_bytes": len(_canonical_json(response).encode()),
        }
    return {
        "diagnostic_only": True,
        "note": "Synthetic warm-store timings are not a real-data performance gate.",
        "measurements": results,
    }


def evaluate(*, repeat: int) -> dict[str, Any]:
    fixture = build_synthetic_fixture()
    store = DocumentStore.from_session_graphs([fixture.graph])
    summary = evaluate_summary(fixture, store)
    search = evaluate_search(fixture, store)
    performance = evaluate_performance(fixture, store, repeat=repeat)
    current = search["aggregate"]["current_structural_lexical"]
    lexical = search["aggregate"]["lexical_snippet_baseline"]
    gates = {
        "summary_behavior": summary["score"] == 1.0,
        "search_invariants": search["invariants_passed"] == search["invariants_total"],
        "search_recall_at_10": current["recall_at_10"] >= 0.9,
        "search_mrr": current["mrr"] >= 0.85,
        "search_ndcg_at_10": current["ndcg_at_10"] >= 0.9,
        "structural_not_worse_than_lexical": current["ndcg_at_10"]
        >= lexical["ndcg_at_10"],
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": BENCHMARK_NAME,
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": {
            "data": "synthetic canonical SessionGraph only",
            "provider_logs": False,
            "semantic_ranking": False,
            "ablation_scope": "ranking only; candidate generation is held constant",
            "claim": "behavioral evaluation, not real-world retrieval quality",
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "fixture": {
            "sessions": len(fixture.graph.sessions),
            "turns": sum(len(session.turns) for session in fixture.graph.sessions),
            "items": sum(
                len(turn.items)
                for session in fixture.graph.sessions
                for turn in session.turns
            ),
            "search_cases": len(fixture.search_cases),
        },
        "summary": summary,
        "search": search,
        "performance": performance,
        "gates": gates,
        "passed": all(gates.values()),
    }


def _print_report(report: dict[str, Any]) -> None:
    summary = report["summary"]
    search = report["search"]
    print("Synthetic session retrieval benchmark")
    print("=" * 45)
    print(f"summary behavior: {summary['passed']}/{summary['total']}")
    print(f"summary compression ratio: {summary['compression_ratio']:.1%}")
    print(
        f"search invariants: {search['invariants_passed']}/{search['invariants_total']}"
    )
    print("\nRanking ablations")
    for strategy, metrics in search["aggregate"].items():
        print(
            f"  {strategy:30} "
            f"recall@10={metrics['recall_at_10']:.3f} "
            f"MRR={metrics['mrr']:.3f} nDCG@10={metrics['ndcg_at_10']:.3f}"
        )
    print("\nWarm-store diagnostics")
    for method, measurement in report["performance"]["measurements"].items():
        print(
            f"  {method:20} median={measurement['median_ms']:.3f}ms "
            f"p95={measurement['p95_ms']:.3f}ms bytes={measurement['response_bytes']}"
        )
    print("\nGates")
    for name, passed in report["gates"].items():
        print(f"  {'PASS' if passed else 'FAIL'} {name}")
    print(f"\nOverall: {'PASS' if report['passed'] else 'FAIL'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repeat",
        type=int,
        default=30,
        help="Warm-store timing repetitions per method (default: 30).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"JSON report path (default: {DEFAULT_OUTPUT.relative_to(REPO_ROOT)}).",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Evaluate without writing the JSON report.",
    )
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be a positive integer")

    report = evaluate(repeat=args.repeat)
    _print_report(report)
    if not args.no_write:
        output = args.output
        if not output.is_absolute():
            output = REPO_ROOT / output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Report: {output}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
