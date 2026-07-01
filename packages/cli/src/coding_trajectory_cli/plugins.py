"""Plugin manifest discovery and subprocess dispatch.

Plugins are independently packaged command extensions mounted under the
``ct plugin NAME ...`` namespace. Each plugin owns a ``plugin.toml`` manifest
in its source directory; core discovers plugins by scanning the configured
plugin root instead of hardcoding a command table.

Plugins run as separate executables and never import ``coding_trajectory`` or
``coding_trajectory_cli``. They call the ``ct`` CLI or the documented service
API surface. The only core<->plugin coupling is the ``requires_methods``
contract version check.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from coding_trajectory.contracts import SERVICE_CONTRACTS

RESERVED_PLUGIN_NAMES = {"list"}


@dataclass(frozen=True)
class PluginTool:
    """One help row for a plugin-owned tool.

    Descriptive only: summaries may lag the plugin executable's own ``-h``.
    """

    name: str
    summary: str


@dataclass(frozen=True)
class PluginCommand:
    """A discovered plugin, loaded from ``plugin.toml``."""

    name: str
    description: str
    dir: Path  # plugin source directory (absolute)
    entry: str  # entry script relative to ``dir``
    tools: list[PluginTool] = field(default_factory=list)
    requires_methods: dict[str, int] = field(default_factory=dict)

    @property
    def entry_path(self) -> Path:
        return self.dir / self.entry


class _ManifestError(Exception):
    """Raised when a plugin manifest is missing required fields or malformed."""


def discover_plugins() -> dict[str, PluginCommand]:
    """Scan the plugin root for ``plugin.toml`` manifests and load them.

    Unreadable or invalid manifests are reported on stderr and skipped rather
    than aborting discovery, so one bad plugin does not hide the others.
    """
    root = _plugin_root()
    commands: dict[str, PluginCommand] = {}
    if not root.is_dir():
        return commands
    for manifest in sorted(root.glob("*/plugin.toml")):
        try:
            command = _load_manifest(manifest)
        except (_ManifestError, tomllib.TOMLDecodeError, OSError) as exc:
            print(
                f"error: failed to load plugin manifest {manifest}: {exc}",
                file=sys.stderr,
            )
            continue
        if command.name in RESERVED_PLUGIN_NAMES:
            print(
                f"error: plugin name '{command.name}' is reserved "
                f"(declared in {manifest})",
                file=sys.stderr,
            )
            continue
        if command.name in commands:
            print(
                f"error: duplicate plugin name '{command.name}' "
                f"(declared in {manifest})",
                file=sys.stderr,
            )
            continue
        commands[command.name] = command
    return commands


def _load_manifest(path: Path) -> PluginCommand:
    with path.open("rb") as handle:
        data = tomllib.load(handle)

    name = data.get("name")
    description = data.get("description") or ""
    entry = data.get("entry")
    if not isinstance(name, str) or not name:
        raise _ManifestError("missing or invalid 'name'")
    if not isinstance(entry, str) or not entry:
        raise _ManifestError("missing or invalid 'entry'")

    requires_raw = data.get("requires") or {}
    if not isinstance(requires_raw, dict):
        raise _ManifestError("'requires' must be a table of method -> version")
    requires_methods: dict[str, int] = {}
    for method, version in requires_raw.items():
        if not isinstance(method, str) or not isinstance(version, int):
            raise _ManifestError(
                "'requires' keys must be strings and values must be integers"
            )
        requires_methods[method] = version

    tools_raw = data.get("tools") or []
    if not isinstance(tools_raw, list):
        raise _ManifestError("'tools' must be an array of tables")
    tools: list[PluginTool] = []
    for entry_table in tools_raw:
        if not isinstance(entry_table, dict) or "name" not in entry_table:
            raise _ManifestError("each [[tools]] entry needs a 'name'")
        tools.append(
            PluginTool(
                name=str(entry_table["name"]),
                summary=str(entry_table.get("summary") or ""),
            )
        )

    return PluginCommand(
        name=name,
        description=description,
        dir=path.parent,
        entry=entry,
        tools=tools,
        requires_methods=requires_methods,
    )


def _plugin_root() -> Path:
    override = os.environ.get("CT_PLUGIN_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return _default_plugin_root()


def _default_plugin_root() -> Path:
    # plugins.py lives at packages/cli/src/coding_trajectory_cli/plugins.py.
    # parents[3] is packages/, so plugins live next to the cli package.
    return Path(__file__).resolve().parents[3] / "plugins"


def plugin_names() -> list[str]:
    return sorted(PLUGIN_COMMANDS)


def get_plugin(name: str) -> PluginCommand | None:
    return PLUGIN_COMMANDS.get(name)


def run_plugin(name: str, plugin_args: list[str]) -> int:
    """Execute a plugin entry point from its source directory."""
    command = PLUGIN_COMMANDS[name]
    completed = subprocess.run(
        [sys.executable, str(command.entry_path), *plugin_args],
        cwd=command.dir,
        check=False,
    )
    return completed.returncode


def plugin_payload() -> dict[str, Any]:
    """Structured payload for ``ct plugin list``."""
    return {
        "plugins": [
            {
                "name": cmd.name,
                "description": cmd.description,
                "entry": str(cmd.entry_path),
                "requires_methods": dict(cmd.requires_methods),
                "tools": [
                    {"name": tool.name, "summary": tool.summary}
                    for tool in cmd.tools
                ],
                "status": "loaded",
                "error": compatibility_error(cmd),
            }
            for cmd in (PLUGIN_COMMANDS[name] for name in plugin_names())
        ]
    }


def compatibility_error(command: PluginCommand) -> str | None:
    """Return ``None`` if the plugin's required ct methods are available, else a
    human-readable reason the plugin cannot run."""
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


# Discovered once at import time; manifests are static for the lifetime of the
# process. Tests or callers that swap the plugin root should mutate
# ``CT_PLUGIN_DIR`` before importing this module.
PLUGIN_COMMANDS: dict[str, PluginCommand] = discover_plugins()