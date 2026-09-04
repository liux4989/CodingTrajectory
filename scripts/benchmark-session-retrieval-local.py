"""Evaluate session summary and search against private local judgments.

The synthetic fixture remains the deterministic default. This companion is
opt-in: it reads a user-owned JSON configuration containing explicit local
session IDs and source-derived judgments. Detailed reports stay under the
ignored ``.artifacts/session-retrieval-local`` directory.

- targeted source discovery and cold store-build cost;
- summary invariants: bounded sections, truthful truncation, evidence
  references that resolve, private-reasoning and self-retrieval exclusion,
  objective selection, deterministic responses;
- search invariants: truthful totals and limits, kind/turn scoping, rank
  ordering, bounded snippets, reference resolution, determinism;
- candidate-generation recall against configured canonical relevance, kept
  separate from ranking and ranking ablations;
- source-derived summary evidence assertions, never facts copied from output;
- warm-store execution cost at real scale.

Usage:
    uv run python scripts/benchmark-session-retrieval-local.py \
      --config .artifacts/session-retrieval-local/judgments.json
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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages" / "core" / "src"))

from coding_trajectory.analysis.projection_utils import truncate_text_preview  # noqa: E402 - repository-local imports after sys.path setup
from coding_trajectory.analysis.request_lineage import extract_user_request  # noqa: E402 - repository-local imports after sys.path setup
from coding_trajectory.analysis.session_retrieval import (  # noqa: E402 - repository-local imports after sys.path setup
    _SEARCH_SNIPPET_LIMIT,
    _SUMMARY_LIMITS,
    _SUMMARY_TEXT_LIMIT,
    _is_retrieval_item,
    _lexical_score,
    _low_value_request,
    _search_documents,
)
from coding_trajectory.discovery import discover_store_from_files, locate_session_files  # noqa: E402 - repository-local imports after sys.path setup
from coding_trajectory.ingestion.indexes import build_session_graph_index  # noqa: E402 - repository-local imports after sys.path setup
from coding_trajectory.ingestion.models import (  # noqa: E402 - repository-local imports after sys.path setup
    CommandExecutionItem,
    FileChangeItem,
    ReasoningItem,
    Session,
)
from coding_trajectory.query import DocumentStore  # noqa: E402 - repository-local imports after sys.path setup
from coding_trajectory.service import IndexCache, dispatch  # noqa: E402 - repository-local imports after sys.path setup

BENCHMARK_NAME = "session-retrieval-local"
SCHEMA_VERSION = 1
DEFAULT_OUTPUT = REPO_ROOT / ".artifacts" / "session-retrieval-local" / "report.json"
MAX_COHORT_SESSIONS = 20
_MAX_FAILURE_EXAMPLES = 10
_SINGLE_TOKEN_PATH_RE = re.compile(r"^[\w./:@+-]{4,200}$")
_TOKEN_RE = re.compile(r"[\w./:@+-]+", re.UNICODE)


class RelevantResource(BaseModel):
    """One canonical source reference in a private local judgment file."""

    identity: str = Field(pattern=r"^(?:[a-z_]+):(item|event):[0-9a-f-]{36}$")
    relevance: int = Field(ge=1, le=3)


class SearchJudgment(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    session_id: str
    query: str = Field(min_length=1, max_length=1000)
    mode: Literal["text", "path"] = "text"
    tier: Literal["exact", "paraphrase"] = "exact"
    turn_id: str | None = None
    kinds: list[str] | None = None
    judgments: list[RelevantResource] = Field(min_length=1)


class SummaryJudgment(BaseModel):
    session_id: str
    objective: str | None = None
    sections_include: dict[str, list[str]] = Field(default_factory=dict)
    sections_exclude: dict[str, list[str]] = Field(default_factory=dict)


class LocalEvaluationConfig(BaseModel):
    schema_version: Literal[1] = 1
    session_ids: list[str] = Field(min_length=1)
    summary_judgments: list[SummaryJudgment] = Field(default_factory=list)
    search_judgments: list[SearchJudgment] = Field(default_factory=list)

    @field_validator("session_ids")
    @classmethod
    def unique_session_ids(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("session_ids must be unique")
        return values

    @model_validator(mode="after")
    def scoped_judgments(self) -> LocalEvaluationConfig:
        allowed = set(self.session_ids)
        for judgment in [*self.summary_judgments, *self.search_judgments]:
            if judgment.session_id not in allowed:
                raise ValueError("every judgment session_id must appear in session_ids")
        return self


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
            }
            for name in names
        }

    def all_passed(self, prefix: str) -> bool:
        return all(
            count == 0 for name, count in self.failed.items() if name.startswith(prefix)
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
    return str(references.get("item_id") or (references.get("event_ids") or [""])[0])


def _match_identity(match: dict[str, Any]) -> str:
    references = match.get("references") or {}
    item_id = references.get("item_id")
    if item_id:
        return f"{match['kind']}:item:{item_id}"
    event_ids = references.get("event_ids") or []
    return f"{match['kind']}:event:{event_ids[0]}" if event_ids else ""


def _document_identity(document: Any) -> str:
    references = document.references
    item_id = references.get("item_id")
    if item_id:
        return f"{document.kind}:item:{item_id}"
    event_ids = references.get("event_ids") or []
    return f"{document.kind}:event:{event_ids[0]}" if event_ids else ""


def _candidate_identities(session: Session, judgment: SearchJudgment) -> list[str]:
    """Run the lexical candidate stage without the structural rank ordering.

    The candidate sequence is canonical source order.  It deliberately does
    not reuse API output, so configured source relevance remains the universe
    for recall rather than becoming whatever the matcher returned.
    """
    graph = _single_session_graph(session)
    turns = [
        turn
        for turn in session.turns
        if judgment.turn_id is None or str(turn.turn_id) == judgment.turn_id
    ]
    query = " ".join(judgment.query.split())
    query_folded = query.casefold()
    terms = tuple(dict.fromkeys(_tokens(query)))
    identities = []
    for document in _search_documents(
        graph,
        session,
        turns,
        mode=judgment.mode,
        kinds=judgment.kinds,
    ):
        score, _fields = _lexical_score(
            document,
            query_folded=query_folded,
            query_terms=terms,
            mode=judgment.mode,
        )
        if score > 0:
            identities.append(_document_identity(document))
    return identities


def _recall_at(identities: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 1.0
    return len(set(identities[:k]) & relevant) / len(relevant)


def _ranking_metrics(
    ranked: list[str], judgments: list[RelevantResource]
) -> dict[str, float]:
    relevance = {entry.identity: entry.relevance for entry in judgments}
    top = ranked[:10]
    found = {identity for identity in top if identity in relevance}
    precision = len(found) / len(top) if top else 1.0
    mrr = next(
        (1 / rank for rank, identity in enumerate(ranked, 1) if identity in relevance),
        0.0,
    )

    def dcg(values: list[int]) -> float:
        return sum(
            (2**value - 1) / math.log2(index + 2) for index, value in enumerate(values)
        )

    ideal = dcg(sorted(relevance.values(), reverse=True)[:10])
    return {
        "recall_at_5": round(_recall_at(ranked, set(relevance), 5), 4),
        "recall_at_10": round(_recall_at(ranked, set(relevance), 10), 4),
        "mrr": round(mrr, 4),
        "ndcg_at_10": round(
            dcg([relevance.get(identity, 0) for identity in top]) / ideal
            if ideal
            else 1.0,
            4,
        ),
        "precision_at_returned_10": round(precision, 4),
    }


def _mean_metrics(rows: list[dict[str, float]]) -> dict[str, float]:
    return {
        key: round(statistics.fmean(row[key] for row in rows), 4) for key in rows[0]
    }


def _lexical_baseline_score(match: dict[str, Any], query: str) -> int:
    text = f"{match.get('label') or ''} {match.get('snippet') or ''}".casefold()
    return sum(text.count(term) for term in dict.fromkeys(_tokens(query)))


def _rank_ablation(
    matches: list[dict[str, Any]], query: str, strategy: str
) -> list[str]:
    if strategy == "current_structural_lexical":
        ordered = matches
    elif strategy == "lexical_snippet_baseline":
        ordered = sorted(
            matches,
            key=lambda match: (
                -_lexical_baseline_score(match, query),
                str(match["timestamp"]),
                _match_identity(match),
            ),
        )
    else:
        ordered = sorted(
            matches,
            key=lambda match: (str(match["timestamp"]), _match_identity(match)),
            reverse=True,
        )
    return [_match_identity(match) for match in ordered]


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


def _item_count(session: Session) -> int:
    return sum(len(turn.items) for turn in session.turns)


def _has_failed_command(session: Session) -> bool:
    return any(
        isinstance(item, CommandExecutionItem)
        and (item.status == "failed" or bool(item.exit_code))
        for turn in session.turns
        for item in turn.items
    )


def select_cohort(store: DocumentStore) -> list[tuple[Session, list[str]]]:
    """Pick a deterministic audited cohort covering vendors, sizes, behaviors.

    Every selected session carries the coverage slots it fills so report
    readers can audit why each session belongs to the cohort. Ties break by
    session id, keeping the cohort stable across runs on an unchanged corpus.
    """
    slots: dict[str, list[Any]] = {}

    def add(slot: str, session: Session | None) -> None:
        if session is not None:
            key = str(session.session_id)
            if key not in slots:
                slots[key] = [session, []]
            slots[key][1].append(slot)

    sessions = sorted(store.sessions.values(), key=lambda s: str(s.session_id))
    by_items = sorted(sessions, key=lambda s: (_item_count(s), str(s.session_id)))
    vendors = sorted({session.vendor.value for session in sessions})
    for vendor in vendors:
        vendor_sessions = [s for s in sessions if s.vendor.value == vendor]
        vendor_by_items = [s for s in by_items if s.vendor.value == vendor]
        add(f"{vendor}:largest", vendor_by_items[-1])
        add(f"{vendor}:typical", vendor_by_items[len(vendor_by_items) // 2])
        add(
            f"{vendor}:with_failures",
            next((s for s in vendor_sessions if _has_failed_command(s)), None),
        )
        add(
            f"{vendor}:with_file_changes",
            next(
                (
                    s
                    for s in vendor_sessions
                    if any(
                        isinstance(item, FileChangeItem)
                        for turn in s.turns
                        for item in turn.items
                    )
                ),
                None,
            ),
        )
        add(
            f"{vendor}:multi_turn",
            next((s for s in vendor_sessions if len(s.turns) >= 3), None),
        )
    add("smallest", by_items[0] if by_items else None)
    add(
        "measurements_retention",
        next((s for s in sessions if s.measurements is not None), None),
    )

    cohort = sorted(slots.values(), key=lambda entry: str(entry[0].session_id))
    if len(cohort) > MAX_COHORT_SESSIONS:
        # Shed sessions that only fill the soft "typical" slot first.
        essential = [
            entry
            for entry in cohort
            if any(not name.endswith(":typical") for name in entry[1])
        ]
        cohort = essential[:MAX_COHORT_SESSIONS]
    return [(entry[0], entry[1]) for entry in cohort]


def _turn_request(index: Any, session: Session, turn: Any) -> dict[str, str] | None:
    request = extract_user_request(index, turn, session=session)
    if request and request.get("content"):
        return request
    return None


def evaluate_summary(
    store: DocumentStore,
    session: Session,
    index: Any,
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


def _pick_token_case(index: Any, session: Session) -> tuple[str, str, str] | None:
    """Pick a distinctive request token; return (query, turn_id, request_text)."""
    for turn in session.turns:
        request = _turn_request(index, session, turn)
        if request is None:
            continue
        content = _normalize(request["content"])
        if not content or len(content) > 16_000:
            continue
        tokens = sorted(
            {token for token in _tokens(content) if 6 <= len(token) <= 200},
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
            # The search contract caps query length at 1000 characters.
            if failed and 8 <= len(command) <= 1_000:
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
        all(_refs_resolve(store, match["references"]) for match in matches),
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
    index: Any,
    checks: CheckLog,
) -> dict[str, Any]:
    sid = str(session.session_id)
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
        _check_search_response(store, checks, sid, "search.token", response, limit=50)
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


# One cache shared across dispatches mirrors ServiceRuntime's store-lifetime
# reuse and keeps entry-point indexing out of the per-call measurement noise.
_SHARED_CACHE = IndexCache()


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
        cache=_SHARED_CACHE,
    )


def _assert_summary_judgments(
    summary: dict[str, Any], judgment: SummaryJudgment, checks: CheckLog
) -> None:
    def identities(section: str) -> set[str]:
        if section == "objective":
            entry = summary.get("objective")
            entries = [entry] if entry else []
        else:
            entries = summary.get(section, [])
        return {
            f"{kind}:{value}"
            for entry in entries
            for kind, value in [
                ("item", (entry.get("references") or {}).get("item_id")),
                (
                    "event",
                    next(
                        iter((entry.get("references") or {}).get("event_ids") or []),
                        None,
                    ),
                ),
            ]
            if value
        }

    def source_keys(values: list[str]) -> set[str]:
        return {":".join(value.split(":")[-2:]) for value in values}

    if judgment.objective is not None:
        checks.record(
            "summary.judged_objective",
            source_keys([judgment.objective]) <= identities("objective"),
        )
    for section, expected in judgment.sections_include.items():
        checks.record(
            f"summary.judged_include.{section}",
            source_keys(expected) <= identities(section),
        )
    for section, excluded in judgment.sections_exclude.items():
        checks.record(
            f"summary.judged_exclude.{section}",
            not (source_keys(excluded) & identities(section)),
        )


def _measure(
    store: DocumentStore, method: str, params: dict[str, Any], repeat: int
) -> dict[str, Any]:
    runs = []
    response: dict[str, Any] | None = None
    for _ in range(repeat):
        started = time.perf_counter_ns()
        response = _dispatch(store, method, params)
        runs.append((time.perf_counter_ns() - started) / 1_000_000)
    assert response is not None
    return {
        "repeat": repeat,
        "median_ms": round(statistics.median(runs), 4),
        "p95_ms": round(_percentile(runs, 0.95), 4),
        "response_bytes": len(_canonical_json(response).encode()),
        "candidate_count": response.get("total"),
    }


def evaluate(
    *, args: argparse.Namespace, config: LocalEvaluationConfig
) -> dict[str, Any]:
    started = time.perf_counter()
    source_paths = []
    for session_id in config.session_ids:
        source_paths.extend(
            locate_session_files(
                session_id=UUID(session_id),
                current_dir=REPO_ROOT,
                global_scope=True,
                include_descendants=True,
            )
        )
    store = discover_store_from_files(source_paths).store
    build_s = time.perf_counter() - started
    selected = [
        store.sessions[UUID(session_id)]
        for session_id in config.session_ids
        if UUID(session_id) in store.sessions
    ]
    if len(selected) != len(config.session_ids):
        raise ValueError("one or more configured session_ids were not discovered")

    checks = CheckLog()
    summary_bytes: list[float] = []
    compression: list[float] = []
    for session in selected:
        index = build_session_graph_index(_single_session_graph(session))
        result = evaluate_summary(store, session, index, checks)
        summary_bytes.append(result["summary_bytes"])
        if result["canonical_bytes"]:
            compression.append(result["summary_bytes"] / result["canonical_bytes"])
    for judgment in config.summary_judgments:
        summary = _dispatch(
            store, "session.summary", {"session_id": judgment.session_id}
        )
        _assert_summary_judgments(summary, judgment, checks)

    candidate_rows: dict[str, list[dict[str, float]]] = {"exact": [], "paraphrase": []}
    rank_rows: dict[str, list[dict[str, float]]] = {
        "current_structural_lexical": [],
        "lexical_snippet_baseline": [],
        "recent_first_baseline": [],
    }
    response_bytes: list[float] = []
    for judgment in config.search_judgments:
        session = store.sessions[UUID(judgment.session_id)]
        params: dict[str, Any] = {
            "session_id": judgment.session_id,
            "query": judgment.query,
            "mode": judgment.mode,
            "limit": 50,
        }
        if judgment.turn_id:
            params["turn_id"] = judgment.turn_id
        if judgment.kinds is not None:
            params["kinds"] = judgment.kinds
        response = _dispatch(store, "session.search", params)
        repeated = _dispatch(store, "session.search", params)
        checks.record("search.deterministic", response == repeated)
        _check_search_response(
            store,
            checks,
            judgment.session_id,
            "search.judged",
            response,
            limit=50,
            mode=judgment.mode,
            kinds=judgment.kinds,
            turn_id=judgment.turn_id,
        )
        relevant = {entry.identity for entry in judgment.judgments}
        candidates = _candidate_identities(session, judgment)
        candidate_rows[judgment.tier].append(
            {
                "source_order_candidate_recall_at_5": _recall_at(
                    candidates, relevant, 5
                ),
                "source_order_candidate_recall_at_10": _recall_at(
                    candidates, relevant, 10
                ),
                "candidate_universe_recall": _recall_at(
                    candidates, relevant, len(candidates)
                ),
            }
        )
        for strategy, rows in rank_rows.items():
            rows.append(
                _ranking_metrics(
                    _rank_ablation(response["matches"], judgment.query, strategy),
                    judgment.judgments,
                )
            )
        response_bytes.append(len(_canonical_json(response).encode()))

    performance: dict[str, dict[str, Any]] = {}
    primary = selected[0]
    performance["session.summary"] = _measure(
        store, "session.summary", {"session_id": str(primary.session_id)}, args.repeat
    )
    performance["session.summary"]["session_item_count"] = _item_count(primary)
    text_judgment = next((j for j in config.search_judgments if j.mode == "text"), None)
    path_judgment = next((j for j in config.search_judgments if j.mode == "path"), None)
    for label, judgment in (
        ("text_search", text_judgment),
        ("high_hit_search", text_judgment),
        ("path_search", path_judgment),
        ("low_hit_search", path_judgment),
    ):
        if judgment is None:
            continue
        params = {
            "session_id": judgment.session_id,
            "query": judgment.query,
            "mode": judgment.mode,
            "limit": 50,
        }
        performance[label] = _measure(store, "session.search", params, args.repeat)
        performance[label]["session_item_count"] = _item_count(
            store.sessions[UUID(judgment.session_id)]
        )
    largest = max(selected, key=_item_count)
    if config.search_judgments:
        query = config.search_judgments[0].query
        performance["large_session_search"] = _measure(
            store,
            "session.search",
            {"session_id": str(largest.session_id), "query": query, "limit": 50},
            args.repeat,
        )
        performance["large_session_search"]["session_item_count"] = _item_count(largest)

    vendor_counts: dict[str, int] = {}
    for session in selected:
        vendor_counts[session.vendor.value] = (
            vendor_counts.get(session.vendor.value, 0) + 1
        )
    gates = {
        "summary_invariants": checks.all_passed("summary."),
        "search_invariants": checks.all_passed("search."),
        "judged_search_present": bool(config.search_judgments),
        "deterministic_responses": checks.failed.get("summary.deterministic", 0) == 0
        and checks.failed.get("search.deterministic", 0) == 0,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": BENCHMARK_NAME,
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": {
            "data": "private local provider logs",
            "source_derived_judgments": True,
            "private_configuration_written": False,
            "semantic_ranking": False,
            "claim": "local evidence and performance diagnostics; paraphrase results are diagnostic",
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "coverage": {
            "selected_sessions": len(selected),
            "vendors": vendor_counts,
            "judged_summaries": len(config.summary_judgments),
            "judged_queries": {
                tier: len(rows) for tier, rows in candidate_rows.items()
            },
            "session_items": _distribution(
                [float(_item_count(session)) for session in selected]
            ),
        },
        "store_build": {"elapsed_s": round(build_s, 3)},
        "summary": {
            "checks": checks.report(),
            "response_bytes": _distribution(summary_bytes),
            "compression_ratio": _distribution(compression),
        },
        "search": {
            "candidate_generation": {
                tier: _mean_metrics(rows) if rows else {"count": 0}
                for tier, rows in candidate_rows.items()
            },
            "ranking": {
                strategy: _mean_metrics(rows) if rows else {"count": 0}
                for strategy, rows in rank_rows.items()
            },
            "response_bytes": _distribution(response_bytes),
            "ablation_candidate_set": "Each ablation reorders the same returned lexical candidates; source-order candidate recall is only a prefix diagnostic, while candidate-universe recall measures matching completeness.",
        },
        "performance": {"diagnostic_only": True, "measurements": performance},
        "gates": gates,
        "passed": all(gates.values()),
    }


def _print_report(report: dict[str, Any]) -> None:
    print("Local session retrieval benchmark")
    print("=" * 45)
    coverage = report["coverage"]
    print(f"selected sessions: {coverage['selected_sessions']}")
    print(f"vendors: {coverage['vendors']}")
    print(f"store build: {report['store_build']['elapsed_s']:.1f}s")
    print(f"judged queries: {coverage['judged_queries']}")
    print("\nChecks")
    for name, entry in report["summary"]["checks"].items():
        marker = "PASS" if entry["failed"] == 0 else "FAIL"
        print(
            f"  {marker} {name:52} {entry['passed']} passed / {entry['failed']} failed"
        )
    print("\nCandidate generation")
    for tier, metrics in report["search"]["candidate_generation"].items():
        print(f"  {tier:12} {metrics}")
    print("\nRanking ablations")
    for strategy, metrics in report["search"]["ranking"].items():
        print(f"  {strategy:30} {metrics}")
    print("\nWarm-store diagnostics")
    for name, measurement in report["performance"]["measurements"].items():
        print(
            f"  {name:22} median={measurement['median_ms']:.2f}ms "
            f"p95={measurement['p95_ms']:.2f}ms bytes={measurement['response_bytes']}"
        )
    print("\nGates")
    for name, passed in report["gates"].items():
        print(f"  {'PASS' if passed else 'FAIL'} {name}")
    print(f"\nOverall: {'PASS' if report['passed'] else 'FAIL'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Ignored private JSON with session IDs and source-derived judgments.",
    )
    parser.add_argument(
        "--repeat", type=int, default=30, help="Warm repetitions (default: 30)."
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
    if args.repeat < 30:
        parser.error("--repeat must be at least 30 for local performance diagnostics")
    try:
        config = LocalEvaluationConfig.model_validate_json(
            args.config.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        parser.error(f"invalid --config: {exc}")

    report = evaluate(args=args, config=config)
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
