"""Teammate/team-state projection helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any

from coding_trajectory.analysis.activity_flow import build_flows
from coding_trajectory.analysis.projection_utils import prune_empty_collections
from coding_trajectory.ingestion.common import prune_nones
from coding_trajectory.ingestion.models import Session, Turn

_TEAM_TASK_ID_RE = re.compile(r"Task\s*#?\s*(\d+)", re.IGNORECASE)
_LEAD_ERROR_RE = re.compile(
    r"\b(error|failed|failure|exception|traceback)\b", re.IGNORECASE
)
_LEAD_CHECK_RESULT_RE = re.compile(
    r"\b(clean|passed|success|succeeded|no output|all solid)\b", re.IGNORECASE
)


@dataclass(frozen=True)
class MemberSessionCandidate:
    session_id: str
    started_at: datetime
    ended_at: datetime | None


def build_member_session_lookup(
    session_graph,
) -> dict[str, list[MemberSessionCandidate]]:
    lookup: dict[str, dict[str, MemberSessionCandidate]] = {}
    for session in session_graph.sessions:
        candidates: set[str] = set()
        if session.agent_name:
            candidates.add(session.agent_name)
            if "@" in session.agent_name:
                candidates.add(session.agent_name.split("@", 1)[0])
        extensions = session.extensions
        if extensions and extensions.claude_code:
            if extensions.claude_code.agent_name:
                agent_name = extensions.claude_code.agent_name
                candidates.add(agent_name)
                if "@" in agent_name:
                    candidates.add(agent_name.split("@", 1)[0])
            if extensions.claude_code.agent_role:
                candidates.add(extensions.claude_code.agent_role)
        session_candidate = MemberSessionCandidate(
            session_id=str(session.session_id),
            started_at=session.started_at,
            ended_at=session.ended_at,
        )
        for candidate in candidates:
            key = _normalize_member_lookup_key(candidate)
            if not key:
                continue
            lookup.setdefault(key, {})
            lookup[key].setdefault(session_candidate.session_id, session_candidate)
    return {
        key: sorted(
            session_map.values(),
            key=lambda item: (
                item.started_at,
                item.ended_at or item.started_at,
                item.session_id,
            ),
        )
        for key, session_map in lookup.items()
    }


def merge_teammate_turn_nodes(
    current: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    current_summary = current.get("teammate_summary")
    incoming_summary = incoming.get("teammate_summary")
    if not isinstance(current_summary, dict) or not isinstance(incoming_summary, dict):
        return current

    merged_summary = dict(current_summary)
    merged_summary["lead_flow"] = [
        *[
            item
            for item in current_summary.get("lead_flow", [])
            if isinstance(item, dict)
        ],
        *[
            item
            for item in incoming_summary.get("lead_flow", [])
            if isinstance(item, dict)
        ],
    ]
    merged_summary["members"] = _merge_member_records(
        [item for item in current_summary.get("members", []) if isinstance(item, dict)],
        [
            item
            for item in incoming_summary.get("members", [])
            if isinstance(item, dict)
        ],
    )
    merged_summary["tasks"] = _merge_task_records(
        [item for item in current_summary.get("tasks", []) if isinstance(item, dict)],
        [item for item in incoming_summary.get("tasks", []) if isinstance(item, dict)],
    )
    merged_summary["item_ids"] = _merge_ordered_unique(
        [item for item in current_summary.get("item_ids", []) if isinstance(item, str)],
        [
            item
            for item in incoming_summary.get("item_ids", [])
            if isinstance(item, str)
        ],
    )

    merged = dict(current)
    merged["teammate_summary"] = prune_nones(merged_summary)
    if merged.get("user_request") is None:
        merged.pop("user_request", None)
    return merged


def is_teammate_turn(
    session: Session, turn: Turn, *, user_request: dict[str, Any] | None
) -> bool:
    if session.parent_session_id is not None:
        return False
    return turn.team_state is not None and bool(
        turn.team_state.members or turn.team_state.tasks
    )


def build_teammate_summary(
    turn: Turn,
    *,
    user_request: dict[str, Any] | None,
    member_session_lookup: dict[str, list[MemberSessionCandidate]],
) -> dict[str, Any]:
    if turn.team_state is None:
        return {
            "lead_flow": _build_lead_flow(turn, user_request=user_request),
            "members": [],
            "tasks": [],
            "item_ids": [str(item.item_id) for item in turn.items],
        }

    members: list[dict[str, Any]] = []
    for member in turn.team_state.members:
        member_data = prune_nones(member.model_dump(mode="json"))
        if "session_id" not in member_data:
            session_id = _resolve_member_session_id(
                turn,
                member_id=member.member_id,
                member_name=member.name,
                member_session_lookup=member_session_lookup,
            )
            if session_id is not None:
                member_data["session_id"] = session_id
        members.append(prune_empty_collections(member_data))
    return {
        "lead_flow": _build_lead_flow(turn, user_request=user_request),
        "members": members,
        "tasks": [
            _project_teammate_task(task.model_dump(mode="json"))
            for task in turn.team_state.tasks
        ],
        "item_ids": [str(item.item_id) for item in turn.items],
    }


def _normalize_member_lookup_key(value: str) -> str:
    return value.strip().lower()


def _merge_ordered_unique(existing: list[str], incoming: list[str]) -> list[str]:
    seen = set(existing)
    merged = list(existing)
    for value in incoming:
        if value not in seen:
            seen.add(value)
            merged.append(value)
    return merged


def _merge_member_records(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    merge_keys: list[set[str]] = []

    for record in existing + incoming:
        member_id = record.get("member_id")
        if not isinstance(member_id, str) or not member_id:
            continue

        record_keys = set(
            _member_lookup_keys(
                member_id,
                record.get("name") if isinstance(record.get("name"), str) else None,
            )
        )
        record_session_id = (
            record.get("session_id")
            if isinstance(record.get("session_id"), str)
            else None
        )

        target_index: int | None = None
        for idx, current in enumerate(merged):
            current_session_id = (
                current.get("session_id")
                if isinstance(current.get("session_id"), str)
                else None
            )
            if (
                record_session_id
                and current_session_id
                and record_session_id == current_session_id
            ):
                target_index = idx
                break
            if record_keys and merge_keys[idx].intersection(record_keys):
                target_index = idx
                break

        if target_index is None:
            merged.append(dict(record))
            merge_keys.append(record_keys)
            continue

        target = merged[target_index]
        merge_keys[target_index].update(record_keys)
        for key, value in record.items():
            if key == "member_id" or value in (None, "", [], {}):
                continue
            target[key] = value

    return merged


def _merge_task_records(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in existing + incoming:
        task_id = record.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        if task_id not in merged:
            merged[task_id] = dict(record)
            order.append(task_id)
            continue
        target = merged[task_id]
        for key, value in record.items():
            if key == "task_id" or value in (None, "", {}, []):
                continue
            if key in {"blocked_by", "updated_fields"} and isinstance(value, list):
                target[key] = _merge_ordered_unique(
                    [item for item in target.get(key, []) if isinstance(item, str)],
                    [item for item in value if isinstance(item, str)],
                )
            else:
                target[key] = value
    return [merged[task_id] for task_id in order]


def _build_lead_flow(
    turn: Turn, *, user_request: dict[str, Any] | None
) -> list[dict[str, Any]]:
    flow: list[dict[str, Any]] = []

    if (
        user_request
        and user_request.get("type") == "message"
        and user_request.get("source") == "team_lead"
        and isinstance(user_request.get("content"), str)
    ):
        for raw_line in user_request["content"].splitlines():
            line = raw_line.strip()
            if not line:
                continue
            member_id, separator, summary = line.partition(":")
            event = {
                "type": "teammate_update",
                "summary": line if not separator else summary.strip(),
            }
            if separator:
                event["member_id"] = member_id.strip()
                task_match = _TEAM_TASK_ID_RE.search(summary)
                if task_match:
                    event["task_id"] = task_match.group(1)
            flow.append(event)

    for item in build_flows(turn.items):
        if item.get("type") == "tool_call":
            flow.append({"type": "lead_tool_call", **_public_tool_flow_item(item)})
        elif item.get("type") == "tool_call_group":
            flow.append(
                {"type": "lead_tool_call_group", **_public_tool_flow_item(item)}
            )
        elif item.get("type") == "assistant_response":
            flow.extend(_build_lead_text_events(item["text"]))

    return flow


def _public_tool_flow_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in item.items()
        if key not in {"type", "optimization_profile"}
    }


def _build_lead_text_events(text: str) -> list[dict[str, Any]]:
    normalized = text.strip()
    if not normalized:
        return []

    if (
        len(normalized) > 160
        or "\n\n" in normalized
        or any(token in normalized for token in ("|", "- `", "**"))
    ):
        return [{"type": "lead_response", "agent_response": normalized}]

    events: list[dict[str, Any]] = []
    for raw_line in normalized.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^[\-\*\u2022]\s*", "", line)
        if not line:
            continue
        if _LEAD_ERROR_RE.search(line):
            events.append({"type": "lead_error", "summary": line})
        elif _LEAD_CHECK_RESULT_RE.search(line):
            events.append({"type": "lead_check_result", "summary": line})
        else:
            events.append({"type": "lead_status", "summary": line})

    return events or [{"type": "lead_response", "agent_response": normalized}]


def _project_teammate_task(task: dict[str, Any]) -> dict[str, Any]:
    return prune_empty_collections(
        prune_nones(
            {
                "task_id": task.get("task_id"),
                "title": task.get("title"),
                "status": task.get("status"),
                "member_id": task.get("member_id"),
            }
        )
    )


def _member_lookup_keys(member_id: str, member_name: str | None) -> list[str]:
    keys: list[str] = []
    for raw in (member_id, member_name):
        if not raw:
            continue
        values = [raw]
        if "@" in raw:
            values.append(raw.split("@", 1)[0])
        for value in values:
            key = _normalize_member_lookup_key(value)
            if key and key not in keys:
                keys.append(key)
    return keys


def _resolve_member_session_id(
    turn: Turn,
    *,
    member_id: str,
    member_name: str | None,
    member_session_lookup: dict[str, list[MemberSessionCandidate]],
) -> str | None:
    candidates_by_session: dict[str, MemberSessionCandidate] = {}
    for key in _member_lookup_keys(member_id, member_name):
        for candidate in member_session_lookup.get(key, []):
            candidates_by_session.setdefault(candidate.session_id, candidate)

    candidates = list(candidates_by_session.values())
    if len(candidates) == 1:
        return candidates[0].session_id
    if not candidates:
        return None

    turn_start = turn.started_at
    turn_end = turn.ended_at or turn.started_at

    completed_before_turn = [
        item
        for item in candidates
        if item.ended_at is not None and item.ended_at <= turn_start
    ]
    if completed_before_turn:
        latest_end = max(
            item.ended_at for item in completed_before_turn if item.ended_at is not None
        )
        latest = [item for item in completed_before_turn if item.ended_at == latest_end]
        if len(latest) == 1:
            return latest[0].session_id

    active_during_turn = [
        item
        for item in candidates
        if item.started_at <= turn_end
        and (item.ended_at is None or item.ended_at >= turn_start)
    ]
    if len(active_during_turn) == 1:
        return active_during_turn[0].session_id

    spawned_in_turn = [
        item for item in candidates if turn_start <= item.started_at <= turn_end
    ]
    if spawned_in_turn:
        earliest_start = min(item.started_at for item in spawned_in_turn)
        earliest = [
            item for item in spawned_in_turn if item.started_at == earliest_start
        ]
        if len(earliest) == 1:
            return earliest[0].session_id

    return None
