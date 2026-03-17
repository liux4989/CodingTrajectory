"""Canonical concept registry — maps vendor tool names to step types."""

from __future__ import annotations

# Maps tool_name -> canonical step type.
# Only tools that carry semantic meaning beyond generic tool_call are listed.
# tool_call is the implicit fallback for anything not in this map.
TOOL_CONCEPT_MAP: dict[str, str] = {
    # --- subagent spawning ---
    "spawn_agent": "plan_subagent",   # Codex
    "Agent":       "plan_subagent",   # Claude Code

    # --- todo list / planning ---
    "update_plan": "todo_list",       # Codex
    "TodoWrite":   "todo_list",       # Claude Code
    "TodoRead":    "todo_list",       # Claude Code

    # --- session handoff ---
    "handoff":    "session_handoff",  # Amp
    "handoff_to": "session_handoff",  # Amp
}
