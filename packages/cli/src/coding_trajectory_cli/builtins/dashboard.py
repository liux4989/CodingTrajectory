"""Project/session management dashboard plugin.

Exposes `ct plugin dashboard` with management subcommands. Running the bare
`dashboard` command launches an interactive Textual TUI; the subcommands
(`projects`, `sessions`, `cleanup`) provide the data and actions the TUI is
built around and are also usable directly from the CLI.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from coding_trajectory_cli.builtins.cleanup import register_cleanup
from coding_trajectory_cli.plugins import CtPluginContext

try:
    import asyncio

    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal
    from textual.widgets import DataTable, Footer, Header, ListItem, ListView, Label

    _HAS_TEXTUAL = True
except ImportError:
    _HAS_TEXTUAL = False


class DashboardPlugin:
    name = "dashboard"

    def register(
        self, namespace_subparsers: argparse._SubParsersAction, ctx: CtPluginContext
    ) -> None:
        dashboard = namespace_subparsers.add_parser(
            "dashboard",
            help="Project and session management dashboard.",
        )
        # Bare `dashboard` (no subcommand) launches the interactive TUI.
        dashboard.set_defaults(
            _plugin_handler=lambda args: _handle_tui(args, ctx),
            _render_payload=_render_dashboard,
        )
        dashboard_sub = dashboard.add_subparsers(
            dest="dashboard_action", required=False
        )

        projects = dashboard_sub.add_parser(
            "projects",
            help="List managed projects.",
        )
        projects.add_argument(
            "--agent-vendor",
            default=None,
            help="Filter by vendor. Known values: codex_cli, codex, pi.",
        )
        projects.add_argument(
            "--detail",
            action="store_true",
            help="Print the structured JSON data instead of the overview.",
        )
        ctx.bind_command(
            projects,
            handler=lambda args: _handle_projects(args, ctx),
            renderer=_render_projects,
        )

        sessions = dashboard_sub.add_parser(
            "sessions",
            help="List sessions for a project.",
        )
        sessions.add_argument(
            "project_name",
            metavar="PROJECT_NAME",
            nargs="?",
            default=None,
            help="Project name to list sessions for. Defaults to the current directory.",
        )
        sessions.add_argument(
            "--since-days",
            type=int,
            default=30,
            metavar="N",
            help="Only scan sessions modified in the last N days. Defaults to 30.",
        )
        sessions.add_argument(
            "--all-time",
            action="store_true",
            help="Scan all matching sessions, ignoring the default 30-day window.",
        )
        sessions.add_argument(
            "--agent-vendor",
            default=None,
            help="Filter by vendor. Known values: codex_cli, codex, pi.",
        )
        sessions.add_argument(
            "--detail",
            action="store_true",
            help="Print the structured JSON data instead of the overview.",
        )
        ctx.bind_command(
            sessions,
            handler=lambda args: _handle_sessions(args, ctx),
            renderer=_render_sessions,
        )

        register_cleanup(dashboard_sub, ctx)


# ---------------------------------------------------------------------------
# Data handlers (shared by CLI and TUI)
# ---------------------------------------------------------------------------


def _load_projects(ctx: CtPluginContext, *, agent_vendor: str | None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {}
    if agent_vendor:
        params["agent_vendor"] = agent_vendor
    result = ctx.dispatch_core(
        method="project.list",
        params=params,
        global_scope=True,
        current_dir=Path.cwd(),
    )
    items = result.get("items") or {}
    return [
        {
            "project": name,
            "path": meta.get("path"),
            "vendors": meta.get("vendors") or [],
            "category": meta.get("category", "project"),
        }
        for name, meta in sorted(items.items())
    ]


def _load_sessions(
    ctx: CtPluginContext,
    *,
    project_name: str | None,
    since_days: int | None,
    agent_vendor: str | None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"since_days": since_days}
    if project_name:
        params["project_name"] = project_name
    if agent_vendor:
        params["agent_vendor"] = agent_vendor
    result = ctx.dispatch_core(
        method="project.sessions",
        params=params,
        global_scope=True,
        current_dir=Path.cwd(),
    )
    return list(result.get("items") or [])


def _handle_projects(args: argparse.Namespace, ctx: CtPluginContext) -> dict[str, Any]:
    projects = _load_projects(ctx, agent_vendor=args.agent_vendor)
    return {"command": "dashboard projects", "projects": projects}


def _handle_sessions(args: argparse.Namespace, ctx: CtPluginContext) -> dict[str, Any]:
    sessions = _load_sessions(
        ctx,
        project_name=args.project_name,
        since_days=None if args.all_time else args.since_days,
        agent_vendor=args.agent_vendor,
    )
    return {
        "command": "dashboard sessions",
        "project": args.project_name,
        "sessions": sessions,
    }


def _handle_tui(args: argparse.Namespace, ctx: CtPluginContext) -> dict[str, Any]:
    if not _HAS_TEXTUAL:
        return {
            "command": "dashboard",
            "launched": False,
            "note": "Textual is not installed. Install the 'tui' extra: pip install 'coding-trajectory[tui]'.",
        }
    app = DashboardApp(ctx)
    asyncio.run(app.run_async())
    return {"command": "dashboard", "launched": True}


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _render_dashboard(args: argparse.Namespace, payload: dict[str, Any]) -> str:
    if payload.get("launched"):
        return "dashboard closed"
    note = payload.get("note")
    return note or "dashboard"


def _render_projects(args: argparse.Namespace, payload: dict[str, Any]) -> str:
    if getattr(args, "detail", False):
        return json.dumps(payload, indent=2, ensure_ascii=False)
    projects = payload.get("projects") or []
    lines = [f"Projects ({len(projects)})"]
    if not projects:
        lines.append("  No projects found.")
        return "\n".join(lines)
    for item in projects:
        vendors = ", ".join(item.get("vendors") or []) or "-"
        lines.append(f"  {item['project']:<28} {vendors:<20} {item.get('path') or '-'}")
    return "\n".join(lines)


def _render_sessions(args: argparse.Namespace, payload: dict[str, Any]) -> str:
    if getattr(args, "detail", False):
        return json.dumps(payload, indent=2, ensure_ascii=False)
    sessions = payload.get("sessions") or []
    project = payload.get("project") or "(current directory)"
    lines = [f"Sessions for {project} ({len(sessions)})"]
    if not sessions:
        lines.append("  No sessions found.")
        return "\n".join(lines)
    for item in sessions:
        root = str(item.get("root_session_id") or "-")[:8]
        title = item.get("title") or "-"
        vendors = ", ".join(item.get("vendors") or []) or "-"
        count = len(item.get("session_ids") or [])
        lines.append(f"  {root:<10} {vendors:<14} sessions {count:<3} {title}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Interactive TUI
# ---------------------------------------------------------------------------

if _HAS_TEXTUAL:

    class DashboardApp(App[None]):
        """Interactive project/session browsing dashboard."""

        CSS = """
        #projects {
            width: 40%;
            border: solid $primary;
        }
        #sessions {
            width: 60%;
            border: solid $primary;
        }
        """

        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("r", "refresh", "Refresh"),
        ]

        def __init__(self, ctx: CtPluginContext) -> None:
            super().__init__()
            self._ctx = ctx
            self._projects: list[dict[str, Any]] = []

        def compose(self) -> ComposeResult:
            yield Header()
            with Horizontal():
                yield ListView(id="projects")
                yield DataTable(id="sessions")
            yield Footer()

        def on_mount(self) -> None:
            table = self.query_one("#sessions", DataTable)
            table.add_columns("Session", "Vendors", "#", "Title")
            table.cursor_type = "row"
            self.action_refresh()

        def action_refresh(self) -> None:
            self._projects = _load_projects(self._ctx, agent_vendor=None)
            listview = self.query_one("#projects", ListView)
            listview.clear()
            for item in self._projects:
                vendors = ", ".join(item.get("vendors") or []) or "-"
                listview.append(ListItem(Label(f"{item['project']}  [{vendors}]")))
            self.sub_title = f"{len(self._projects)} project(s)"
            if self._projects:
                listview.index = 0
                self._show_sessions(self._projects[0])

        def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
            index = self.query_one("#projects", ListView).index
            if index is None or not (0 <= index < len(self._projects)):
                return
            self._show_sessions(self._projects[index])

        def _show_sessions(self, project: dict[str, Any]) -> None:
            table = self.query_one("#sessions", DataTable)
            table.clear()
            try:
                sessions = _load_sessions(
                    self._ctx,
                    project_name=project["project"],
                    since_days=None,
                    agent_vendor=None,
                )
            except Exception:
                sessions = []
            for item in sessions:
                root = str(item.get("root_session_id") or "-")[:8]
                vendors = ", ".join(item.get("vendors") or []) or "-"
                count = str(len(item.get("session_ids") or []))
                title = item.get("title") or "-"
                table.add_row(root, vendors, count, title)


plugin = DashboardPlugin()
