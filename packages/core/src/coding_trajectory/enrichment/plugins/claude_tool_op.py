"""Enrichment plugin: interprets Claude Code-specific tool calls on a Step."""

from __future__ import annotations

from typing import Any

from coding_trajectory.ingestion.models import Step, StepToolItem, Vendor
from coding_trajectory.query import DocumentStore

from ..base import EnrichmentPlugin
from ..models import EnrichmentOverlay

def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _task_op(tool_name: str, inp: Any, out: Any) -> dict[str, Any]:
    inp, out = _as_dict(inp), _as_dict(out)
    op = tool_name.removeprefix("Task").lower()  # create | update | get | list | stop | output
    result: dict[str, Any] = {"operation": op}
    if op == "create":
        task = out.get("task", {})
        result["task_id"]    = task.get("id") or inp.get("id")
        result["subject"]    = task.get("subject") or inp.get("subject")
        result["status"]     = task.get("status", "todo")
        result["blocks"]     = inp.get("blocks", [])
        result["blocked_by"] = inp.get("blockedBy", [])
    elif op == "update":
        result["task_id"] = inp.get("taskId") or inp.get("id")
        result["status"]  = inp.get("status")
        result["owner"]   = inp.get("owner")
    else:
        result["task_id"] = inp.get("taskId") or inp.get("id")
    return result


def _team_op(tool_name: str, inp: Any, out: Any) -> dict[str, Any]:
    inp, out = _as_dict(inp), _as_dict(out)
    op = tool_name.removeprefix("Team").lower()  # create | delete
    result: dict[str, Any] = {"operation": op, "team_name": inp.get("team_name") or out.get("team_name")}
    if op == "create":
        members = inp.get("members", [])
        result["member_count"] = len(members)
        result["member_names"] = [m.get("name") for m in members if isinstance(m, dict)]
    return result


def _message(inp: Any, out: Any) -> dict[str, Any]:
    inp = _as_dict(inp)
    return {"to": inp.get("to"), "summary": inp.get("summary")}


class ClaudeToolOpPlugin(EnrichmentPlugin):
    """Interprets Claude Code agentic tool calls (Agent, Task*, Team*, SendMessage)
    and exposes structured overlays under the ``claude.tool_op`` plugin namespace.

    Output shape:
        plugins["claude.tool_op"] = {
            "task_ops":  [...],   # TaskCreate/Update/List/… — task record mutations
            "team_ops":  [...],   # TeamCreate/Delete — team lifecycle
            "messages":  [...],   # SendMessage — inter-agent messages
        }

    Agent/Task spawns are NOT captured here — those are already expressed as
    TrajectoryEdge entries (spawned_subagent, teammate_of) at the trajectory level.

    Only runs on Steps from Claude Code sessions.
    """

    namespace = "claude.tool_op"

    def enrich_step(
        self,
        step: Step,
        *,
        store: DocumentStore | None = None,
    ) -> EnrichmentOverlay | None:
        if step.vendor != Vendor.CLAUDE_CODE:
            return None

        task_ops: list[dict[str, Any]] = []
        team_ops: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = []

        for item in step.items:
            if not isinstance(item, StepToolItem) or not item.tool_name:
                continue
            name = item.tool_name
            if name.startswith("Task") and name != "Task":
                task_ops.append(_task_op(name, item.input, item.output))
            elif name.startswith("Team"):
                team_ops.append(_team_op(name, item.input, item.output))
            elif name == "SendMessage":
                messages.append(_message(item.input, item.output))

        payload: dict[str, Any] = {}
        if task_ops:
            payload["task_ops"] = task_ops
        if team_ops:
            payload["team_ops"] = team_ops
        if messages:
            payload["messages"] = messages

        if not payload:
            return None

        overlay = EnrichmentOverlay()
        overlay.plugins[self.namespace] = payload
        return overlay
