"""Command-line interface for reading coding session graph data."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from coding_trajectory.analysis.projection_utils import truncate_text_preview
from coding_trajectory_cli.plugins import LoadedPlugin, discover_plugins, plugin_payload, run_plugin
from coding_trajectory.query import DocumentError, ResourceNotFoundError
from coding_trajectory.service import IndexCache, dispatch, project_list_metadata, resolve_store


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    current_dir = Path.cwd()

    method: str = args._method
    params: dict[str, Any] = args._params(args)

    effective_global_scope = True if method == "project.list" else args.global_scope
    if method == "project.list":
        return project_list_metadata(
            params,
            global_scope=effective_global_scope,
            current_dir=current_dir,
        )

    cache = IndexCache.load()
    store, discovery_note = resolve_store(
        params,
        log_file=None,
        global_scope=effective_global_scope,
        current_dir=current_dir,
        cache=cache,
    )
    result = dispatch(
        method,
        params,
        store=store,
        global_scope=effective_global_scope,
        current_dir=current_dir,
        discovery_note=discovery_note,
        cache=cache,
    )
    cache.save()
    return result


def _project_list_params(args: argparse.Namespace) -> dict[str, Any]:
    params = _params_from_json(args)
    agent_vendor = getattr(args, "agent_vendor", None)
    if agent_vendor is not None:
        params["agent_vendor"] = agent_vendor
    return params


def _project_sessions_params(args: argparse.Namespace) -> dict[str, Any]:
    params = _params_from_json(args)
    if args.project_name:
        params["project_name"] = args.project_name
    if args.all_time is True:
        params["since_days"] = None
    elif args.since_days is not None:
        params["since_days"] = args.since_days
    elif "since_days" not in params:
        params["since_days"] = 30
    agent_vendor = getattr(args, "agent_vendor", None)
    if agent_vendor is not None:
        params["agent_vendor"] = agent_vendor
    return params


def _session_turn_window_params(args: argparse.Namespace) -> dict[str, Any]:
    params = _params_from_json(args)
    if args.session_id:
        params["session_id"] = args.session_id
    if args.num_turns is not None:
        params["num_turns"] = args.num_turns
    if args.drop_turns is not None:
        params["drop_turns"] = args.drop_turns
    return params


def _session_overview_params(args: argparse.Namespace) -> dict[str, Any]:
    return _session_turn_window_params(args)


def _session_stats_params(args: argparse.Namespace) -> dict[str, Any]:
    params = _params_from_json(args)
    if args.extra_billing is not None:
        params["extra_billing"] = args.extra_billing
    elif "extra_billing" not in params:
        params["extra_billing"] = False
    if args.session_id:
        params["session_id"] = args.session_id
    return params


def _session_usage_params(args: argparse.Namespace) -> dict[str, Any]:
    params = _params_from_json(args)
    if args.extra_billing is not None:
        params["extra_billing"] = args.extra_billing
    elif "extra_billing" not in params:
        params["extra_billing"] = False
    if args.session_id:
        params["session_id"] = args.session_id
    if args.turn_id:
        params["turn_id"] = args.turn_id
    return params


def _params_from_json(args: argparse.Namespace) -> dict[str, Any]:
    params = getattr(args, "params_json", None)
    return dict(params or {})


def _json_object_arg(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("must be a JSON object")
    return parsed


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _add_turn_window_flags(p: argparse.ArgumentParser, *, view_name: str) -> None:
    p.add_argument(
        "--turns",
        dest="num_turns",
        type=_positive_int,
        default=None,
        metavar="N",
        help=f"Limit each session {view_name} to its last N visible turns.",
    )
    p.add_argument(
        "--drop-turns",
        dest="drop_turns",
        type=_positive_int,
        default=None,
        metavar="K",
        help="Drop the last K visible turns, matching thread/rollback semantics.",
    )


_EPILOG = """\
PROJECT
  ct project list                                  list all known projects
  ct project sessions [PROJECT_NAME]               list sessions for a project

SESSION
  ct session overview [SESSION_ID]                 compact session hierarchy
  ct session stats [SESSION_ID]                    compact context/token usage overview
  ct session usage [SESSION_ID] [--turn TURN_ID]   turn-level token and cost accounting
  ct session step-detail STEP_ID [...]             full detail for one or more steps
  ct session event-scan [SESSION_ID] --type TYPE [--filter KEY=VALUE]
                                                   query raw events by type
  ct session event-detail EVENT_ID                 expand $truncated fields from step details

NOTE
  Sessions are located automatically via cache; pass a SESSION_ID to use
  that coding session as the session tree entry point, or omit it to use the
  most-recent session in the current working directory.
"""

_EVENT_SCAN_EPILOG = """\
EVENT TYPES
  user.prompt.submitted    A user prompt submission
  tool.call.requested      A tool invocation request
  tool.call.succeeded      A tool call that succeeded
  tool.call.failed         A tool call that failed
  llm.response             An LLM response
  vendor.raw               A vendor-specific raw event

FILTER SYNTAX
  key=value     Exact match on a payload field
  key=*         Field must exist
  key=!         Field must be absent/null
  Dot-paths supported: result.error=*
"""

_PLUGIN_EPILOG = """\
PLUGIN COMMANDS
  ct plugin list                                 list installed ct CLI plugins

NOTE
  Plugins are manifest-backed executables discovered from
  `packages/plugins/*/ct-plugin.json`, `.ct/plugins/*.json`,
  `~/.ct/plugins/*.json`, and CT_PLUGIN_MANIFEST_PATH directories.
"""

_PLUGIN_STATE: list[LoadedPlugin] = []
_OUTPUT_CHOICES = ("markdown", "json")


class _GhFormatter(argparse.RawDescriptionHelpFormatter):
    """Help formatter that matches the gh/git style: ALL CAPS sections, USAGE prefix."""

    def start_section(self, heading: str | None) -> None:
        _renames = {"positional arguments": "ARGUMENTS", "options": "FLAGS"}
        super().start_section(_renames.get(heading or "", heading or ""))

    def add_arguments(self, actions: object) -> None:
        # Promote "ARGUMENTS" → "CORE COMMANDS" when the section holds subparsers.
        if any(isinstance(a, argparse._SubParsersAction) for a in actions):  # type: ignore[attr-defined]
            self._current_section.heading = "CORE COMMANDS"
        super().add_arguments(actions)  # type: ignore[arg-type]

    def format_help(self) -> str:
        text = super().format_help()
        # "usage:" → "USAGE\n " (gh style)
        text = re.sub(r"^usage:", "USAGE\n ", text, flags=re.MULTILINE)
        # Remove trailing colon from auto-generated section headers (e.g. "FLAGS:" → "FLAGS")
        text = re.sub(r"^([A-Z][A-Z ]+):$", r"\1", text, flags=re.MULTILINE)
        # Move FLAGS section to the end (after epilog content)
        lines = text.split("\n")
        flags_start = next((i for i, ln in enumerate(lines) if ln == "FLAGS"), None)
        if flags_start is not None:
            flags_end = len(lines)
            for i in range(flags_start + 1, len(lines)):
                if lines[i] and re.match(r"^[A-Z][A-Z ]+$", lines[i]):
                    flags_end = i
                    break
            if flags_end < len(lines):  # only reorder when there's content after FLAGS
                flags_block = lines[flags_start:flags_end]
                remaining = lines[:flags_start] + lines[flags_end:]
                while remaining and not remaining[-1].strip():
                    remaining.pop()
                text = "\n".join(remaining) + "\n\n" + "\n".join(flags_block).rstrip() + "\n"
        return text


def _add_session_source(p: argparse.ArgumentParser) -> None:
    """Add optional SESSION_ID positional as the session tree entry point."""
    p.add_argument(
        "session_id",
        metavar="SESSION_ID",
        nargs="?",
        default=None,
        help="Session ID to use as the session tree entry point. Omit to use the most-recent session.",
    )


def _add_base_output_flags(p: argparse.ArgumentParser) -> None:
    """Add output/render flags (for ID-based lookups that don't need scope)."""
    p.add_argument(
        "--output",
        "-o",
        dest="output_format",
        choices=_OUTPUT_CHOICES,
        default=None,
        metavar="{" + ",".join(_OUTPUT_CHOICES) + "}",
        help="Select stdout format.",
    )
    p.set_defaults(global_scope=False)


def _add_output_flags(p: argparse.ArgumentParser) -> None:
    """Add output/render flags and --global-scope (for commands that need scope)."""
    p.add_argument(
        "--output",
        "-o",
        dest="output_format",
        choices=_OUTPUT_CHOICES,
        default=None,
        metavar="{" + ",".join(_OUTPUT_CHOICES) + "}",
        help="Select stdout format.",
    )
    p.add_argument("--global-scope", action="store_true", help="Search all known log files instead of the most-recent session.")


def _add_params_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--params",
        dest="params_json",
        type=_json_object_arg,
        default=None,
        metavar="JSON",
        help="Merge JSON object params into the command request. Explicit CLI flags override matching params.",
    )


def _add_metrics_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--extra-billing",
        action="store_true",
        default=None,
        help="Mark cost estimates as outside-plan/API billing instead of plan-usage estimates.",
    )


def _add_agent_vendor_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--agent-vendor",
        metavar="AGENT_VENDOR",
        dest="agent_vendor",
        default=None,
        help=(
            "Filter by agent vendor. "
            "Known values: claude_code, codex_cli, pi."
        ),
    )


def _json_renderer(_args: argparse.Namespace, payload: dict[str, Any]) -> str:
    return _json_text(payload)


def _json_text(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _selected_output(args: argparse.Namespace) -> str:
    return getattr(args, "output_format", None) or getattr(args, "_default_output", "markdown")


def _drop_none(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if value is not None}


def _compact_usage(usage: dict[str, Any] | None, *, include_cost: bool = True) -> dict[str, Any] | None:
    if not isinstance(usage, dict):
        return None
    return _drop_none(
        {
            "in": usage.get("input_tokens"),
            "cache": usage.get("cached_input_tokens"),
            "out": usage.get("output_tokens"),
            "reason": usage.get("reasoning_output_tokens"),
            "total": usage.get("total_tokens"),
            "cost": usage.get("cost_usd") if include_cost else None,
        }
    ) or None


def _compact_request(request: Any) -> Any:
    if not isinstance(request, dict):
        return request
    text = request.get("content") or request.get("summary") or request.get("text")
    return _drop_none(
        {
            "text": text,
            "src": request.get("source"),
            "type": request.get("type") if request.get("type") not in {None, "message"} else None,
        }
    ) or None


def _compact_activity(activity: Any) -> Any:
    if not isinstance(activity, dict):
        return activity
    if "tool" in activity:
        compact = {
            "tool": activity.get("tool"),
            "n": activity.get("count"),
            "status": activity.get("status"),
        }
        for key in ("cmd", "path", "query", "url", "text"):
            if activity.get(key) is not None:
                compact[key] = activity.get(key)
        for key in ("paths", "queries", "urls", "targets"):
            if activity.get(key) is not None:
                compact[key] = activity.get(key)
        if compact.get("n") == 1:
            compact.pop("n", None)
        return _drop_none(compact)
    if "text" in activity:
        return {"text": activity.get("text")}
    if "teammate_summary" in activity:
        return {"team": activity.get("teammate_summary")}
    return activity


def _compact_relationship(relationship: Any) -> Any:
    if not isinstance(relationship, dict):
        return relationship
    if relationship.get("role") == "main":
        return _drop_none({"role": "main", "forks": relationship.get("forked_session_ids")})
    return _drop_none(
        {
            "type": relationship.get("relationship"),
            "parent": relationship.get("parent_session_id"),
            "forks": relationship.get("forked_session_ids"),
        }
    ) or None


def _compact_context_category(category: Any) -> Any:
    if not isinstance(category, dict):
        return category
    return _drop_none(
        {
            "k": category.get("key"),
            "l": category.get("label"),
            "t": category.get("tokens"),
            "p": category.get("percent"),
            "c": [_compact_context_category(child) for child in category.get("children") or []] or None,
        }
    )


def _compact_payload(method: str, payload: Any) -> Any:
    if method == "project.list" and isinstance(payload, dict):
        items = payload.get("items") or {}
        return {
            "items": {
                name: _drop_none({"p": item.get("path"), "v": item.get("vendors"), "sessions": item.get("sessions")})
                for name, item in items.items()
                if isinstance(item, dict)
            }
        }

    if method == "project.sessions" and isinstance(payload, dict):
        return {
            "items": [
                _drop_none(
                    {
                        "id": item.get("root_session_id"),
                        "title": item.get("title"),
                        "v": item.get("vendors"),
                        "sessions": item.get("session_ids"),
                    }
                )
                for item in payload.get("items") or []
                if isinstance(item, dict)
            ]
        }

    if method == "session.overview" and isinstance(payload, dict):
        return {
            "id": payload.get("root_session_id"),
            "sessions": [
                _drop_none(
                    {
                        "id": session.get("session_id"),
                        "rel": _compact_relationship(session.get("relationship")),
                        "v": session.get("vendor"),
                        "status": session.get("status"),
                        "agent": session.get("agent_name"),
                        "cwd": session.get("cwd"),
                        "turns": [
                            _drop_none(
                                {
                                    "id": turn.get("turn_id"),
                                    "status": turn.get("status"),
                                    "req": _compact_request(turn.get("user_request")),
                                    "act": [_compact_activity(activity) for activity in turn.get("activity") or []] or None,
                                    "team": turn.get("teammate_summary"),
                                    "steps": ((turn.get("refs") or {}).get("step_ids") if isinstance(turn.get("refs"), dict) else None),
                                }
                            )
                            for turn in session.get("turns") or []
                            if isinstance(turn, dict)
                        ],
                    }
                )
                for session in payload.get("sessions") or []
                if isinstance(session, dict)
            ],
        }

    if method == "session.usage" and isinstance(payload, dict):
        return _drop_none(
            {
                "id": payload.get("session_id"),
                "extra_billing": payload.get("extra_billing"),
                "usage": _compact_usage(payload.get("total_usage")),
                "cost": payload.get("cost_usd"),
                "turns": [
                    _drop_none(
                        {
                            "id": turn.get("turn_id"),
                            "session": turn.get("session_id"),
                            "usage": _compact_usage(turn.get("usage")),
                            "cost": turn.get("cost_usd"),
                            "act": [
                                _drop_none(
                                    {
                                        "kind": item.get("category"),
                                        "usage": _compact_usage(item.get("usage")),
                                        "cost": item.get("cost_usd"),
                                    }
                                )
                                for item in turn.get("activity_usage") or []
                                if isinstance(item, dict)
                            ]
                            or None,
                        }
                    )
                    for turn in payload.get("turns") or []
                    if isinstance(turn, dict)
                ],
                "warn": payload.get("warnings") or None,
            }
        )

    if method == "session.stats" and isinstance(payload, dict):
        model = payload.get("model") or {}
        ctx = payload.get("context_window") or {}
        runtime = payload.get("runtime") or {}
        messages = payload.get("messages") or {}
        quota = payload.get("quota") or {}
        return _drop_none(
            {
                "id": payload.get("root_session_id"),
                "v": payload.get("vendor"),
                "model": _drop_none({"name": model.get("name"), "ctx": model.get("context_window_tokens")}) or None,
                "ctx": _drop_none(
                    {
                        "used": ctx.get("used_tokens"),
                        "pct": ctx.get("used_percent"),
                        "cats": [_compact_context_category(item) for item in ctx.get("categories") or []] or None,
                    }
                )
                or None,
                "rt": _drop_none(
                    {
                        "status": runtime.get("status"),
                        "start": runtime.get("started_at"),
                        "end": runtime.get("ended_at"),
                        "dur_s": runtime.get("duration_seconds"),
                        "turns": runtime.get("turns"),
                        "steps": runtime.get("model_steps"),
                        "tools": runtime.get("tool_calls"),
                        "subs": runtime.get("subagent_sessions"),
                        "compactions": runtime.get("compactions"),
                    }
                )
                or None,
                "msg": _drop_none(
                    {
                        "user": messages.get("user"),
                        "assistant": messages.get("assistant"),
                        "developer": messages.get("developer"),
                        "tools": messages.get("tool_outputs"),
                        "reasoning": messages.get("reasoning_items"),
                        "compacted": messages.get("compacted_contexts"),
                    }
                )
                or None,
                "usage": _compact_usage(payload.get("usage"), include_cost=False),
                "quota": _drop_none(
                    {
                        "plan": quota.get("plan_type"),
                        "primary_pct": quota.get("primary_used_percent"),
                        "secondary_pct": quota.get("secondary_used_percent"),
                        "reset_at": quota.get("resets_at"),
                    }
                )
                or None,
                "warn": payload.get("warnings") or None,
            }
        )

    if method == "step.details" and isinstance(payload, list):
        return [
            _drop_none(
                {
                    "id": item.get("step_id"),
                    "type": item.get("type"),
                    "ops": item.get("operations"),
                    "shape": item.get("shape"),
                    "events": item.get("event_ids"),
                }
            )
            for item in payload
            if isinstance(item, dict)
        ]

    if method == "event.detail" and isinstance(payload, dict):
        return _drop_none(
            {
                "id": payload.get("event_id"),
                "session": payload.get("session_id"),
                "ts": payload.get("timestamp"),
                "type": payload.get("type"),
                "tool": payload.get("tool_call"),
                "llm": payload.get("llm"),
                "text": payload.get("text"),
            }
        )

    if method == "event.scan" and isinstance(payload, dict):
        return _drop_none(
            {
                "id": payload.get("root_session_id"),
                "type": payload.get("type"),
                "matches": [
                    _drop_none(
                        {
                            "id": item.get("event_id"),
                            "session": item.get("session_id"),
                            "ts": item.get("timestamp"),
                            "payload": item.get("payload"),
                        }
                    )
                    for item in payload.get("matches") or []
                    if isinstance(item, dict)
                ],
            }
        )

    return payload


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


def _render_project_list_markdown(payload: dict[str, Any]) -> str:
    items = payload.get("items") or {}
    lines = [
        "# Projects",
        "",
        "| Project | Vendors | Path |",
        "| --- | --- | --- |",
    ]
    for name, item in items.items():
        if not isinstance(item, dict):
            continue
        vendors = ", ".join(item.get("vendors") or []) or "-"
        path = item.get("path") or "-"
        lines.append(f"| `{name}` | {vendors} | `{path}` |")
    return "\n".join(lines)


def _render_project_sessions_markdown(payload: dict[str, Any]) -> str:
    items = payload.get("items") or []
    lines = ["# Sessions", ""]
    for item in items:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or "-"
        vendors = ", ".join(item.get("vendors") or []) or "-"
        lines.append(f"- `{_short_id(item.get('root_session_id'))}` {title} [{vendors}]")
    if len(lines) == 2:
        lines.append("No sessions found.")
    return "\n".join(lines)


def _plugin_list_payload(plugins: list[LoadedPlugin]) -> dict[str, Any]:
    return plugin_payload(plugins)


def _handle_plugin_list(_args: argparse.Namespace) -> dict[str, Any]:
    return _plugin_list_payload(_PLUGIN_STATE)


def _handle_plugin_exec(args: argparse.Namespace) -> int:
    plugin = getattr(args, "_plugin", None)
    if not isinstance(plugin, LoadedPlugin) or plugin.manifest is None:
        print(json.dumps({"error": {"message": "Plugin is not available"}}, indent=2), file=sys.stderr)
        return 1
    plugin_args = getattr(args, "plugin_args", None) or []
    return run_plugin(plugin.manifest, plugin.source, plugin_args)


def _dispatch_plugin_argv(raw_args: list[str]) -> int | None:
    if len(raw_args) < 2 or raw_args[0] != "plugin":
        return None
    plugin_name = raw_args[1]
    plugin_args = raw_args[2:]
    if plugin_name in {"list", "-h", "--help"}:
        return None
    if not plugin_args:
        return None
    if all(item in {"-h", "--help"} for item in plugin_args):
        return None
    plugins = discover_plugins()
    for plugin in plugins:
        if plugin.manifest and plugin.manifest.name == plugin_name:
            return run_plugin(plugin.manifest, plugin.source, plugin_args)
    print(json.dumps({"error": {"message": f"Plugin not found: {plugin_name}"}}, indent=2), file=sys.stderr)
    return 2


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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ct",
        description="Inspect coding sessions stored in JSONL log files.",
        usage="ct <command> <subcommand> [flags]",
        epilog=_EPILOG,
        formatter_class=_GhFormatter,
    )
    _add_output_flags(parser)

    subparsers = parser.add_subparsers(dest="command", required=True)

    # -- project --------------------------------------------------------
    project_parser = subparsers.add_parser(
        "project",
        help="List projects or sessions within a project.",
        formatter_class=_GhFormatter,
    )
    project_sub = project_parser.add_subparsers(dest="action", required=True)

    project_list = project_sub.add_parser(
        "list",
        help="List all known projects.",
        formatter_class=_GhFormatter,
    )
    _add_agent_vendor_flag(project_list)
    _add_output_flags(project_list)
    _add_params_flag(project_list)
    project_list.set_defaults(
        _method="project.list",
        _params=_project_list_params,
        _default_output="markdown",
    )

    project_sessions = project_sub.add_parser(
        "sessions",
        help="List sessions for a given project.",
        formatter_class=_GhFormatter,
    )
    project_sessions.add_argument(
        "project_name",
        metavar="PROJECT_NAME",
        nargs="?",
        default=None,
        help="Project name to list sessions for. Defaults to the current directory.",
    )
    project_sessions.add_argument(
        "--since-days",
        type=_positive_int,
        default=None,
        metavar="N",
        help="Only scan sessions modified in the last N days. Defaults to 30.",
    )
    project_sessions.add_argument(
        "--all-time",
        action="store_true",
        default=None,
        help="Scan all matching sessions, ignoring the default 30-day window.",
    )
    _add_agent_vendor_flag(project_sessions)
    _add_output_flags(project_sessions)
    _add_params_flag(project_sessions)
    project_sessions.set_defaults(
        _method="project.sessions",
        _params=_project_sessions_params,
        _default_output="markdown",
    )

    # -- session ---------------------------------------------------------
    session_parser = subparsers.add_parser(
        "session",
        help="Analyze a session and its connected session tree.",
        formatter_class=_GhFormatter,
    )
    session_sub = session_parser.add_subparsers(dest="action", required=True)

    session_overview = session_sub.add_parser(
        "overview",
        help="Show a compact session hierarchy.",
        formatter_class=_GhFormatter,
    )
    _add_session_source(session_overview)
    _add_turn_window_flags(session_overview, view_name="projection")
    _add_output_flags(session_overview)
    _add_params_flag(session_overview)
    session_overview.set_defaults(
        _method="session.overview",
        _params=_session_overview_params,
        _default_output="markdown",
    )

    session_stats = session_sub.add_parser(
        "stats",
        help="Show compact context/token usage composition.",
        formatter_class=_GhFormatter,
    )
    _add_session_source(session_stats)
    _add_output_flags(session_stats)
    _add_params_flag(session_stats)
    _add_metrics_flags(session_stats)
    session_stats.set_defaults(
        _method="session.stats",
        _params=_session_stats_params,
        _default_output="markdown",
    )

    session_usage = session_sub.add_parser(
        "usage",
        help="Show turn-level token and cost accounting.",
        formatter_class=_GhFormatter,
    )
    _add_session_source(session_usage)
    session_usage.add_argument(
        "--turn",
        dest="turn_id",
        metavar="TURN_ID",
        default=None,
        help="Limit usage analysis to one turn.",
    )
    _add_output_flags(session_usage)
    _add_params_flag(session_usage)
    _add_metrics_flags(session_usage)
    session_usage.set_defaults(
        _method="session.usage",
        _params=_session_usage_params,
        _default_output="markdown",
    )

    session_step_detail = session_sub.add_parser(
        "step-detail",
        help="Show full detail for one or more steps.",
        formatter_class=_GhFormatter,
    )
    session_step_detail.add_argument("resource_ids", metavar="STEP_ID", nargs="*")
    _add_base_output_flags(session_step_detail)
    _add_params_flag(session_step_detail)
    session_step_detail.set_defaults(
        _method="step.details",
        _params=lambda args: {
            **_params_from_json(args),
            **({"step_ids": args.resource_ids} if args.resource_ids else {}),
        },
        _default_output="json",
    )

    session_event_detail = session_sub.add_parser(
        "event-detail",
        help="Expand the full content of a single event (resolves $truncated refs).",
        formatter_class=_GhFormatter,
    )
    session_event_detail.add_argument("resource_id", metavar="EVENT_ID", nargs="?")
    _add_base_output_flags(session_event_detail)
    _add_params_flag(session_event_detail)
    session_event_detail.set_defaults(
        _method="event.detail",
        _params=lambda args: {
            **_params_from_json(args),
            **({"event_id": args.resource_id} if args.resource_id else {}),
        },
        _default_output="json",
    )

    session_event_scan = session_sub.add_parser(
        "event-scan",
        help="Query events matching --type and optional --filter expressions.",
        epilog=_EVENT_SCAN_EPILOG,
        formatter_class=_GhFormatter,
    )
    _add_session_source(session_event_scan)
    _add_output_flags(session_event_scan)
    _add_params_flag(session_event_scan)
    session_event_scan.add_argument(
        "--type",
        dest="event_type",
        required=False,
        metavar="TYPE",
        help="Event type to match (e.g. tool.call.succeeded, llm.response).",
    )
    session_event_scan.add_argument(
        "--filter",
        dest="filters",
        action="append",
        metavar="KEY=VALUE",
        default=None,
        help=(
            "Filter on event payload fields. Repeatable. "
            "VALUE=* means field must exist; VALUE=! means field must be absent."
        ),
    )
    session_event_scan.set_defaults(
        _method="event.scan",
        _params=lambda args: {
            **_params_from_json(args),
            **({"type": args.event_type} if args.event_type else {}),
            **({"filters": args.filters} if args.filters is not None else {}),
            **({"session_id": args.session_id} if args.session_id else {}),
        },
        _default_output="json",
    )

    plugin_parser = subparsers.add_parser(
        "plugin",
        help="Run plugin-provided ct commands.",
        epilog=_PLUGIN_EPILOG,
        formatter_class=_GhFormatter,
    )
    plugin_sub = plugin_parser.add_subparsers(dest="plugin_action", required=True)

    global _PLUGIN_STATE
    _PLUGIN_STATE = discover_plugins()

    plugin_list = plugin_sub.add_parser(
        "list",
        help="List installed ct CLI plugins.",
        formatter_class=_GhFormatter,
    )
    _add_base_output_flags(plugin_list)
    plugin_list.set_defaults(
        _plugin_handler=_handle_plugin_list,
        _render_payload=lambda args, payload: _json_renderer(args, payload)
        if _selected_output(args) == "json"
        else _render_plugin_list_text(payload),
        _default_output="markdown",
    )

    for plugin in _PLUGIN_STATE:
        manifest = plugin.manifest
        if manifest is None or manifest.name == "list":
            continue
        plugin_command = plugin_sub.add_parser(
            manifest.name,
            help=manifest.description,
            epilog=_plugin_epilog(plugin),
            formatter_class=_GhFormatter,
        )
        plugin_command.add_argument("plugin_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
        plugin_command.set_defaults(_plugin_handler=_handle_plugin_exec, _plugin=plugin)

    return parser


def _short_id(value: Any) -> str:
    text = str(value or "")
    return text[:8] if text else "-"


def _display_value(value: Any) -> str:
    if hasattr(value, "value"):
        return str(value.value)
    text = str(value or "")
    if "." in text:
        return text.rsplit(".", 1)[-1].lower()
    return text


def _one_line(value: Any, *, limit: int = 96) -> str:
    return truncate_text_preview(value, max_len=limit)


def _format_tokens(value: Any) -> str:
    try:
        tokens = int(value or 0)
    except (TypeError, ValueError):
        return "-"
    if abs(tokens) >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}m"
    if abs(tokens) >= 10_000:
        return f"{tokens / 1_000:.1f}k"
    if abs(tokens) >= 1_000:
        return f"{tokens / 1_000:.1f}k"
    return str(tokens)


def _format_percent(value: Any) -> str:
    try:
        percent = float(value or 0)
    except (TypeError, ValueError):
        return ""
    return f"({percent:.1f}%)"


def _format_duration(seconds: Any) -> str:
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        return "-"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _format_cost(value: Any) -> str:
    try:
        cost = float(value or 0)
    except (TypeError, ValueError):
        return "$0.00"
    return f"${cost:.4f}" if cost and cost < 0.01 else f"${cost:.2f}"


def _render_usage_line(usage: dict[str, Any]) -> str:
    return (
        f"input {_format_tokens(usage.get('input_tokens'))}  "
        f"cached {_format_tokens(usage.get('cached_input_tokens'))}  "
        f"output {_format_tokens(usage.get('output_tokens'))}  "
        f"reasoning {_format_tokens(usage.get('reasoning_output_tokens'))}  "
        f"total {_format_tokens(usage.get('total_tokens'))}  "
        f"cost {_format_cost(usage.get('cost_usd'))}"
    )


def _overview_request_label(request: Any) -> str:
    if not isinstance(request, dict):
        return "-"
    content = request.get("content") or request.get("summary") or request.get("text")
    return _one_line(content, limit=88)


def _overview_activity_label(activity: dict[str, Any]) -> str:
    if "tool" in activity:
        tool = str(activity.get("tool") or "tool")
        count = activity.get("count")
        suffix = f" x{count}" if count and count != 1 else ""
        for key in ("cmd", "path", "query", "url"):
            if activity.get(key):
                return f"{tool}{suffix}: {_one_line(activity[key], limit=72)}"
        for key in ("paths", "queries", "urls", "targets"):
            values = activity.get(key)
            if isinstance(values, list) and values:
                joined = ", ".join(_one_line(item, limit=32) for item in values[:3])
                more = f" +{len(values) - 3}" if len(values) > 3 else ""
                return f"{tool}{suffix}: {joined}{more}"
        return f"{tool}{suffix}"
    if "teammate_summary" in activity:
        return "teammate summary"
    if "text" in activity:
        return f"assistant: {_one_line(activity.get('text'), limit=84)}"
    return _one_line(activity, limit=80)


def _render_session_overview_text(payload: dict[str, Any]) -> str:
    sessions = payload.get("sessions") or []
    turn_count = sum(len(session.get("turns") or []) for session in sessions)
    lines = [
        f"# Session `{_short_id(payload.get('root_session_id'))}`",
        "",
        f"{len(sessions)} session{'s' if len(sessions) != 1 else ''}, {turn_count} visible turn{'s' if turn_count != 1 else ''}",
        "",
    ]

    for session_index, session in enumerate(sessions):
        relationship = session.get("relationship") or {}
        role = relationship.get("role") or relationship.get("relationship") or "session"
        header = f"- session `{_short_id(session.get('session_id'))}`"
        header += f"  {role}, {session.get('vendor') or '-'}, {_display_value(session.get('status')) or '-'}"
        if session.get("agent_name"):
            header += f", {session['agent_name']}"
        lines.append(header)
        if session.get("cwd"):
            lines.append(f"   cwd: {session['cwd']}")

        turns = session.get("turns") or []
        for turn_index, turn in enumerate(turns):
            turn_prefix = "  -"
            lines.append(
                f"{turn_prefix} turn {_short_id(turn.get('turn_id'))}  "
                f"{_display_value(turn.get('status')) or '-'}  {_overview_request_label(turn.get('user_request'))}"
            )

            activities = turn.get("activity") or []
            if turn.get("teammate_summary"):
                activities = [{"teammate_summary": turn.get("teammate_summary")}]
            for activity_index, activity in enumerate(activities):
                branch = "    -"
                if isinstance(activity, dict):
                    lines.append(f"{branch} {_overview_activity_label(activity)}")

    return "\n".join(lines).rstrip()


_CONTEXT_CATEGORY_WIDTH = 56


def _render_context_category(lines: list[str], category: dict[str, Any], *, indent: int = 0) -> None:
    label = str(category.get("label") or category.get("key") or "-")
    display_width = max(_CONTEXT_CATEGORY_WIDTH - indent, 16)
    label = truncate_text_preview(label, max_len=display_width)
    lines.append(
        f"{' ' * indent}{label:<{display_width}} {_format_tokens(category.get('tokens')):>7} "
        f"{_format_percent(category.get('percent')):>8}"
    )
    for child in category.get("children") or []:
        if isinstance(child, dict):
            _render_context_category(lines, child, indent=indent + 2)


def _render_session_stats_text(payload: dict[str, Any]) -> str:
    model = payload.get("model") or {}
    context_window = payload.get("context_window") or {}
    runtime = payload.get("runtime") or {}
    messages = payload.get("messages") or {}
    usage = payload.get("usage") or {}

    model_name = model.get("name") or "-"
    context_tokens = model.get("context_window_tokens")
    lines = [
        "# Session Stats",
        "",
        f"Model: {model_name} ({_format_tokens(context_tokens)} context)",
        "",
        f"{'Category':<{_CONTEXT_CATEGORY_WIDTH}} {'Tokens':>7} {'Context':>8}",
    ]

    for category in context_window.get("categories") or []:
        if isinstance(category, dict):
            _render_context_category(lines, category)

    used_tokens = context_window.get("used_tokens") or usage.get("input_tokens")
    used_percent = context_window.get("used_percent")
    lines.extend(
        [
            "",
            f"Used: {_format_tokens(used_tokens)} tokens {_format_percent(used_percent)} of context",
            (
                f"Runtime: {_format_duration(runtime.get('duration_seconds'))}, "
                f"{runtime.get('turns') or 0} turns, "
                f"{runtime.get('model_steps') or 0} model steps, "
                f"{runtime.get('tool_calls') or 0} tool calls, "
                f"{runtime.get('subagent_sessions') or 0} subagent sessions"
            ),
        ]
    )
    if runtime.get("compactions"):
        lines[-1] += f", {runtime['compactions']} compactions"
    if messages:
        lines.append(
            "Messages: "
            f"{messages.get('user') or 0} user, "
            f"{messages.get('assistant') or 0} assistant, "
            f"{messages.get('tool_outputs') or 0} tool outputs, "
            f"{messages.get('reasoning_items') or 0} reasoning items"
        )
    quota = payload.get("quota") or {}
    if quota:
        quota_bits = [f"plan {quota.get('plan_type')}"] if quota.get("plan_type") else []
        if quota.get("primary_used_percent") is not None:
            quota_bits.append(f"primary {quota['primary_used_percent']:.1f}%")
        if quota.get("secondary_used_percent") is not None:
            quota_bits.append(f"secondary {quota['secondary_used_percent']:.1f}%")
        if quota_bits:
            lines.append("Quota: " + ", ".join(quota_bits))
    warnings = payload.get("warnings") or []
    if warnings:
        lines.append("")
        lines.extend(f"Warning: {_one_line(warning, limit=110)}" for warning in warnings)
    return "\n".join(lines).rstrip()


def _render_session_usage_text(payload: dict[str, Any]) -> str:
    lines = ["# Session Usage", "", "Total"]
    lines.append(f"  {_render_usage_line(payload.get('total_usage') or {})}")

    turns = payload.get("turns") or []
    if turns:
        lines.extend(["", "Turns"])
    for turn in turns:
        lines.append(f"  turn {_short_id(turn.get('turn_id'))}")
        lines.append(f"    {_render_usage_line(turn.get('usage') or {})}")
        for activity in turn.get("activity_usage") or []:
            category = str(activity.get("category") or "-")
            lines.append(f"    {category:<14} {_render_usage_line(activity.get('usage') or {})}")

    warnings = payload.get("warnings") or []
    if warnings:
        lines.append("")
        lines.extend(f"Warning: {_one_line(warning, limit=110)}" for warning in warnings)
    return "\n".join(lines).rstrip()


def _render_payload(args: argparse.Namespace, payload: dict[str, Any]) -> str:
    plugin_renderer = getattr(args, "_render_payload", None)
    if callable(plugin_renderer):
        return plugin_renderer(args, payload)

    if _selected_output(args) == "json":
        return _json_text(_compact_payload(args._method, payload))

    if args._method == "project.list":
        return _render_project_list_markdown(payload)
    if args._method == "project.sessions":
        return _render_project_sessions_markdown(payload)
    if args._method == "session.overview":
        return _render_session_overview_text(payload)
    if args._method == "session.stats":
        return _render_session_stats_text(payload)
    if args._method == "session.usage":
        return _render_session_usage_text(payload)

    return _json_text(_compact_payload(args._method, payload))


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    plugin_exit = _dispatch_plugin_argv(raw_args)
    if plugin_exit is not None:
        return plugin_exit

    parser = _build_parser()
    args = parser.parse_args(raw_args)

    try:
        plugin_handler = getattr(args, "_plugin_handler", None)
        payload = plugin_handler(args) if callable(plugin_handler) else _dispatch(args)
        if isinstance(payload, int):
            return payload
    except ResourceNotFoundError as exc:
        print(json.dumps({"error": {"message": str(exc)}}, indent=2), file=sys.stderr)
        return 1
    except DocumentError as exc:
        print(json.dumps({"error": {"message": str(exc)}}, indent=2), file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - defensive CLI fallback
        print(json.dumps({"error": {"message": str(exc)}}, indent=2), file=sys.stderr)
        return 1

    text = _render_payload(args, payload)

    print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
