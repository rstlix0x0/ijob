"""The modal that shows a learned recipe and asks the user to accept it."""

from typing import ClassVar, override

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from ijob_app.llm_text import recipe_proposal_lines
from ijob_app.recipe_ops import RecipeProposal


class RecipeProposalScreen(ModalScreen[bool]):
    """Show what a learned recipe reads and ask whether to store the source.

    Accept dismisses with `True`. Cancel and Escape dismiss with `False`. Cancel
    has the focus on opening, so a stray Enter stores nothing. The lines scroll
    when they are taller than the screen, so the buttons stay in view.
    """

    AUTO_FOCUS = "#cancel"

    DEFAULT_CSS = """
    RecipeProposalScreen { align: center middle; }
    RecipeProposalScreen > Vertical {
        width: 90%;
        height: auto;
        max-height: 100%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    RecipeProposalScreen #lines { height: auto; max-height: 60vh; }
    RecipeProposalScreen .buttons { height: auto; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "cancel"),
    ]

    def __init__(self, proposal: RecipeProposal) -> None:
        """Build the modal.

        Args:
            proposal: The recipe to show.
        """
        super().__init__()
        self._proposal = proposal

    @override
    def compose(self) -> ComposeResult:
        """Lay out one line per fact about the recipe, then the two buttons.

        The lines scroll in their own region above the buttons.

        The lines carry text from the page and from the model, so they are not
        read as markup.
        """
        with Vertical():
            with VerticalScroll(id="lines"):
                for line in recipe_proposal_lines(self._proposal):
                    yield Static(line, classes="line", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Accept", id="accept", variant="primary")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#accept")
    def _accept(self) -> None:
        """Dismiss, telling the caller to store the source."""
        self.dismiss(True)  # noqa: FBT003 - the result is Textual's positional argument

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        """Dismiss without storing anything."""
        self.dismiss(False)  # noqa: FBT003 - the result is Textual's positional argument
