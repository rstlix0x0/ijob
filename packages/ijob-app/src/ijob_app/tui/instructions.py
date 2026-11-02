"""The screen that edits the standing instructions and confirms what they would set."""

import sqlite3
from typing import ClassVar, override

from ijob_ai.errors import LlmError
from ijob_core.errors import InvalidValueError
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, Footer, Static, TextArea

from ijob_app.config import FilterLayers
from ijob_app.llm_ops import Proposal
from ijob_app.llm_text import instruction_lines, shadow_line
from ijob_app.service import Service

_READING = "reading the instructions…"
_HINT = "Describe the postings you want and the ones you do not."


def _proposal_text(proposal: Proposal) -> str:
    """Lay a proposal out as `filters:` and `rules:` blocks, then its warnings."""
    lines = instruction_lines(proposal.instruction_filters, proposal.rules)
    lines += [shadow_line(shadowed) for shadowed in proposal.shadowed]
    return "\n".join(lines)


class InstructionsScreen(Screen[None]):
    """Edit the instructions text and confirm what it would set before storing it.

    Submit has the model read the text into filters and judge rules without
    storing anything; the screen then lists them, and the warnings for any filter
    that a flag or the user's file overrides, under Accept and Cancel. Accept
    stores the proposal and returns; Cancel and escape return without storing.
    A text the service refuses, an answer with nothing to act on or a provider
    failure shows its reason under the input. The model runs in a worker, and
    everything it produced is shown as plain text.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "back", "back"),
    ]
    DEFAULT_CSS = """
    InstructionsScreen #hint { height: auto; padding: 1 2 0 2; }
    InstructionsScreen #text { height: 8; margin: 0 2; }
    InstructionsScreen #submit { margin: 0 2; }
    InstructionsScreen #error { color: $error; height: auto; padding: 0 2; }
    InstructionsScreen #proposal { height: auto; padding: 1 2; }
    InstructionsScreen #confirm { height: auto; padding: 0 2; }
    """

    def __init__(self, service: Service, layers: FilterLayers) -> None:
        """Build the screen over an open service.

        Args:
            service: The service that reads, proposes and stores the
                instructions; it must have been opened on the thread that runs
                the app.
            layers: The filters the packaged config, the user's file and the
                app's inputs set, which the proposal is merged with to find the
                filters that would be shadowed.
        """
        super().__init__()
        self._service = service
        self._layers = layers
        self._proposal: Proposal | None = None

    @override
    def compose(self) -> ComposeResult:
        """Lay out the text, the Submit button, the error and the confirmation."""
        stored = self._service.instructions()
        yield Static(_HINT, id="hint", markup=False)
        yield TextArea("" if stored is None else stored.text, id="text")
        yield Button("Submit", id="submit")
        yield Static("", id="error", markup=False)
        yield Static("", id="proposal", markup=False)
        with Horizontal(id="confirm"):
            yield Button("Accept", id="accept", variant="success")
            yield Button("Cancel", id="cancel")
        yield Footer()

    def on_mount(self) -> None:
        """Hide the confirmation until there is a proposal to confirm."""
        self.query_one("#confirm").display = False
        self.query_one("#text", TextArea).focus()

    def action_back(self) -> None:
        """Return to the screen below without storing anything."""
        self.dismiss(None)

    @on(Button.Pressed, "#submit")
    def _submit(self) -> None:
        """Start reading the text, dropping any proposal shown before."""
        self._clear_proposal()
        self._propose(self.query_one("#text", TextArea).text)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        """Discard the proposal and return."""
        self.dismiss(None)

    @on(Button.Pressed, "#accept")
    def _accept(self) -> None:
        """Store the proposal and return, or show why it cannot be stored."""
        if self._proposal is None:
            return
        try:
            self._service.accept_instructions(self._proposal)
        except (InvalidValueError, LlmError, sqlite3.Error) as error:
            self._show_error(error)
            return
        self.dismiss(None)

    @work(exclusive=True, group="instructions")
    async def _propose(self, text: str) -> None:
        """Work out what the text would set and show it, or show why not.

        Runs on the app's loop because the service is bound to the thread that
        opened it. A replaced submission is cancelled where it waits.
        """
        self.query_one("#error", Static).update(_READING)
        try:
            proposal = await self._service.propose_instructions(self._layers, text)
        except (InvalidValueError, LlmError, sqlite3.Error) as error:
            self._show_error(error)
            return
        self.query_one("#error", Static).update("")
        self._proposal = proposal
        self.query_one("#proposal", Static).update(_proposal_text(proposal))
        self.query_one("#confirm").display = True
        self.query_one("#accept", Button).focus()

    def _clear_proposal(self) -> None:
        """Forget the proposal shown and hide its confirmation."""
        self._proposal = None
        self.query_one("#proposal", Static).update("")
        self.query_one("#confirm").display = False

    def _show_error(self, error: Exception) -> None:
        """Show an error under the input, in place of any proposal."""
        self._clear_proposal()
        self.query_one("#error", Static).update(f"error: {error}")
