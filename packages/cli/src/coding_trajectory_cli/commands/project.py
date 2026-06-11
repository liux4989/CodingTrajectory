"""Project command registration and renderers."""

from __future__ import annotations

import argparse
from typing import Any

from coding_trajectory_cli._shared import (
    GhFormatter,
    add_agent_vendor_flag,
    add_output_flags,
    add_params_flag,
    add_schema_flag,
    params_from_json,
    positive_int,
)


def _project_list_params(args: argparse.Namespace) -> dict[str, Any]:
    params = params_from_json(args)
    agent_vendor = getattr(args, "agent_vendor", None)
    if agent_vendor is not None:
        params["agent_vendor"] = agent_vendor
    return params


def _project_sessions_params(args: argparse.Namespace) -> dict[str, Any]:
    params = params_from_json(args)
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
        session_id = item.get("root_session_id") or "-"
        lines.append(f"- `{session_id}` {title} [{vendors}]")
    if len(lines) == 2:
        lines.append("No sessions found.")
    return "\n".join(lines)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    project_parser = subparsers.add_parser(
        "project",
        prog="ct project",
        usage="ct project <command> [flags]",
        help="List projects or sessions within a project.",
        formatter_class=GhFormatter,
    )
    project_sub = project_parser.add_subparsers(dest="action", required=True)

    project_list = project_sub.add_parser(
        "list",
        prog="ct project list",
        help="List all known projects.",
        formatter_class=GhFormatter,
    )
    add_agent_vendor_flag(project_list)
    add_output_flags(project_list)
    add_params_flag(project_list)
    add_schema_flag(project_list)
    project_list.set_defaults(
        _method="project.list",
        _params=_project_list_params,
        _default_output="markdown",
        _renderer=_render_project_list_markdown,
    )

    project_sessions = project_sub.add_parser(
        "sessions",
        prog="ct project sessions",
        help="List sessions for a given project.",
        formatter_class=GhFormatter,
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
        type=positive_int,
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
    add_agent_vendor_flag(project_sessions)
    add_output_flags(project_sessions)
    add_params_flag(project_sessions)
    add_schema_flag(project_sessions)
    project_sessions.set_defaults(
        _method="project.sessions",
        _params=_project_sessions_params,
        _default_output="markdown",
        _renderer=_render_project_sessions_markdown,
    )
