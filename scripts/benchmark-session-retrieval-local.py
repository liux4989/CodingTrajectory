#!/usr/bin/env python3
"""Evaluate session summary and search on the real local session corpus.

This benchmark extends ``scripts/benchmark-session-retrieval.py`` from the
synthetic fixture to the local provider logs on this machine (Codex, Claude
Code, and Pi). The local corpus has no human relevance judgments, so ranking
quality (nDCG/MRR) remains a synthetic-benchmark claim. What this benchmark
measures at real scale:

- corpus size and cold store-build cost;
- summary invariants: bounded sections, truthful truncation, evidence
  references that resolve, private-reasoning and self-retrieval exclusion,
  objective selection, deterministic responses;
- search invariants: truthful totals and limits, kind/turn scoping, rank
  ordering, bounded snippets, reference resolution, determinism;
- exact recall/precision against derived ground truth: queries built from the
  canonical model itself (a changed path, a request token, a failed command
  phrase) whose expected matches are computable from first principles;
- warm-store execution cost distribution across many real sessions.

Usage:
    uv run python scripts/benchmark-session-retrieval-local.py
    uv run python scripts/benchmark-session-retrieval-local.py --max-sessions 400
    uv run python scripts/benchmark-session-retrieval-local.py --vendor codex
    uv run python scripts/benchmark-session-retrieval-local.py --since-days 30
    uv run python scripts/benchmark-session-retrieval-local.py --no-write
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import random
import re
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages" / "core" / "src"))

from coding_trajectory.analysis.projection_utils import truncate_text_preview
from coding_trajectory.analysis.request_lineage import extract_user_request
from coding_trajectory.analysis.session_retrieval import (
    _SEARCH_SNIPPET_LIMIT,
    _SUMMARY_LIMITS,
    _SUMMARY_TEXT_LIMIT,
    _is_retrieval_item,
    _low_value_request,
)
from coding_trajectory.ingestion.indexes import build_session_graph_index
from coding_trajectory.ingestion.models import (
    CommandExecutionItem,
    FileChangeItem,
    ReasoningItem,
    Session,
)
from coding_trajectory.query import DocumentStore
from coding_trajectory.service import IndexCache, dispatch, resolve_store

BENCHMARK_NAME = "session-retrieval-local"
SCHEMA_VERSION = 1
DEFAULT_OUTPUT = (
    REPO_ROOT / "benchmarks" / "results" / "session-retrieval-local-v1.json"
)
SAMPLE_SEED = 20260901
_MAX_FAILURE_EXAMPLES = 10
_SINGLE_TOKEN_PATH_RE = re.compile(r"^[\w./:@+-]{4,200}$")
_TOKEN_RE = re.compile(r"[\w./:@+-]+", re.UNICODE)


class CheckLog:
    """Pass/fail counters with a bounded set of failure examples."""

    def __init__(self) -> None:
        self.passed: dict[str, int] = {}
        self.failed: dict[str, int] = {}
        self.examples: dict[str, list[str]] = {}

    def record(self, name: str, ok: bool, example: str | None = None) -> None:
        bucket = self.passed if ok else self.failed
        bucket[name] = bucket.get(name, 0) + 1
        if not ok and example is not None:
            entries = self.examples.setdefault(name, [])
            if len(entries) < _MAX_FAILURE_EXAMPLES:
                entries.append(example)

    def report(self) -> dict[str, Any]:
        names = sorted(set(self.passed) | set(self.failed))
        return {
            name: {
                "passed": self.passed.get(name, 0),
                "failed": self.failed.get(name, 0),
                "failure_examples": self.examples.get(name, []),
            }
            for name in names
        }

    def all_passed(self, prefix: str) -> bool:
        return all(
            count == 0
            for name, count in self.failed.items()
            if name.startswith(prefix)
        )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalize(value: str) -> str:
    return " ".join(value.split())


def _tokens(value: str) -> list[str]:
    return [match.group(0).casefold() for match in _TOKEN_RE.finditer(value)]


def _refs_resolve(store: DocumentStore, references: dict[str, Any]) -> bool:
    session_id = references.get("session_id")
    if not session_id or UUID(session_id) not in store.sessions:
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
    for key in _SUMMARY_LIMITS:
        for entry in summary.get(key) or []:
            if isinstance(entry, dict) and isinstance(entry.get("references"), dict):
                references.append(entry["references"])
    return references


def _match_stable_id(match: dict[str, Any]) -> str:
    references = match.get("references") or {}
    return str(
        references.get("item_id") or (references.get("event_ids") or [""])[0]
    )


def _rank_order_ok(matches: list[dict[str, Any]]) -> bool:
    keys = []
    for match in matches:
        timestamp = match.get("timestamp") or ""
        keys.append(
            (-float(match.get("score") or 0.0), str(timestamp), _match_stable_id(match))
        )
    return keys == sorted(keys)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(math.ceil(percentile * len(ordered)) - 1, 0)
    return ordered[index]


def _distribution(values: list[float]) -> dict[str, float]:
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "median": round(statistics.median(values), 4),
        "p95": round(_percentile(values, 0.95), 4),
        "max": round(max(values), 4),
    }


def _session_sort_key(session: Session) -> str:
    return str(session.session_id)


def select_sessions(store: DocumentStore, max_sessions: int) -> list[Session]:
    """Deterministic seeded sample plus the largest sessions by item count."""
    sessions = sorted(store.sessions.values(), key=_session_sort_key)
    if max_sessions < 1:
        return sessions
    sample_size = min(max_sessions, len(sessions))
    sampled = random.Random(SAMPLE_SEED).sample(sessions, sample_size)
    by_items = sorted(
        sessions,
        key=lambda session: (
            -sum(len(turn.items) for turn in session.turns),
            str(session.session_id),
        ),
    )
    selected = {session.session_id: session for session in sampled}
    for session in by_items[:10]:
        selected.setdefault(session.session_id, session)
    return sorted(selected.values(), key=_session_sort_key)


def _turn_request(index: Any, session: Session, turn: Any) -> dict[str, str] | None:
    request = extract_user_request(index, turn, session=session)
    if request and request.get("content"):
        return request
    return None


def evaluate_summary(
    store: DocumentStore,
    session: Session,
    checks: CheckLog,
) -> dict[str, Any]:
    sid = str(session.session_id)
    params = {"session_id": sid}
    started = time.perf_counter_ns()
    summary = _dispatch(store, "session.summary", params)
    first_ms = (time.perf_counter_ns() - started) / 1_000_000
    repeated = _dispatch(store, "session.summary", params)

    checks.record(
        "summary.deterministic", summary == repeated, f"{sid}: response drifted"
    )

    # Section bounds and truthful truncation flags.
    bounds_ok = True
    for name, limit in _SUMMARY_LIMITS.items():
        section = summary.get(name) or []
        truncation = summary["truncation"][name]
        if len(section) > limit:
            bounds_ok = False
        if truncation["truncated"] != (truncation["total"] > len(section)):
            bounds_ok = False
        if truncation["total"] < len(section):
            bounds_ok = False
    checks.record(
        "summary.sections_bounded_and_truncation_truthful",
        bounds_ok,
        f"{sid}: {summary['truncation']}",
    )

    # Text previews stay inside the documented character budget.
    texts: list[str] = []
    objective = summary.get("objective")
    if isinstance(objective, dict):
        texts.append(str(objective.get("text") or ""))
    for entry in summary.get("decisions") or []:
        texts.append(str(entry.get("text") or ""))
    for entry in summary.get("next_actions") or []:
        texts.append(str(entry.get("text") or ""))
    for key in ("verification", "unresolved", "recent_activity"):
        for entry in summary.get(key) or []:
            texts.append(str(entry.get("label") or ""))
    checks.record(
        "summary.text_bounded",
        all(len(text) <= _SUMMARY_TEXT_LIMIT for text in texts),
        f"{sid}: max={max((len(text) for text in texts), default=0)}",
    )

    # All evidence references resolve to canonical resources.
    references = _summary_references(summary)
    checks.record(
        "summary.references_resolve",
        all(_refs_resolve(store, reference) for reference in references),
        f"{sid}: {references[:2]}",
    )

    # Recent activity never exposes private reasoning or retrieval self-references.
    recent_items = []
    for entry in summary.get("recent_activity") or []:
        item_id = (entry.get("references") or {}).get("item_id")
        if item_id:
            item = store.items.get(UUID(item_id))
            if item is not None:
                recent_items.append(item)
    checks.record(
        "summary.recent_excludes_private_and_self_retrieval",
        all(
            not isinstance(item, ReasoningItem) and not _is_retrieval_item(item)
            for item in recent_items
        ),
        f"{sid}: {[str(item.item_id) for item in recent_items][:5]}",
    )

    # Objective is the truncated content of the last non-low-value request.
    index = build_session_graph_index(_single_session_graph(session))
    requests = [
        (turn, request)
        for turn in session.turns
        if (request := _turn_request(index, session, turn)) is not None
    ]
    non_low_value = [
        (turn, request)
        for turn, request in requests
        if not _low_value_request(request["content"])
    ]
    if non_low_value:
        expected_text = truncate_text_preview(
            non_low_value[-1][1]["content"], max_len=_SUMMARY_TEXT_LIMIT
        )
        expected_turn = str(non_low_value[-1][0].turn_id)
        objective_ok = (
            objective is not None
            and objective.get("text") == expected_text
            and (objective.get("references") or {}).get("turn_id") == expected_turn
        )
    else:
        objective_ok = objective is None
    checks.record(
        "summary.objective_is_latest_material_request",
        objective_ok,
        f"{sid}: objective={objective!r}",
    )

    # Coverage flags agree with warnings.
    coverage = summary["coverage"]
    checks.record(
        "summary.coverage_consistent",
        (
            coverage["retention"]
            == ("trajectory" if coverage["content_complete"] else "measurements")
        )
        and bool(summary["warnings"]) == (not coverage["content_complete"]),
        f"{sid}: coverage={coverage} warnings={summary['warnings']}",
    )

    # Turn-scoped summaries only reference the selected turn.
    turn_scope_ok = True
    if session.turns:
        turn = session.turns[0]
        turn_summary = _dispatch(
            store,
            "session.summary",
            {"session_id": sid, "turn_id": str(turn.turn_id)},
        )
        for reference in _summary_references(turn_summary):
            referenced_turn = reference.get("turn_id")
            if referenced_turn and referenced_turn != str(turn.turn_id):
                turn_scope_ok = False
    checks.record(
        "summary.turn_scope_isolated", turn_scope_ok, f"{sid}: first turn leaked"
    )

    canonical_bytes = len(session.model_dump_json().encode())
    summary_bytes = len(_canonical_json(summary).encode())
    return {
        "first_ms": first_ms,
        "summary_bytes": summary_bytes,
        "canonical_bytes": canonical_bytes,
        "content_complete": coverage["content_complete"],
    }


def _path_from_input(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("file_path", "path", "target_file", "absolute_path", "file"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _pick_path_case(session: Session) -> tuple[str, set[str]] | None:
    """Pick a changed path whose exact match set is derivable from the model."""
    file_items = [
        item
        for turn in session.turns
        for item in turn.items
        if isinstance(item, FileChangeItem)
    ]
    candidates: dict[str, int] = {}
    for item in file_items:
        path = (item.path or _path_from_input(item.input) or "").strip()
        if _SINGLE_TOKEN_PATH_RE.match(path):
            candidates[path] = candidates.get(path, 0) + 1
    if not candidates:
        return None
    query = max(candidates.items(), key=lambda entry: (entry[1], entry[0]))[0]
    folded = query.casefold()
    expected = {
        str(item.item_id)
        for item in file_items
        if folded
        in (item.path or _path_from_input(item.input) or "").strip().casefold()
    }
    if not expected or len(expected) > 50:
        return None
    return query, expected


def _pick_token_case(
    index: Any, session: Session
) -> tuple[str, str, str] | None:
    """Pick a distinctive request token; return (query, turn_id, request_text)."""
    for turn in session.turns:
        request = _turn_request(index, session, turn)
        if request is None:
            continue
        content = _normalize(request["content"])
        if not content or len(content) > 16_000:
            continue
        tokens = sorted(
            {token for token in _tokens(content) if len(token) >= 6},
            key=lambda token: (-len(token), token),
        )
        if not tokens:
            continue
        return tokens[0], str(turn.turn_id), content
    return None


def _pick_failed_command_case(session: Session) -> tuple[str, str] | None:
    """Pick a failed command phrase; return (query, item_id)."""
    for turn in session.turns:
        for item in turn.items:
            if not isinstance(item, CommandExecutionItem):
                continue
            if _is_retrieval_item(item):
                continue
            failed = item.status == "failed" or bool(item.exit_code)
            command = _normalize(str(item.command or ""))
            if failed and 8 <= len(command) <= 2_000:
                return command, str(item.item_id)
    return None


def _check_search_response(
    store: DocumentStore,
    checks: CheckLog,
    sid: str,
    case: str,
    response: dict[str, Any],
    *,
    limit: int,
    mode: str = "text",
    kinds: list[str] | None = None,
    turn_id: str | None = None,
) -> None:
    matches = response["matches"]
    checks.record(
        f"{case}.limit_and_truncation_truthful",
        len(matches) == min(limit, response["total"])
        and response["truncated"] == (response["total"] > len(matches)),
        f"{sid}: total={response['total']} returned={len(matches)} limit={limit}",
    )
    checks.record(
        f"{case}.references_resolve",
        all(
            _refs_resolve(store, match["references"]) for match in matches
        ),
        f"{sid}: unresolved match references",
    )
    checks.record(
        f"{case}.rank_order_consistent",
        _rank_order_ok(matches),
        f"{sid}: scores={[match.get('score') for match in matches][:8]}",
    )
    checks.record(
        f"{case}.snippets_bounded",
        all(
            len(str(match.get("snippet") or "")) <= _SEARCH_SNIPPET_LIMIT + 6
            for match in matches
        ),
        f"{sid}: oversized snippet",
    )
    resolved_items = [
        store.items.get(UUID(match["references"]["item_id"]))
        for match in matches
        if match["references"].get("item_id")
    ]
    checks.record(
        f"{case}.private_reasoning_and_self_retrieval_excluded",
        all(
            item is not None
            and not isinstance(item, ReasoningItem)
            and not _is_retrieval_item(item)
            for item in resolved_items
        ),
        f"{sid}: reasoning or retrieval item matched",
    )
    if mode == "path":
        checks.record(
            f"{case}.path_mode_matches_path_field_only",
            all(match.get("matched_fields") == ["path"] for match in matches),
            f"{sid}: matched_fields={[match.get('matched_fields') for match in matches][:5]}",
        )
    else:
        checks.record(
            f"{case}.matched_fields_reported",
            all(bool(match.get("matched_fields")) for match in matches),
            f"{sid}: match without matched_fields",
        )
    if kinds is not None:
        checks.record(
            f"{case}.kind_filter_honored",
            all(match["kind"] in kinds for match in matches),
            f"{sid}: kinds={[match['kind'] for match in matches][:5]}",
        )
    if turn_id is not None:
        checks.record(
            f"{case}.turn_scope_isolated",
            all(match["references"].get("turn_id") == turn_id for match in matches),
            f"{sid}: turn leak for {turn_id}",
        )


def evaluate_search(
    store: DocumentStore,
    session: Session,
    checks: CheckLog,
) -> dict[str, Any]:
    sid = str(session.session_id)
    index = build_session_graph_index(_single_session_graph(session))
    result: dict[str, Any] = {"cases": []}

    token_case = _pick_token_case(index, session)
    if token_case is not None:
        query, turn_id, _content = token_case
        params = {"session_id": sid, "query": query, "limit": 50}
        started = time.perf_counter_ns()
        response = _dispatch(store, "session.search", params)
        first_ms = (time.perf_counter_ns() - started) / 1_000_000
        repeated = _dispatch(store, "session.search", params)
        checks.record(
            "search.deterministic", response == repeated, f"{sid}: response drifted"
        )
        _check_search_response(
            store, checks, sid, "search.token", response, limit=50
        )
        source_found = any(
            match["kind"] == "user_message"
            and match["references"].get("turn_id") == turn_id
            for match in response["matches"]
        )
        # The query term comes from this turn's request, so its document must
        # match; with more than 50 matches it may legitimately fall outside
        # the returned window, which is only a recall signal, not a violation.
        checks.record(
            "search.token.source_document_matched",
            any(
                match["kind"] == "user_message"
                and match["references"].get("turn_id") == turn_id
                for match in response["matches"]
            )
            or response["total"] > 50,
            f"{sid}: query={query!r} turn={turn_id} total={response['total']}",
        )
        bounded = _dispatch(
            store, "session.search", {"session_id": sid, "query": query, "limit": 2}
        )
        _check_search_response(
            store, checks, sid, "search.token_bounded", bounded, limit=2
        )
        checks.record(
            "search.token.total_stable_across_limits",
            bounded["total"] == response["total"],
            f"{sid}: limit50={response['total']} limit2={bounded['total']}",
        )
        kind_filtered = _dispatch(
            store,
            "session.search",
            {
                "session_id": sid,
                "query": query,
                "kinds": ["file_change"],
                "limit": 50,
            },
        )
        _check_search_response(
            store,
            checks,
            sid,
            "search.kind_filtered",
            kind_filtered,
            limit=50,
            kinds=["file_change"],
        )
        turn_scoped = _dispatch(
            store,
            "session.search",
            {
                "session_id": sid,
                "query": query,
                "turn_id": turn_id,
                "limit": 50,
            },
        )
        _check_search_response(
            store,
            checks,
            sid,
            "search.turn_scoped",
            turn_scoped,
            limit=50,
            turn_id=turn_id,
        )
        rank1_kind = response["matches"][0]["kind"] if response["matches"] else None
        result["cases"].append(
            {
                "case": "request_token",
                "query": query,
                "total": response["total"],
                "rank1_kind": rank1_kind,
                "source_found_at_rank": next(
                    (
                        match["rank"]
                        for match in response["matches"]
                        if match["kind"] == "user_message"
                        and match["references"].get("turn_id") == turn_id
                    ),
                    None,
                ),
                "source_found": source_found,
                "first_ms": first_ms,
                "response_bytes": len(_canonical_json(response).encode()),
                "content_complete": response["coverage"]["content_complete"],
            }
        )

    path_case = _pick_path_case(session)
    if path_case is not None:
        query, expected = path_case
        response = _dispatch(
            store,
            "session.search",
            {"session_id": sid, "query": query, "mode": "path", "limit": 50},
        )
        _check_search_response(
            store, checks, sid, "search.path", response, limit=50, mode="path"
        )
        returned = {
            str(match["references"].get("item_id")) for match in response["matches"]
        }
        checks.record(
            "search.path.exact_ground_truth",
            response["total"] == len(expected) and returned == expected,
            f"{sid}: query={query!r} total={response['total']} expected={len(expected)} "
            f"missing={sorted(expected - returned)[:3]} extra={sorted(returned - expected)[:3]}",
        )
        result["cases"].append(
            {
                "case": "changed_path",
                "query": query,
                "expected": len(expected),
                "total": response["total"],
                "response_bytes": len(_canonical_json(response).encode()),
            }
        )

    command_case = _pick_failed_command_case(session)
    if command_case is not None:
        query, item_id = command_case
        response = _dispatch(
            store,
            "session.search",
            {"session_id": sid, "query": query, "limit": 50},
        )
        _check_search_response(
            store, checks, sid, "search.failed_command", response, limit=50
        )
        found = any(
            match["references"].get("item_id") == item_id
            for match in response["matches"]
        )
        checks.record(
            "search.failed_command.source_item_matched",
            found or response["total"] > 50,
            f"{sid}: item={item_id} total={response['total']}",
        )
        result["cases"].append(
            {
                "case": "failed_command_phrase",
                "total": response["total"],
                "source_found": found,
                "source_rank": next(
                    (
                        match["rank"]
                        for match in response["matches"]
                        if match["references"].get("item_id") == item_id
                    ),
                    None,
                ),
                "response_bytes": len(_canonical_json(response).encode()),
            }
        )

    return result


def _single_session_graph(session: Session) -> Any:
    from coding_trajectory.ingestion.models import SessionGraph

    return SessionGraph(
        root_session_id=session.session_id,
        project_identifier="local-benchmark",
        sessions=[session],
        edges=[],
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
        discovery_note="local corpus benchmark",
        cache=IndexCache(),
    )


def evaluate(*, args: argparse.Namespace) -> dict[str, Any]:
    cache = IndexCache()
    started = time.perf_counter()
    store, note = resolve_store(
        {
            key: value
            for key, value in {
                "agent_vendor": args.vendor,
                "since_days": args.since_days,
            }.items()
            if value is not None
        },
        global_scope=True,
        current_dir=REPO_ROOT,
        cache=cache,
    )
    build_s = time.perf_counter() - started

    sessions = select_sessions(store, args.max_sessions)
    checks = CheckLog()
    summary_perf: list[float] = []
    summary_bytes: list[float] = []
    compression: list[float] = []
    search_perf: list[float] = []
    search_bytes: list[float] = []
    content_complete_count = 0
    rank1_kinds: dict[str, int] = {}
    match_totals: list[float] = []
    derived = {"path_cases": 0, "token_cases": 0, "failed_command_cases": 0}

    for session in sessions:
        summary_result = evaluate_summary(store, session, checks)
        summary_perf.append(summary_result["first_ms"])
        summary_bytes.append(summary_result["summary_bytes"])
        if summary_result["canonical_bytes"]:
            compression.append(
                summary_result["summary_bytes"] / summary_result["canonical_bytes"]
            )
        content_complete_count += int(summary_result["content_complete"])

        search_result = evaluate_search(store, session, checks)
        for case in search_result["cases"]:
            search_bytes.append(case["response_bytes"])
            if case["case"] == "request_token":
                derived["token_cases"] += 1
                search_perf.append(case["first_ms"])
                match_totals.append(case["total"])
                if case["rank1_kind"]:
                    rank1_kinds[case["rank1_kind"]] = (
                        rank1_kinds.get(case["rank1_kind"], 0) + 1
                    )
            elif case["case"] == "changed_path":
                derived["path_cases"] += 1
            elif case["case"] == "failed_command_phrase":
                derived["failed_command_cases"] += 1

    vendors: dict[str, int] = {}
    for session in store.sessions.values():
        vendors[session.vendor.value] = vendors.get(session.vendor.value, 0) + 1
    started_times = [
        session.started_at for session in store.sessions.values() if session.started_at
    ]

    gates = {
        "summary_invariants": checks.all_passed("summary."),
        "search_invariants": checks.all_passed("search."),
        "derived_ground_truth": (
            derived["path_cases"] > 0
            and derived["token_cases"] > 0
            and checks.failed.get("search.path.exact_ground_truth", 0) == 0
        ),
        "deterministic_responses": (
            checks.failed.get("summary.deterministic", 0) == 0
            and checks.failed.get("search.deterministic", 0) == 0
        ),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": BENCHMARK_NAME,
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": {
            "data": "real local provider logs (Codex, Claude Code, Pi)",
            "provider_logs": True,
            "relevance_judgments": False,
            "claim": (
                "contract behavior, derived ground-truth recall, and execution "
                "cost at real scale; ranking quality (nDCG/MRR) is only "
                "claimed by the synthetic benchmark"
            ),
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "store_build": {
            "elapsed_s": round(build_s, 3),
            "sources": note,
        },
        "corpus": {
            "graphs": len(store.session_graphs),
            "sessions": len(store.sessions),
            "turns": len(store.turns),
            "items": len(store.items),
            "events": len(store.events),
            "vendors": vendors,
            "first_session_at": min(started_times).isoformat()
            if started_times
            else None,
            "last_session_at": max(started_times).isoformat()
            if started_times
            else None,
        },
        "sample": {
            "sessions": len(sessions),
            "seed": SAMPLE_SEED,
            "selection": (
                f"seeded sample of up to {args.max_sessions} sessions plus the "
                "10 largest sessions by item count"
            ),
        },
        "derived_ground_truth": derived,
        "checks": checks.report(),
        "ranking_profile": {
            "rank1_kind_distribution": rank1_kinds,
            "token_match_totals": _distribution(match_totals),
            "content_complete_share": round(
                content_complete_count / len(sessions), 4
            )
            if sessions
            else None,
        },
        "performance": {
            "diagnostic_only": True,
            "note": (
                "Warm timings go through the full dispatch path, including "
                "per-call entry-point indexing of the whole corpus store."
            ),
            "store_build_s": round(build_s, 3),
            "session_summary_ms": _distribution(summary_perf),
            "session_search_ms": _distribution(search_perf),
            "summary_response_bytes": _distribution(summary_bytes),
            "search_response_bytes": _distribution(search_bytes),
            "summary_compression_ratio": _distribution(compression),
        },
        "gates": gates,
        "passed": all(gates.values()),
    }


def _print_report(report: dict[str, Any]) -> None:
    corpus = report["corpus"]
    print("Local session retrieval benchmark")
    print("=" * 45)
    print(
        f"corpus: {corpus['sessions']} sessions / {corpus['graphs']} graphs / "
        f"{corpus['turns']} turns / {corpus['items']} items"
    )
    print(f"vendors: {corpus['vendors']}")
    print(f"store build: {report['store_build']['elapsed_s']:.1f}s")
    print(f"sampled sessions: {report['sample']['sessions']}")
    print(f"derived ground truth: {report['derived_ground_truth']}")
    print("\nChecks")
    for name, entry in report["checks"].items():
        marker = "PASS" if entry["failed"] == 0 else "FAIL"
        print(f"  {marker} {name:52} {entry['passed']} passed / {entry['failed']} failed")
        for example in entry["failure_examples"][:3]:
            print(f"       example: {example}")
    print("\nRanking profile (no judgments)")
    print(f"  rank-1 kinds: {report['ranking_profile']['rank1_kind_distribution']}")
    print(
        f"  content-complete sessions: "
        f"{report['ranking_profile']['content_complete_share']}"
    )
    print("\nWarm-store diagnostics")
    for name in ("session_summary_ms", "session_search_ms"):
        dist = report["performance"][name]
        if dist.get("count"):
            print(
                f"  {name:22} median={dist['median']:.2f}ms "
                f"p95={dist['p95']:.2f}ms max={dist['max']:.2f}ms"
            )
    compression = report["performance"]["summary_compression_ratio"]
    if compression.get("count"):
        print(
            f"  summary compression   median={compression['median']:.4f} "
            f"p95={compression['p95']:.4f} max={compression['max']:.4f}"
        )
    print("\nGates")
    for name, passed in report["gates"].items():
        print(f"  {'PASS' if passed else 'FAIL'} {name}")
    print(f"\nOverall: {'PASS' if report['passed'] else 'FAIL'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-sessions",
        type=int,
        default=250,
        help="Seeded sample size (0 = every session; default: 250).",
    )
    parser.add_argument(
        "--vendor",
        choices=["codex", "claude_code", "pi"],
        default=None,
        help="Restrict discovery to one vendor.",
    )
    parser.add_argument(
        "--since-days",
        type=int,
        default=None,
        help="Restrict discovery to logs modified in the last N days.",
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
    if args.max_sessions < 0:
        parser.error("--max-sessions must be >= 0")

    report = evaluate(args=args)
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
