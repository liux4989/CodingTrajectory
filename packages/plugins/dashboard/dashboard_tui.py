from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import Any, Callable

import cleanup as cleanup_mod
from cleanup import CleanupPreview, ProjectTarget, SessionTarget
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Label, Static


@dataclass(slots=True)
class DashboardServices:
    load_projects: Callable[[], dict[str, Any]]
    load_sessions: Callable[[], dict[str, Any]]
    preview_project_cleanup: Callable[[], CleanupPreview]
    preview_session_cleanup: Callable[[], CleanupPreview]
    apply_project_cleanup: Callable[
        [CleanupPreview, str, list[ProjectTarget]], dict[str, Any]
    ]
    apply_session_cleanup: Callable[
        [CleanupPreview, str, list[SessionTarget]], dict[str, Any]
    ]


class TextModal(ModalScreen[None]):
    BINDINGS = [Binding("escape", "dismiss", "Close"), Binding("q", "dismiss", "Close")]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(self._title, id="text-modal-title"),
            VerticalScroll(
                Static(self._body, id="text-modal-body"), id="text-modal-scroll"
            ),
            Button("Close", variant="primary", id="text-modal-close"),
            id="text-modal",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "text-modal-close":
            self.dismiss()

    def action_dismiss(self) -> None:
        self.dismiss()


class ConfirmTextModal(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(self._title, id="confirm-title"),
            VerticalScroll(Static(self._body, id="confirm-body"), id="confirm-scroll"),
            Horizontal(
                Button("Confirm", variant="error", id="confirm-yes"),
                Button("Cancel", variant="primary", id="confirm-no"),
                id="confirm-buttons",
            ),
            id="confirm-modal",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


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

    #dashboard-body {
        height: 1fr;
    }

    #dashboard-sidebar {
        width: 28;
        height: 1fr;
        border-right: solid $panel;
        padding: 1;
    }

    #dashboard-sidebar Button {
        width: 100%;
        margin-bottom: 1;
    }

    #dashboard-hint {
        margin-top: 1;
        color: $text-muted;
        height: auto;
    }

    #dashboard-main {
        width: 1fr;
        height: 1fr;
        padding: 1;
    }

    #dashboard-toolbar {
        height: auto;
        margin-bottom: 1;
    }

    #dashboard-toolbar Button {
        margin-right: 1;
    }

    #dashboard-table {
        height: 1fr;
        border: solid $panel;
    }

    #dashboard-status {
        dock: bottom;
        margin: 0 1;
        padding: 0 2;
        color: $text-muted;
        height: 1;
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

    #confirm-modal {
        width: 75%;
        height: 70%;
        border: thick $error;
        background: $surface;
        padding: 1 2;
    }

    #confirm-title {
        text-style: bold;
        padding: 1 0;
        text-align: center;
    }

    #confirm-scroll {
        height: 1fr;
        border: solid $panel;
        padding: 1;
    }

    #confirm-buttons {
        height: auto;
        margin-top: 1;
        align: center middle;
    }

    #confirm-buttons Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("p", "browse_projects", "Projects"),
        Binding("s", "browse_sessions", "Sessions"),
        Binding("c", "cleanup_current", "Cleanup"),
        Binding("x", "skipped_current", "Skipped"),
        Binding("P", "cleanup_projects", "Clean projects", show=False),
        Binding("S", "cleanup_sessions", "Clean sessions", show=False),
        Binding("space", "toggle_selected", "Select"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, services: DashboardServices) -> None:
        super().__init__()
        self._services = services
        self._busy = False
        self._section = "projects"
        self._view = "browse"
        self._cleanup_kind = "project"
        self._projects_payload: dict[str, Any] = {}
        self._sessions_payload: dict[str, Any] = {}
        self._project_cleanup_preview: CleanupPreview | None = None
        self._session_cleanup_preview: CleanupPreview | None = None
        self._rows: dict[str, dict[str, Any]] = {}
        self._visible_row_keys: list[str] = []
        self._selected_cleanup_keys: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("CodingTrajectory Dashboard", id="dashboard-title")
        with Horizontal(id="dashboard-body"):
            yield Vertical(
                Button("Projects", variant="primary", id="nav-projects"),
                Button("Sessions", variant="default", id="nav-sessions"),
                Static(
                    "Keys: p/s browse, c cleanup, x skipped, Space select, r refresh",
                    id="dashboard-hint",
                ),
                id="dashboard-sidebar",
            )
            yield Vertical(
                Horizontal(
                    Button("Browse", variant="primary", id="browse-current"),
                    Button("Cleanup", variant="warning", id="cleanup-current"),
                    Button("Skipped", variant="default", id="skipped-current"),
                    Button(
                        "Trash Selected (0)",
                        variant="warning",
                        id="trash-selected",
                        disabled=True,
                    ),
                    Button(
                        "Delete Selected (0)",
                        variant="error",
                        id="delete-selected",
                        disabled=True,
                    ),
                    Button("Refresh", variant="default", id="refresh"),
                    id="dashboard-toolbar",
                ),
                DataTable(id="dashboard-table"),
                id="dashboard-main",
            )
        yield Static("", id="dashboard-status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#dashboard-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        self.action_refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "nav-projects": self.action_browse_projects,
            "nav-sessions": self.action_browse_sessions,
            "browse-current": self.action_browse_current,
            "cleanup-current": self.action_cleanup_current,
            "skipped-current": self.action_skipped_current,
            "trash-selected": lambda: self._confirm_cleanup("trash"),
            "delete-selected": lambda: self._confirm_cleanup("delete"),
            "refresh": self.action_refresh,
        }
        handler = actions.get(event.button.id or "")
        if handler is not None:
            handler()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = str(event.row_key.value)
        if (self._rows.get(key) or {}).get("kind") == "cleanup":
            self._toggle_cleanup_key(key)

    def action_browse_projects(self) -> None:
        if not self._busy:
            self._section = "projects"
            self._view = "browse"
            self._cleanup_kind = "project"
            self._render_current_section()

    def action_browse_sessions(self) -> None:
        if not self._busy:
            self._section = "sessions"
            self._view = "browse"
            self._cleanup_kind = "session"
            self._render_current_section()

    def action_browse_current(self) -> None:
        if not self._busy:
            self._view = "browse"
            self._cleanup_kind = self._current_entity_kind()
            self._render_current_section()

    def action_cleanup_current(self) -> None:
        if not self._busy:
            self._view = "cleanup"
            self._cleanup_kind = self._current_entity_kind()
            self._render_current_section()

    def action_cleanup_projects(self) -> None:
        if not self._busy:
            self._section = "projects"
            self._view = "cleanup"
            self._cleanup_kind = "project"
            self._render_current_section()

    def action_cleanup_sessions(self) -> None:
        if not self._busy:
            self._section = "sessions"
            self._view = "cleanup"
            self._cleanup_kind = "session"
            self._render_current_section()

    def action_skipped_current(self) -> None:
        if not self._busy:
            self._view = "skipped"
            self._cleanup_kind = self._current_entity_kind()
            self._render_current_section()

    def action_refresh(self) -> None:
        if not self._busy:
            self._refresh_summary()

    def action_toggle_selected(self) -> None:
        table = self.query_one("#dashboard-table", DataTable)
        if not self._visible_row_keys or table.cursor_row >= len(
            self._visible_row_keys
        ):
            return
        self._toggle_cleanup_key(self._visible_row_keys[table.cursor_row])

    def action_quit(self) -> None:
        self.exit(None)

    def _set_busy(self, message: str) -> None:
        self._busy = True
        self.query_one("#dashboard-status", Static).update(message)
        for button in self.query("Button").results(Button):
            if button.id not in {"trash-selected", "delete-selected"}:
                button.disabled = True
        self._update_cleanup_buttons()

    def _clear_busy(self, message: str = "") -> None:
        self._busy = False
        self.query_one("#dashboard-status", Static).update(message)
        for button in self.query("Button").results(Button):
            button.disabled = False
        self._update_cleanup_buttons()

    @work(exclusive=True)
    async def _refresh_summary(self) -> None:
        self._set_busy("Loading dashboard data...")
        try:
            (
                self._projects_payload,
                self._sessions_payload,
                self._project_cleanup_preview,
                self._session_cleanup_preview,
            ) = await asyncio.gather(
                asyncio.to_thread(self._services.load_projects),
                asyncio.to_thread(self._services.load_sessions),
                asyncio.to_thread(self._services.preview_project_cleanup),
                asyncio.to_thread(self._services.preview_session_cleanup),
            )
        except Exception as exc:
            self.query_one("#dashboard-status", Static).update(f"Load failed: {exc}")
            self._clear_busy()
            return

        self._selected_cleanup_keys.clear()
        self._render_current_section()
        self._clear_busy(self._summary_line())

    def _summary_line(self) -> str:
        projects = self._projects_payload.get("items") or {}
        sessions = self._sessions_payload.get("items") or []
        pc = len(self._project_cleanup_preview.candidates) if self._project_cleanup_preview else 0
        sc = len(self._session_cleanup_preview.candidates) if self._session_cleanup_preview else 0
        return f"Projects: {len(projects)} | Sessions: {len(sessions)} | Cleanup: {pc} projects, {sc} sessions"

    def _render_current_section(self) -> None:
        self._sync_nav_buttons()
        self._sync_toolbar()
        if self._view == "cleanup":
            self._render_cleanup_table()
        elif self._view == "skipped":
            self._render_skipped_table()
        elif self._section == "projects":
            self._render_projects_table()
        else:
            self._render_sessions_table()
        self._update_cleanup_buttons()

    def _sync_nav_buttons(self) -> None:
        variants = {
            "nav-projects": "primary" if self._section == "projects" else "default",
            "nav-sessions": "primary" if self._section == "sessions" else "default",
        }
        for button_id, variant in variants.items():
            self.query_one(f"#{button_id}", Button).variant = variant

    def _sync_toolbar(self) -> None:
        for button_id in (
            "trash-selected",
            "delete-selected",
        ):
            self.query_one(f"#{button_id}", Button).display = self._view == "cleanup"
        self.query_one("#browse-current", Button).variant = (
            "primary" if self._view == "browse" else "default"
        )
        self.query_one("#cleanup-current", Button).variant = (
            "primary" if self._view == "cleanup" else "warning"
        )
        self.query_one("#skipped-current", Button).variant = (
            "primary" if self._view == "skipped" else "default"
        )

    def _reset_table(self, columns: list[str]) -> DataTable:
        table = self.query_one("#dashboard-table", DataTable)
        table.clear(columns=True)
        table.add_columns(*columns)
        self._rows.clear()
        self._visible_row_keys.clear()
        return table

    def _current_entity_kind(self) -> str:
        return "session" if self._section == "sessions" else "project"

    def _render_projects_table(self) -> None:
        table = self._reset_table(["Project", "Vendors", "Path"])
        items = self._projects_payload.get("items") or {}
        for name, meta in sorted(items.items()):
            vendors = ", ".join(meta.get("v") or []) or "-"
            path = meta.get("p") or "-"
            key = f"project:{name}"
            self._rows[key] = {"kind": "project", "name": name, "meta": meta}
            self._visible_row_keys.append(key)
            table.add_row(name, vendors, _one_line(path, 80), key=key)
        self._update_status(f"Projects: {len(self._visible_row_keys)} row(s)")

    def _render_sessions_table(self) -> None:
        table = self._reset_table(["Session", "Vendors", "Title", "Merged"])
        items = self._sessions_payload.get("items") or []
        for item in items:
            root_id = str(item.get("id") or "-")
            vendors = ", ".join(item.get("v") or []) or "-"
            title = _one_line(item.get("title") or "-", 90)
            merged = str(len(item.get("sessions") or []))
            key = f"session:{root_id}"
            self._rows[key] = {"kind": "session", "item": item}
            self._visible_row_keys.append(key)
            table.add_row(root_id[:8], vendors, title, merged, key=key)
        self._update_status(f"Sessions: {len(self._visible_row_keys)} row(s)")

    def _render_cleanup_table(self) -> None:
        table = self._reset_table(["Sel", "Target", "Label", "Detail"])
        preview = self._active_cleanup_preview()
        if preview is None:
            self._update_status("Cleanup preview has not loaded.")
            return
        for index, candidate in enumerate(preview.candidates):
            key = f"cleanup:{self._cleanup_kind}:{index}"
            selected = "*" if key in self._selected_cleanup_keys else ""
            target_kind = _cleanup_target_kind(candidate)
            self._rows[key] = {
                "kind": "cleanup",
                "candidate": candidate,
                "index": index,
            }
            self._visible_row_keys.append(key)
            table.add_row(
                selected,
                target_kind,
                _one_line(candidate.display_label, 46),
                _one_line(candidate.display_detail, 90),
                key=key,
            )
        self._update_status(
            f"{self._cleanup_kind.capitalize()} cleanup: {len(self._visible_row_keys)} candidate(s)"
        )

    def _render_skipped_table(self) -> None:
        table = self._reset_table(["Target", "Reason", "Path"])
        preview = self._active_cleanup_preview()
        rows: list[cleanup_mod.SkippedTarget] = preview.skipped if preview else []
        for index, target in enumerate(rows):
            reasons = ", ".join(target.reason or ["unknown"])
            key = f"skipped:{index}"
            self._rows[key] = {
                "kind": "skipped",
                "target_kind": self._cleanup_kind,
                "target": target,
                "reasons": reasons,
            }
            self._visible_row_keys.append(key)
            table.add_row(
                f"{self._cleanup_kind}:{target.kind}",
                reasons,
                _one_line(target.path, 90),
                key=key,
            )
        self._update_status(
            f"{self._cleanup_kind.capitalize()} skipped: {len(self._visible_row_keys)} item(s)"
        )

    def _update_status(self, text: str) -> None:
        self.query_one("#dashboard-status", Static).update(text)

    def _toggle_cleanup_key(self, key: str) -> None:
        row = self._rows.get(key)
        if not row or row.get("kind") != "cleanup":
            return
        if key in self._selected_cleanup_keys:
            self._selected_cleanup_keys.remove(key)
        else:
            self._selected_cleanup_keys.add(key)
        self._render_cleanup_table()

    def _active_cleanup_preview(self) -> CleanupPreview | None:
        if self._cleanup_kind == "session":
            return self._session_cleanup_preview
        return self._project_cleanup_preview

    def _selected_cleanup_targets(self) -> list[cleanup_mod.AnyTarget]:
        preview = self._active_cleanup_preview()
        if preview is None:
            return []
        selected: list[cleanup_mod.AnyTarget] = []
        for key in sorted(self._selected_cleanup_keys):
            prefix = f"cleanup:{self._cleanup_kind}:"
            if not key.startswith(prefix):
                continue
            try:
                index = int(key.removeprefix(prefix))
            except ValueError:
                continue
            if 0 <= index < len(preview.candidates):
                selected.append(preview.candidates[index])
        return selected

    def _update_cleanup_buttons(self) -> None:
        count = len(self._selected_cleanup_targets()) if self._view == "cleanup" else 0
        trash = self.query_one("#trash-selected", Button)
        delete = self.query_one("#delete-selected", Button)
        trash.label = f"Trash Selected ({count})"
        delete.label = f"Delete Selected ({count})"
        trash.disabled = self._busy or count == 0
        delete.disabled = self._busy or count == 0

    def _confirm_cleanup(self, action: str) -> None:
        if not self._busy:
            self._run_cleanup_action(action)

    @work(exclusive=True)
    async def _run_cleanup_action(self, action: str) -> None:
        preview = self._active_cleanup_preview()
        selected = self._selected_cleanup_targets()
        if preview is None or not selected:
            return
        body = "\n".join(
            [
                f"{action.capitalize()} {len(selected)} {self._cleanup_kind} cleanup item(s)?",
                "",
                "First selected targets:",
                *[f"  {target.display_label}" for target in selected[:8]],
                "",
                "Delete is permanent. Trash uses the system trash when available.",
            ]
        )
        confirmed = await self.push_screen_wait(
            ConfirmTextModal("Confirm Cleanup", body)
        )
        if not confirmed:
            return
        self._set_busy(f"Applying {action} to {len(selected)} item(s)...")
        if self._cleanup_kind == "session":
            payload = await asyncio.to_thread(
                self._services.apply_session_cleanup,
                preview,
                action,
                [target for target in selected if isinstance(target, SessionTarget)],
            )
            title = "Session Cleanup Result"
        else:
            payload = await asyncio.to_thread(
                self._services.apply_project_cleanup,
                preview,
                action,
                [target for target in selected if isinstance(target, ProjectTarget)],
            )
            title = "Project Cleanup Result"
        self._clear_busy()
        rendered = cleanup_mod.render(argparse.Namespace(detail=False), payload)
        await self.push_screen_wait(TextModal(title, rendered))
        self._refresh_summary()


def _one_line(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _cleanup_target_kind(candidate: cleanup_mod.AnyTarget) -> str:
    if isinstance(candidate, ProjectTarget):
        return "project"
    if isinstance(candidate, SessionTarget):
        return "session"
    return "target"


def run_dashboard_tui(services: DashboardServices) -> None:
    app = DashboardApp(services)
    asyncio.run(app.run_async())
