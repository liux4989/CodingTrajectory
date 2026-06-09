"""Plugin command registration and dispatch helpers."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from coding_trajectory_cli._shared import GhFormatter, add_base_output_flags
from coding_trajectory_cli.plugins import LoadedPlugin, PluginManifest, PluginTool, discover_plugins, plugin_payload, run_plugin

PLUGIN_EPILOG = """\
PLUGIN COMMANDS
  ct plugin list                                 list installed ct CLI plugins

NOTE
  Plugins are manifest-backed executables discovered from
  `packages/plugins/*/ct-plugin.json`, `.ct/plugins/*.json`,
  `~/.ct/plugins/*.json`, and CT_PLUGIN_MANIFEST_PATH directories.
"""

PLUGIN_STATE: list[LoadedPlugin] = []


def _plugin_list_payload(plugins: list[LoadedPlugin]) -> dict[str, Any]:
    return plugin_payload(plugins)


def _render_plugin_list_text(payload: dict[str, Any]) -> str:
    plugins = payload.get("plugins") or []
    loaded = sum(1 for plugin in plugins if plugin.get("status") == "loaded")
    failed = len(plugins) - loaded
    lines = [
        f"Plugins: {loaded} available, {failed} failed",
        "",
    ]
    for plugin in plugins:
        name = plugin.get("name") or "-"
        version = plugin.get("version") or "-"
        command = " ".join(plugin.get("run") or []) or "-"
        description = plugin.get("description") or ""
        lines.append(f"{name:<24} {version:<10} {command:<24} {description}".rstrip())
        source = plugin.get("source")
        if source:
            lines.append(f"  source: {source}")
        requires_ct = plugin.get("requires_ct")
        if requires_ct:
            lines.append(f"  requires ct: {requires_ct}")
        error = plugin.get("error")
        if error:
            lines.append(f"  error: {error}")
    if not plugins:
        lines.append("No plugin manifests found.")
    return "\n".join(lines).rstrip()


def _handle_plugin_list(_args: argparse.Namespace) -> dict[str, Any]:
    return _plugin_list_payload(PLUGIN_STATE)


def _handle_plugin_exec(args: argparse.Namespace) -> int:
    plugin = getattr(args, "_plugin", None)
    if not isinstance(plugin, LoadedPlugin) or plugin.manifest is None:
        print(json.dumps({"error": {"message": "Plugin is not available"}}, indent=2), file=sys.stderr)
        return 1
    plugin_args = getattr(args, "plugin_args", None) or []
    return run_plugin(plugin.manifest, plugin.source, plugin_args)


def dispatch_plugin_argv(raw_args: list[str]) -> int | None:
    if len(raw_args) < 2 or raw_args[0] != "plugin":
        return None
    plugin_name = raw_args[1]
    plugin_args = raw_args[2:]
    if plugin_name == "list" or (not plugin_args and plugin_name in {"-h", "--help"}):
        return None
    plugins = discover_plugins()
    for plugin in plugins:
        if plugin.manifest and plugin.manifest.name == plugin_name:
            help_exit = _plugin_manifest_help(plugin.manifest, plugin_args)
            if help_exit is not None:
                return help_exit
            return run_plugin(plugin.manifest, plugin.source, plugin_args)
    print(json.dumps({"error": {"message": f"Plugin not found: {plugin_name}"}}, indent=2), file=sys.stderr)
    return 2


def _plugin_manifest_help(manifest: PluginManifest, plugin_args: list[str]) -> int | None:
    if not plugin_args or plugin_args[-1] not in {"-h", "--help"}:
        return None
    command_path = plugin_args[:-1]
    children = _plugin_help_children(manifest.tools, command_path)
    if not children:
        return None
    print(_render_plugin_manifest_help(manifest, command_path, children))
    return 0


def _plugin_help_children(tools: list[PluginTool], command_path: list[str]) -> list[PluginTool]:
    prefix_parts = command_path
    children: list[PluginTool] = []
    for tool in tools:
        if tool.name == ".":
            continue
        parts = tool.name.split("/")
        if parts[: len(prefix_parts)] != prefix_parts:
            continue
        if len(parts) == len(prefix_parts) + 1:
            children.append(tool)
    return children


def _plugin_tool_summary(tools: list[PluginTool], command_path: list[str], default: str) -> str:
    if not command_path:
        return default
    tool_name = "/".join(command_path)
    for tool in tools:
        if tool.name == tool_name:
            return tool.summary
    return default


def _render_plugin_manifest_help(
    manifest: PluginManifest,
    command_path: list[str],
    children: list[PluginTool],
) -> str:
    prog = " ".join(["ct", "plugin", manifest.name, *command_path])
    description = _plugin_tool_summary(manifest.tools, command_path, manifest.description)
    lines = [
        f"usage: {prog} [-h] <command> ...",
        "",
        description,
        "",
        "Commands:",
    ]
    usage_rows = [
        (f"{prog} {tool.name.split('/')[-1]}", tool.summary)
        for tool in children
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


def _plugin_epilog(plugin: LoadedPlugin) -> str | None:
    manifest = plugin.manifest
    if manifest is None:
        return None
    lines: list[str] = []
    if manifest.tools:
        lines.append("PLUGIN COMMANDS")
        for tool in manifest.tools:
            if "/" in tool.name:
                continue
            usage = manifest.name if tool.name == "." else f"{manifest.name} {tool.name}"
            lines.append(f"  ct plugin {usage:<32} {tool.summary}")
    if manifest.requires_ct:
        if lines:
            lines.append("")
        lines.append(f"REQUIRES CT\n  {manifest.requires_ct}")
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

    global PLUGIN_STATE
    PLUGIN_STATE = discover_plugins()

    plugin_list = plugin_sub.add_parser(
        "list",
        prog="ct plugin list",
        help="List installed ct CLI plugins.",
        formatter_class=GhFormatter,
    )
    add_base_output_flags(plugin_list)
    plugin_list.set_defaults(
        _plugin_handler=_handle_plugin_list,
        _renderer=_render_plugin_list_text,
        _default_output="markdown",
    )

    for plugin in PLUGIN_STATE:
        manifest = plugin.manifest
        if manifest is None or manifest.name == "list":
            continue
        plugin_command = plugin_sub.add_parser(
            manifest.name,
            prog=f"ct plugin {manifest.name}",
            help=manifest.description,
            epilog=_plugin_epilog(plugin),
            formatter_class=GhFormatter,
        )
        plugin_command.add_argument("plugin_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
        plugin_command.set_defaults(_plugin_handler=_handle_plugin_exec, _plugin=plugin)
