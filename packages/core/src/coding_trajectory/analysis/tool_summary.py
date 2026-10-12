"""Compact, readable per-tool-call summaries for analysis views."""

from __future__ import annotations

from typing import Any

from coding_trajectory.analysis.tool_arguments import tool_argument_detail
from coding_trajectory.ingestion.models import Item, ToolStatus


def pending_plan_actions(value: Any) -> list[str]:
    """Extract explicit unfinished actions from a plan-tool snapshot."""
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


def _activity_metadata(item: Item) -> dict[str, str | bool | int]:
    """Return outcome-bearing activity facts retained into compact sessions.

    Adapters may supply a stricter provenance record.  In its absence a
    canonical command/tool item is agent-produced, and a completed/failed
    lifecycle is the provider's direct completion evidence.  An explicit
    ``outcome=unknown`` prevents the generic lifecycle fallback from inventing
    a terminal result.
    """

    vendor_data = getattr(item, "vendor_data", None)
    raw_activity = (
        vendor_data.get("activity")
        if isinstance(vendor_data, dict)
        and isinstance(vendor_data.get("activity"), dict)
        else {}
    )
    metadata: dict[str, str | bool] = {}
    activity_kind = raw_activity.get("kind")
    if isinstance(activity_kind, str):
        metadata["kind"] = activity_kind
    elif item.kind == "command_execution":
        metadata["kind"] = "command"
    source = raw_activity.get("source")
    metadata["source"] = source if isinstance(source, str) else "agent"

    outcome = raw_activity.get("outcome")
    if outcome not in {"succeeded", "failed", "unknown"}:
        status = getattr(item, "status", None)
        if status in {ToolStatus.FAILED, ToolStatus.FAILED.value, "failed"}:
            outcome = "failed"
        elif status in {
            ToolStatus.COMPLETED,
            ToolStatus.COMPLETED.value,
            "completed",
        }:
            outcome = "succeeded"
        else:
            outcome = "unknown"
    metadata["outcome"] = outcome

    fidelity = raw_activity.get("fidelity")
    if isinstance(fidelity, str):
        metadata["fidelity"] = fidelity
    wrapper_status = raw_activity.get("wrapper_status")
    if isinstance(wrapper_status, str):
        metadata["wrapper_status"] = wrapper_status
    terminal_identity = raw_activity.get("background_terminal_identity")
    if isinstance(terminal_identity, str) and terminal_identity:
        metadata["background_terminal_identity"] = terminal_identity
    return metadata


def summarize_tool_call(item: Item) -> dict[str, Any] | None:
    """Describe every tool by its recorded identity, independent of its input.

    Predefined aliases and semantic inference are disabled. Literal argument
    details and adapter lifecycle evidence remain independent of categorization.
    """
    tool_name_raw = getattr(item, "tool_name", None)
    tool_name = tool_name_raw.strip() if isinstance(tool_name_raw, str) else ""
    if not tool_name:
        if item.kind not in {"tool_call", "command_execution", "file_change", "plan"}:
            return None
        tool_name = item.kind.replace("_", " ").capitalize()

    measurements = getattr(item, "measurements", None)
    result = (
        dict(measurements.tool_summary)
        if measurements is not None and measurements.tool_summary
        else {}
    )
    retained_detail = {
        key: result[key]
        for key in ("description", "description_truncated", "detail", "detail_basis")
        if key in result and result.get("detail_basis") == "arguments"
    }
    # Canonical identity wins over old semantic labels. Retain only descriptions
    # produced by generic argument extraction, never the old optimization path.
    result["name"] = tool_name
    result["classification"] = "identity"
    for key in (
        "description",
        "description_truncated",
        "detail",
        "breakdown",
        "optimization_profile",
        "detail_basis",
    ):
        result.pop(key, None)
    detail = tool_argument_detail(item)
    if detail is not None:
        result.update(
            description=detail.description,
            description_truncated=detail.truncated,
            detail_basis="arguments",
            detail={
                "kind": detail.kind,
                "target": detail.description,
                "scope": detail.scope,
                "truncated": detail.truncated,
                "safety": "sanitized",
            },
        )
    else:
        result.update(retained_detail)

    status = getattr(item, "status", None)
    if status in {ToolStatus.FAILED, ToolStatus.FAILED.value, "failed"}:
        result["status"] = "failed"
    activity = _activity_metadata(item)
    for source, target in (
        ("kind", "activity_kind"),
        ("source", "activity_source"),
        ("outcome", "activity_outcome"),
        ("fidelity", "activity_fidelity"),
        ("wrapper_status", "activity_wrapper_status"),
        ("background_terminal_identity", "background_terminal_identity"),
    ):
        # Preserve retained adapter evidence when its raw metadata was dropped.
        if target not in result and source in activity:
            result[target] = activity[source]

    return result
