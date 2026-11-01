"""The screen that shows the stored CV and stores a new one from a path."""

from pathlib import Path
from typing import ClassVar, override

from ijob_core.model import StoredCv
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, Static

from ijob_app.service import InvalidCvError, Service


def _describe(cv: StoredCv | None) -> str:
    """Say which CV is stored: its name, size and when it was stored."""
    if cv is None:
        return "no CV stored"
    return f"{cv.name}  {cv.size} bytes  stored {cv.stored_at:%Y-%m-%d %H:%M}"


class CvScreen(Screen[None]):
    """Show the stored CV and store another PDF from a typed path.

    There is no file browser: the path is typed, with `~` expanded by the
    service. A file that cannot be stored, or a copy that cannot be written, shows
    its reason under the input.
    Escape returns to the screen below.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "back", "back"),
    ]
    DEFAULT_CSS = """
    CvScreen #current { height: auto; padding: 1 2; }
    CvScreen .entry { height: auto; }
    CvScreen .entry Input { width: 1fr; }
    CvScreen #error { color: $error; height: auto; padding: 0 2; }
    """

    def __init__(self, service: Service) -> None:
        """Build the screen over an open service.

        Args:
            service: The service that reads and stores the CV; it must have been
                opened on the thread that runs the app.
        """
        super().__init__()
        self._service = service

    @override
    def compose(self) -> ComposeResult:
        """Lay out the stored CV, the path input with its button, and the error."""
        yield Static(_describe(self._service.cv()), id="current", markup=False)
        with Horizontal(classes="entry"):
            yield Input(placeholder="path to a PDF", id="path")
            yield Button("Store", id="store")
        yield Static("", id="error", markup=False)
        yield Footer()

    def action_back(self) -> None:
        """Return to the screen below."""
        self.dismiss(None)

    @on(Button.Pressed, "#store")
    @on(Input.Submitted, "#path")
    def _store(self) -> None:
        """Store the file the path names, or show why it cannot be stored."""
        path = Path(self.query_one("#path", Input).value.strip())
        try:
            cv = self._service.store_cv(path)
        except InvalidCvError as error:
            self.query_one("#error", Static).update(error.reason)
            return
        except OSError as error:
            self.query_one("#error", Static).update(str(error))
            return
        self.query_one("#error", Static).update("")
        self.query_one("#current", Static).update(_describe(cv))
