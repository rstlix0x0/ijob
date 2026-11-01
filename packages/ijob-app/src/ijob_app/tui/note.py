"""The modal that asks for the note on a posting's vote."""

from typing import ClassVar, override

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label


class NoteScreen(ModalScreen[str | None]):
    """Ask for the note that goes with a vote.

    Enter dismisses with the typed text, trimmed; an empty text means the note
    is to be removed. Escape dismisses with `None`, meaning no change.
    """

    DEFAULT_CSS = """
    NoteScreen { align: center middle; }
    NoteScreen > Vertical {
        width: 60%;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "cancel"),
    ]

    def __init__(self, note: str | None) -> None:
        """Build the modal.

        Args:
            note: The posting's current note, which the input starts with.
        """
        super().__init__()
        self._note = note or ""

    @override
    def compose(self) -> ComposeResult:
        """Lay out the prompt and the input."""
        with Vertical():
            yield Label("note (enter saves, escape cancels)")
            yield Input(self._note, id="note")

    @on(Input.Submitted, "#note")
    def _submitted(self, event: Input.Submitted) -> None:
        """Dismiss with the typed text."""
        event.stop()
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        """Dismiss without changing the note."""
        self.dismiss(None)
