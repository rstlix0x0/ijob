"""The modal that shows a source the agent found and asks the user to add it."""

from collections.abc import Sequence
from typing import ClassVar, override

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from ijob_app.agent_ops import FoundSource
from ijob_app.view import clean

_STEP_WIDTH = 4
_WHAT_WIDTH = 30


class SourceFoundScreen(ModalScreen[bool]):
    """Show the source the agent verified and ask whether to add it.

    Accept dismisses with `True`. Cancel and Escape dismiss with `False`. Cancel
    has the focus on opening, so a stray Enter adds nothing. The lines scroll
    when they are taller than the screen, so the buttons stay in view.
    """

    AUTO_FOCUS = "#cancel"

    DEFAULT_CSS = """
    SourceFoundScreen { align: center middle; }
    SourceFoundScreen > Vertical {
        width: 90%;
        height: auto;
        max-height: 100%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    SourceFoundScreen #lines { height: auto; max-height: 60vh; }
    SourceFoundScreen .heading { text-style: bold; }
    SourceFoundScreen .buttons { height: auto; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "cancel"),
    ]

    def __init__(
        self, found: FoundSource, steps: Sequence[tuple[str, str]], cost: str
    ) -> None:
        """Build the modal.

        Args:
            found: The source to offer.
            steps: The WHAT and the RESULT of each step the agent took, in order.
            cost: What the run spent against its cap, as one line.
        """
        super().__init__()
        self._found = found
        self._steps = steps
        self._cost = cost

    @override
    def compose(self) -> ComposeResult:
        """Lay out the source, the steps that led to it and the two buttons.

        The lines scroll in their own region above the buttons.

        The lines carry text from pages and from the model, so none of them is
        read as markup.
        """
        found = self._found
        with Vertical():
            yield Static(
                f"Found {clean(str(found.ref))}", classes="heading", markup=False
            )
            with VerticalScroll(id="lines"):
                yield Static(
                    f"{'STEP':>{_STEP_WIDTH}}  {'WHAT':<{_WHAT_WIDTH}}  RESULT",
                    classes="line",
                    markup=False,
                )
                for number, (what, result) in enumerate(self._steps, start=1):
                    yield Static(
                        f"{number:>{_STEP_WIDTH}}  "
                        f"{clean(what):<{_WHAT_WIDTH}}  {clean(result)}",
                        classes="line",
                        markup=False,
                    )
                yield Static(f"{found.jobs:,} jobs read", classes="line", markup=False)
                if found.sample:
                    titles = " · ".join(clean(title) for title in found.sample)
                    yield Static(f"Sample: {titles}", classes="line", markup=False)
                if found.known:
                    yield Static(
                        "You already have this source.", classes="line", markup=False
                    )
                yield Static(self._cost, classes="line", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Accept", id="accept", variant="primary")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#accept")
    def _accept(self) -> None:
        """Dismiss, telling the caller to add the source."""
        self.dismiss(True)  # noqa: FBT003 - the result is Textual's positional argument

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        """Dismiss without adding anything."""
        self.dismiss(False)  # noqa: FBT003 - the result is Textual's positional argument
