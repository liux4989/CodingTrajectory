"""Activity-flow projection helpers shared by overview and teammate views."""

from __future__ import annotations

from typing import Any

from coding_trajectory.analysis.tool_optimization import tool_optimization_profile
from coding_trajectory.ingestion.common import prune_nones
from coding_trajectory.ingestion.models import Step, StepTextItem, StepToolItem


def build_flows(steps: list[Step]) -> list[dict[str, Any]]:
    from coding_trajectory.analysis.tool_summary import summarize_tool_call

    result: list[dict[str, Any]] = []
    for step in steps:
        for item in step.items:
            if isinstance(item, StepToolItem):
                summary = summarize_tool_call(item)
                if summary is not None:
                    result.append({"type": "tool_call", **summary})
                continue
            if isinstance(item, StepTextItem):
                text = item.text.strip()
                if text:
                    result.append({"type": "assistant_response", "text": text})
    return _group_consecutive_tool_calls(result)


def build_compact_flows(steps: list[Step]) -> list[dict[str, Any]]:
    return [_compact_flow_item(item) for item in build_flows(steps)]


def _compact_flow_item(item: dict[str, Any]) -> dict[str, Any]:
    if item.get("type") == "assistant_response":
        return {"text": item.get("text")}

    if item.get("type") == "tool_call_group":
        profile = tool_optimization_profile(
            str(item.get("name") or ""),
            _profile_name(item),
        )
        return prune_nones({
            "tool": item.get("name"),
            "status": item.get("status"),
            "count": item.get("count"),
            profile.detail_list_key: item.get("descriptions"),
            profile.detail_counts_key or "": item.get("description_counts"),
        })

    if item.get("type") == "tool_call":
        profile = tool_optimization_profile(
            str(item.get("name") or ""),
            _profile_name(item),
        )
        return prune_nones({
            "tool": item.get("name"),
            "status": item.get("status"),
            profile.detail_key: item.get("description"),
        })

    return item


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
    description_counts = _description_counts(descriptions) if profile.dedupe_repeated_details else None
    if profile.dedupe_repeated_details:
        descriptions = list(dict.fromkeys(descriptions))
    grouped = prune_nones({
        "type": "tool_call_group",
        "name": items[0].get("name"),
        "optimization_profile": _profile_name(items[0]),
        "status": items[0].get("status"),
        "count": len(items),
        "descriptions": descriptions or None,
        "description_counts": description_counts,
    })
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
    repeated = {description: count for description, count in counts.items() if count > 1}
    return repeated or None
