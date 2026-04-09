"""View builders for the CLI navigation tree and step evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any
from uuid import UUID

_COMMAND_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)

_MISSING = object()

_STEP_DETAIL_TRUNCATE_LEN = 500


def _truncation_marker(length: int, event_ids: list) -> str:
    """Return an inline truncation marker, e.g. ``[5,234 chars → event.detail abc123]``."""
    ref = " | ".join(str(eid) for eid in event_ids)
    return f"[{length:,} chars → event.detail {ref}]"


def _truncate_with_ref(value: Any, event_ids: list, max_len: int = _STEP_DETAIL_TRUNCATE_LEN) -> Any:
    """Recursively truncate long strings, replacing them with inline markers.

    A truncated value becomes a plain string like::

        "[5,234 chars → event.detail <uuid>]"

    so callers can resolve the full content via ``event.detail <event_id>`` only when needed.
    """
    if isinstance(value, str) and len(value) > max_len:
        return _truncation_marker(len(value), event_ids)
    if isinstance(value, dict):
        return {k: _truncate_with_ref(v, event_ids, max_len) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate_with_ref(v, event_ids, max_len) for v in value]
    return value


def _prune_empty_collections(value: Any) -> Any:
    if isinstance(value, dict):
        pruned: dict[str, Any] = {}
        for key, child in value.items():
            child_pruned = _prune_empty_collections(child)
            if child_pruned in (None, [], {}, ""):
                continue
            pruned[key] = child_pruned
        return pruned
    if isinstance(value, list):
        return [_prune_empty_collections(child) for child in value]
    return value


def _resolve_path(obj: Any, path: str) -> Any:
    """Resolve a dot-separated path into a nested dict, returning _MISSING if absent."""
    cur: Any = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return _MISSING
        cur = cur.get(part, _MISSING)
        if cur is _MISSING:
            return _MISSING
    return cur


def _match_filter(shape: dict[str, Any], expr: str) -> bool:
    """Evaluate one filter expression against a step shape.

    Syntax:
        key=value   exact match (string comparison)
        key=*       key exists and is not None
        key=!       key absent or None
    """
    if "=" not in expr:
        raise ValueError(f"invalid filter expression {expr!r}: expected key=value, key=*, or key=!")
    key, _, value = expr.partition("=")
    resolved = _resolve_path(shape, key)
    if value == "*":
        return resolved is not _MISSING and resolved is not None
    if value == "!":
        return resolved is _MISSING or resolved is None
    return str(resolved) == value

from coding_trajectory.analysis.concepts import TOOL_CONCEPT_MAP, StepType
from coding_trajectory.analysis.structure import build_trajectory_structure
from coding_trajectory.analysis.structure_models import TrajectoryStructure
from coding_trajectory.ingestion.common import prune_nones
from coding_trajectory.ingestion.models import Session, Step, StepTextItem, StepToolItem, ToolStatus, Trajectory, Turn
from coding_trajectory.query import DocumentStore
from coding_trajectory.team_state import extract_high_value_teammate_request, has_teammate_messages


@dataclass(frozen=True)
class _MemberSessionCandidate:
    session_id: str
    started_at: datetime
    ended_at: datetime | None


def _normalize_member_lookup_key(value: str) -> str:
    return value.strip().lower()


def _parse_user_request_info(raw: str, *, session: Session | None = None) -> dict[str, Any] | None:
    """Classify raw user request text into a structured dict."""
    m = _COMMAND_NAME_RE.search(raw)
    if m:
        return {"type": "command", "source": "human_user", "content": m.group(1).strip()}
    stripped = raw.strip()
    if not stripped:
        return None
    if has_teammate_messages(stripped):
        filtered = extract_high_value_teammate_request(stripped)
        if not filtered:
            return None
        source = "parent_agent" if session and session.parent_session_id is not None else "team_lead"
        return {"type": "message", "source": source, "content": filtered}
    return {"type": "message", "source": "human_user", "content": stripped}


def _extract_user_request(store: DocumentStore, turn: Turn, *, session: Session | None = None) -> dict[str, Any] | None:
    if turn.user_request_event_id is None:
        return None
    try:
        event = store.get_event(turn.user_request_event_id)
    except Exception:
        return None
    for key in ("text", "message", "content"):
        value = event.payload.get(key)
        if isinstance(value, str) and value.strip():
            return _parse_user_request_info(value, session=session)
    return None


def _latest_human_user_request(
    store: DocumentStore,
    session: Session,
    *,
    before: datetime | None = None,
) -> dict[str, Any] | None:
    turns = sorted(session.turns, key=lambda item: (item.started_at, item.sequence), reverse=True)
    for turn in turns:
        if before is not None and turn.started_at > before:
            continue
        request = _extract_user_request(store, turn, session=session)
        if request and request.get("source") == "human_user":
            return request
    return None


def _incoming_operation(structure: TrajectoryStructure, session_id: UUID):
    for op in structure.operations:
        if op.target_session_id == session_id:
            return op
    return None


def _resolve_originating_human_request(
    store: DocumentStore,
    session: Session,
    *,
    structure: TrajectoryStructure,
) -> dict[str, Any] | None:
    current_session = session
    visited: set[UUID] = set()
    cutoff: datetime | None = None

    while current_session.parent_session_id is not None and current_session.session_id not in visited:
        visited.add(current_session.session_id)
        try:
            parent_session = store.get_session(current_session.parent_session_id)
        except Exception:
            return None

        incoming_op = _incoming_operation(structure, current_session.session_id)
        if incoming_op is not None and incoming_op.source_turn_id is not None:
            try:
                source_turn = store.get_turn(incoming_op.source_turn_id)
            except Exception:
                source_turn = None
            if source_turn is not None:
                request = _extract_user_request(store, source_turn, session=parent_session)
                if request and request.get("source") == "human_user":
                    return request
                cutoff = source_turn.started_at
            else:
                cutoff = current_session.started_at
        else:
            cutoff = current_session.started_at

        request = _latest_human_user_request(store, parent_session, before=cutoff)
        if request is not None:
            return request
        current_session = parent_session

    return None


def _effective_user_request(
    store: DocumentStore,
    turn: Turn,
    *,
    session: Session,
    structure: TrajectoryStructure,
) -> dict[str, Any] | None:
    request = _extract_user_request(store, turn, session=session)
    if request is None:
        return None
    if request.get("source") != "parent_agent":
        return request
    return _resolve_originating_human_request(store, session, structure=structure)


# Commands that carry no work output — UI state, config, or session management only.
# Derived from Claude Code and Codex CLI official command references.
_LOW_VALUE_COMMANDS: frozenset[str] = frozenset({
    # Context / session reset
    "/clear", "/reset", "/new",
    # Context management
    "/compact", "/context",
    # Cost / usage display
    "/cost", "/usage", "/stats",
    # Exit
    "/exit", "/quit",
    # Help / info
    "/help", "/release-notes",
    # Settings / config UI
    "/config", "/settings",
    # Model / mode toggles
    "/model", "/fast", "/effort", "/vim",
    # Visual / terminal config
    "/theme", "/color", "/statusline", "/keybindings", "/terminal-setup",
    # Auth
    "/login", "/logout",
    # Status display
    "/status",
    # Non-work / marketing
    "/stickers", "/mobile", "/ios", "/android", "/upgrade", "/privacy-settings",
    # Codex-specific low-value
    "/personality", "/debug-config",
})


def _is_low_value_turn(steps: list, user_request: dict[str, Any] | None) -> bool:
    """Return True for turns that carry no work output and should be hidden in overview."""
    if not steps:
        return True
    if user_request and user_request.get("type") == "command":
        return user_request.get("content") in _LOW_VALUE_COMMANDS
    return False


def _classify_step(step: Step) -> StepType:
    tool_items = [item for item in step.items if isinstance(item, StepToolItem)]
    if not tool_items:
        return StepType.ASSISTANT_RESPONSE
    for item in tool_items:
        if item.tool_name:
            concept = TOOL_CONCEPT_MAP.get(item.tool_name)
            if concept is not None:
                return concept
    return StepType.TOOL_CALL


def _session_connection(session: Session, *, structure: TrajectoryStructure) -> dict[str, Any]:
    node = structure.session_tree.nodes_by_session_id.get(session.session_id)
    if node is None or (node.is_root and not node.incoming_edge_type):
        return {"role": "main"}
    return prune_nones({
        "relationship": node.incoming_edge_type,
        "parent_session_id": str(node.parent_session_id) if node.parent_session_id else None,
    })


def _include_session_in_overview(session: Session, *, structure: TrajectoryStructure) -> bool:
    node = structure.session_tree.nodes_by_session_id.get(session.session_id)
    if node is None:
        return True
    return node.incoming_edge_type != "spawned_subagent"


def build_trajectory_overview(trajectory: Trajectory, *, store: DocumentStore) -> dict[str, Any]:
    structure = build_trajectory_structure(trajectory)
    sessions_by_id = {s.session_id: s for s in trajectory.sessions}
    member_session_lookup = _build_member_session_lookup(trajectory)

    ordered: list[dict[str, Any]] = []
    visited: set[UUID] = set()
    queue = list(structure.session_tree.root_session_ids)
    while queue:
        session_id = queue.pop(0)
        if session_id in visited:
            continue
        visited.add(session_id)
        session = sessions_by_id.get(session_id)
        if session and _include_session_in_overview(session, structure=structure):
            ordered.append(
                _session_nav_node(
                    session,
                    store=store,
                    structure=structure,
                    member_session_lookup=member_session_lookup,
                )
            )
        node = structure.session_tree.nodes_by_session_id.get(session_id)
        if node:
            queue.extend(node.child_session_ids)

    for session in trajectory.sessions:
        if session.session_id not in visited and _include_session_in_overview(session, structure=structure):
            ordered.append(
                _session_nav_node(
                    session,
                    store=store,
                    structure=structure,
                    member_session_lookup=member_session_lookup,
                )
            )

    return {
        "trajectory_id": str(trajectory.trajectory_id),
        "sessions": ordered,
    }


def _session_nav_node(
    session: Session,
    *,
    store: DocumentStore,
    structure: TrajectoryStructure,
    member_session_lookup: dict[str, list[_MemberSessionCandidate]],
) -> dict[str, Any]:
    turns: list[dict[str, Any]] = []
    pending_teammate: dict[str, Any] | None = None

    for turn in session.turns:
        node = _turn_nav_node(
            turn,
            session=session,
            store=store,
            structure=structure,
            member_session_lookup=member_session_lookup,
        )
        if node is None:
            continue

        if "teammate_summary" not in node:
            if pending_teammate is not None:
                turns.append(pending_teammate)
                pending_teammate = None
            turns.append(node)
            continue

        request = node.get("user_request")
        request_source = request.get("source") if isinstance(request, dict) else None
        if pending_teammate is None:
            pending_teammate = node
            continue

        if request_source == "human_user":
            turns.append(pending_teammate)
            pending_teammate = node
            continue

        pending_teammate = _merge_teammate_turn_nodes(pending_teammate, node)

    if pending_teammate is not None:
        turns.append(pending_teammate)

    return {
        "turns": turns,
    }


def _merge_ordered_unique(existing: list[str], incoming: list[str]) -> list[str]:
    seen = set(existing)
    merged = list(existing)
    for value in incoming:
        if value not in seen:
            seen.add(value)
            merged.append(value)
    return merged


def _merge_member_records(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    merge_keys: list[set[str]] = []

    for record in existing + incoming:
        member_id = record.get("member_id")
        if not isinstance(member_id, str) or not member_id:
            continue

        record_keys = set(_member_lookup_keys(member_id, record.get("name") if isinstance(record.get("name"), str) else None))
        record_session_id = record.get("session_id") if isinstance(record.get("session_id"), str) else None

        target_index: int | None = None
        for idx, current in enumerate(merged):
            current_session_id = current.get("session_id") if isinstance(current.get("session_id"), str) else None
            if record_session_id and current_session_id and record_session_id == current_session_id:
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


def _merge_task_records(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
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


def _merge_teammate_turn_nodes(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    current_summary = current.get("teammate_summary")
    incoming_summary = incoming.get("teammate_summary")
    if not isinstance(current_summary, dict) or not isinstance(incoming_summary, dict):
        return current

    merged_summary = dict(current_summary)
    merged_summary["lead_flow"] = [
        *[item for item in current_summary.get("lead_flow", []) if isinstance(item, dict)],
        *[item for item in incoming_summary.get("lead_flow", []) if isinstance(item, dict)],
    ]
    merged_summary["members"] = _merge_member_records(
        [item for item in current_summary.get("members", []) if isinstance(item, dict)],
        [item for item in incoming_summary.get("members", []) if isinstance(item, dict)],
    )
    merged_summary["tasks"] = _merge_task_records(
        [item for item in current_summary.get("tasks", []) if isinstance(item, dict)],
        [item for item in incoming_summary.get("tasks", []) if isinstance(item, dict)],
    )
    merged_summary["step_ids"] = _merge_ordered_unique(
        [item for item in current_summary.get("step_ids", []) if isinstance(item, str)],
        [item for item in incoming_summary.get("step_ids", []) if isinstance(item, str)],
    )

    merged = dict(current)
    merged["teammate_summary"] = prune_nones(merged_summary)
    if merged.get("user_request") is None:
        merged.pop("user_request", None)
    return merged


def _build_member_session_lookup(trajectory: Trajectory) -> dict[str, list[_MemberSessionCandidate]]:
    lookup: dict[str, dict[str, _MemberSessionCandidate]] = {}
    for session in trajectory.sessions:
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
        session_candidate = _MemberSessionCandidate(
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
            key=lambda item: (item.started_at, item.ended_at or item.started_at, item.session_id),
        )
        for key, session_map in lookup.items()
    }


_TEXT_PREVIEW_LEN = 300
_TEAM_TASK_ID_RE = re.compile(r"Task\s*#?\s*(\d+)", re.IGNORECASE)
_LEAD_ERROR_RE = re.compile(r"\b(error|failed|failure|exception|traceback)\b", re.IGNORECASE)
_LEAD_CHECK_RESULT_RE = re.compile(r"\b(clean|passed|success|succeeded|no output|all solid)\b", re.IGNORECASE)


def _collapse_tool_sequence(tools: list[str], failed: set[int]) -> str:
    """Collapse a flat list of tool names into a compact flow string, e.g. 'Read → Grep×3 → Edit×2'.

    Indices in *failed* mark tools that errored; they are annotated with ``!``,
    e.g. ``Edit!`` or ``Edit!×2``.
    """
    groups: list[str] = []
    i = 0
    while i < len(tools):
        tool = tools[i]
        count = 1
        is_failed = i in failed
        while i + count < len(tools) and tools[i + count] == tool and (i + count in failed) == is_failed:
            count += 1
        suffix = "!" if is_failed else ""
        groups.append(f"{tool}{suffix}×{count}" if count > 1 else f"{tool}{suffix}")
        i += count
    return " → ".join(groups)


_FILE_PATH_KEYS = ("file_path", "path", "file", "pattern")


def _extract_files(items: list) -> list[str]:
    """Extract unique file basenames from tool input fields."""
    from coding_trajectory.ingestion.models import StepToolItem
    import os
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if not isinstance(item, StepToolItem) or not isinstance(item.input, dict):
            continue
        for key in _FILE_PATH_KEYS:
            val = item.input.get(key)
            if isinstance(val, str) and val:
                name = os.path.basename(val.rstrip("/"))
                if name and name not in seen:
                    seen.add(name)
                    result.append(name)
    return result


def _build_flows(steps: list[Step]) -> list[dict[str, Any]]:
    """Build an interleaved narrative flow from a flat step list.

    Consecutive tool calls are collapsed into a single ``tool_calls`` entry.
    Each assistant response becomes an ``agent_response`` entry.
    """
    result: list[dict[str, Any]] = []
    pending_tools: list[str] = []
    pending_failed: set[int] = set()
    pending_items: list[StepToolItem] = []

    def _flush_tools() -> None:
        if not pending_tools:
            return
        entry: dict[str, Any] = {"tool_calls": _collapse_tool_sequence(pending_tools, pending_failed)}
        files = _extract_files(pending_items)
        if files:
            entry["files"] = files
        result.append(entry)
        pending_tools.clear()
        pending_failed.clear()
        pending_items.clear()

    for step in steps:
        tool_items = [item for item in step.items if isinstance(item, StepToolItem)]
        if tool_items:
            for item in tool_items:
                if item.tool_name:
                    idx = len(pending_tools)
                    pending_tools.append(item.tool_name)
                    if item.status == ToolStatus.FAILED:
                        pending_failed.add(idx)
            pending_items.extend(tool_items)
        else:
            _flush_tools()
            text_items = [item for item in step.items if isinstance(item, StepTextItem)]
            text = "\n".join(item.text for item in text_items if item.text).strip()
            if text:
                preview = text[:_TEXT_PREVIEW_LEN] + ("…" if len(text) > _TEXT_PREVIEW_LEN else "")
                result.append({"agent_response": preview})

    _flush_tools()

    return result


def _build_lead_flow(turn: Turn, *, user_request: dict[str, Any] | None) -> list[dict[str, Any]]:
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

    for item in _build_flows(turn.steps):
        if "tool_calls" in item:
            flow.append({"type": "lead_tool_calls", **item})
        elif "agent_response" in item:
            flow.extend(_build_lead_text_events(item["agent_response"]))

    return flow


def _build_lead_text_events(text: str) -> list[dict[str, Any]]:
    normalized = text.strip()
    if not normalized:
        return []

    # Preserve detailed wrap-up messages as responses instead of flattening them
    # into status lines.
    if len(normalized) > 160 or "\n\n" in normalized or any(token in normalized for token in ("|", "- `", "**")):
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


def _is_teammate_turn(session: Session, turn: Turn, *, user_request: dict[str, Any] | None) -> bool:
    if session.parent_session_id is not None:
        return False
    return turn.team_state is not None and bool(turn.team_state.members or turn.team_state.tasks)


def _build_teammate_summary(
    turn: Turn,
    *,
    user_request: dict[str, Any] | None,
    member_session_lookup: dict[str, list[_MemberSessionCandidate]],
) -> dict[str, Any]:
    if turn.team_state is None:
        return {
            "lead_flow": _build_lead_flow(turn, user_request=user_request),
            "members": [],
            "tasks": [],
            "step_ids": [str(step.step_id) for step in turn.steps],
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
        members.append(_prune_empty_collections(member_data))
    return {
        "lead_flow": _build_lead_flow(turn, user_request=user_request),
        "members": members,
        "tasks": [_project_teammate_task(task.model_dump(mode="json")) for task in turn.team_state.tasks],
        "step_ids": [str(step.step_id) for step in turn.steps],
    }


def _project_teammate_task(task: dict[str, Any]) -> dict[str, Any]:
    return _prune_empty_collections(prune_nones({
        "task_id": task.get("task_id"),
        "title": task.get("title"),
        "status": task.get("status"),
        "member_id": task.get("member_id"),
    }))


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
    member_session_lookup: dict[str, list[_MemberSessionCandidate]],
) -> str | None:
    candidates_by_session: dict[str, _MemberSessionCandidate] = {}
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

    completed_before_turn = [item for item in candidates if item.ended_at is not None and item.ended_at <= turn_start]
    if completed_before_turn:
        latest_end = max(item.ended_at for item in completed_before_turn if item.ended_at is not None)
        latest = [item for item in completed_before_turn if item.ended_at == latest_end]
        if len(latest) == 1:
            return latest[0].session_id

    active_during_turn = [
        item
        for item in candidates
        if item.started_at <= turn_end and (item.ended_at is None or item.ended_at >= turn_start)
    ]
    if len(active_during_turn) == 1:
        return active_during_turn[0].session_id

    spawned_in_turn = [item for item in candidates if turn_start <= item.started_at <= turn_end]
    if spawned_in_turn:
        earliest_start = min(item.started_at for item in spawned_in_turn)
        earliest = [item for item in spawned_in_turn if item.started_at == earliest_start]
        if len(earliest) == 1:
            return earliest[0].session_id

    return None


def _turn_nav_node(
    turn: Turn,
    *,
    session: Session,
    store: DocumentStore,
    structure: TrajectoryStructure,
    member_session_lookup: dict[str, list[_MemberSessionCandidate]],
) -> dict[str, Any] | None:
    user_request = _effective_user_request(store, turn, session=session, structure=structure)
    visible_user_request = user_request
    if isinstance(user_request, dict) and user_request.get("source") == "team_lead":
        visible_user_request = None
    if _is_low_value_turn(turn.steps, user_request):
        return None
    if _is_teammate_turn(session, turn, user_request=user_request):
        return prune_nones({
            "turn_id": str(turn.turn_id),
            "user_request": visible_user_request,
            "teammate_summary": _build_teammate_summary(
                turn,
                user_request=user_request,
                member_session_lookup=member_session_lookup,
            ),
        })
    return prune_nones({
        "turn_id": str(turn.turn_id),
        "user_request": user_request,
        "work_summary": {
            "flows": _build_flows(turn.steps),
            "step_ids": [str(step.step_id) for step in turn.steps],
        },
    })


# ---------------------------------------------------------------------------
# Step details
# ---------------------------------------------------------------------------

def _assistant_response_shape(step: Step) -> dict[str, Any]:
    text_items = [item for item in step.items if isinstance(item, StepTextItem)]
    text = "\n".join(item.text for item in text_items if item.text).strip() or None
    stop_reason = step.vendor_data.get("stop_reason")
    usage_raw = step.vendor_data.get("usage")
    usage = usage_raw if isinstance(usage_raw, dict) else None
    return prune_nones({"text": text, "stop_reason": stop_reason, "usage": usage})


def _tool_call_shape(tool_items: list[StepToolItem]) -> dict[str, Any]:
    if len(tool_items) == 1:
        item = tool_items[0]
        return _truncate_with_ref(prune_nones({
            "tool_name": item.tool_name,
            "tool_input": item.input,
            "tool_output": item.output,
        }), item.event_ids)
    return {
        "tools": [
            _truncate_with_ref(prune_nones({
                "tool_name": item.tool_name,
                "tool_input": item.input,
                "tool_output": item.output,
            }), item.event_ids)
            for item in tool_items
        ]
    }


def _lookup_target_session(step: Step, *, store: DocumentStore, edge_type: str) -> str | None:
    try:
        session = store.get_session(step.session_id)
        trajectory = store.get_trajectory(session.trajectory_id)
        structure = build_trajectory_structure(trajectory)
        for op in structure.operations:
            if op.source_step_id == step.step_id and op.edge_type == edge_type:
                return str(op.target_session_id)
    except Exception:
        pass
    return None


def _plan_subagent_shape(step: Step, *, store: DocumentStore) -> dict[str, Any]:
    tool_items = [item for item in step.items if isinstance(item, StepToolItem)]
    spawn_item = next(
        (item for item in tool_items if TOOL_CONCEPT_MAP.get(item.tool_name or "") == "plan_subagent"),
        tool_items[0] if tool_items else None,
    )
    agent_session_id = _lookup_target_session(step, store=store, edge_type="spawned_subagent")
    if spawn_item is None:
        return prune_nones({"agent_session_id": agent_session_id})
    return prune_nones({
        "agent_input": spawn_item.input,
        "agent_output": spawn_item.output,
        "agent_session_id": agent_session_id,
    })


def _todo_list_shape(tool_items: list[StepToolItem]) -> dict[str, Any]:
    return _tool_call_shape(tool_items)


def _session_handoff_shape(step: Step, *, store: DocumentStore) -> dict[str, Any]:
    tool_items = [item for item in step.items if isinstance(item, StepToolItem)]
    handoff_item = next(
        (item for item in tool_items if TOOL_CONCEPT_MAP.get(item.tool_name or "") == "session_handoff"),
        tool_items[0] if tool_items else None,
    )
    handoff_session_id = _lookup_target_session(step, store=store, edge_type="handoff_to")
    if handoff_item is None:
        return prune_nones({"handoff_session_id": handoff_session_id})
    return _truncate_with_ref(prune_nones({
        "handoff_input": handoff_item.input,
        "handoff_session_id": handoff_session_id,
    }), handoff_item.event_ids)


def build_step_details(step: Step, *, store: DocumentStore) -> dict[str, Any]:
    step_type = _classify_step(step)
    tool_items = [item for item in step.items if isinstance(item, StepToolItem)]

    if step_type == StepType.ASSISTANT_RESPONSE:
        operations: list[str] = ["text_reply"]
        shape = _assistant_response_shape(step)
    elif step_type == StepType.PLAN_SUBAGENT:
        operations = ["spawn", "collect_result"]
        shape = _plan_subagent_shape(step, store=store)
    elif step_type == StepType.TODO_LIST:
        operations = ["update"]
        shape = _todo_list_shape(tool_items)
    elif step_type == StepType.SESSION_HANDOFF:
        operations = ["handoff"]
        shape = _session_handoff_shape(step, store=store)
    else:
        operations = [item.tool_name for item in tool_items if item.tool_name]
        shape = _tool_call_shape(tool_items)

    return prune_nones({
        "step_id": str(step.step_id),
        "type": step_type,
        "operations": operations or None,
        "shape": shape or None,
        "event_ids": [str(eid) for eid in step.event_ids] or None,
    })


# ---------------------------------------------------------------------------
# Event scan
# ---------------------------------------------------------------------------

_EVENT_SCAN_PAYLOAD_PREVIEW_LEN = 300


def _truncate_payload_strings(obj: Any, max_len: int = _EVENT_SCAN_PAYLOAD_PREVIEW_LEN) -> Any:
    """Recursively truncate long strings in an event payload for scan output."""
    if isinstance(obj, str):
        if len(obj) > max_len:
            return f"[{len(obj):,} chars]"
        return obj
    if isinstance(obj, dict):
        return {k: _truncate_payload_strings(v, max_len) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_truncate_payload_strings(v, max_len) for v in obj]
    return obj


def build_event_scan(
    trajectory: Trajectory,
    *,
    event_type: str,
    filters: list[str] | None = None,
) -> dict[str, Any]:
    """Flatten all events across sessions and return those matching *event_type*.

    Each ``--filter key=value`` expression is ANDed and applied against the
    event's ``payload`` dict.  Supports dot-path keys, ``key=*`` (exists),
    and ``key=!`` (absent).
    """
    from coding_trajectory.ingestion.models import EventType

    # Validate event_type against known values
    valid_types = {e.value for e in EventType}
    if event_type not in valid_types:
        valid = ", ".join(sorted(valid_types))
        raise ValueError(f"unknown event type {event_type!r}. Valid types: {valid}")

    matches: list[dict[str, Any]] = []

    for session in trajectory.sessions:
        for event in session.events:
            if event.type.value != event_type:
                continue
            payload = event.payload
            if filters:
                if not all(_match_filter(payload, f) for f in filters):
                    continue
            matches.append(prune_nones({
                "event_id": str(event.event_id),
                "session_id": str(event.session_id),
                "timestamp": event.timestamp.isoformat().replace("+00:00", "Z") if event.timestamp else None,
                "type": event.type.value,
                "payload": _truncate_payload_strings(payload) or None,
            }))

    return {
        "trajectory_id": str(trajectory.trajectory_id),
        "type": event_type,
        "matches": matches,
    }
