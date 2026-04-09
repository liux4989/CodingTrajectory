"""Canonical team state extraction from turn content."""

from __future__ import annotations

import json
import re

from coding_trajectory.ingestion.common import prune_nones
from coding_trajectory.ingestion.models import StepToolItem, TeamMemberState, TeamTaskState, TeamTurnState, Turn

_TEAMMATE_MESSAGE_RE = re.compile(r"<teammate-message(?P<attrs>[^>]*)>(?P<body>.*?)</teammate-message>", re.DOTALL)
_TEAMMATE_ATTR_RE = re.compile(r'(\w+)="(.*?)"')
_TASK_ID_RE = re.compile(r"Task\s*#?\s*(\d+)", re.IGNORECASE)
_NOISY_TEAMMATE_EVENT_TYPES: frozenset[str] = frozenset({
    "idle_notification",
    "teammate_terminated",
    "shutdown_approved",
})


def _parse_teammate_message_blocks(raw: str | None) -> list[dict[str, object]]:
    if not raw:
        return []

    blocks: list[dict[str, object]] = []
    for match in _TEAMMATE_MESSAGE_RE.finditer(raw):
        attrs = {key: value for key, value in _TEAMMATE_ATTR_RE.findall(match.group("attrs") or "")}
        body = (match.group("body") or "").strip()
        payload: dict[str, object] | None = None
        if body.startswith("{") and body.endswith("}"):
            try:
                loaded = json.loads(body)
            except json.JSONDecodeError:
                loaded = None
            if isinstance(loaded, dict):
                payload = loaded
        blocks.append({
            "attrs": attrs,
            "body": body,
            "payload": payload,
        })
    return blocks


def has_teammate_messages(raw: str | None) -> bool:
    return bool(_parse_teammate_message_blocks(raw))


def extract_high_value_teammate_request(raw: str | None) -> str | None:
    lines: list[str] = []
    for block in _parse_teammate_message_blocks(raw):
        attrs = block["attrs"] if isinstance(block["attrs"], dict) else {}
        payload = block["payload"] if isinstance(block["payload"], dict) else None
        body = block["body"] if isinstance(block["body"], str) else ""
        teammate_id = attrs.get("teammate_id") if isinstance(attrs.get("teammate_id"), str) else None
        if not teammate_id or teammate_id == "system":
            continue
        event_type = payload.get("type") if payload else None
        if event_type in _NOISY_TEAMMATE_EVENT_TYPES:
            continue
        summary = attrs.get("summary") if isinstance(attrs.get("summary"), str) else None
        message_text = summary
        if not message_text:
            first_line = next((line.strip() for line in body.splitlines() if line.strip()), None)
            message_text = first_line
        if message_text:
            lines.append(f"{teammate_id}: {message_text}")
    if not lines:
        return None
    return "\n".join(lines)


def _looks_completed(text: str | None) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(token in lowered for token in ("already completed", " complete", " completed", " done"))


def _merge_record(target: dict[str, dict[str, object]], key: str, patch: dict[str, object]) -> None:
    existing = target.get(key)
    if existing is None:
        target[key] = prune_nones(patch)
        return
    for field, value in patch.items():
        if value is None:
            continue
        if field not in existing or existing[field] in (None, [], {}):
            existing[field] = value


def build_turn_team_state(turn: Turn, *, user_request_text: str | None) -> TeamTurnState | None:
    members: dict[str, dict[str, object]] = {}
    tasks: dict[str, dict[str, object]] = {}

    for block in _parse_teammate_message_blocks(user_request_text):
        attrs = block["attrs"] if isinstance(block["attrs"], dict) else {}
        payload = block["payload"] if isinstance(block["payload"], dict) else None
        body = block["body"] if isinstance(block["body"], str) else None
        event_type = payload.get("type") if payload else None
        teammate_id = attrs.get("teammate_id") if isinstance(attrs.get("teammate_id"), str) else None

        if teammate_id and teammate_id != "system" and event_type not in _NOISY_TEAMMATE_EVENT_TYPES:
            _merge_record(
                members,
                teammate_id,
                {
                    "member_id": teammate_id,
                    "color": attrs.get("color"),
                    "summary": attrs.get("summary"),
                },
            )

        task_text = attrs.get("summary") if isinstance(attrs.get("summary"), str) else body
        task_match = _TASK_ID_RE.search(task_text or "")
        if teammate_id and teammate_id != "system" and task_match:
            task_id = task_match.group(1)
            _merge_record(
                tasks,
                task_id,
                {
                    "task_id": task_id,
                    "member_id": teammate_id,
                    "summary": attrs.get("summary"),
                    "status": "completed" if _looks_completed(task_text) else None,
                },
            )

    for step in turn.steps:
        for item in step.items:
            if not isinstance(item, StepToolItem):
                continue
            tool_input = item.input if isinstance(item.input, dict) else {}
            tool_output = item.output if isinstance(item.output, dict) else {}

            if item.tool_name == "Agent":
                member_id = (
                    tool_output.get("teammate_id")
                    or tool_output.get("agent_id")
                    or tool_output.get("name")
                )
                if isinstance(member_id, str) and member_id:
                    _merge_record(
                        members,
                        member_id,
                        {
                            "member_id": member_id,
                            "name": tool_output.get("name"),
                            "team_name": tool_output.get("team_name"),
                            "agent_type": tool_output.get("agent_type"),
                        },
                    )

            elif item.tool_name == "TaskCreate":
                task_info = tool_output.get("task") if isinstance(tool_output.get("task"), dict) else {}
                task_id = task_info.get("id")
                if isinstance(task_id, (str, int)):
                    task_id_str = str(task_id)
                    _merge_record(
                        tasks,
                        task_id_str,
                        {
                            "task_id": task_id_str,
                            "title": task_info.get("subject") or tool_input.get("subject"),
                            "status": "created",
                        },
                    )

            elif item.tool_name == "TaskUpdate":
                task_id = tool_input.get("taskId") or tool_output.get("taskId")
                if isinstance(task_id, (str, int)):
                    task_id_str = str(task_id)
                    _merge_record(
                        tasks,
                        task_id_str,
                        {
                            "task_id": task_id_str,
                            "status": "updated",
                            "blocked_by": [
                                str(value) for value in tool_input.get("addBlockedBy", [])
                                if isinstance(value, (str, int))
                            ],
                            "updated_fields": [
                                str(value) for value in tool_output.get("updatedFields", [])
                                if isinstance(value, str)
                            ],
                        },
                    )

    if not members and not tasks:
        return None

    return TeamTurnState(
        members=[TeamMemberState.model_validate(record) for record in members.values()],
        tasks=[TeamTaskState.model_validate(record) for record in tasks.values()],
    )
