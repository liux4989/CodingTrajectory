from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import Any, Callable

import cleanup as cleanup_mod
from cleanup import CleanupPreview, ProjectTarget, SessionTarget
from cleanup_tui import CleanupScreen, NoCandidatesScreen
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Label, Static


@dataclass(slots=True)
class DashboardServices:
    load_projects: Callable[[], dict[str, Any]]
    load_sessions: Callable[[], dict[str, Any]]
    preview_project_cleanup: Callable[[], CleanupPreview]
    preview_session_cleanup: Callable[[], CleanupPreview]
    apply_project_cleanup: Callable[[CleanupPreview, str, list[ProjectTarget]], dict[str, Any]]
    apply_session_cleanup: Callable[[CleanupPreview, str, list[SessionTarget]], dict[str, Any]]


class TextModal(ModalScreen[None]):
    BINDINGS = [Binding("escape", "dismiss", "Close"), Binding("q", "dismiss", "Close")]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(self._title, id="text-modal-title"),
            VerticalScroll(Static(self._body, id="text-modal-body"), id="text-modal-scroll"),
            Button("Close", variant="primary", id="text-modal-close"),
            id="text-modal",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "text-modal-close":
            self.dismiss()

    def action_dismiss(self) -> None:
        self.dismiss()


class DashboardApp(App[None]):
    CSS = """
    DashboardApp {
        width: 100%;
        height: 100%;
    }

    #dashboard-title {
        padding: 1 2;
        background: $primary;
        color: $text;
        text-style: bold;
        height: 3;
        content-align: center middle;
    }

    #dashboard-summary {
        margin: 1 2 0 2;
        padding: 1 2;
        border: solid $panel;
        color: $text-muted;
        height: auto;
    }

    #dashboard-actions {
        margin: 1 2;
        height: auto;
        align: center middle;
    }

    #dashboard-actions Button {
        margin: 0 1 1 1;
        min-width: 24;
    }

    #dashboard-hint {
        margin: 0 2 1 2;
        color: $text-muted;
        text-align: center;
    }

    #text-modal {
        width: 85%;
        height: 85%;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }

    #text-modal-title {
        text-style: bold;
        padding: 1 0;
        text-align: center;
    }

    #text-modal-scroll {
        height: 1fr;
        border: solid $panel;
        padding: 1;
    }

    #text-modal-close {
        margin-top: 1;
    }
    """

    BINDINGS = [Binding("q", "quit", "Quit")]

    def __init__(self, services: DashboardServices) -> None:
        super().__init__()
        self._services = services

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("CodingTrajectory Dashboard", id="dashboard-title")
        yield Static("Loading dashboard summary...", id="dashboard-summary")
        yield Vertical(
            Horizontal(
                Button("Browse Projects", variant="primary", id="browse-projects"),
                Button("Browse Sessions", variant="primary", id="browse-sessions"),
            ),
            Horizontal(
                Button("Cleanup Projects", variant="warning", id="cleanup-projects"),
                Button("Cleanup Sessions", variant="warning", id="cleanup-sessions"),
            ),
            Horizontal(
                Button("Refresh", variant="default", id="refresh"),
                Button("Quit", variant="default", id="quit"),
            ),
            id="dashboard-actions",
        )
        yield Static(
            "Project/session browsing uses the same ct JSON surfaces as the CLI. Cleanup actions stay on the existing cleanup backend.",
            id="dashboard-hint",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_summary()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "browse-projects":
            await self._show_projects()
            return
        if button_id == "browse-sessions":
            await self._show_sessions()
            return
        if button_id == "cleanup-projects":
            await self._run_project_cleanup()
            return
        if button_id == "cleanup-sessions":
            await self._run_session_cleanup()
            return
        if button_id == "refresh":
            self._refresh_summary()
            return
        if button_id == "quit":
            self.exit(None)

    def _refresh_summary(self) -> None:
        try:
            projects = self._services.load_projects().get("items") or {}
            sessions = self._services.load_sessions().get("items") or []
        except Exception as exc:
            self.query_one("#dashboard-summary", Static).update(
                f"Dashboard load failed.\n\n{exc}"
            )
            return

        project_count = len(projects)
        temporary_count = len((projects.get("(temporary)") or {}).get("sessions") or [])
        session_count = len(sessions)
        vendor_counts: dict[str, int] = {}
        for item in projects.values():
            for vendor in item.get("vendors") or []:
                vendor_counts[vendor] = vendor_counts.get(vendor, 0) + 1
        vendor_text = ", ".join(
            f"{vendor}: {count}" for vendor, count in sorted(vendor_counts.items())
        ) or "-"
        summary = "\n".join(
            [
                f"Projects: {project_count}",
                f"Recent sessions: {session_count}",
                f"Temporary project sessions: {temporary_count}",
                f"Project vendors: {vendor_text}",
            ]
        )
        self.query_one("#dashboard-summary", Static).update(summary)

    async def _show_projects(self) -> None:
        try:
            payload = self._services.load_projects()
        except Exception as exc:
            await self.push_screen_wait(TextModal("Project Load Error", str(exc)))
            return
        await self.push_screen_wait(TextModal("Projects", _render_projects(payload)))

    async def _show_sessions(self) -> None:
        try:
            payload = self._services.load_sessions()
        except Exception as exc:
            await self.push_screen_wait(TextModal("Session Load Error", str(exc)))
            return
        await self.push_screen_wait(TextModal("Sessions", _render_sessions(payload)))

    async def _run_project_cleanup(self) -> None:
        try:
            preview = self._services.preview_project_cleanup()
        except Exception as exc:
            await self.push_screen_wait(TextModal("Project Cleanup Error", str(exc)))
            return
        await self._run_cleanup_flow(
            preview,
            apply_selection=lambda current_preview, action, selected: self._services.apply_project_cleanup(
                current_preview,
                action,
                [target for target in selected if isinstance(target, ProjectTarget)],
            ),
            result_title="Project Cleanup Result",
        )

    async def _run_session_cleanup(self) -> None:
        try:
            preview = self._services.preview_session_cleanup()
        except Exception as exc:
            await self.push_screen_wait(TextModal("Session Cleanup Error", str(exc)))
            return
        await self._run_cleanup_flow(
            preview,
            apply_selection=lambda current_preview, action, selected: self._services.apply_session_cleanup(
                current_preview,
                action,
                [target for target in selected if isinstance(target, SessionTarget)],
            ),
            result_title="Session Cleanup Result",
        )

    async def _run_cleanup_flow(
        self,
        preview: CleanupPreview,
        *,
        apply_selection: Callable[[CleanupPreview, str, list[cleanup_mod.AnyTarget]], dict[str, Any]],
        result_title: str,
    ) -> None:
        if not preview.candidates:
            await self.push_screen_wait(NoCandidatesScreen(preview.skipped))
            return

        result = await self.push_screen_wait(
            CleanupScreen(preview.candidates, preview.skipped, preview.target_kind)
        )
        if result is None:
            return

        action, selected = result
        if action == "cancelled":
            return

        payload = apply_selection(preview, action, selected)
        rendered = cleanup_mod.render(argparse.Namespace(detail=False), payload)
        await self.push_screen_wait(TextModal(result_title, rendered))
        self._refresh_summary()

    def action_quit(self) -> None:
        self.exit(None)


def _render_projects(payload: dict[str, Any]) -> str:
    items = payload.get("items") or {}
    lines = [f"Projects ({len(items)})", ""]
    for name, meta in sorted(items.items()):
        vendors = ", ".join(meta.get("vendors") or []) or "-"
        lines.append(f"{name} [{vendors}]")
        path = meta.get("path")
        if path:
            lines.append(f"  {path}")
        else:
            lines.append("  path: -")
        temp_sessions = meta.get("sessions") or []
        for session in temp_sessions:
            lines.append(
                f"  temporary: {session.get('project') or '-'} -> {session.get('path') or '-'}"
            )
        lines.append("")
    return "\n".join(lines).rstrip()


def _render_sessions(payload: dict[str, Any]) -> str:
    items = payload.get("items") or []
    lines = [f"Sessions ({len(items)})", ""]
    for item in items:
        root_id = str(item.get("root_session_id") or "-")
        vendors = ", ".join(item.get("vendors") or []) or "-"
        title = _one_line(item.get("title") or "-", 120)
        lines.append(f"{root_id[:8]} [{vendors}]")
        lines.append(f"  {title}")
        session_ids = item.get("session_ids") or []
        if len(session_ids) > 1:
            lines.append(f"  merged sessions: {len(session_ids)}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _one_line(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def run_dashboard_tui(services: DashboardServices) -> None:
    app = DashboardApp(services)
    asyncio.run(app.run_async())
