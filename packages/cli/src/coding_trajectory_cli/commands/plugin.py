"""Plugin command dispatch helpers.

There is one dispatch path for ``ct plugin NAME ...``: an early raw-argv
handler in ``cli.main`` delegates to :func:`dispatch_plugin_argv`, which
resolves the plugin against the discovered manifest table, runs compatibility
and entry-point preflight checks, then spawns the entry script as a
subprocess. argparse only owns the ``ct plugin`` parent help and
``ct plugin list`` subcommand.

Plugin help is forwarded to the executable: ``ct plugin NAME -h`` and any
``ct plugin NAME sub ... -h`` are passed through unchanged so the plugin owns
its full flag and help surface. Core keeps only the brief ``ct plugin``
index (names + descriptions from the manifests).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from coding_trajectory_cli._shared import GhFormatter, add_base_output_flags
from coding_trajectory_cli.outcome import CommandOutcome, EarlyDispatchOutcome, status_error
from coding_trajectory_cli.plugins import (
    PLUGIN_COMMANDS,
    compatibility_error,
    plugin_names,
    plugin_payload,
    run_plugin,
)


def _render_plugin_list_text(payload: dict[str, Any]) -> str:
    plugins = payload.get("plugins") or []
    loaded = sum(1 for p in plugins if p.get("status") == "loaded" and not p.get("error"))
    failed = len(plugins) - loaded
    lines = [
        f"Plugins: {loaded} available, {failed} failed",
        "",
    ]
    for plugin in plugins:
        name = plugin.get("name") or "-"
        description = plugin.get("description") or ""
        entry = plugin.get("entry") or "-"
        lines.append(f"{name:<16} {description}".rstrip())
        lines.append(f"  entry: {entry}")
        error = plugin.get("error")
        if error:
            lines.append(f"  error: {error}")
    if not plugins:
        lines.append("No plugins found.")
    return "\n".join(lines).rstrip()


def _handle_plugin_list(_args: argparse.Namespace) -> dict[str, Any]:
    return plugin_payload()


def _fail_outcome(command: str, message: str, exit_code: int) -> EarlyDispatchOutcome:
    print(json.dumps({"error": {"message": message}}, indent=2), file=sys.stderr)
    return EarlyDispatchOutcome(
        command=command,
        outcome=CommandOutcome.failed(exit_code=exit_code, error=message),
    )


def dispatch_plugin_argv(raw_args: list[str]) -> EarlyDispatchOutcome | None:
    """Early dispatch for ``ct plugin NAME ...``.

    Returns ``None`` for forms argparse owns (``ct plugin``, ``ct plugin -h``/
    ``--help``, and ``ct plugin list ...``) so they fall through to the normal
    argparse path. Every other ``ct plugin NAME ...`` is handled here: name
    resolution, compatibility preflight, missing-entry reporting, and
    subprocess execution.
    """
    if len(raw_args) < 2 or raw_args[0] != "plugin":
        return None
    plugin_name = raw_args[1]
    following = raw_args

    # ``ct plugin`` alone, bare help, and the argparse-owned `list` subcommand
    # all fall through to argparse.
    if (
        plugin_name in {"list", "-h", "--help"}
        or plugin_name.startswith("-")
    ):
        return None

    command_label = f"plugin.{plugin_name}"

    if plugin_name not in PLUGIN_COMMANDS:
        return _fail_outcome(command_label, f"Plugin not found: {plugin_name}", 2)

    command = PLUGIN_COMMANDS[plugin_name]

    compat_error = compatibility_error(command)
    if compat_error is not None:
        return _fail_outcome(command_label, compat_error, 1)

    if not command.entry_path.exists():
        return _fail_outcome(
            command_label,
            f"Plugin entry point not found: {command.entry_path}",
            127,
        )

    exit_code = run_plugin(plugin_name, following[2:])
    if exit_code == 0:
        outcome = CommandOutcome.completed(exit_code=0)
    else:
        outcome = CommandOutcome.failed(
            exit_code=exit_code,
            error=status_error(command_label, exit_code),
        )
    return EarlyDispatchOutcome(command=command_label, outcome=outcome)


def _plugin_index_epilog() -> str:
    lines = [
        "PLUGIN COMMANDS",
        "  ct plugin list                list available ct CLI plugins",
        "  ct plugin NAME ...            run one plugin command (NAME -h for help)",
    ]
    if PLUGIN_COMMANDS:
        lines.append("")
        lines.append("PLUGINS")
        for name in plugin_names():
            command = PLUGIN_COMMANDS[name]
            lines.append(f"  {name:<14} {command.description}".rstrip())
    return "\n".join(lines)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    plugin_parser = subparsers.add_parser(
        "plugin",
        prog="ct plugin",
        usage="ct plugin <command> [flags]",
        help="Run plugin-provided ct commands.",
        epilog=_plugin_index_epilog(),
        formatter_class=GhFormatter,
    )
    plugin_sub = plugin_parser.add_subparsers(dest="plugin_action", required=True)

    plugin_list = plugin_sub.add_parser(
        "list",
        prog="ct plugin list",
        help="List available ct CLI plugins.",
        formatter_class=GhFormatter,
    )
    add_base_output_flags(plugin_list)
    plugin_list.set_defaults(
        _plugin_handler=_handle_plugin_list,
        _renderer=_render_plugin_list_text,
        _default_output="markdown",
    )