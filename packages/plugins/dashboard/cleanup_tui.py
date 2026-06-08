from __future__ import annotations

import asyncio
from collections import Counter

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Footer, Header, Label, Static

from cleanup import AnyTarget, SkippedTarget


class CandidateRow(Static):
    def __init__(self, candidate: AnyTarget, index: int) -> None:
        self.candidate = candidate
        self.index = index
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Checkbox(f" {self.candidate.display_label}", id=f"cb-{self.index}")
        yield Label(f"    {self.candidate.display_detail}", id=f"detail-{self.index}")


class SkippedList(Static):
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
            for item in sorted(items, key=lambda value: (value.kind, value.path)):
                lines.append(f"    [{item.kind}] {item.path}")
        return "\n".join(lines)


class SkippedModal(ModalScreen[None]):
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
    def __init__(self, action: str, count: int) -> None:
        super().__init__()
        self.action = action
        self.count = count

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(f"{self.action.capitalize()} {self.count} item(s)?", id="confirm-title"),
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
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

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
            for index, candidate in enumerate(self.candidates):
                yield CandidateRow(candidate, index)

        yield Static(self._skipped_summary_text(), id="skipped-summary")

        with Horizontal(id="select-buttons"):
            yield Button("Select All", variant="default", id="select-all")
            yield Button("Deselect All", variant="default", id="deselect-all")

        with Horizontal(id="action-buttons"):
            trash_btn = Button(
                "Trash Selected (0)",
                variant="warning",
                id="trash",
                disabled=True,
            )
            trash_btn.tooltip = "Move selected items to system trash"
            yield trash_btn

            delete_btn = Button(
                "Delete Selected (0)",
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
        reasons = ", ".join(f"{reason}: {count}" for reason, count in sorted(by_reason.items()))
        return f"Skipped: {len(self.skipped)} ({reasons})"

    def _update_button_states(self) -> None:
        count = len(self.selected)
        trash_btn = self.query_one("#trash", Button)
        delete_btn = self.query_one("#delete", Button)
        trash_btn.label = f"Trash Selected ({count})"
        delete_btn.label = f"Delete Selected ({count})"
        trash_btn.disabled = count == 0
        delete_btn.disabled = count == 0

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        try:
            index = int(event.checkbox.id.split("-")[1])
        except (AttributeError, IndexError, ValueError):
            return
        if event.checkbox.value:
            self.selected.add(index)
        else:
            self.selected.discard(index)
        self._update_button_states()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id

        if button_id == "select-all":
            for index in range(len(self.candidates)):
                self.selected.add(index)
                try:
                    self.query_one(f"#cb-{index}", Checkbox).value = True
                except Exception:
                    pass
            self._update_button_states()
            return

        if button_id == "deselect-all":
            self.selected.clear()
            for index in range(len(self.candidates)):
                try:
                    self.query_one(f"#cb-{index}", Checkbox).value = False
                except Exception:
                    pass
            self._update_button_states()
            return

        if button_id == "view-skipped":
            self.app.push_screen(SkippedModal(self.skipped))
            return

        if button_id == "cancel":
            self.dismiss(("cancelled", []))
            return

        if button_id in {"trash", "delete"} and self.selected:
            self._confirm_action(button_id)

    async def _confirm_action(self, action: str) -> None:
        confirmed = await self.app.push_screen_wait(ConfirmModal(action, len(self.selected)))
        if not confirmed:
            return
        selected_targets = [self.candidates[index] for index in sorted(self.selected)]
        self.dismiss((action, selected_targets))

    def action_cancel(self) -> None:
        self.dismiss(("cancelled", []))


class CleanupTui(App[tuple[str, list[AnyTarget]]]):
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
            self.push_screen(
                NoCandidatesScreen(self._skipped),
                callback=self._on_no_candidates_done,
            )
            return
        self.push_screen(
            CleanupScreen(self._candidates, self._skipped, self._target_kind),
            callback=self._on_cleanup_done,
        )

    def _on_no_candidates_done(self, _result: None) -> None:
        self.exit(("cancelled", []))

    def _on_cleanup_done(self, result: tuple[str, list[AnyTarget]] | None) -> None:
        if result is None:
            self.exit(("cancelled", []))
            return
        self.exit(result)


def run_tui(
    candidates: list[AnyTarget],
    skipped: list[SkippedTarget],
    target_kind: str,
) -> tuple[str, list[AnyTarget]]:
    app = CleanupTui(candidates, skipped, target_kind)
    result = asyncio.run(app.run_async())
    if result is None:
        return ("cancelled", [])
    return result
