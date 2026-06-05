from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from coding_trajectory_cli.plugins import CtPluginContext

try:
    import asyncio
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen
    from textual.widgets import Button, Checkbox, Footer, Header, Label, Static

    _HAS_TEXTUAL = True
except ImportError:
    _HAS_TEXTUAL = False

Action = Literal["interactive", "trash", "delete", "cancelled"]


class CleanupTarget(BaseModel):
    path: str
    reason: list[str] = Field(default_factory=list)


class ProjectTarget(CleanupTarget):
    project: str
    cleanup_paths: list[str] = Field(default_factory=list)
    cleanup_config_paths: list[str] = Field(default_factory=list)
    last_activity_at: str | None = None
    session_count: int = 0
    vendors: list[str] = Field(default_factory=list)

    @property
    def display_label(self) -> str:
        return self.project

    @property
    def display_detail(self) -> str:
        parts = [f"path: {self.path}"]
        if self.vendors:
            parts.append(f"vendors: {', '.join(self.vendors)}")
        if self.reason:
            parts.append(f"reasons: {', '.join(self.reason)}")
        return "  ".join(parts)


class SessionTarget(CleanupTarget):
    vendor: str
    modified_at: str | None = None
    session_id: str | None = None

    @property
    def display_label(self) -> str:
        return self.vendor or "session"

    @property
    def display_detail(self) -> str:
        parts = [f"path: {self.path}"]
        if self.session_id:
            parts.append(f"id: {self.session_id}")
        if self.modified_at:
            parts.append(f"modified: {self.modified_at}")
        return "  ".join(parts)


type AnyTarget = ProjectTarget | SessionTarget


class SkippedTarget(CleanupTarget):
    kind: str


if _HAS_TEXTUAL:

    # ---------------------------------------------------------------------------
    # Textual TUI
    # ---------------------------------------------------------------------------
    
    class CandidateRow(Static):
        """A single candidate row with a checkbox and details."""
    
        def __init__(self, candidate: AnyTarget, index: int) -> None:
            self.candidate = candidate
            self.index = index
            super().__init__()
    
        def compose(self) -> ComposeResult:
            yield Checkbox(
                f" {self.candidate.display_label}",
                id=f"cb-{self.index}",
            )
            yield Label(f"    {self.candidate.display_detail}", id=f"detail-{self.index}")
    
    
    class SkippedList(Static):
        """Displays a list of skipped targets."""
    
        def __init__(self, skipped: list[SkippedTarget]) -> None:
            self.skipped = skipped
            super().__init__()
    
        def render(self) -> str:
            if not self.skipped:
                return "No skipped items."
            grouped: dict[str, list[SkippedTarget]] = {}
            for item in self.skipped:
                for reason in item.reason or ["unknown"]:
                    grouped.setdefault(reason, []).append(item)
    
            lines = [f"Skipped: {len(self.skipped)} item(s)", ""]
            for reason, items in sorted(grouped.items()):
                lines.append(f"  {reason}: {len(items)}")
                for item in sorted(items, key=lambda x: (x.kind, x.path)):
                    lines.append(f"    [{item.kind}] {item.path}")
            return "\n".join(lines)
    
    
    class SkippedModal(ModalScreen[None]):
        """Modal showing skipped targets."""
    
        BINDINGS = [Binding("escape", "dismiss", "Close")]
    
        def __init__(self, skipped: list[SkippedTarget]) -> None:
            super().__init__()
            self.skipped = skipped
    
        def compose(self) -> ComposeResult:
            yield Vertical(
                Label(f"Skipped Targets ({len(self.skipped)})", id="skipped-title"),
                VerticalScroll(SkippedList(self.skipped), id="skipped-scroll"),
                Button("Close", variant="primary", id="close-skipped"),
                id="skipped-dialog",
            )
    
        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "close-skipped":
                self.dismiss()
    
        def action_dismiss(self) -> None:
            self.dismiss()
    
    
    class ConfirmModal(ModalScreen[bool]):
        """Confirmation dialog before trashing/deleting."""
    
        def __init__(self, action: str, count: int) -> None:
            super().__init__()
            self.action = action
            self.count = count
    
        def compose(self) -> ComposeResult:
            yield Vertical(
                Label(
                    f"{self.action.capitalize()} {self.count} item(s)?",
                    id="confirm-title",
                ),
                Label("This action cannot be undone.", id="confirm-warning"),
                Horizontal(
                    Button("Confirm", variant="error", id="confirm-yes"),
                    Button("Cancel", variant="primary", id="confirm-no"),
                    id="confirm-buttons",
                ),
                id="confirm-dialog",
            )
    
        def on_button_pressed(self, event: Button.Pressed) -> None:
            self.dismiss(event.button.id == "confirm-yes")
    
    
    class NoCandidatesScreen(ModalScreen[None]):
        """Shown when there are no candidates but skipped items exist."""
    
        BINDINGS = [Binding("escape", "dismiss", "Close")]
    
        def __init__(self, skipped: list[SkippedTarget]) -> None:
            super().__init__()
            self.skipped = skipped
    
        def compose(self) -> ComposeResult:
            yield Vertical(
                Label("No candidates found.", id="no-cand-title"),
                Static(
                    f"{len(self.skipped)} item(s) were skipped (see reasons below)."
                    if self.skipped
                    else "No skipped items either.",
                    id="no-cand-msg",
                ),
                VerticalScroll(SkippedList(self.skipped), id="no-cand-scroll"),
                Button("Close", variant="primary", id="no-cand-close"),
                id="no-cand-dialog",
            )
    
        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "no-cand-close":
                self.dismiss()
    
        def action_dismiss(self) -> None:
            self.dismiss()
    
    
    class CleanupScreen(ModalScreen[tuple[str, list[AnyTarget]]]):
        """Main interactive cleanup selection screen."""
    
        BINDINGS = [
            Binding("escape", "cancel", "Cancel"),
        ]
    
        def __init__(
            self,
            candidates: list[AnyTarget],
            skipped: list[SkippedTarget],
            target_kind: str,
        ) -> None:
            super().__init__()
            self.candidates = candidates
            self.skipped = skipped
            self.target_kind = target_kind
            self.selected: set[int] = set()
    
        def compose(self) -> ComposeResult:
            yield Header()
            yield Label(
                f"Cleanup {self.target_kind}: {len(self.candidates)} candidate(s)",
                id="main-title",
            )
    
            with VerticalScroll(id="candidate-scroll"):
                for i, candidate in enumerate(self.candidates):
                    yield CandidateRow(candidate, i)
    
            yield Static(self._skipped_summary_text(), id="skipped-summary")
    
            with Horizontal(id="select-buttons"):
                yield Button("Select All", variant="default", id="select-all")
                yield Button("Deselect All", variant="default", id="deselect-all")
    
            with Horizontal(id="action-buttons"):
                trash_btn = Button(
                    "🗑 Trash Selected (0)",
                    variant="warning",
                    id="trash",
                    disabled=True,
                )
                trash_btn.tooltip = "Move selected items to system trash"
                yield trash_btn
    
                delete_btn = Button(
                    "✕ Delete Selected (0)",
                    variant="error",
                    id="delete",
                    disabled=True,
                )
                delete_btn.tooltip = "Permanently delete selected items"
                yield delete_btn
    
                yield Button("View Skipped", variant="default", id="view-skipped")
                yield Button("Cancel", variant="primary", id="cancel")
    
            yield Footer()
    
        def _skipped_summary_text(self) -> str:
            if not self.skipped:
                return "Skipped: 0"
            by_reason = Counter(
                reason for item in self.skipped for reason in (item.reason or ["unknown"])
            )
            reasons = ", ".join(f"{r}: {c}" for r, c in sorted(by_reason.items()))
            return f"Skipped: {len(self.skipped)} ({reasons})"
    
        def _update_button_states(self) -> None:
            count = len(self.selected)
            trash_btn = self.query_one("#trash", Button)
            delete_btn = self.query_one("#delete", Button)
            trash_btn.label = f"🗑 Trash Selected ({count})"
            delete_btn.label = f"✕ Delete Selected ({count})"
            trash_btn.disabled = count == 0
            delete_btn.disabled = count == 0
    
        def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
            try:
                index = int(event.checkbox.id.split("-")[1])
            except (IndexError, ValueError):
                return
            if event.checkbox.value:
                self.selected.add(index)
            else:
                self.selected.discard(index)
            self._update_button_states()
    
        def on_button_pressed(self, event: Button.Pressed) -> None:
            btn_id = event.button.id
    
            if btn_id == "select-all":
                for i in range(len(self.candidates)):
                    self.selected.add(i)
                    try:
                        self.query_one(f"#cb-{i}", Checkbox).value = True
                    except Exception:
                        pass
                self._update_button_states()
    
            elif btn_id == "deselect-all":
                self.selected.clear()
                for i in range(len(self.candidates)):
                    try:
                        self.query_one(f"#cb-{i}", Checkbox).value = False
                    except Exception:
                        pass
                self._update_button_states()
    
            elif btn_id == "view-skipped":
                self.app.push_screen(SkippedModal(self.skipped))
    
            elif btn_id == "cancel":
                self.dismiss(("cancelled", []))
    
            elif btn_id in ("trash", "delete"):
                if not self.selected:
                    return
                self._confirm_action(btn_id)
    
        async def _confirm_action(self, action: str) -> None:
            confirmed = await self.app.push_screen_wait(
                ConfirmModal(action, len(self.selected))
            )
            if not confirmed:
                return
            selected_targets = [
                self.candidates[i] for i in sorted(self.selected)
            ]
            self.dismiss((action, selected_targets))
    
        def action_cancel(self) -> None:
            self.dismiss(("cancelled", []))
    
    
    class CleanupTui(App[tuple[str, list[AnyTarget]]]):
        """Textual application for interactive cleanup selection."""
    
        CSS = """
        CleanupTui {
            width: 90%;
            height: 90%;
        }
    
        #main-title {
            padding: 1 2;
            background: $primary;
            color: $text;
            text-style: bold;
            height: 3;
            content-align: center middle;
        }
    
        #candidate-scroll {
            height: 1fr;
            margin: 1 2;
            border: solid $primary;
            padding: 1;
        }
    
        CandidateRow {
            height: auto;
            padding: 0 1;
            margin: 1 0;
        }
    
        CandidateRow:focus {
            background: $surface;
        }
    
        #skipped-summary {
            margin: 0 2;
            padding: 1 2;
            color: $text-muted;
            height: auto;
        }
    
        #select-buttons {
            margin: 0 2 1 2;
            height: 3;
            align: center middle;
        }
    
        #select-buttons Button {
            margin: 0 1;
        }
    
        #action-buttons {
            margin: 0 2 1 2;
            height: 3;
            align: center middle;
        }
    
        #action-buttons Button {
            margin: 0 1;
        }
    
        /* Confirmation modal */
        #confirm-dialog {
            width: 50;
            height: auto;
            border: thick $error;
            background: $surface;
            padding: 1 2;
            align: center middle;
        }
    
        #confirm-title {
            text-style: bold;
            padding: 1 0;
            text-align: center;
        }
    
        #confirm-warning {
            color: $text-muted;
            text-align: center;
            padding-bottom: 1;
        }
    
        #confirm-buttons {
            padding: 1 0;
            align: center middle;
        }
    
        #confirm-buttons Button {
            margin: 0 1;
        }
    
        /* Skipped modal */
        #skipped-dialog {
            width: 70%;
            height: 80%;
            border: thick $primary;
            background: $surface;
            padding: 1 2;
        }
    
        #skipped-title {
            text-style: bold;
            padding: 1 0;
            text-align: center;
        }
    
        #skipped-scroll {
            height: 1fr;
            border: solid $panel;
            padding: 1;
        }
    
        #close-skipped {
            margin-top: 1;
        }
    
        /* No candidates screen */
        #no-cand-dialog {
            width: 60%;
            height: auto;
            border: thick $warning;
            background: $surface;
            padding: 1 2;
        }
    
        #no-cand-title {
            text-style: bold;
            padding: 1 0;
            text-align: center;
            color: $warning;
        }
    
        #no-cand-msg {
            text-align: center;
            padding: 1 0;
        }
    
        #no-cand-scroll {
            height: auto;
            max-height: 20;
            border: solid $panel;
            padding: 1;
            margin: 1 0;
        }
    
        #no-cand-close {
            margin-top: 1;
        }
        """
    
        def __init__(
            self,
            candidates: list[AnyTarget],
            skipped: list[SkippedTarget],
            target_kind: str,
        ) -> None:
            super().__init__()
            self._candidates = candidates
            self._skipped = skipped
            self._target_kind = target_kind
    
        def on_mount(self) -> None:
            if not self._candidates:
                self.push_screen(NoCandidatesScreen(self._skipped), callback=self._on_no_candidates_done)
            else:
                self.push_screen(
                    CleanupScreen(self._candidates, self._skipped, self._target_kind),
                    callback=self._on_cleanup_done,
                )
    
        def _on_no_candidates_done(self, _result: None) -> None:
            self.exit(("cancelled", []))
    
        def _on_cleanup_done(self, result: tuple[str, list[AnyTarget]]) -> None:
            if result is None:
                self.exit(("cancelled", []))
            else:
                self.exit(result)
    
    
    def _run_tui(
        candidates: list[AnyTarget],
        skipped: list[SkippedTarget],
        target_kind: str,
    ) -> tuple[str, list[AnyTarget]]:
        """Launch the Textual TUI and return (action, selected_targets)."""
        app = CleanupTui(candidates, skipped, target_kind)
        result = asyncio.run(app.run_async())
        if result is None:
            return ("cancelled", [])
        return result
    
    

else:

    # -------------------------------------------------------------------
    # Fallback interactive helpers
    # -------------------------------------------------------------------

    def _print_interactive_candidates(
        candidates: list[AnyTarget],
        *,
        target_kind: str,
    ) -> None:
        print(f"Cleanup {target_kind}: {len(candidates)} candidate(s)")
        for index, candidate in enumerate(candidates, start=1):
            label = candidate.display_label
            print(f"  c{index:<2} {label}")
            print(f"     {candidate.path}")

    def _print_skipped_summary(skipped: list[SkippedTarget]) -> None:
        if not skipped:
            return
        grouped = _skipped_by_reason(skipped)
        print(f"Skipped: {len(skipped)} item(s)")
        for index, (reason, items) in enumerate(grouped.items(), start=1):
            print(f"  s{index:<2} {reason}: {len(items)}")

    def _browse_skipped_targets_when_no_candidates(
        skipped: list[SkippedTarget],
        *,
        target_kind: str,
    ) -> None:
        print(f"Cleanup {target_kind}: 0 candidate(s)")
        _print_skipped_summary(skipped)
        if not skipped:
            return
        raw = input("Inspect skipped items [s=skips, q=close]: ").strip().lower()
        if raw in {"s", "skip", "skips"}:
            _browse_skipped_targets(skipped)

    def _browse_skipped_targets(skipped: list[SkippedTarget]) -> None:
        if not skipped:
            print("No skipped items.")
            return
        grouped = _skipped_by_reason(skipped)
        categories = list(grouped.items())
        while True:
            print("Skipped categories:")
            for index, (reason, items) in enumerate(categories, start=1):
                print(f"  s{index:<2} {reason}: {len(items)}")
            raw = (
                input("Expand skipped category [sN/number, b/q=back]: ")
                .strip()
                .lower()
            )
            if raw in {"", "b", "back", "q", "quit", "cancel"}:
                return
            try:
                idx = _parse_prefixed_index(raw, prefix="s")
            except ValueError:
                print("Choose a category number.", file=sys.stderr)
                continue
            if not 1 <= idx <= len(categories):
                print("Choose a category number.", file=sys.stderr)
                continue
            reason, items = categories[idx - 1]
            print(f"Skipped: {reason}")
            for item in sorted(items, key=lambda x: (x.kind, x.path)):
                print(f"  {item.kind}")
                print(f"    {item.path}")

    def _skipped_by_reason(
        skipped: list[SkippedTarget],
    ) -> dict[str, list[SkippedTarget]]:
        grouped: dict[str, list[SkippedTarget]] = {}
        for item in skipped:
            for reason in item.reason or ["unknown"]:
                grouped.setdefault(reason, []).append(item)
        return dict(sorted(grouped.items()))

    def _selected_candidates(
        candidates: list[AnyTarget],
        raw_selection: str,
    ) -> list[AnyTarget]:
        indexes: set[int] = set()
        for part in re.split(r"[\s,]+", raw_selection):
            if not part:
                continue
            try:
                index = _parse_prefixed_index(part, prefix="c")
            except ValueError:
                continue
            if 1 <= index <= len(candidates):
                indexes.add(index)
        return [
            c for i, c in enumerate(candidates, start=1) if i in indexes
        ]

    def _parse_prefixed_index(value: str, *, prefix: str) -> int:
        stripped = value.strip().lower()
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):]
        return int(stripped)

    def _run_tui(
        candidates: list[AnyTarget],
        skipped: list[SkippedTarget],
        target_kind: str,
    ) -> tuple[str, list[AnyTarget]]:
        """Fallback interactive selection using raw terminal I/O."""
        if not candidates:
            _browse_skipped_targets_when_no_candidates(skipped, target_kind=target_kind)
            return ("cancelled", [])

        selected: list[AnyTarget] = []
        while not selected:
            _print_interactive_candidates(candidates, target_kind=target_kind)
            _print_skipped_summary(skipped)
            raw_selection = (
                input("Select candidates [cN/numbers, a=all, s=skips, q=cancel]: ")
                .strip()
                .lower()
            )
            if raw_selection in {"", "q", "quit", "cancel"}:
                return ("cancelled", [])
            if raw_selection in {"s", "skip", "skips"}:
                _browse_skipped_targets(skipped)
                continue
            if raw_selection == "a":
                selected = list(candidates)
            else:
                selected = _selected_candidates(candidates, raw_selection)
            if not selected:
                print("No candidates selected.", file=sys.stderr)

        raw_action = (
            input("Action [t=trash, d=delete, q=cancel]: ").strip().lower()
        )
        if raw_action in {"t", "trash"}:
            result_action = "trash"
        elif raw_action in {"d", "delete"}:
            result_action = "delete"
        else:
            return ("cancelled", [])

        confirmation = (
            input(f"Type {result_action} to confirm {len(selected)} item(s): ")
            .strip()
            .lower()
        )
        if confirmation != result_action:
            return ("cancelled", [])
        return (result_action, selected)


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


class CleanupPlugin:
    name = "cleanup"

    def register(
        self, namespace_subparsers: argparse._SubParsersAction, ctx: CtPluginContext
    ) -> None:
        cleanup = namespace_subparsers.add_parser(
            "cleanup",
            help="Clean up old project directories and empty session logs.",
        )
        cleanup_sub = cleanup.add_subparsers(dest="cleanup_action", required=True)

        project = cleanup_sub.add_parser(
            "project",
            help="Preview or remove old project directories.",
        )
        project.add_argument(
            "--older-than",
            default="30d",
            type=_parse_age,
            metavar="AGE",
            help="Select projects with no activity newer than AGE. Supports Nd or Nh. Defaults to 30d.",
        )
        project.add_argument(
            "--path",
            default=None,
            help="Only consider project paths under this cleanup root. Defaults to all projects from ct project list.",
        )
        _add_action_flags(project)
        project.set_defaults(_cleanup_handler=lambda args: _handle_project(args, ctx))
        ctx.bind_command(
            project,
            handler=lambda args: args._cleanup_handler(args),
            renderer=_render_cleanup,
        )

        session = cleanup_sub.add_parser(
            "session",
            help="Preview or remove empty session logs.",
        )
        session.add_argument(
            "--agent-vendor",
            default=None,
            help="Filter by vendor. Known values: codex_cli, codex, pi.",
        )
        _add_action_flags(session)
        session.set_defaults(_cleanup_handler=_handle_session)
        ctx.bind_command(
            session,
            handler=lambda args: args._cleanup_handler(args),
            renderer=_render_cleanup,
        )


def _add_action_flags(parser: argparse.ArgumentParser) -> None:
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--trash",
        action="store_true",
        help="Move all selected candidates to the user trash after confirmation.",
    )
    action.add_argument(
        "--delete",
        action="store_true",
        help="Permanently delete all selected candidates after confirmation.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required with --trash or --delete.",
    )
    parser.add_argument(
        "--detail",
        action="store_true",
        help="Print the structured JSON data instead of the human-readable overview.",
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _handle_project(args: argparse.Namespace, ctx: CtPluginContext) -> dict[str, Any]:
    action = _resolve_action(args)
    root = _cleanup_root(args.path) if args.path else None
    cutoff = datetime.now(timezone.utc) - args.older_than
    all_projects = ctx.dispatch_core(
        method="project.list",
        params={},
        global_scope=True,
        current_dir=Path.cwd(),
    )
    recent_projects = ctx.dispatch_core(
        method="project.list",
        params={"modified_since": cutoff},
        global_scope=True,
        current_dir=Path.cwd(),
    )
    recent_keys = set((recent_projects.get("items") or {}).keys())
    codex_config_paths_by_project = _codex_config_paths_by_project()
    claude_metadata_paths_by_project = _claude_metadata_paths_by_project()
    pi_metadata_paths_by_project = _pi_metadata_paths_by_project()

    candidates: list[ProjectTarget] = []
    skipped: list[SkippedTarget] = []
    for project_name, item in (all_projects.get("items") or {}).items():
        metadata_paths = [
            *claude_metadata_paths_by_project.get(project_name, []),
            *pi_metadata_paths_by_project.get(project_name, []),
        ]
        target, skip = _project_metadata_target(
            project_name,
            item,
            root=root,
            is_recent=project_name in recent_keys,
            codex_config_paths=codex_config_paths_by_project.get(project_name, []),
            metadata_paths=metadata_paths,
        )
        if target is not None:
            candidates.append(target)
        skipped.extend(skip)

    candidates = sorted(candidates, key=lambda item: item.path)
    skipped = _dedupe_skips(skipped)
    action, selected = _resolve_interactive_selection(
        action, candidates, skipped=skipped, target_kind="project"
    )
    manifest_path, action_errors = _apply_action(
        action,
        [
            Path(cleanup_path)
            for target in selected
            for cleanup_path in _target_cleanup_paths(target)
        ],
        target_kind="project",
        config_entries=[
            (Path(config_path), target.path)
            for target in selected
            for config_path in target.cleanup_config_paths
        ],
    )
    return _payload(
        command="cleanup project",
        action=action,
        targets=[target.model_dump(mode="json") for target in selected],
        candidate_count=len(candidates),
        skipped=[
            item.model_dump(mode="json")
            for item in sorted(skipped, key=lambda item: (item.kind, item.path))
        ],
        manifest_path=manifest_path,
        filters={
            "older_than": _format_timedelta(args.older_than),
            "cutoff": cutoff.isoformat(),
            "path": str(root) if root else None,
        },
        discovery_note=None,
        action_errors=action_errors,
    )


def _handle_session(args: argparse.Namespace) -> dict[str, Any]:
    action = _resolve_action(args)
    vendor_filter = _normalize_vendor(args.agent_vendor)
    candidates: list[SessionTarget] = []
    skipped: list[SkippedTarget] = []
    for vendor, base_dir in _session_sources(vendor_filter):
        if not base_dir.is_dir():
            skipped.append(
                SkippedTarget(
                    kind="session",
                    path=str(base_dir),
                    reason=["missing_session_directory"],
                )
            )
            continue
        for path in sorted(base_dir.rglob("*.jsonl")):
            target, skip = _session_target(path, vendor=vendor)
            if target is not None:
                candidates.append(target)
            if skip is not None:
                skipped.append(skip)

    candidates = sorted(candidates, key=lambda item: item.path)
    skipped = _dedupe_skips(skipped)
    action, selected = _resolve_interactive_selection(
        action, candidates, skipped=skipped, target_kind="session"
    )
    manifest_path, action_errors = _apply_action(
        action, [Path(target.path) for target in selected], target_kind="session"
    )
    return _payload(
        command="cleanup session",
        action=action,
        targets=[target.model_dump(mode="json") for target in selected],
        candidate_count=len(candidates),
        skipped=[
            item.model_dump(mode="json")
            for item in sorted(skipped, key=lambda item: (item.kind, item.path))
        ],
        manifest_path=manifest_path,
        filters={"agent_vendor": vendor_filter},
        discovery_note=None,
        action_errors=action_errors,
    )


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


def _project_metadata_target(
    project_name: str,
    item: dict[str, Any],
    *,
    root: Path | None,
    is_recent: bool,
    codex_config_paths: list[Path],
    metadata_paths: list[Path],
) -> tuple[ProjectTarget | None, list[SkippedTarget]]:
    raw_path = item.get("path")
    project_path = (
        Path(raw_path).expanduser() if isinstance(raw_path, str) and raw_path else None
    )
    if project_path is None:
        return None, [_skip("project", project_name, "missing_project_path")]
    try:
        resolved = project_path.resolve()
    except OSError:
        resolved = project_path

    reasons: list[str] = []
    skips: list[SkippedTarget] = []
    if root is not None and not _is_relative_to(resolved, root):
        skips.append(_skip("project", str(resolved), "outside_cleanup_root"))
    if _is_current_or_parent(resolved, Path.cwd().resolve()):
        skips.append(_skip("project", str(resolved), "current_directory_or_parent"))

    if not resolved.exists():
        reasons.append("project_path_missing")
    else:
        if is_recent:
            skips.append(_skip("project", str(resolved), "newer_than_retention"))
        else:
            reasons.append("older_than_retention")
        if not _looks_like_project_directory(resolved):
            reasons.append("not_project_directory")
        if (resolved / ".ct-cleanup-keep").exists():
            skips.append(_skip("project", str(resolved), "keep_marker"))
        git_skips = _git_skip_reasons(resolved)
        skips.extend(_skip("project", str(resolved), reason) for reason in git_skips)

    if skips:
        return None, skips

    cleanup_config_paths = (
        [str(path) for path in codex_config_paths] if not resolved.exists() else []
    )
    cleanup_paths = (
        [str(path) for path in metadata_paths] if not resolved.exists() else []
    )
    return (
        ProjectTarget(
            project=project_name,
            path=str(resolved),
            cleanup_paths=cleanup_paths,
            cleanup_config_paths=cleanup_config_paths,
            reason=reasons or ["old_project"],
            last_activity_at=None,
            session_count=len(metadata_paths) if cleanup_paths else 0,
            vendors=sorted(item.get("vendors") or []),
        ),
        [],
    )


def _session_target(
    path: Path,
    *,
    vendor: str,
) -> tuple[SessionTarget | None, SkippedTarget | None]:
    if _recently_modified(path, timedelta(hours=24)):
        return None, _skip("session", str(path), "modified_in_last_24h")
    records = _load_jsonl(path)
    if records is None:
        return None, _skip("session", str(path), "unreadable_or_invalid")

    if _has_useful_session_records(vendor, records):
        return None, None

    return (
        SessionTarget(
            vendor=vendor,
            path=str(path.resolve()),
            reason=["empty"],
            modified_at=_modified_at(path),
            session_id=_session_id_from_records(vendor, records),
        ),
        None,
    )


# ---------------------------------------------------------------------------
# Codex config cleanup
# ---------------------------------------------------------------------------


def _remove_codex_project_config_entry(config_path: Path, project_path: str) -> None:
    text = config_path.read_text(encoding="utf-8")
    header = f'[projects."{project_path}"]'
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    index = 0
    removed = False
    while index < len(lines):
        if lines[index].strip() != header:
            output.append(lines[index])
            index += 1
            continue
        removed = True
        index += 1
        while index < len(lines) and not lines[index].lstrip().startswith("["):
            index += 1
    if removed:
        config_path.write_text("".join(output), encoding="utf-8")


# ---------------------------------------------------------------------------
# Path discovery helpers
# ---------------------------------------------------------------------------


def _codex_config_paths_by_project() -> dict[str, list[Path]]:
    paths_by_project: dict[str, set[Path]] = {}
    config_path = _codex_config_path()
    if not config_path.is_file():
        return {}
    try:
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}

    projects = config.get("projects")
    if not isinstance(projects, dict):
        return {}
    for raw_path in projects:
        if not isinstance(raw_path, str) or not raw_path:
            continue
        project_name = Path(raw_path).expanduser().name
        if not project_name:
            continue
        paths_by_project.setdefault(_normalize_project_key(project_name), set()).add(
            config_path
        )
    return {
        project: sorted(paths)
        for project, paths in paths_by_project.items()
    }


def _claude_metadata_paths_by_project() -> dict[str, list[Path]]:
    paths_by_project: dict[str, set[Path]] = {}
    base = Path.home() / ".claude" / "projects"
    if not base.is_dir():
        return {}
    for path in base.iterdir():
        if not path.is_dir():
            continue
        decoded = _decode_claude_encoded_path(path.name)
        if decoded is None:
            continue
        project_name = Path(decoded).name
        if not project_name:
            continue
        paths_by_project.setdefault(_normalize_project_key(project_name), set()).add(
            path
        )
    return {
        project: sorted(paths)
        for project, paths in paths_by_project.items()
    }


def _decode_claude_encoded_path(encoded: str) -> str | None:
    if not encoded:
        return None
    stripped = encoded.lstrip("-")
    if not stripped:
        return None
    return "/" + stripped.replace("-", "/")


def _pi_metadata_paths_by_project() -> dict[str, list[Path]]:
    paths_by_project: dict[str, set[Path]] = {}
    session_root, custom_session_dir = _pi_session_root()
    if not session_root.is_dir():
        return {}

    if custom_session_dir:
        for path in session_root.iterdir():
            if not path.is_file() or path.suffix != ".jsonl":
                continue
            project_key = _project_key_from_pi_session_file(path)
            if project_key:
                paths_by_project.setdefault(project_key, set()).add(path)
    else:
        for path in session_root.iterdir():
            if not path.is_dir():
                continue
            project_key = _project_key_from_pi_project_dir(path)
            if project_key:
                paths_by_project.setdefault(project_key, set()).add(path)

    return {
        project: sorted(paths)
        for project, paths in paths_by_project.items()
    }


def _pi_session_root() -> tuple[Path, bool]:
    configured = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
    if configured:
        return Path(configured).expanduser(), True

    agent_dir = _pi_agent_dir()
    settings_path = agent_dir / "settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        settings = {}
    if isinstance(settings, dict):
        session_dir = settings.get("sessionDir")
        if isinstance(session_dir, str) and session_dir:
            return _resolve_pi_settings_path(session_dir, base_dir=agent_dir), True

    return agent_dir / "sessions", False


def _pi_agent_dir() -> Path:
    configured = os.environ.get("PI_CODING_AGENT_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".pi" / "agent"


def _resolve_pi_settings_path(raw_path: str, *, base_dir: Path) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return base_dir / path


def _project_key_from_pi_project_dir(path: Path) -> str | None:
    for session_file in sorted(path.iterdir()):
        if not session_file.is_file() or session_file.suffix != ".jsonl":
            continue
        project_key = _project_key_from_pi_session_file(session_file)
        if project_key:
            return project_key
    decoded = _decode_pi_encoded_path(path.name)
    if decoded is None:
        return None
    project_name = Path(decoded).name
    return _normalize_project_key(project_name) if project_name else None


def _project_key_from_pi_session_file(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8") as handle:
            for raw_line in handle:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                record = json.loads(stripped)
                if not isinstance(record, dict) or record.get("type") != "session":
                    return None
                cwd = record.get("cwd")
                if not isinstance(cwd, str) or not cwd:
                    return None
                project_name = Path(cwd).expanduser().name
                return _normalize_project_key(project_name) if project_name else None
    except (OSError, json.JSONDecodeError):
        return None
    return None


def _decode_pi_encoded_path(encoded: str) -> str | None:
    stripped = encoded.strip("-")
    if not stripped:
        return None
    return "/" + stripped.replace("-", "/")


def _codex_config_path() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser() / "config.toml"
    return Path.home() / ".codex" / "config.toml"


def _normalize_project_key(value: str) -> str:
    collapsed = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", value.strip())
    return re.sub(r"[^a-zA-Z0-9]+", "-", collapsed).strip("-").lower()


# ---------------------------------------------------------------------------
# Payload construction
# ---------------------------------------------------------------------------


def _payload(
    *,
    command: str,
    action: Action,
    targets: list[dict[str, Any]],
    candidate_count: int,
    skipped: list[dict[str, Any]],
    manifest_path: str | None,
    filters: dict[str, Any],
    discovery_note: str | None,
    action_errors: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "command": command,
        "action": action,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "filters": filters,
        "summary": {
            "target_count": len(targets),
            "candidate_count": candidate_count,
            "skipped_count": len(skipped),
            "error_count": len(action_errors),
            "skipped_reasons": dict(
                Counter(reason for item in skipped for reason in item.get("reason", []))
            ),
        },
        "targets": targets,
        "skipped": skipped,
        "manifest_path": manifest_path,
        "discovery_note": discovery_note,
        "errors": action_errors,
    }


def _dedupe_skips(skipped: list[SkippedTarget]) -> list[SkippedTarget]:
    merged: dict[tuple[str, str], set[str]] = {}
    for item in skipped:
        key = (item.kind, item.path)
        merged.setdefault(key, set()).update(item.reason)
    return [
        SkippedTarget(
            kind=kind,
            path=path,
            reason=sorted(reasons),
        )
        for (kind, path), reasons in merged.items()
    ]


# ---------------------------------------------------------------------------
# Actions and selection
# ---------------------------------------------------------------------------


def _resolve_action(args: argparse.Namespace) -> Action:
    if getattr(args, "delete", False):
        action: Action = "delete"
    elif getattr(args, "trash", False):
        action = "trash"
    else:
        return "interactive"
    if not getattr(args, "confirm", False):
        raise ValueError(f"--{action} requires --confirm")
    return action


def _resolve_interactive_selection(
    action: Action,
    candidates: list[ProjectTarget | SessionTarget],
    *,
    skipped: list[SkippedTarget],
    target_kind: str,
) -> tuple[Action, list[ProjectTarget | SessionTarget]]:
    if action != "interactive":
        return action, list(candidates)
    return _run_tui(list(candidates), skipped, target_kind)


def _target_cleanup_paths(target: ProjectTarget | SessionTarget) -> list[str]:
    if not isinstance(target, ProjectTarget):
        return [target.path]
    if "project_path_missing" in target.reason:
        return target.cleanup_paths
    if target.cleanup_config_paths:
        return target.cleanup_paths
    return target.cleanup_paths or [target.path]


def _apply_action(
    action: Action,
    paths: list[Path],
    *,
    target_kind: str,
    config_entries: list[tuple[Path, str]] | None = None,
) -> tuple[str | None, list[dict[str, str]]]:
    config_entries = config_entries or []
    if action in {"interactive", "cancelled"} or (not paths and not config_entries):
        return None, []
    manifest = {
        "action": action,
        "target_kind": target_kind,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "paths": [str(path) for path in paths],
        "config_entries": [
            {"config_path": str(path), "project_path": project_path}
            for path, project_path in config_entries
        ],
        "errors": [],
    }
    errors: list[dict[str, str]] = []
    for config_path, project_path in config_entries:
        try:
            _remove_codex_project_config_entry(config_path, project_path)
        except OSError as exc:
            errors.append({"path": str(config_path), "error": str(exc)})
    for path in paths:
        try:
            if action == "trash":
                _move_to_trash(path)
            elif action == "delete":
                _delete_path(path)
        except OSError as exc:
            errors.append({"path": str(path), "error": str(exc)})
    manifest["errors"] = errors
    manifest_path = _write_manifest(manifest)
    return str(manifest_path), errors


def _write_manifest(manifest: dict[str, Any]) -> Path:
    directory = Path.home() / ".coding-trajectory" / "cleanup-manifests"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"cleanup-{stamp}.json"
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path


def _move_to_trash(path: Path) -> None:
    trash = Path.home() / ".Trash"
    trash.mkdir(exist_ok=True)
    destination = _unique_destination(trash / path.name)
    shutil.move(str(path), str(destination))


def _delete_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _unique_destination(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 10_000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise ValueError(f"could not find unique trash destination for {path}")


# ---------------------------------------------------------------------------
# Session sources
# ---------------------------------------------------------------------------


def _session_sources(vendor_filter: str | None) -> list[tuple[str, Path]]:
    home = Path.home()
    sources: list[tuple[str, Path]] = [
        ("codex_cli", home / ".codex" / "sessions"),
        ("pi", home / ".pi" / "agent" / "sessions"),
    ]
    if vendor_filter is None:
        return sources
    return [source for source in sources if source[0] == vendor_filter]


def _normalize_vendor(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    aliases = {"codex": "codex_cli", "codex-cli": "codex_cli"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"codex_cli", "pi"}:
        raise ValueError(
            "unknown --agent-vendor value; expected codex_cli, codex, or pi"
        )
    return normalized


# ---------------------------------------------------------------------------
# Time / age helpers
# ---------------------------------------------------------------------------


def _parse_age(value: str) -> timedelta:
    match = re.fullmatch(r"\s*(\d+)\s*([dh])\s*", value)
    if not match:
        raise argparse.ArgumentTypeError("must look like 30d or 12h")
    amount = int(match.group(1))
    if amount < 1:
        raise argparse.ArgumentTypeError("must be positive")
    unit = match.group(2)
    return timedelta(days=amount) if unit == "d" else timedelta(hours=amount)


def _format_timedelta(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds % 86_400 == 0:
        return f"{seconds // 86_400}d"
    if seconds % 3_600 == 0:
        return f"{seconds // 3_600}h"
    return f"{seconds}s"


def _cleanup_root(raw_path: str | None) -> Path:
    return Path(raw_path).expanduser().resolve()


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _git_skip_reasons(path: Path) -> list[str]:
    if not (path / ".git").exists():
        return []
    reasons: list[str] = []
    status = _git(path, "status", "--porcelain")
    if status is None:
        reasons.append("git_status_failed")
    elif status.strip():
        reasons.append("git_dirty")
    unpushed = _git(path, "rev-list", "--count", "@{u}..HEAD")
    if unpushed is None:
        reasons.append("git_upstream_missing")
    else:
        try:
            if int(unpushed.strip() or "0") > 0:
                reasons.append("git_unpushed_commits")
        except ValueError:
            reasons.append("git_unpushed_check_failed")
    return reasons


def _git(path: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


# ---------------------------------------------------------------------------
# Session record analysis
# ---------------------------------------------------------------------------


def _has_useful_session_records(vendor: str, records: list[dict[str, Any]]) -> bool:
    if vendor == "codex_cli":
        return any(_is_useful_codex_record(record) for record in records)
    if vendor == "pi":
        return any(_is_useful_pi_record(record) for record in records)
    return True


def _session_id_from_records(vendor: str, records: list[dict[str, Any]]) -> str | None:
    if vendor == "codex_cli":
        for record in records:
            if record.get("type") != "session_meta":
                continue
            payload = record.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("id"), str):
                return payload["id"]
    if vendor == "pi":
        for record in records:
            if record.get("type") == "session" and isinstance(record.get("id"), str):
                return record["id"]
    return None


def _is_useful_codex_record(record: dict[str, Any]) -> bool:
    record_type = record.get("type")
    payload = record.get("payload")
    if record_type == "response_item":
        return True
    if record_type != "event_msg" or not isinstance(payload, dict):
        return False
    return payload.get("type") in {"user_message", "task_complete"}


def _is_useful_pi_record(record: dict[str, Any]) -> bool:
    if record.get("type") != "message":
        return False
    message = record.get("message")
    if not isinstance(message, dict):
        return False
    return message.get("role") in {"user", "assistant", "toolResult", "bashExecution"}


def _load_jsonl(path: Path) -> list[dict[str, Any]] | None:
    records: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                value = json.loads(stripped)
                if isinstance(value, dict):
                    records.append(value)
    except (OSError, json.JSONDecodeError):
        return None
    return records


# ---------------------------------------------------------------------------
# File system helpers
# ---------------------------------------------------------------------------


def _recently_modified(path: Path, duration: timedelta) -> bool:
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return False
    return modified >= datetime.now(timezone.utc) - duration


def _modified_at(path: Path) -> str | None:
    modified_at = _path_modified_at_datetime(path)
    return modified_at.isoformat() if modified_at is not None else None


def _path_modified_at_datetime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_current_or_parent(path: Path, current: Path) -> bool:
    return path == current or path in current.parents


def _looks_like_project_directory(path: Path) -> bool:
    if not path.is_dir():
        return False
    project_markers = {
        ".git",
        ".hg",
        ".svn",
        "pyproject.toml",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "settings.gradle",
        "Makefile",
        "README.md",
    }
    return any((path / marker).exists() for marker in project_markers)


def _skip(kind: str, path: str, reason: str) -> SkippedTarget:
    return SkippedTarget(kind=kind, path=path, reason=[reason])


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def _render_cleanup(args: argparse.Namespace, payload: dict[str, Any]) -> str:
    if getattr(args, "detail", False):
        return json.dumps(payload, indent=2, ensure_ascii=False)
    summary = payload.get("summary") or {}
    lines = [
        payload.get("command", "cleanup"),
        f"Action: {payload.get('action') or 'interactive'}",
        f"Selected: {summary.get('target_count') or 0} of {summary.get('candidate_count') or 0}",
        f"Skipped: {summary.get('skipped_count') or 0}",
    ]
    if summary.get("error_count"):
        lines.append(f"Errors: {summary['error_count']}")
    skipped_reasons = summary.get("skipped_reasons") or {}
    if skipped_reasons:
        lines.append("Skip reasons:")
        for reason, count in sorted(skipped_reasons.items()):
            lines.append(f"  {reason}: {count}")
    targets = payload.get("targets") or []
    if targets:
        lines.append("Candidates:")
        for item in targets[:20]:
            details = []
            if item.get("project"):
                details.append(str(item["project"]))
            if item.get("vendor"):
                details.append(str(item["vendor"]))
            if details:
                lines.append("  " + "  ".join(details))
            lines.append(f"    {item.get('path')}")
        if len(targets) > 20:
            lines.append(
                f"  ... {len(targets) - 20} more. Use --detail for the full list."
            )
    if payload.get("manifest_path"):
        lines.append(f"Manifest: {payload['manifest_path']}")
    errors = payload.get("errors") or []
    if errors:
        lines.append("Action errors:")
        for item in errors[:10]:
            lines.append(f"  {item.get('path')}: {item.get('error')}")
    return "\n".join(lines).rstrip()


plugin = CleanupPlugin()
