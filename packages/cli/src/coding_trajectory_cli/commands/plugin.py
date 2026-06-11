"""Plugin command registration and dispatch helpers."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from coding_trajectory_cli._shared import GhFormatter, add_base_output_flags
from coding_trajectory_cli.plugins import (
    LoadedPlugin,
    PluginManifest,
    PluginTool,
    builtin_plugin_manifests,
    discover_plugins,
    load_plugin_registry,
    plugin_payload,
    plugin_registry_path,
    register_plugin,
    run_plugin,
    save_plugin_registry,
    unregister_plugin,
)

PLUGIN_EPILOG = """\
PLUGIN COMMANDS
  ct plugin list                                 list registered ct CLI plugins
  ct plugin register MANIFEST                    register one plugin manifest
  ct plugin unregister NAME                      remove one plugin registration
  ct plugin register-builtins                    register repository built-ins
  ct plugin publish-local                        publish this checkout to the global uv tool

NOTE
  Plugin registration is explicit. Registering or unregistering a plugin
  changes CLI routing only; it does not install or delete plugin files.
"""

PLUGIN_STATE: list[LoadedPlugin] = []
PLUGIN_LIFECYCLE_COMMANDS = {
    "list",
    "publish-local",
    "register",
    "register-builtins",
    "unregister",
}


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


def _handle_plugin_register(args: argparse.Namespace) -> dict[str, Any]:
    plugin = register_plugin(args.manifest, replace=args.replace)
    return {
        "name": plugin.name,
        "source": str(plugin.source),
        "status": "registered",
    }


def _handle_plugin_unregister(args: argparse.Namespace) -> dict[str, Any]:
    removed = unregister_plugin(args.name)
    return {
        "name": args.name,
        "source": removed.manifest,
        "status": "unregistered",
    }


def _handle_plugin_register_builtins(args: argparse.Namespace) -> dict[str, Any]:
    registered: list[dict[str, Any]] = []
    for manifest_path in builtin_plugin_manifests():
        plugin = register_plugin(manifest_path, replace=args.replace)
        registered.append({"name": plugin.name, "source": str(plugin.source)})
    return {"status": "registered", "plugins": registered}


def _handle_plugin_publish_local(args: argparse.Namespace) -> dict[str, Any]:
    repo = _local_repo_root(args.repo)
    plugin_dirs = _local_plugin_package_dirs(repo)
    install_command = _local_publish_command(repo, plugin_dirs)
    payload: dict[str, Any] = {
        "status": "planned" if args.dry_run else "published",
        "repo": str(repo),
        "tool": "coding-trajectory",
        "command": install_command,
        "registry": str(plugin_registry_path()),
        "plugins": [],
        "pruned": [],
    }

    if args.dry_run:
        payload["plugins"] = [
            {"name": _manifest_name(path), "source": str(path)}
            for path in _local_builtin_manifests(repo)
        ]
        return payload

    env = os.environ.copy()
    env.setdefault("UV_PROJECT", str(repo))
    completed = subprocess.run(
        install_command,
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    payload["install"] = {
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }
    if completed.returncode != 0:
        if completed.stdout:
            sys.stderr.write(completed.stdout)
        if completed.stderr:
            sys.stderr.write(completed.stderr)
        raise RuntimeError(f"uv tool install failed with exit code {completed.returncode}")

    registered: list[dict[str, str]] = []
    current_names: set[str] = set()
    for manifest_path in _local_builtin_manifests(repo):
        plugin = register_plugin(manifest_path, replace=True)
        if plugin.name:
            current_names.add(plugin.name)
            registered.append({"name": plugin.name, "source": str(plugin.source)})
    payload["plugins"] = registered
    payload["pruned"] = _prune_local_builtin_registry(repo, current_names)
    return payload


def _render_plugin_mutation_text(payload: dict[str, Any]) -> str:
    plugins = payload.get("plugins")
    if isinstance(plugins, list):
        lines = [f"Registered {len(plugins)} built-in plugins"]
        lines.extend(f"- {item['name']}: {item['source']}" for item in plugins)
        return "\n".join(lines)
    return f"{payload.get('name')}: {payload.get('status')} ({payload.get('source')})"


def _render_plugin_publish_local_text(payload: dict[str, Any]) -> str:
    lines = [
        f"Local publish: {payload.get('status')}",
        f"Repo: {payload.get('repo')}",
        f"Registry: {payload.get('registry')}",
    ]
    command = payload.get("command")
    if isinstance(command, list):
        lines.append(f"Command: {' '.join(command)}")
    install = payload.get("install")
    if isinstance(install, dict):
        lines.append(f"Install exit: {install.get('returncode')}")
        stderr = install.get("stderr")
        if stderr:
            lines.append("")
            lines.append(stderr)
    plugins = payload.get("plugins") or []
    if plugins:
        lines.append("")
        lines.append(f"Registered {len(plugins)} built-in plugins")
        lines.extend(f"- {item['name']}: {item['source']}" for item in plugins)
    pruned = payload.get("pruned") or []
    if pruned:
        lines.append("")
        lines.append(f"Pruned {len(pruned)} stale built-in registrations")
        lines.extend(f"- {item['name']}: {item['source']}" for item in pruned)
    return "\n".join(lines)


def _handle_plugin_exec(args: argparse.Namespace) -> int:
    plugin = getattr(args, "_plugin", None)
    if not isinstance(plugin, LoadedPlugin) or plugin.manifest is None:
        print(
            json.dumps({"error": {"message": "Plugin is not available"}}, indent=2),
            file=sys.stderr,
        )
        return 1
    plugin_args = getattr(args, "plugin_args", None) or []
    return run_plugin(plugin.manifest, plugin.source, plugin_args)


def dispatch_plugin_argv(raw_args: list[str]) -> int | None:
    if len(raw_args) < 2 or raw_args[0] != "plugin":
        return None
    plugin_name = raw_args[1]
    plugin_args = raw_args[2:]
    if plugin_name in PLUGIN_LIFECYCLE_COMMANDS or (
        not plugin_args and plugin_name in {"-h", "--help"}
    ):
        return None
    plugins = discover_plugins()
    for plugin in plugins:
        if plugin.manifest and plugin.manifest.name == plugin_name:
            help_exit = _plugin_manifest_help(plugin.manifest, plugin_args)
            if help_exit is not None:
                return help_exit
            return run_plugin(plugin.manifest, plugin.source, plugin_args)
    print(
        json.dumps(
            {"error": {"message": f"Plugin not found: {plugin_name}"}}, indent=2
        ),
        file=sys.stderr,
    )
    return 2


def _plugin_manifest_help(
    manifest: PluginManifest, plugin_args: list[str]
) -> int | None:
    if plugin_args and plugin_args[-1] not in {"-h", "--help"}:
        return None
    command_path = plugin_args[:-1] if plugin_args else []
    children = _plugin_help_children(manifest.tools, command_path)
    if not children:
        return None
    print(_render_plugin_manifest_help(manifest, command_path, children))
    return 0


def _plugin_help_children(
    tools: list[PluginTool], command_path: list[str]
) -> list[PluginTool]:
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


def _render_plugin_manifest_help(
    manifest: PluginManifest,
    command_path: list[str],
    children: list[PluginTool],
) -> str:
    prog = " ".join(["ct", "plugin", manifest.name, *command_path])
    description = _plugin_tool_summary(
        manifest.tools, command_path, manifest.description
    )
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
            usage = (
                manifest.name if tool.name == "." else f"{manifest.name} {tool.name}"
            )
            lines.append(f"  ct plugin {usage:<32} {tool.summary}")
    if manifest.requires_ct:
        if lines:
            lines.append("")
        lines.append(f"REQUIRES CT\n  {manifest.requires_ct}")
    return "\n".join(lines) if lines else None


def _local_repo_root(raw: str | None) -> Path:
    repo = Path(raw).expanduser() if raw else Path(__file__).resolve().parents[5]
    repo = repo.resolve()
    required = [
        repo / "packages" / "cli" / "pyproject.toml",
        repo / "packages" / "core" / "pyproject.toml",
        repo / "packages" / "plugins",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise ValueError(
            "publish-local requires a CodingTrajectory checkout; missing "
            + ", ".join(missing)
        )
    return repo


def _local_plugin_package_dirs(repo: Path) -> list[Path]:
    plugin_root = repo / "packages" / "plugins"
    return sorted(path.parent for path in plugin_root.glob("*/pyproject.toml"))


def _local_builtin_manifests(repo: Path) -> list[Path]:
    plugin_root = repo / "packages" / "plugins"
    return sorted(plugin_root.glob("*/ct-plugin.json"))


def _manifest_name(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return str(payload.get("name") or path.parent.name)


def _local_publish_command(repo: Path, plugin_dirs: list[Path]) -> list[str]:
    command = [
        "uv",
        "tool",
        "install",
        "--force",
        "--editable",
        str(repo / "packages" / "cli"),
        "--with-editable",
        str(repo / "packages" / "core"),
    ]
    for plugin_dir in plugin_dirs:
        command.extend(["--with-editable", str(plugin_dir)])
    return command


def _prune_local_builtin_registry(
    repo: Path, current_names: set[str]
) -> list[dict[str, str]]:
    plugin_root = (repo / "packages" / "plugins").resolve()
    registry = load_plugin_registry()
    pruned: list[dict[str, str]] = []
    for name, entry in list(registry.plugins.items()):
        if name in current_names:
            continue
        try:
            source = Path(entry.manifest).expanduser().resolve()
            source.relative_to(plugin_root)
        except ValueError:
            continue
        del registry.plugins[name]
        pruned.append({"name": name, "source": entry.manifest})
    if pruned:
        save_plugin_registry(registry)
    return pruned


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

    plugin_register = plugin_sub.add_parser(
        "register",
        prog="ct plugin register",
        help="Register one plugin manifest.",
        formatter_class=GhFormatter,
    )
    plugin_register.add_argument("manifest", metavar="MANIFEST")
    plugin_register.add_argument(
        "--replace",
        action="store_true",
        help="Replace an existing registration with the same plugin name.",
    )
    add_base_output_flags(plugin_register)
    plugin_register.set_defaults(
        _plugin_handler=_handle_plugin_register,
        _renderer=_render_plugin_mutation_text,
        _default_output="markdown",
    )

    plugin_unregister = plugin_sub.add_parser(
        "unregister",
        prog="ct plugin unregister",
        help="Remove a plugin registration without deleting its files.",
        formatter_class=GhFormatter,
    )
    plugin_unregister.add_argument("name", metavar="NAME")
    add_base_output_flags(plugin_unregister)
    plugin_unregister.set_defaults(
        _plugin_handler=_handle_plugin_unregister,
        _renderer=_render_plugin_mutation_text,
        _default_output="markdown",
    )

    plugin_register_builtins = plugin_sub.add_parser(
        "register-builtins",
        prog="ct plugin register-builtins",
        help="Register all built-in manifests in this repository or installation.",
        formatter_class=GhFormatter,
    )
    plugin_register_builtins.add_argument(
        "--replace",
        action="store_true",
        help="Replace existing registrations.",
    )
    add_base_output_flags(plugin_register_builtins)
    plugin_register_builtins.set_defaults(
        _plugin_handler=_handle_plugin_register_builtins,
        _renderer=_render_plugin_mutation_text,
        _default_output="markdown",
    )

    plugin_publish_local = plugin_sub.add_parser(
        "publish-local",
        prog="ct plugin publish-local",
        help="Publish this checkout to the global uv tool install.",
        formatter_class=GhFormatter,
    )
    plugin_publish_local.add_argument(
        "--repo",
        default=None,
        metavar="PATH",
        help="CodingTrajectory checkout root. Defaults to the current installed source checkout.",
    )
    plugin_publish_local.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the install and registration plan without changing anything.",
    )
    add_base_output_flags(plugin_publish_local)
    plugin_publish_local.set_defaults(
        _plugin_handler=_handle_plugin_publish_local,
        _renderer=_render_plugin_publish_local_text,
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
        plugin_command.add_argument(
            "plugin_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS
        )
        plugin_command.set_defaults(_plugin_handler=_handle_plugin_exec, _plugin=plugin)
