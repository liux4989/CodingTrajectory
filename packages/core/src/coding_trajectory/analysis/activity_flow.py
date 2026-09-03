"""Activity-flow projection helpers shared by overview and teammate views."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from coding_trajectory.analysis.projection_utils import truncate_text_preview
from coding_trajectory.analysis.tool_optimization import tool_optimization_profile
from coding_trajectory.analysis.tool_summary_shared import (
    LIST_FILES,
    READ_FILE,
    RUN_COMMAND,
    SEARCH_TEXT,
)
from coding_trajectory.ingestion.common import prune_nones
from coding_trajectory.ingestion.models import AgentMessageItem, Item

_OVERVIEW_TEXT_PREVIEW_LEN = 220

_TOOL_SHAPED_KINDS: frozenset[str] = frozenset(
    {"tool_call", "command_execution", "file_change", "plan"}
)
_OUTCOMELESS_ACTIVITY_KINDS = frozenset(
    {"background_terminal_wait", "background_terminal_interaction"}
)
_EXPLORATION_CONCEPTS: frozenset[str] = frozenset({READ_FILE, SEARCH_TEXT, LIST_FILES})
_EXPLORATION_CELL_KEY = ("exploration", None, None)
_COMMAND_CELL_KEY = ("command", None, None)
_MAX_ACTIVITY_CELL_ITEMS = 32


class _ToolActivityCell(BaseModel):
    """One compactable, temporally contiguous activity cell."""

    group_key: tuple[str, str | None, str | None]
    items: list[dict[str, Any]]


def build_flows(items: list[Item]) -> list[dict[str, Any]]:
    from coding_trajectory.analysis.tool_summary import summarize_tool_call

    result: list[dict[str, Any]] = []
    for item in items:
        if item.kind in _TOOL_SHAPED_KINDS:
            summary = summarize_tool_call(item)
            if summary is not None:
                if summary.get("activity_hidden") is True:
                    continue
                if summary.get("activity_kind") == "command":
                    # TUI cells represent an executed command as a command even
                    # when CT's deeper semantic classifier recognizes its shell
                    # intent as read/search/list.  Keep that semantic evidence
                    # in the item summary while the overview mirrors the cell.
                    summary["name"] = RUN_COMMAND
                    summary["optimization_profile"] = "activity:command"
                    summary["description"] = summary.get("command") or summary.get(
                        "description"
                    )
                summary.setdefault("item_id", str(item.item_id))
                result.append({"type": "tool_call", **summary})
            continue
        if isinstance(item, AgentMessageItem):
            measurements = getattr(item, "measurements", None)
            if measurements is not None:
                if measurements.text_preview:
                    result.append(
                        {
                            "type": "assistant_response",
                            "text": measurements.text_preview,
                            "item_id": str(item.item_id),
                        }
                    )
                continue
            text = (item.text or "").strip()
            if text:
                result.append(
                    {
                        "type": "assistant_response",
                        "text": text,
                        "item_id": str(item.item_id),
                    }
                )
    return _project_activity_cells(result)


def build_overview_flows(items: list[Item]) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for item in build_flows(items):
        if item.get("type") == "assistant_response":
            text = _truncate_text(item.get("text"))
            if text:
                item_id = item.get("item_id")
                compacted.append(
                    prune_nones(
                        {
                            "text": text,
                            "item_ids": [item_id] if isinstance(item_id, str) else None,
                        }
                    )
                )
            continue

        compacted.append(_compact_flow_item(item))

    return [prune_nones(item) for item in compacted]


def _compact_flow_item(item: dict[str, Any]) -> dict[str, Any]:
    if item.get("type") == "assistant_response":
        return {"text": item.get("text")}

    if item.get("type") == "tool_call_group":
        profile = tool_optimization_profile(
            str(item.get("name") or ""),
            _profile_name(item),
        )
        return prune_nones(
            {
                "tool": item.get("name"),
                "status": item.get("status"),
                "count": item.get("count"),
                profile.detail_list_key: item.get("descriptions"),
                profile.detail_counts_key or "": item.get("description_counts"),
                "item_ids": item.get("item_ids"),
                "outcome": item.get("activity_outcome"),
                "wrapper_status": item.get("activity_wrapper_status"),
            }
        )

    if item.get("type") == "background_terminal_wait":
        return prune_nones(
            {
                "tool": "Waited for background terminal",
                "count": item.get("count"),
                "item_ids": item.get("item_ids"),
            }
        )

    if item.get("type") == "tool_call":
        outcome_bearing = (
            item.get("activity_kind") not in _OUTCOMELESS_ACTIVITY_KINDS
        )
        profile = tool_optimization_profile(
            str(item.get("name") or ""),
            _profile_name(item),
        )
        return prune_nones(
            {
                "tool": item.get("name"),
                "status": item.get("status"),
                profile.detail_key: item.get("description"),
                "item_ids": [item.get("item_id")]
                if isinstance(item.get("item_id"), str)
                else None,
                "outcome": (
                    item.get("activity_outcome") if outcome_bearing else None
                ),
                "wrapper_status": (
                    item.get("activity_wrapper_status") if outcome_bearing else None
                ),
            }
        )

    return item


def _truncate_text(
    value: Any, *, limit: int = _OVERVIEW_TEXT_PREVIEW_LEN
) -> str | None:
    text = truncate_text_preview(value, max_len=limit)
    if not text:
        return None
    return text


def _project_activity_cells(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project ordered tool facts into Codex-style activity cells.

    The active cell only accepts compatible, agent-originated successful tool
    facts. Every other item flushes it, preserving the temporal boundary for
    messages, commands, web activity, mutations, failures, and unknown forms.
    """

    projected: list[dict[str, Any]] = []
    active: _ToolActivityCell | None = None

    def flush_active() -> None:
        nonlocal active
        if active is None:
            return
        projected.extend(_project_tool_activity_cell(active))
        active = None

    for item in items:
        group_key = _tool_activity_group_key(item)
        if group_key is None:
            flush_active()
            projected.append(item)
            continue

        if (
            active is not None
            and active.group_key == group_key
            and len(active.items) < _MAX_ACTIVITY_CELL_ITEMS
        ):
            active.items.append(item)
            continue

        flush_active()
        active = _ToolActivityCell(group_key=group_key, items=[item])

    flush_active()
    return projected


def _tool_activity_group_key(
    item: dict[str, Any],
) -> tuple[str, str | None, str | None] | None:
    if item.get("type") != "tool_call":
        return None
    if item.get("activity_source") != "agent":
        return None

    # Empty write_stdin calls are not commands. Codex presents a contiguous
    # same-terminal polling run as one terminal wait, even though persisted
    # history cannot prove when the underlying process completed.
    if item.get("activity_kind") == "background_terminal_wait":
        identity = item.get("background_terminal_identity")
        if (
            isinstance(identity, str)
            and identity
            and item.get("activity_wrapper_status") == "completed"
        ):
            return ("background_terminal_wait", identity, None)
        return None
    if item.get("activity_outcome") != "succeeded":
        return None

    if item.get("activity_kind") == "command":
        return _COMMAND_CELL_KEY

    name = str(item.get("name") or "")
    profile_name = _profile_name(item)
    profile = tool_optimization_profile(name, profile_name)
    if not profile.group_repeated:
        return None
    if name in _EXPLORATION_CONCEPTS:
        return _EXPLORATION_CELL_KEY
    return ("repeated", name, profile_name)


def _project_tool_activity_cell(cell: _ToolActivityCell) -> list[dict[str, Any]]:
    if cell.group_key[0] == "background_terminal_wait":
        return [_project_background_terminal_wait_cell(cell.items)]
    if len(cell.items) == 1:
        return [cell.items[0]]
    if cell.group_key == _COMMAND_CELL_KEY:
        return [_project_command_cell(cell.items)]
    if cell.group_key == _EXPLORATION_CELL_KEY:
        return [_project_exploration_cell(cell.items)]
    return [_project_repeated_tool_cell(cell.items)]


def _project_background_terminal_wait_cell(
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Collapse one contiguous empty-stdin polling streak by terminal identity."""

    item_ids = [
        item["item_id"]
        for item in items
        if isinstance(item.get("item_id"), str) and item.get("item_id")
    ]
    return prune_nones(
        {
            "type": "background_terminal_wait",
            "count": len(items),
            "item_ids": item_ids or None,
        }
    )


def _project_exploration_cell(items: list[dict[str, Any]]) -> dict[str, Any]:
    targets: list[str] = []
    for item in items:
        target = _exploration_target(item)
        if target is not None:
            targets.append(target)
    item_ids = [
        item["item_id"]
        for item in items
        if isinstance(item.get("item_id"), str) and item.get("item_id")
    ]
    return prune_nones(
        {
            "type": "tool_call_group",
            "name": "Explore",
            "optimization_profile": "activity:exploration",
            "count": len(items),
            "descriptions": targets or None,
            "item_ids": item_ids or None,
        }
    )


def _project_command_cell(items: list[dict[str, Any]]) -> dict[str, Any]:
    commands = [
        item["description"]
        for item in items
        if isinstance(item.get("description"), str) and item.get("description")
    ]
    item_ids = [
        item["item_id"]
        for item in items
        if isinstance(item.get("item_id"), str) and item.get("item_id")
    ]
    return prune_nones(
        {
            "type": "tool_call_group",
            "name": RUN_COMMAND,
            "optimization_profile": "activity:command",
            "count": len(items),
            "descriptions": commands or None,
            "item_ids": item_ids or None,
        }
    )


def _exploration_target(item: dict[str, Any]) -> str | None:
    description = item.get("description")
    if not isinstance(description, str) or not description:
        return None
    verb = {
        READ_FILE: "read",
        SEARCH_TEXT: "search",
        LIST_FILES: "list",
    }.get(str(item.get("name") or ""), "explore")
    return f"{verb} {description}"


def _project_repeated_tool_cell(items: list[dict[str, Any]]) -> dict[str, Any]:
    profile = tool_optimization_profile(
        str(items[0].get("name") or ""),
        _profile_name(items[0]),
    )
    descriptions = [
        item["description"]
        for item in items
        if isinstance(item.get("description"), str) and item.get("description")
    ]
    item_ids = [
        item["item_id"]
        for item in items
        if isinstance(item.get("item_id"), str) and item.get("item_id")
    ]
    description_counts = (
        _description_counts(descriptions) if profile.dedupe_repeated_details else None
    )
    if profile.dedupe_repeated_details:
        descriptions = list(dict.fromkeys(descriptions))
    grouped = prune_nones(
        {
            "type": "tool_call_group",
            "name": items[0].get("name"),
            "optimization_profile": _profile_name(items[0]),
            "status": items[0].get("status"),
            "count": len(items),
            "descriptions": descriptions or None,
            "description_counts": description_counts,
            "item_ids": item_ids or None,
        }
    )
    return [grouped]


def _profile_name(item: dict[str, Any]) -> str | None:
    profile = item.get("optimization_profile")
    if isinstance(profile, str) and profile:
        return profile
    return None


def _description_counts(descriptions: list[str]) -> dict[str, int] | None:
    counts: dict[str, int] = {}
    for description in descriptions:
        counts[description] = counts.get(description, 0) + 1
    repeated = {
        description: count for description, count in counts.items() if count > 1
    }
    return repeated or None
