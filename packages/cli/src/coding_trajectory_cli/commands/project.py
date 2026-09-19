"""Project command registration and renderers."""

from __future__ import annotations

import argparse
from typing import Any

from coding_trajectory_cli._shared import (
    GhFormatter,
    add_agent_vendor_flag,
    add_global_scope_flag,
    add_output_flags,
    positive_int,
)


def _project_list_params(args: argparse.Namespace) -> dict[str, Any]:
    params = {
        key: getattr(args, key)
        for key in ("cursor", "limit", "modified_since")
        if getattr(args, key, None) is not None
    }
    agent_vendor = getattr(args, "agent_vendor", None)
    if agent_vendor is not None:
        params["agent_vendor"] = agent_vendor
    return params


def _project_sessions_params(args: argparse.Namespace) -> dict[str, Any]:
    params = _project_list_params(args)
    if args.project_name:
        params["project_name"] = args.project_name
    if args.project_id:
        if args.project_name:
            raise ValueError("use --project-id or PROJECT_NAME, not both")
        params["project_id"] = args.project_id
    return params


def _render_project_list_markdown(payload: dict[str, Any]) -> str:
    items = payload.get("items") or []
    lines = [
        "# Projects",
        "",
        "| Project | ID | Vendors | Path |",
        "| --- | --- | --- | --- |",
    ]
    for item in items:
        if not isinstance(item, dict):
            continue
        vendors = ", ".join(item.get("vendors") or []) or "-"
        path = item.get("path") or "-"
        lines.append(
            f"| `{item['display_name']}` | `{item['project_id']}` | {vendors} | `{path}` |"
        )
    if payload.get("next_cursor"):
        lines.extend(
            [
                "",
                f"Next page: repeat the same filters and limit with `--cursor {payload['next_cursor']}`.",
            ]
        )
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
    if payload.get("next_cursor"):
        lines.extend(
            [
                "",
                f"Next page: repeat the same filters and limit with `--cursor {payload['next_cursor']}`.",
            ]
        )
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
        help="Project name to list sessions for. Omit to list the selected inventory.",
    )
    project_sessions.add_argument(
        "--project-id",
        help="Stable project ID returned by project list for the selected authority.",
    )
    for parser in (project_list, project_sessions):
        parser.add_argument(
            "--cursor",
            help="Opaque next_cursor from the previous page; keep filters unchanged.",
        )
        parser.add_argument(
            "--limit", type=positive_int, help="Requested page count, up to 200."
        )
        parser.add_argument(
            "--modified-since",
            help="Absolute ISO timestamp; reuse it unchanged across pages.",
        )
    add_agent_vendor_flag(project_sessions)
    add_output_flags(project_sessions)
    add_global_scope_flag(project_sessions)
    project_sessions.set_defaults(
        _method="project.sessions",
        _params=_project_sessions_params,
        _default_output="markdown",
        _renderer=_render_project_sessions_markdown,
    )
