"""The screen that shows the stored CV and its profile, and stores a new CV."""

import sqlite3
from pathlib import Path
from typing import ClassVar, Final, override

from ijob_ai.errors import LlmError, MissingModelError
from ijob_ai.llm.settings import Task
from ijob_core.errors import InvalidValueError
from ijob_core.model import ResumeProfile, StoredCv
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, Static

from ijob_app.llm_config import read_llm_settings
from ijob_app.llm_text import profile_lines
from ijob_app.service import InvalidCvError, Service

_NO_PROFILE: Final = "no profile yet"
_NO_MODEL_NOTE: Final = "profile not built; set [models].cv_profile"
_BUILDING: Final = "reading the CV…"


def _describe(cv: StoredCv | None) -> str:
    """Say which CV is stored: its name, size and when it was stored."""
    if cv is None:
        return "no CV stored"
    return f"{cv.name}  {cv.size} bytes  stored {cv.stored_at:%Y-%m-%d %H:%M}"


def _profile_text(profile: ResumeProfile | None) -> str:
    """Lay a CV profile out as `key: value` lines, or say there is none."""
    if profile is None:
        return _NO_PROFILE
    return "\n".join(profile_lines(profile))


class CvScreen(Screen[None]):
    """Show the stored CV and its profile, and store another PDF from a typed path.

    There is no file browser: the path is typed, with `~` expanded by the
    service. A file that cannot be stored, or a copy that cannot be written, shows
    its reason under the input.
    The profile read from the CV sits below it, or `no profile yet`. Storing a
    CV builds the profile when `[models].cv_profile` is set and otherwise shows a
    note saying so; Refresh reads the CV again. The model runs in a worker, and
    every profile field and note is shown as plain text.
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
    CvScreen #profile { height: auto; padding: 1 2; }
    CvScreen #note { height: auto; padding: 0 2; }
    """

    def __init__(self, service: Service, config_path: Path | None = None) -> None:
        """Build the screen over an open service.

        Args:
            service: The service that reads and stores the CV; it must have been
                opened on the thread that runs the app.
            config_path: The config file the service reads, used to see whether
                `[models].cv_profile` is set before any model code loads. When
                `None`, the profile is attempted and a missing model shows the
                same note.
        """
        super().__init__()
        self._service = service
        self._config_path = config_path

    @override
    def compose(self) -> ComposeResult:
        """Lay out the stored CV, the path input with its button, and the error."""
        yield Static(_describe(self._service.cv()), id="current", markup=False)
        with Horizontal(classes="entry"):
            yield Input(placeholder="path to a PDF", id="path")
            yield Button("Store", id="store")
        yield Static("", id="error", markup=False)
        yield Static(
            _profile_text(self._service.cv_profile()), id="profile", markup=False
        )
        yield Button("Refresh", id="refresh")
        yield Static("", id="note", markup=False)
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
        self._build_profile(refresh=False)

    @on(Button.Pressed, "#refresh")
    def _refresh(self) -> None:
        """Read the CV again and show the new profile."""
        self._build_profile(refresh=True)

    def _build_profile(self, *, refresh: bool) -> None:
        """Start the profile build, or show the note when no model is set."""
        if self._config_path is not None:
            try:
                configured = (
                    Task.CV_PROFILE in read_llm_settings(self._config_path).models
                )
            except InvalidValueError as error:
                self._show_note(str(error))
                return
            if not configured:
                self._show_note(_NO_MODEL_NOTE)
                return
        self._read_cv(refresh=refresh)

    def _show_note(self, text: str) -> None:
        """Show a line under the profile."""
        self.query_one("#note", Static).update(text)

    @work(exclusive=True, group="profile")
    async def _read_cv(self, *, refresh: bool) -> None:
        """Build the profile of the stored CV and show it, or show why not.

        Runs on the app's loop because the service is bound to the thread that
        opened it. A replaced build is cancelled where it waits.
        """
        self._show_note(_BUILDING)
        try:
            profile, report = await self._service.build_profile(refresh=refresh)
        except MissingModelError:
            self._show_note(_NO_MODEL_NOTE)
            return
        except (LlmError, InvalidValueError, sqlite3.Error) as error:
            self._show_note(str(error))
            return
        self._show_note("")
        self.query_one("#profile", Static).update(_profile_text(profile))
        for warning in report.warnings:
            self.notify(warning, severity="warning", markup=False)
