"""View builders for the CLI navigation tree and step evidence."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

_COMMAND_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)

_MISSING = object()

_STEP_DETAIL_TRUNCATE_LEN = 500


def _truncate_with_ref(value: Any, event_ids: list, max_len: int = _STEP_DETAIL_TRUNCATE_LEN) -> Any:
    """Recursively truncate long strings, replacing them with event-ref objects.

    A truncated value becomes::

        {"$truncated": true, "preview": "<first N chars>…", "event_ids": ["<uuid>", ...]}

    so callers can resolve the full content via ``event.detail <event_id>``.
    """
    if isinstance(value, str) and len(value) > max_len:
        return {
            "$truncated": True,
            "preview": value[:max_len] + "…",
            "event_ids": [str(eid) for eid in event_ids],
        }
    if isinstance(value, dict):
        return {k: _truncate_with_ref(v, event_ids, max_len) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate_with_ref(v, event_ids, max_len) for v in value]
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

from coding_trajectory.analysis.concepts import TOOL_CONCEPT_MAP
from coding_trajectory.analysis.structure import build_trajectory_structure
from coding_trajectory.analysis.structure_models import TrajectoryStructure
from coding_trajectory.ingestion.models import Session, Step, StepTextItem, StepToolItem, Trajectory, Turn
from coding_trajectory.query import DocumentStore
from coding_trajectory.service import prune_nones


def _parse_user_request_info(raw: str) -> dict[str, Any]:
    """Classify raw user request text into a structured dict."""
    m = _COMMAND_NAME_RE.search(raw)
    if m:
        return {"type": "command", "name": m.group(1).strip()}
    stripped = raw.strip()
    # System-only noise (e.g. empty local-command-stdout wrappers)
    if not stripped or re.fullmatch(r"<[^>]+>\s*</[^>]+>", stripped):
        return {"type": "system"}
    return {"type": "message", "text": stripped}


def _extract_user_request(store: DocumentStore, turn: Turn) -> dict[str, Any] | None:
    if turn.user_request_event_id is None:
        return None
    try:
        event = store.get_event(turn.user_request_event_id)
    except Exception:
        return None
    for key in ("text", "message", "content"):
        value = event.payload.get(key)
        if isinstance(value, str) and value.strip():
            return _parse_user_request_info(value)
    return None


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
        return user_request.get("name") in _LOW_VALUE_COMMANDS
    return False


def _classify_step(step: Step) -> str:
    tool_items = [item for item in step.items if isinstance(item, StepToolItem)]
    if not tool_items:
        return "assistant_response"
    for item in tool_items:
        if item.tool_name:
            concept = TOOL_CONCEPT_MAP.get(item.tool_name)
            if concept:
                return concept
    return "tool_call"


def _session_connection(session: Session, *, structure: TrajectoryStructure) -> dict[str, Any]:
    node = structure.session_tree.nodes_by_session_id.get(session.session_id)
    if node is None or (node.is_root and not node.incoming_edge_type):
        return {"role": "main"}
    return prune_nones({
        "relationship": node.incoming_edge_type,
        "parent_session_id": str(node.parent_session_id) if node.parent_session_id else None,
    })


def build_trajectory_overview(trajectory: Trajectory, *, store: DocumentStore) -> dict[str, Any]:
    structure = build_trajectory_structure(trajectory)
    sessions_by_id = {s.session_id: s for s in trajectory.sessions}

    ordered: list[dict[str, Any]] = []
    visited: set[UUID] = set()
    queue = list(structure.session_tree.root_session_ids)
    while queue:
        session_id = queue.pop(0)
        if session_id in visited:
            continue
        visited.add(session_id)
        session = sessions_by_id.get(session_id)
        if session:
            ordered.append(_session_nav_node(session, store=store, structure=structure))
        node = structure.session_tree.nodes_by_session_id.get(session_id)
        if node:
            queue.extend(node.child_session_ids)

    for session in trajectory.sessions:
        if session.session_id not in visited:
            ordered.append(_session_nav_node(session, store=store, structure=structure))

    return {
        "trajectory_id": str(trajectory.trajectory_id),
        "sessions": ordered,
    }


def _session_nav_node(session: Session, *, store: DocumentStore, structure: TrajectoryStructure) -> dict[str, Any]:
    turns = [n for turn in session.turns if (n := _turn_nav_node(turn, store=store)) is not None]
    return {
        "session_id": str(session.session_id),
        "connection": _session_connection(session, structure=structure),
        "turns": turns,
    }


_TEXT_PREVIEW_LEN = 120


def _collapse_tool_sequence(tools: list[str]) -> str:
    """Collapse a flat list of tool names into a compact flow string, e.g. 'Read → Grep×3 → Edit×2'."""
    groups: list[str] = []
    i = 0
    while i < len(tools):
        tool = tools[i]
        count = 1
        while i + count < len(tools) and tools[i + count] == tool:
            count += 1
        groups.append(f"{tool}×{count}" if count > 1 else tool)
        i += count
    return " → ".join(groups)


def _build_flows(steps: list[Step]) -> list[dict[str, Any]]:
    """Build an interleaved narrative flow from a flat step list.

    Consecutive tool calls are collapsed into a single ``tool_calls`` entry.
    Each assistant response becomes an ``agent_response`` entry.
    """
    result: list[dict[str, Any]] = []
    pending_tools: list[str] = []

    for step in steps:
        tool_items = [item for item in step.items if isinstance(item, StepToolItem)]
        if tool_items:
            for item in tool_items:
                if item.tool_name:
                    pending_tools.append(item.tool_name)
        else:
            if pending_tools:
                result.append({"tool_calls": _collapse_tool_sequence(pending_tools)})
                pending_tools = []
            text_items = [item for item in step.items if isinstance(item, StepTextItem)]
            text = "\n".join(item.text for item in text_items if item.text).strip()
            if text:
                preview = text[:_TEXT_PREVIEW_LEN] + ("…" if len(text) > _TEXT_PREVIEW_LEN else "")
                result.append({"agent_response": preview})

    if pending_tools:
        result.append({"tool_calls": _collapse_tool_sequence(pending_tools)})

    return result


def _turn_nav_node(turn: Turn, *, store: DocumentStore) -> dict[str, Any] | None:
    user_request = _extract_user_request(store, turn)
    if _is_low_value_turn(turn.steps, user_request):
        return None
    return prune_nones({
        "turn_id": str(turn.turn_id),
        "user_request": user_request,
        "steps": {
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

    if step_type == "assistant_response":
        operations: list[str] = ["text_reply"]
        shape = _assistant_response_shape(step)
    elif step_type == "plan_subagent":
        operations = ["spawn", "collect_result"]
        shape = _plan_subagent_shape(step, store=store)
    elif step_type == "todo_list":
        operations = ["update"]
        shape = _todo_list_shape(tool_items)
    elif step_type == "session_handoff":
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
# Trajectory scan
# ---------------------------------------------------------------------------

_SCAN_STRING_PREVIEW_LEN = 300


def _truncate_shape_strings(obj: Any, event_ids: list | None = None, max_len: int = _SCAN_STRING_PREVIEW_LEN) -> Any:
    """Recursively truncate long strings in a shape dict for scan output.

    When *event_ids* are provided, truncated strings become event-ref objects
    (same structure as ``_truncate_with_ref``) so callers can resolve full
    content via ``event.detail``.  Already-truncated ref objects are passed
    through unchanged.
    """
    if isinstance(obj, str):
        if len(obj) > max_len:
            if event_ids:
                return {"$truncated": True, "preview": obj[:max_len] + "…", "event_ids": [str(eid) for eid in event_ids]}
            return obj[:max_len] + "…"
        return obj
    if isinstance(obj, dict):
        if "$truncated" in obj:  # already a ref — pass through unchanged
            return obj
        return {k: _truncate_shape_strings(v, event_ids, max_len) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_truncate_shape_strings(v, event_ids, max_len) for v in obj]
    return obj


def build_trajectory_scan(
    trajectory: Trajectory,
    *,
    store: DocumentStore,
    step_type: str,
    filters: list[str] | None = None,
) -> dict[str, Any]:
    """Flatten the trajectory tree and return all steps matching *step_type*.

    Each ``--filter key=value`` expression is ANDed and applied against the
    step's ``shape`` dict.  Supports dot-path keys (e.g. ``tool_output.error``),
    ``key=*`` (exists), and ``key=!`` (absent).
    """
    # Assistant-response and plan-subagent shapes contain narrative text —
    # truncating them would hide meaning. Only truncate tool_call shapes.
    _NO_TRUNCATE_TYPES = {"assistant_response", "plan_subagent"}
    matches: list[dict[str, Any]] = []

    for session in trajectory.sessions:
        for turn in session.turns:
            user_request = _extract_user_request(store, turn)
            for step in turn.steps:
                if _classify_step(step) != step_type:
                    continue
                detail = build_step_details(step, store=store)
                shape: dict[str, Any] = detail.get("shape") or {}
                if filters:
                    if not all(_match_filter(shape, f) for f in filters):
                        continue
                if step_type in _NO_TRUNCATE_TYPES:
                    truncated_shape = shape
                else:
                    truncated_shape = _truncate_shape_strings(shape, event_ids=step.event_ids)
                matches.append(prune_nones({
                    "step_id": str(step.step_id),
                    "session_id": str(session.session_id),
                    "turn_id": str(turn.turn_id),
                    "user_request": user_request,
                    "shape": truncated_shape or None,
                    "event_ids": [str(eid) for eid in step.event_ids] or None,
                }))

    return {
        "trajectory_id": str(trajectory.trajectory_id),
        "type": step_type,
        "matches": matches,
    }
