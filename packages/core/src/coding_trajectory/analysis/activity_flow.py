"""Activity-flow projection helpers shared by overview and teammate views."""

from __future__ import annotations

from typing import Any

from coding_trajectory.analysis.projection_utils import truncate_text_preview
from coding_trajectory.analysis.tool_optimization import tool_optimization_profile
from coding_trajectory.ingestion.common import prune_nones
from coding_trajectory.ingestion.models import AgentMessageItem, Item

_OVERVIEW_TEXT_PREVIEW_LEN = 220

_TOOL_SHAPED_KINDS: frozenset[str] = frozenset(
    {"tool_call", "command_execution", "file_change", "plan"}
)


def build_flows(items: list[Item]) -> list[dict[str, Any]]:
    from coding_trajectory.analysis.tool_summary import summarize_tool_call

    result: list[dict[str, Any]] = []
    for item in items:
        if item.kind in _TOOL_SHAPED_KINDS:
            summary = summarize_tool_call(item)
            if summary is not None:
                summary.setdefault("item_id", str(item.item_id))
                result.append({"type": "tool_call", **summary})
            continue
        if isinstance(item, AgentMessageItem):
            text = (item.text or "").strip()
            if text:
                result.append(
                    {
                        "type": "assistant_response",
                        "text": text,
                        "item_id": str(item.item_id),
                    }
                )
    return _group_consecutive_tool_calls(result)


def build_overview_flows(items: list[Item]) -> list[dict[str, Any]]:
    return _compact_overview_items(build_flows(items))


def _compact_overview_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    tool_groups: dict[tuple[str, str | None, str | None], dict[str, Any]] = {}

    for item in items:
        if item.get("type") == "assistant_response":
            text = _truncate_text(item.get("text"))
            if text:
                compacted.append({"text": text})
            continue

        if item.get("type") not in {"tool_call", "tool_call_group"}:
            compacted.append(_compact_flow_item(item))
            continue

        key = (
            str(item.get("name") or ""),
            item.get("status") if isinstance(item.get("status"), str) else None,
            _profile_name(item),
        )
        if key not in tool_groups:
            group = _new_overview_tool_group(item)
            tool_groups[key] = group
            compacted.append(group)
            continue
        _merge_overview_tool_group(tool_groups[key], item)

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
            }
        )

    if item.get("type") == "tool_call":
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
            }
        )

    return item


def _new_overview_tool_group(item: dict[str, Any]) -> dict[str, Any]:
    profile = tool_optimization_profile(
        str(item.get("name") or ""),
        _profile_name(item),
    )
    descriptions = _tool_descriptions(item)
    group: dict[str, Any] = {
        "tool": item.get("name"),
        "status": item.get("status"),
        "count": int(item.get("count") or 1),
        profile.detail_list_key: descriptions or None,
        "item_ids": [item["item_id"]] if isinstance(item.get("item_id"), str) else [],
    }
    return group


def _merge_overview_tool_group(group: dict[str, Any], item: dict[str, Any]) -> None:
    profile = tool_optimization_profile(
        str(item.get("name") or ""),
        _profile_name(item),
    )
    group["count"] = int(group.get("count") or 0) + int(item.get("count") or 1)
    detail_key = profile.detail_list_key
    existing = group.get(detail_key)
    if not isinstance(existing, list):
        existing = []
        group[detail_key] = existing
    descriptions = _tool_descriptions(item)
    existing.extend(
        description for description in descriptions if description not in existing
    )
    item_ids = group.get("item_ids")
    if isinstance(item_ids, list):
        incoming = item.get("item_id")
        if isinstance(incoming, str) and incoming not in item_ids:
            item_ids.append(incoming)


def _tool_descriptions(item: dict[str, Any]) -> list[str]:
    if item.get("type") == "tool_call_group":
        descriptions = item.get("descriptions")
        if isinstance(descriptions, list):
            return [
                description
                for description in descriptions
                if isinstance(description, str) and description
            ]
        return []
    description = item.get("description")
    if isinstance(description, str) and description:
        return [description]
    return []


def _truncate_text(
    value: Any, *, limit: int = _OVERVIEW_TEXT_PREVIEW_LEN
) -> str | None:
    text = truncate_text_preview(value, max_len=limit)
    if not text:
        return None
    return text


def _group_consecutive_tool_calls(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    def flush_pending() -> None:
        if not pending:
            return
        grouped.extend(_project_tool_run(pending))
        pending.clear()

    for item in items:
        if item.get("type") != "tool_call":
            flush_pending()
            grouped.append(item)
            continue

        if pending and not _same_groupable_tool_call(pending[-1], item):
            flush_pending()
        pending.append(item)

    flush_pending()
    return grouped


def _same_groupable_tool_call(left: dict[str, Any], right: dict[str, Any]) -> bool:
    profile = tool_optimization_profile(
        str(left.get("name") or ""),
        _profile_name(left),
    )
    if not profile.group_repeated:
        return False
    return (
        left.get("name") == right.get("name")
        and left.get("status") == right.get("status")
        and _profile_name(left) == _profile_name(right)
    )


def _project_tool_run(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(items) == 1:
        return [items[0]]

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
