"""Codex-specific session enrichment derived from canonical session data."""

from __future__ import annotations

from typing import Any

from coding_trajectory.ingestion.models import Session, StepToolItem, Vendor

from .base import EnrichmentPlugin
from .models import EnrichmentNote, EnrichmentOverlay


def _as_non_empty_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _as_plan_items(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []

    items: list[dict[str, str]] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        step = _as_non_empty_str(entry.get("step"))
        status = _as_non_empty_str(entry.get("status"))
        if step is None or status is None:
            continue
        items.append({"step": step, "status": status})
    return items


def _iter_step_tools(session: Session):
    for turn in session.turns:
        for step in turn.steps:
            for item in step.items:
                if isinstance(item, StepToolItem):
                    yield step, item


class CodexWorkflowPlugin(EnrichmentPlugin):
    """Adds Codex session-level workflow references."""

    namespace = "codex.workflow"

    def enrich_session(
        self,
        session: Session,
        *,
        store=None,
    ) -> EnrichmentOverlay | None:
        if session.vendor != Vendor.CODEX_CLI:
            return None

        plans: list[dict[str, Any]] = []
        spawned_agents: list[dict[str, Any]] = []

        for step, item in _iter_step_tools(session):
            if item.tool_name == "update_plan" and isinstance(item.input, dict):
                plan_items = _as_plan_items(item.input.get("plan"))
                if plan_items:
                    entry: dict[str, Any] = {
                        "source_step_id": str(step.step_id),
                        "tool_name": "update_plan",
                        "items": plan_items,
                    }
                    tool_call_id = _as_non_empty_str(item.tool_call_id)
                    if tool_call_id is not None:
                        entry["tool_call_id"] = tool_call_id
                    explanation = _as_non_empty_str(item.input.get("explanation"))
                    if explanation is not None:
                        entry["explanation"] = explanation
                    plans.append(entry)

            if item.tool_name == "spawn_agent":
                spawn_input = item.input if isinstance(item.input, dict) else {}
                spawn_output = item.output if isinstance(item.output, dict) else {}
                entry = {
                    "source_step_id": str(step.step_id),
                    "tool_name": "spawn_agent",
                }
                tool_call_id = _as_non_empty_str(item.tool_call_id)
                if tool_call_id is not None:
                    entry["tool_call_id"] = tool_call_id
                agent_type = _as_non_empty_str(spawn_input.get("agent_type"))
                if agent_type is not None:
                    entry["agent_type"] = agent_type
                role = _as_non_empty_str(spawn_input.get("role"))
                if role is not None:
                    entry["role"] = role
                nickname = _as_non_empty_str(spawn_input.get("nickname")) or _as_non_empty_str(
                    spawn_output.get("nickname")
                )
                if nickname is not None:
                    entry["nickname"] = nickname
                agent_id = _as_non_empty_str(spawn_output.get("agent_id"))
                if agent_id is not None:
                    entry["agent_id"] = agent_id
                spawned_agents.append(entry)

        codex_agent_specific: dict[str, Any] = {}
        if plans:
            codex_agent_specific["plans"] = plans
        if spawned_agents:
            codex_agent_specific["spawned_agents"] = spawned_agents

        codex_ext = session.extensions.codex if session.extensions is not None else None
        if codex_ext is not None and session.parent_session_id is not None:
            spawned_from: dict[str, Any] = {"parent_session_id": str(session.parent_session_id)}
            if codex_ext.spawn_parent_thread_id or codex_ext.forked_from_id:
                spawned_from["spawn_origin"] = "subagent"
            if codex_ext.spawn_agent_role:
                spawned_from["agent_role"] = codex_ext.spawn_agent_role
            elif codex_ext.agent_role:
                spawned_from["agent_role"] = codex_ext.agent_role
            nickname = codex_ext.spawn_agent_nickname or codex_ext.agent_nickname
            if nickname:
                spawned_from["agent_nickname"] = nickname
            codex_agent_specific["spawned_from"] = spawned_from

        notes: list[EnrichmentNote] = []
        if plans:
            notes.append(
                EnrichmentNote(
                    subject="session",
                    message="Codex plan snapshots derived from update_plan tool calls.",
                    provenance="observed",
                    confidence="high",
                )
            )
        if spawned_agents or session.parent_session_id is not None:
            notes.append(
                EnrichmentNote(
                    subject="session",
                    message="Codex workflow references derived from spawn_agent calls and observed parent session links.",
                    provenance="observed",
                    confidence="high",
                )
            )

        if not codex_agent_specific and not notes:
            return None

        return EnrichmentOverlay(
            agent_specific={"codex": codex_agent_specific} if codex_agent_specific else {},
            notes=notes,
        )
