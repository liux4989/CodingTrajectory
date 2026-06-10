"""Claude Code team-message and teammate-task mechanism helpers."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from coding_trajectory.ingestion.common import prune_nones
from coding_trajectory.ingestion.models import (
    Item,
    TeamMemberState,
    TeamTaskState,
    TeamTurnState,
    Turn,
)

_NOISY_TEAMMATE_EVENT_TYPES: frozenset[str] = frozenset({
    "idle_notification",
    "teammate_terminated",
    "shutdown_approved",
})


class ClaudeTeamMessage(BaseModel):
    teammate_id: str | None = None
    color: str | None = None
    summary: str | None = None
    body: str | None = None
    event_type: str | None = None


class ClaudeTeamStateInput(BaseModel):
    messages: list[ClaudeTeamMessage] = Field(default_factory=list)


def high_value_teammate_request(messages: list[ClaudeTeamMessage]) -> str | None:
    lines: list[str] = []
    for message in messages:
        if not _is_actionable_message(message):
            continue
        message_text = message.summary or _first_non_empty_line(message.body)
        if message_text:
            lines.append(f"{message.teammate_id}: {message_text}")
    return "\n".join(lines) if lines else None


def build_turn_team_state(turn: Turn, *, team_input: ClaudeTeamStateInput) -> TeamTurnState | None:
    members: dict[str, dict[str, object]] = {}
    tasks: dict[str, dict[str, object]] = {}

    for message in team_input.messages:
        if not _is_actionable_message(message):
            continue
        teammate_id = message.teammate_id
        if teammate_id is None:
            continue
        _merge_record(
            members,
            teammate_id,
            {
                "member_id": teammate_id,
                "color": message.color,
                "summary": message.summary,
            },
        )

        task_text = message.summary or message.body
        task_id = _task_id_from_text(task_text)
        if task_id is not None:
            _merge_record(
                tasks,
                task_id,
                {
                    "task_id": task_id,
                    "member_id": teammate_id,
                    "summary": message.summary,
                    "status": "completed" if _looks_completed(task_text) else None,
                },
            )

    for item in _turn_tool_items(turn):
        _merge_tool_item(members=members, tasks=tasks, item=item)

    if not members and not tasks:
        return None

    return TeamTurnState(
        members=[TeamMemberState.model_validate(record) for record in members.values()],
        tasks=[TeamTaskState.model_validate(record) for record in tasks.values()],
    )


def _is_actionable_message(message: ClaudeTeamMessage) -> bool:
    return bool(
        message.teammate_id
        and message.teammate_id != "system"
        and message.event_type not in _NOISY_TEAMMATE_EVENT_TYPES
    )


def _first_non_empty_line(raw: str | None) -> str | None:
    if not raw:
        return None
    return next((line.strip() for line in raw.splitlines() if line.strip()), None)


def _task_id_from_text(raw: str | None) -> str | None:
    if not raw:
        return None
    lowered = raw.lower()
    marker = "task"
    index = lowered.find(marker)
    if index < 0:
        return None
    suffix = raw[index + len(marker):].lstrip(" #")
    digits = []
    for char in suffix:
        if not char.isdigit():
            break
        digits.append(char)
    return "".join(digits) or None


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


def _turn_tool_items(turn: Turn) -> list[Item]:
    return [
        item
        for item in turn.items
        if item.kind in {"tool_call", "command_execution", "file_change", "plan"}
    ]


def _merge_tool_item(
    *,
    members: dict[str, dict[str, object]],
    tasks: dict[str, dict[str, object]],
    item: Item,
) -> None:
    tool_input = item.input if isinstance(item.input, dict) else {}  # type: ignore[attr-defined]
    tool_output = item.output if isinstance(item.output, dict) else {}  # type: ignore[attr-defined]
    tool_name = getattr(item, "tool_name", None)

    if tool_name == "Agent":
        member_id = _first_str(tool_output, ("teammate_id", "agent_id", "name"))
        if member_id:
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
        return

    if tool_name == "TaskCreate":
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
        return

    if tool_name == "TaskUpdate":
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


def _first_str(source: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    return None
