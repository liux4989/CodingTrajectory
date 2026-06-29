"""Plugin command dispatch helpers."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from coding_trajectory_cli._shared import GhFormatter, add_base_output_flags
from coding_trajectory_cli.outcome import CommandOutcome, EarlyDispatchOutcome, status_error
from coding_trajectory_cli.plugins import (
    PLUGIN_COMMANDS,
    PluginCommand,
    PluginTool,
    plugin_names,
    plugin_payload,
    run_plugin,
)

PLUGIN_EPILOG = """\
PLUGIN COMMANDS
  ct plugin list                                 list available ct CLI plugins
  ct plugin NAME ...                             run one plugin command
"""


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


def _handle_plugin_exec(args: argparse.Namespace) -> CommandOutcome:
    plugin_name = getattr(args, "_plugin_name", None)
    if plugin_name is None or plugin_name not in PLUGIN_COMMANDS:
        print(
            json.dumps({"error": {"message": "Plugin is not available"}}, indent=2),
            file=sys.stderr,
        )
        return CommandOutcome.failed(exit_code=1, error="Plugin is not available")
    plugin_args = getattr(args, "plugin_args", None) or []
    exit_code = run_plugin(plugin_name, plugin_args)
    if exit_code == 0:
        return CommandOutcome.completed(exit_code=0)
    return CommandOutcome.failed(
        exit_code=exit_code,
        error=status_error(f"plugin.{plugin_name}", exit_code),
    )


def dispatch_plugin_argv(raw_args: list[str]) -> EarlyDispatchOutcome | None:
    if len(raw_args) < 2 or raw_args[0] != "plugin":
        return None
    plugin_name = raw_args[1]
    plugin_args = raw_args[2:]
    if plugin_name in {"list", "-h", "--help"}:
        return None
    if plugin_name not in PLUGIN_COMMANDS:
        print(
            json.dumps(
                {"error": {"message": f"Plugin not found: {plugin_name}"}}, indent=2
            ),
            file=sys.stderr,
        )
        return EarlyDispatchOutcome(
            command=f"plugin.{plugin_name}",
            outcome=CommandOutcome.failed(
                exit_code=2,
                error=f"Plugin not found: {plugin_name}",
            ),
        )
    command = PLUGIN_COMMANDS[plugin_name]
    help_exit = _plugin_help(command, plugin_args)
    if help_exit is not None:
        return EarlyDispatchOutcome(
            command=f"plugin.{plugin_name}",
            outcome=CommandOutcome.completed(exit_code=help_exit),
        )
    exit_code = run_plugin(plugin_name, plugin_args)
    outcome = (
        CommandOutcome.completed(exit_code=0)
        if exit_code == 0
        else CommandOutcome.failed(
            exit_code=exit_code,
            error=status_error(f"plugin.{plugin_name}", exit_code),
        )
    )
    return EarlyDispatchOutcome(command=f"plugin.{plugin_name}", outcome=outcome)


def _plugin_help(command: PluginCommand, plugin_args: list[str]) -> int | None:
    if plugin_args and plugin_args[-1] not in {"-h", "--help"}:
        return None
    command_path = plugin_args[:-1] if plugin_args else []
    children = _plugin_help_children(command.tools, command_path)
    if not children:
        return None
    print(_render_plugin_help(command, command_path, children))
    return 0


def _plugin_help_children(
    tools: list[PluginTool], command_path: list[str]
) -> list[PluginTool]:
    children: list[PluginTool] = []
    for tool in tools:
        if tool.name == ".":
            continue
        parts = tool.name.split("/")
        if parts[: len(command_path)] != command_path:
            continue
        if len(parts) == len(command_path) + 1:
            children.append(tool)
    return children


def _plugin_tool_summary(
    tools: list[PluginTool], command_path: list[str], default: str
) -> str:
    if not command_path:
        return default
    tool_name = "/".join(command_path)
    for tool in tools:
        if tool.name == tool_name:
            return tool.summary
    return default


def _render_plugin_help(
    command: PluginCommand,
    command_path: list[str],
    children: list[PluginTool],
) -> str:
    prog = " ".join(["ct", "plugin", command.name, *command_path])
    description = _plugin_tool_summary(command.tools, command_path, command.description)
    lines = [
        f"usage: {prog} [-h] <command> ...",
        "",
        description,
        "",
        "Commands:",
    ]
    usage_rows = [
        (f"{prog} {tool.name.split('/')[-1]}", tool.summary) for tool in children
    ]
    usage_width = max(len(usage) for usage, _summary in usage_rows)
    for usage, summary in usage_rows:
        lines.append(f"  {usage:<{usage_width}}  {summary}")
    lines.extend(
        [
            "",
            "options:",
            "  -h, --help  show this help message and exit",
        ]
    )
    return "\n".join(lines)


def _plugin_epilog(command: PluginCommand) -> str | None:
    lines: list[str] = []
    if command.tools:
        lines.append("PLUGIN COMMANDS")
        for tool in command.tools:
            if "/" in tool.name:
                continue
            usage = command.name if tool.name == "." else f"{command.name} {tool.name}"
            lines.append(f"  ct plugin {usage:<32} {tool.summary}")
    return "\n".join(lines) if lines else None


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    plugin_parser = subparsers.add_parser(
        "plugin",
        prog="ct plugin",
        usage="ct plugin <command> [flags]",
        help="Run plugin-provided ct commands.",
        epilog=PLUGIN_EPILOG,
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

    for name in plugin_names():
        command = PLUGIN_COMMANDS[name]
        plugin_command = plugin_sub.add_parser(
            command.name,
            prog=f"ct plugin {command.name}",
            help=command.description,
            epilog=_plugin_epilog(command),
            formatter_class=GhFormatter,
        )
        plugin_command.add_argument(
            "plugin_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS
        )
        plugin_command.set_defaults(
            _plugin_handler=_handle_plugin_exec, _plugin_name=name
        )
