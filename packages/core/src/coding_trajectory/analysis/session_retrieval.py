"""Deterministic session summary and canonical evidence search projections."""

from __future__ import annotations

import heapq
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from coding_trajectory.analysis.projection_utils import truncate_text_preview
from coding_trajectory.analysis.request_lineage import extract_user_request
from coding_trajectory.analysis.tool_summary import summarize_tool_call
from coding_trajectory.analysis.tool_summary_shell import classify_command_family
from coding_trajectory.ingestion.common import format_datetime
from coding_trajectory.ingestion.indexes import build_session_graph_index
from coding_trajectory.ingestion.models import (
    AgentMessageItem,
    CommandExecutionItem,
    FileChangeItem,
    Item,
    PlanItem,
    ReasoningItem,
    Session,
    SessionGraph,
    ToolCallItem,
    ToolStatus,
    Turn,
)

SearchKind = Literal[
    "user_message",
    "assistant_message",
    "tool_call",
    "tool_result",
    "file_change",
]

DEFAULT_SEARCH_KINDS: tuple[SearchKind, ...] = (
    "user_message",
    "assistant_message",
    "tool_call",
    "tool_result",
    "file_change",
)

_SEARCH_FIELD_LIMIT = 16_000
_SEARCH_SNIPPET_LIMIT = 240
_SUMMARY_TEXT_LIMIT = 280
_SUMMARY_LIMITS = {
    "decisions": 8,
    "changes": 20,
    "verification": 12,
    "unresolved": 12,
    "next_actions": 10,
    "recent_activity": 12,
}
_TOKEN_RE = re.compile(r"[\w./:@+-]+", re.UNICODE)
_EXPLICIT_DECISION_RE = re.compile(
    r"^(?:[-*]\s*)?(?:decision\s*:|decided\s+|we\s+will\s+|we'll\s+)",
    re.IGNORECASE,
)
_LOW_VALUE_REQUEST_RE = re.compile(
    r"^(?:status(?:\s+update)?|continue|go ahead|yes|ok(?:ay)?|thanks?|agreed?)"
    r"[.!?\s]*$",
    re.IGNORECASE,
)
_RETRIEVAL_COMMAND_RE = re.compile(
    r"(?:^|\s)ct\s+(?:api\s+call\s+session\.(?:summary|search)|"
    r"session\s+(?:summary|search))(?:\s|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _SearchDocument:
    kind: SearchKind
    timestamp: datetime
    label: str
    fields: dict[str, str]
    references: dict[str, Any]
    structural_score: float
    content_complete: bool


@dataclass(slots=True)
class _RankedSummaryItem:
    timestamp: datetime
    structural_score: float
    value: dict[str, Any]


def build_session_summary(
    session_graph: SessionGraph,
    *,
    turn_id: str | None = None,
) -> dict[str, Any]:
    """Build one bounded, evidence-backed summary for a canonical session."""

    session = _only_session(session_graph)
    turns = _selected_turns(session, turn_id)
    index = build_session_graph_index(session_graph)
    content_complete = _content_complete(session, turns)

    request_candidates: list[tuple[Turn, dict[str, str]]] = []
    decisions: list[_RankedSummaryItem] = []
    changes_by_path: dict[str, _RankedSummaryItem] = {}
    verification: list[_RankedSummaryItem] = []
    unresolved_by_key: dict[str, _RankedSummaryItem] = {}
    next_actions: list[_RankedSummaryItem] = []
    recent: list[_RankedSummaryItem] = []

    item_count = sum(len(turn.items) for turn in turns)
    item_position = 0
    for turn in turns:
        request = extract_user_request(index, turn, session=session)
        if request and not _low_value_request(request.get("content")):
            request_candidates.append((turn, request))
            request_references = _references(
                session_id=session.session_id,
                turn_id=turn.turn_id,
                event_ids=(
                    [str(turn.user_request_event_id)]
                    if turn.user_request_event_id is not None
                    else []
                ),
            )
            for decision in _explicit_decisions(request["content"]):
                decisions.append(
                    _RankedSummaryItem(
                        timestamp=turn.started_at,
                        structural_score=18 + _recency_score(item_position, item_count),
                        value={
                            "text": truncate_text_preview(
                                decision, max_len=_SUMMARY_TEXT_LIMIT
                            ),
                            "references": request_references,
                        },
                    )
                )

        for item in turn.items:
            recency = _recency_score(item_position, item_count)
            item_position += 1
            structural_score = _item_structural_score(item) + recency
            references = _item_references(item)

            if isinstance(item, AgentMessageItem):
                text = _item_text(item)
                if text:
                    for decision in _explicit_decisions(text):
                        decisions.append(
                            _RankedSummaryItem(
                                timestamp=item.started_at,
                                structural_score=structural_score,
                                value={
                                    "text": truncate_text_preview(
                                        decision, max_len=_SUMMARY_TEXT_LIMIT
                                    ),
                                    "references": references,
                                },
                            )
                        )

            if isinstance(item, FileChangeItem) and _item_outcome(item) == "succeeded":
                path = (item.path or _path_from_value(item.input) or "").strip()
                if path:
                    existing = changes_by_path.get(path)
                    operation = item.operation or item.tool_name or "edit"
                    if existing is None:
                        changes_by_path[path] = _RankedSummaryItem(
                            timestamp=item.started_at,
                            structural_score=structural_score,
                            value={
                                "path": path,
                                "operations": [operation],
                                "references": references,
                            },
                        )
                    else:
                        operations = existing.value["operations"]
                        if operation not in operations:
                            operations.append(operation)
                        existing.value["references"] = _merge_references(
                            existing.value["references"], references
                        )

            if isinstance(item, CommandExecutionItem):
                family, _head = classify_command_family(item.command)
                if family in {"tests", "build"}:
                    verification.append(
                        _RankedSummaryItem(
                            timestamp=item.started_at,
                            structural_score=structural_score,
                            value={
                                "label": truncate_text_preview(
                                    _stringify(item.command),
                                    max_len=_SUMMARY_TEXT_LIMIT,
                                ),
                                "status": _item_outcome(item),
                                "references": references,
                            },
                        )
                    )

            failure_key = _item_resolution_key(item)
            outcome = _item_outcome(item)
            if failure_key and outcome == "failed":
                unresolved_by_key[failure_key] = _RankedSummaryItem(
                    timestamp=item.started_at,
                    structural_score=structural_score,
                    value={
                        "label": _item_label(item),
                        "status": "failed",
                        "references": references,
                    },
                )
            elif failure_key and outcome == "succeeded":
                unresolved_by_key.pop(failure_key, None)

            if isinstance(item, PlanItem):
                # Plan tools publish snapshots. Only the latest snapshot can
                # describe the session's current explicit next actions.
                next_actions.clear()
                for action in _pending_plan_actions(item.input):
                    next_actions.append(
                        _RankedSummaryItem(
                            timestamp=item.started_at,
                            structural_score=structural_score,
                            value={
                                "text": truncate_text_preview(
                                    action, max_len=_SUMMARY_TEXT_LIMIT
                                ),
                                "references": references,
                            },
                        )
                    )

            if not isinstance(item, ReasoningItem) and not _is_retrieval_item(item):
                recent.append(
                    _RankedSummaryItem(
                        timestamp=item.started_at,
                        structural_score=structural_score,
                        value={
                            "kind": item.kind,
                            "label": _item_label(item),
                            "status": _item_outcome(item),
                            "references": references,
                        },
                    )
                )

    objective = None
    if request_candidates:
        objective_turn, objective_request = request_candidates[-1]
        event_ids = (
            [str(objective_turn.user_request_event_id)]
            if objective_turn.user_request_event_id is not None
            else []
        )
        objective = {
            "text": truncate_text_preview(
                objective_request["content"], max_len=_SUMMARY_TEXT_LIMIT
            ),
            "references": _references(
                session_id=session.session_id,
                turn_id=objective_turn.turn_id,
                event_ids=event_ids,
            ),
        }

    section_candidates = {
        "decisions": _dedupe_summary_items(decisions, value_key="text"),
        "changes": list(changes_by_path.values()),
        "verification": verification,
        "unresolved": list(unresolved_by_key.values()),
        "next_actions": next_actions,
        "recent_activity": recent,
    }
    sections: dict[str, list[dict[str, Any]]] = {}
    truncation: dict[str, dict[str, int | bool]] = {}
    for name, candidates in section_candidates.items():
        if name == "recent_activity":
            selected = sorted(candidates, key=lambda item: item.timestamp)[
                -_SUMMARY_LIMITS[name] :
            ]
        else:
            selected = _select_summary_items(candidates, _SUMMARY_LIMITS[name])
        sections[name] = [entry.value for entry in selected]
        truncation[name] = {
            "total": len(candidates),
            "truncated": len(candidates) > len(selected),
        }

    warnings = []
    if not content_complete:
        warnings.append(
            "Some transcript bodies were not retained; the summary uses compact measurements and may omit text-derived facts."
        )
    return {
        "session_id": str(session.session_id),
        "selected_turn_id": turn_id,
        "latest_turn_status": turns[-1].status.value if turns else None,
        "objective": objective,
        **sections,
        "truncation": truncation,
        "projection": {
            "name": "session_summary",
            "version": 1,
            "strategy": "deterministic_structural",
        },
        "coverage": {
            "retention": "trajectory" if content_complete else "measurements",
            "content_complete": content_complete,
        },
        "warnings": warnings,
    }


def search_session(
    session_graph: SessionGraph,
    *,
    query: str,
    mode: Literal["text", "path"] = "text",
    kinds: list[SearchKind] | None = None,
    limit: int = 20,
    turn_id: str | None = None,
) -> dict[str, Any]:
    """Search one session using bounded lexical matching and structural rank."""

    session = _only_session(session_graph)
    turns = _selected_turns(session, turn_id)
    selected_kinds = tuple(DEFAULT_SEARCH_KINDS if kinds is None else kinds)
    query_text = " ".join(query.split())
    query_folded = query_text.casefold()
    query_terms = tuple(dict.fromkeys(_tokens(query_text)))

    documents = _search_documents(session_graph, session, turns)
    matches: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    retained_content_complete = _content_complete(session, turns)
    content_complete = retained_content_complete
    searched_resources = 0
    for document in documents:
        if document.kind not in selected_kinds:
            continue
        searched_resources += 1
        content_complete = content_complete and document.content_complete
        lexical_score, matched_fields = _lexical_score(
            document,
            query_folded=query_folded,
            query_terms=query_terms,
            mode=mode,
        )
        if lexical_score <= 0:
            continue
        score = round(lexical_score + document.structural_score, 3)
        snippet = _search_snippet(document, query_terms)
        value = {
            "rank": 0,
            "score": score,
            "kind": document.kind,
            "timestamp": format_datetime(document.timestamp),
            "label": document.label,
            "snippet": snippet,
            "matched_fields": matched_fields,
            "references": document.references,
        }
        stable_id = (
            document.references.get("item_id")
            or (document.references.get("event_ids") or [""])[0]
        )
        order = (-score, document.timestamp, str(stable_id))
        matches.append((order, value))

    total = len(matches)
    selected = heapq.nsmallest(limit, matches, key=lambda entry: entry[0])
    ranked = []
    for rank, (_order, value) in enumerate(selected, start=1):
        value["rank"] = rank
        ranked.append(value)

    warnings = []
    if not content_complete:
        warnings.append(
            "Some searchable fields were truncated or not retained; results may be incomplete."
        )
    return {
        "session_id": str(session.session_id),
        "selected_turn_id": turn_id,
        "query": {
            "text": query_text,
            "mode": mode,
            "kinds": list(selected_kinds),
        },
        "matches": ranked,
        "total": total,
        "truncated": total > len(ranked),
        "projection": {
            "name": "session_search",
            "version": 1,
            "strategy": "structural_lexical",
        },
        "coverage": {
            "retention": (
                "trajectory" if retained_content_complete else "measurements"
            ),
            "searched_resources": searched_resources,
            "content_complete": content_complete,
        },
        "warnings": warnings,
    }


def _only_session(session_graph: SessionGraph) -> Session:
    if len(session_graph.sessions) != 1:
        raise ValueError("session projection requires exactly one canonical session")
    return session_graph.sessions[0]


def _selected_turns(session: Session, turn_id: str | None) -> list[Turn]:
    if turn_id is None:
        return list(session.turns)
    for turn in session.turns:
        if str(turn.turn_id) == turn_id:
            return [turn]
    raise ValueError(f"turn not found in selected session: {turn_id}")


def _content_complete(session: Session, turns: list[Turn]) -> bool:
    return session.measurements is None and all(
        item.measurements is None for turn in turns for item in turn.items
    )


def _references(
    *,
    session_id: Any,
    turn_id: Any | None = None,
    item_id: Any | None = None,
    event_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "session_id": str(session_id),
        "turn_id": str(turn_id) if turn_id is not None else None,
        "item_id": str(item_id) if item_id is not None else None,
        "event_ids": event_ids or [],
    }


def _item_references(item: Item) -> dict[str, Any]:
    return _references(
        session_id=item.session_id,
        turn_id=item.turn_id,
        item_id=item.item_id,
        event_ids=[str(event_id) for event_id in item.event_ids],
    )


def _merge_references(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    event_ids = list(
        dict.fromkeys([*(left.get("event_ids") or []), *(right.get("event_ids") or [])])
    )
    return {**left, "event_ids": event_ids}


def _item_outcome(item: Item) -> str:
    summary = summarize_tool_call(item)
    if summary and summary.get("activity_outcome") in {
        "succeeded",
        "failed",
        "unknown",
    }:
        return str(summary["activity_outcome"])
    status = item.status
    if status in {ToolStatus.COMPLETED, ToolStatus.COMPLETED.value, "completed"}:
        return "succeeded"
    if status in {ToolStatus.FAILED, ToolStatus.FAILED.value, "failed"}:
        return "failed"
    return "unknown"


def _item_structural_score(item: Item) -> float:
    score = 0.0
    if isinstance(item, FileChangeItem):
        score += 34
    elif isinstance(item, CommandExecutionItem):
        family, _head = classify_command_family(item.command)
        score += 26 if family == "tests" else 22 if family == "build" else 14
    elif isinstance(item, PlanItem):
        score += 18
    elif isinstance(item, AgentMessageItem):
        score += 10
    elif isinstance(item, ToolCallItem):
        score += 8
    if _item_outcome(item) == "failed":
        score += 24
    return score


def _recency_score(position: int, total: int) -> float:
    if total <= 1:
        return 12.0
    return round(12 * position / (total - 1), 3)


def _select_summary_items(
    candidates: list[_RankedSummaryItem], limit: int
) -> list[_RankedSummaryItem]:
    if len(candidates) <= limit:
        return sorted(candidates, key=lambda item: item.timestamp)
    selected = heapq.nlargest(
        limit,
        candidates,
        key=lambda item: (item.structural_score, item.timestamp),
    )
    return sorted(selected, key=lambda item: item.timestamp)


def _dedupe_summary_items(
    candidates: list[_RankedSummaryItem], *, value_key: str
) -> list[_RankedSummaryItem]:
    selected: dict[str, _RankedSummaryItem] = {}
    for candidate in sorted(candidates, key=lambda item: item.timestamp):
        value = str(candidate.value.get(value_key) or "")
        normalized = " ".join(value.casefold().split())
        if normalized and normalized not in selected:
            selected[normalized] = candidate
    return list(selected.values())


def _item_label(item: Item) -> str:
    if isinstance(item, AgentMessageItem):
        return truncate_text_preview(_item_text(item), max_len=_SUMMARY_TEXT_LIMIT)
    summary = summarize_tool_call(item)
    if summary:
        name = str(summary.get("name") or item.kind)
        description = summary.get("description") or summary.get("command")
        if description:
            return truncate_text_preview(
                f"{name}: {description}", max_len=_SUMMARY_TEXT_LIMIT
            )
        return name
    return item.kind


def _item_resolution_key(item: Item) -> str | None:
    if isinstance(item, FileChangeItem):
        return f"file:{item.path or _path_from_value(item.input) or item.item_id}"
    if isinstance(item, CommandExecutionItem):
        family, head = classify_command_family(item.command)
        return f"command:{family}:{head}"
    tool_name = getattr(item, "tool_name", None)
    return f"tool:{tool_name}" if tool_name else None


def _low_value_request(value: str | None) -> bool:
    return not value or bool(_LOW_VALUE_REQUEST_RE.fullmatch(" ".join(value.split())))


def _explicit_decisions(text: str) -> list[str]:
    decisions = []
    for line in text.splitlines():
        normalized = " ".join(line.split())
        if normalized and _EXPLICIT_DECISION_RE.match(normalized):
            decisions.append(normalized)
    return decisions


def _pending_plan_actions(value: Any) -> list[str]:
    actions: list[str] = []
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, list):
            stack.extend(reversed(current))
            continue
        if not isinstance(current, dict):
            continue
        status = str(current.get("status") or "").casefold()
        text = next(
            (
                current.get(key)
                for key in ("content", "text", "task", "step", "name")
                if isinstance(current.get(key), str) and current.get(key).strip()
            ),
            None,
        )
        if text and status not in {"completed", "done", "cancelled", "canceled"}:
            actions.append(str(text).strip())
        stack.extend(reversed(list(current.values())))
    return list(dict.fromkeys(actions))


def _search_documents(
    session_graph: SessionGraph,
    session: Session,
    turns: list[Turn],
) -> list[_SearchDocument]:
    index = build_session_graph_index(session_graph)
    item_count = sum(len(turn.items) for turn in turns)
    item_position = 0
    documents: list[_SearchDocument] = []
    for turn in turns:
        request = extract_user_request(index, turn, session=session)
        if request and request.get("content"):
            text, complete = _bounded_text(request["content"])
            documents.append(
                _SearchDocument(
                    kind="user_message",
                    timestamp=turn.started_at,
                    label="User request",
                    fields={"text": text},
                    references=_references(
                        session_id=session.session_id,
                        turn_id=turn.turn_id,
                        event_ids=(
                            [str(turn.user_request_event_id)]
                            if turn.user_request_event_id is not None
                            else []
                        ),
                    ),
                    structural_score=18 + _recency_score(item_position, item_count),
                    content_complete=complete,
                )
            )

        for item in turn.items:
            recency = _recency_score(item_position, item_count)
            item_position += 1
            if isinstance(item, ReasoningItem) or _is_retrieval_item(item):
                continue
            documents.extend(_documents_for_item(item, recency=recency))
    return documents


def _documents_for_item(item: Item, *, recency: float) -> list[_SearchDocument]:
    references = _item_references(item)
    structural = _item_structural_score(item) + recency
    if isinstance(item, AgentMessageItem):
        text, complete = _bounded_text(_item_text(item))
        return (
            [
                _SearchDocument(
                    kind="assistant_message",
                    timestamp=item.started_at,
                    label="Assistant response",
                    fields={"text": text},
                    references=references,
                    structural_score=structural,
                    content_complete=complete,
                )
            ]
            if text
            else []
        )

    input_value = (
        item.command
        if isinstance(item, CommandExecutionItem)
        else getattr(item, "input", None)
    )
    output_value = getattr(item, "output", None)
    input_text, input_complete = _bounded_text(input_value)
    output_text, output_complete = _bounded_text(output_value)
    tool_name = str(getattr(item, "tool_name", None) or "")
    documents: list[_SearchDocument] = []

    if isinstance(item, FileChangeItem):
        path = item.path or _path_from_value(item.input) or ""
        documents.append(
            _SearchDocument(
                kind="file_change",
                timestamp=item.started_at,
                label=_item_label(item),
                fields={
                    "path": path,
                    "operation": item.operation or "",
                    "tool_name": tool_name,
                    "tool_input": input_text,
                    "tool_output": output_text,
                },
                references=references,
                structural_score=structural,
                content_complete=input_complete and output_complete,
            )
        )
        return documents

    if input_text or tool_name:
        fields = {"tool_name": tool_name, "tool_input": input_text}
        if isinstance(item, CommandExecutionItem):
            fields["command"] = input_text
        documents.append(
            _SearchDocument(
                kind="tool_call",
                timestamp=item.started_at,
                label=_item_label(item),
                fields=fields,
                references=references,
                structural_score=structural,
                content_complete=input_complete,
            )
        )
    if output_text:
        documents.append(
            _SearchDocument(
                kind="tool_result",
                timestamp=item.completed_at or item.started_at,
                label=f"{_item_label(item)} result",
                fields={"tool_name": tool_name, "tool_output": output_text},
                references=references,
                structural_score=structural
                + (8 if _item_outcome(item) == "failed" else 0),
                content_complete=output_complete,
            )
        )
    return documents


def _bounded_text(value: Any) -> tuple[str, bool]:
    if value is None:
        return "", True
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            text = str(value)
    text = " ".join(text.split())
    if len(text) <= _SEARCH_FIELD_LIMIT:
        return text, True
    half = (_SEARCH_FIELD_LIMIT - 5) // 2
    return f"{text[:half]} ... {text[-half:]}", False


def _item_text(item: AgentMessageItem) -> str:
    if item.text:
        return item.text
    if item.measurements and item.measurements.text_preview:
        return item.measurements.text_preview
    return ""


def _stringify(value: Any) -> str:
    return _bounded_text(value)[0]


def _tokens(value: str) -> list[str]:
    return [match.group(0).casefold() for match in _TOKEN_RE.finditer(value)]


def _lexical_score(
    document: _SearchDocument,
    *,
    query_folded: str,
    query_terms: tuple[str, ...],
    mode: Literal["text", "path"],
) -> tuple[float, list[str]]:
    field_weights = {
        "path": 16.0,
        "command": 9.0,
        "tool_name": 6.0,
        "operation": 5.0,
        "tool_input": 4.0,
        "text": 4.0,
        "tool_output": 2.0,
    }
    matched_fields: list[str] = []
    score = 0.0
    for name, value in document.fields.items():
        folded = value.casefold()
        if mode == "path" and name != "path":
            continue
        term_hits = sum(1 for term in query_terms if term in folded)
        phrase_hit = bool(query_folded and query_folded in folded)
        if not term_hits and not phrase_hit:
            continue
        matched_fields.append(name)
        weight = field_weights.get(name, 1.0)
        score += term_hits * weight
        if phrase_hit:
            score += 2 * weight
    if mode == "path" and "path" not in matched_fields:
        return 0.0, []
    if query_terms and score:
        all_text = " ".join(document.fields.values()).casefold()
        coverage = sum(1 for term in query_terms if term in all_text) / len(query_terms)
        score *= 0.5 + 0.5 * coverage
    return score, matched_fields


def _search_snippet(document: _SearchDocument, terms: tuple[str, ...]) -> str:
    preferred = (
        "path",
        "command",
        "text",
        "tool_input",
        "tool_output",
        "tool_name",
    )
    for key in preferred:
        value = document.fields.get(key, "")
        folded = value.casefold()
        positions = [folded.find(term) for term in terms if term in folded]
        if not positions:
            continue
        start = max(min(positions) - _SEARCH_SNIPPET_LIMIT // 3, 0)
        snippet = value[start : start + _SEARCH_SNIPPET_LIMIT]
        if start:
            snippet = "..." + snippet
        if start + _SEARCH_SNIPPET_LIMIT < len(value):
            snippet += "..."
        return snippet
    return truncate_text_preview(
        next((value for value in document.fields.values() if value), document.label),
        max_len=_SEARCH_SNIPPET_LIMIT,
    )


def _path_from_value(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("file_path", "path", "target_file", "absolute_path", "file"):
        path = value.get(key)
        if isinstance(path, str) and path.strip():
            return path.strip()
    return None


def _is_retrieval_item(item: Item) -> bool:
    if not isinstance(item, CommandExecutionItem):
        return False
    return bool(_RETRIEVAL_COMMAND_RE.search(_stringify(item.command)))
