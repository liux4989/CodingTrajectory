"""Built-in plugin command table and subprocess dispatch."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from coding_trajectory.contracts import SERVICE_CONTRACTS


@dataclass(frozen=True)
class PluginTool:
    """One help row for a plugin-owned tool."""

    name: str
    summary: str


@dataclass(frozen=True)
class PluginCommand:
    """Maps a plugin namespace to a source-dir entry point."""

    name: str
    description: str
    dir: str  # relative to workspace root
    entry: str  # relative to plugin dir
    tools: list[PluginTool] = field(default_factory=list)
    requires_methods: dict[str, int] = field(default_factory=dict)


# Built-in plugin command table.
PLUGIN_COMMANDS: dict[str, PluginCommand] = {
    "dashboard": PluginCommand(
        name="dashboard",
        description="Project and session management dashboard.",
        dir="packages/plugins/dashboard",
        entry="dashboard.py",
        requires_methods={
            "project.list": 1,
            "project.sessions": 1,
            "session.overview": 1,
            "session.stats": 1,
            "session.usage": 1,
            "session.items": 1,
            "session.events": 1,
        },
        tools=[
            PluginTool(".", "Show dashboard entry information."),
            PluginTool("web", "Run the dashboard web program."),
            PluginTool("project", "List managed projects and project actions."),
            PluginTool("session", "List sessions and session actions."),
            PluginTool("project/cleanup", "Clean old project directories."),
            PluginTool("session/cleanup", "Clean empty or low-value session logs."),
            PluginTool(
                "session/context-window",
                "Inspect session context composition and trajectory events.",
            ),
        ],
    ),
    "code-time": PluginCommand(
        name="code-time",
        description="Today's coding work overview: time, sessions, and cost across projects.",
        dir="packages/plugins/code_time",
        entry="code_time.py",
        requires_methods={
            "project.list": 1,
            "project.sessions": 1,
            "session.usage": 1,
        },
        tools=[PluginTool(".", "Show today's coding time summary.")],
    ),
}

RESERVED_PLUGIN_NAMES = {"list"}


def plugin_names() -> list[str]:
    return sorted(PLUGIN_COMMANDS)


def get_plugin(name: str) -> PluginCommand | None:
    return PLUGIN_COMMANDS.get(name)


def run_plugin(name: str, plugin_args: list[str]) -> int:
    """Execute a plugin entry point from its source directory."""
    command = PLUGIN_COMMANDS[name]
    plugin_dir = _plugin_dir(name)
    entry = plugin_dir / command.entry
    if not entry.exists():
        return 127
    completed = subprocess.run(
        [sys.executable, str(entry), *plugin_args],
        cwd=plugin_dir,
        check=False,
    )
    return completed.returncode


def plugin_payload() -> dict[str, Any]:
    """Structured payload for `ct plugin list`."""
    return {
        "plugins": [
            {
                "name": cmd.name,
                "description": cmd.description,
                "entry": str(_plugin_dir(cmd.name) / cmd.entry),
                "requires_methods": cmd.requires_methods,
                "tools": [
                    {"name": tool.name, "summary": tool.summary}
                    for tool in cmd.tools
                ],
                "status": "loaded",
                "error": _compatibility_error(cmd),
            }
            for cmd in (PLUGIN_COMMANDS[name] for name in plugin_names())
        ]
    }


def _plugin_dir(name: str) -> Path:
    return _workspace_root() / PLUGIN_COMMANDS[name].dir


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _compatibility_error(command: PluginCommand) -> str | None:
    for method, required_version in command.requires_methods.items():
        contract = SERVICE_CONTRACTS.get(method)
        if contract is None:
            return f"required ct method is unavailable: {method}"
        if contract.version < required_version:
            return (
                f"required ct method version is unavailable: {method} "
                f"needs {required_version}, found {contract.version}"
            )
    return None
