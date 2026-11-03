"""The CV and Rules tabs: your CV's profile beside what you asked for, in your words.

Reading a CV and reading instructions both call a model, so they run in the
background, and the jobs keep working meanwhile. Filters apply as soon as the
instructions are saved. Rules run only when asked for, from the Jobs tab.
"""

import json
import sqlite3
from pathlib import Path
from typing import ClassVar, Final, Protocol, override

from ijob_ai.errors import LlmError, MissingModelError
from ijob_core.errors import InvalidValueError
from rich.console import Group
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Input, Static, TextArea

from ijob_app.config import FilterLayers
from ijob_app.job_search import preload_models
from ijob_app.llm_ops import Proposal
from ijob_app.llm_text import filter_items, shadow_line
from ijob_app.service import InvalidCvError, Service
from ijob_app.view import (
    AMBER,
    CORAL,
    GROUND,
    MINT,
    MUTED,
    PROMPT_MARK,
    TEXT,
    clean,
    instructions_body,
    profile_body,
)

_NO_MODEL: Final = "Set [models].cv_profile in config.toml to read your CV."


class ProfileHost(Protocol):
    """What the CV and Rules tabs ask of the app that holds them."""

    @property
    def service(self) -> Service:
        """The open service."""
        ...

    @property
    def layers(self) -> FilterLayers:
        """The filters the config file and the filter screen set."""
        ...

    def relist(self) -> None:
        """Read the jobs again, since the filters in force changed."""
        ...


class ProfilePane(Horizontal):
    """Your CV's profile on the left, what you asked for on the right."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("r", "reread", "read CV again"),
    ]
    DEFAULT_CSS = f"""
    ProfilePane #cv-box {{ width: 1fr; height: 100%; }}
    ProfilePane #rules-box {{ width: 1fr; height: 100%; }}
    ProfilePane #cv-path-row {{ height: 1; margin-bottom: 1; }}
    ProfilePane #cv-mark {{ width: 2; color: {AMBER}; text-style: bold; }}
    ProfilePane #cv-path {{ width: 1fr; }}
    ProfilePane #cv-name {{ height: auto; margin-bottom: 1; }}
    ProfilePane #cv-note {{ height: auto; margin-bottom: 1; color: {MUTED}; }}
    ProfilePane #rules-note {{ height: auto; margin-bottom: 1; color: {MUTED}; }}
    ProfilePane #cv-profile, ProfilePane #rules-proposal {{ height: auto; }}
    ProfilePane #rules-text {{
        height: 5; margin-bottom: 1; border: none; background: {GROUND}; padding: 0;
    }}
    ProfilePane #rules-text:focus {{ border: none; }}
    ProfilePane #rules-buttons {{ height: 1; margin-bottom: 1; }}
    """

    def __init__(self, host: ProfileHost, *, id: str) -> None:  # noqa: A002  # Textual's name
        """Build the tab over the app that holds it.

        Args:
            host: The app, which holds the service and the filters.
            id: The widget's id.
        """
        super().__init__(id=id)
        self._host = host
        self._proposal: Proposal | None = None

    @override
    def compose(self) -> ComposeResult:
        """Lay out the two boxes side by side."""
        stored = self._host.service.instructions()
        with VerticalScroll(id="cv-box", classes="box") as cv_box:
            cv_box.border_title = "Your CV"
            yield Static(id="cv-name", markup=False)
            with Horizontal(id="cv-path-row"):
                yield Static(PROMPT_MARK, id="cv-mark", markup=False)
                yield Input(placeholder="path to a PDF, then enter", id="cv-path")
            yield Static(id="cv-note", markup=False)
            yield Static(id="cv-profile", markup=False)
        with VerticalScroll(id="rules-box", classes="box") as rules_box:
            rules_box.border_title = "What you want"
            yield Static("Write it in your own words.", id="rules-note", markup=False)
            yield TextArea(
                "" if stored is None else stored.text,
                id="rules-text",
                tab_behavior="focus",
            )
            with Horizontal(id="rules-buttons"):
                yield Button("Read it", id="rules-read")
                yield Button("Save", id="rules-save")
                yield Button("Clear", id="rules-clear")
            yield Static(id="rules-proposal", markup=False)

    def on_mount(self) -> None:
        """Show what is stored."""
        self._draw_cv()
        self._draw_stored_rules()
        self.query_one("#rules-save", Button).display = False

    def _draw_cv(self) -> None:
        """Show the stored CV and its profile."""
        service = self._host.service
        cv = service.cv()
        name = self.query_one("#cv-name", Static)
        if cv is None:
            name.update(Text("No CV stored yet.", style=MUTED))
        else:
            line = Text(clean(cv.name), style=f"bold {TEXT}")
            line.append("   ✓ stored", style=MINT)
            line.append(f"  {cv.size:,} bytes", style=MUTED)
            name.update(line)
        profile = service.cv_profile()
        widget = self.query_one("#cv-profile", Static)
        if profile is None:
            widget.update(Text("No profile yet.", style=MUTED))
        else:
            widget.update(profile_body(profile))

    def _draw_stored_rules(self) -> None:
        """Show what the stored instructions became."""
        stored = self._host.service.instructions()
        widget = self.query_one("#rules-proposal", Static)
        if stored is None:
            widget.update(Text("Nothing saved yet.", style=MUTED))
            return
        filters = json.loads(stored.filters_json)
        widget.update(instructions_body(filter_items(filters), stored.rules))

    @on(Input.Submitted, "#cv-path")
    def _store_cv(self) -> None:
        """Store the CV the path names, then read it in the background."""
        path = Path(self.query_one("#cv-path", Input).value.strip())
        note = self.query_one("#cv-note", Static)
        try:
            self._host.service.store_cv(path)
        except InvalidCvError as error:
            note.update(Text(error.reason, style=CORAL))
            return
        except OSError as error:
            note.update(Text(str(error), style=CORAL))
            return
        self._draw_cv()
        self._read_cv(refresh=False)

    def action_reread(self) -> None:
        """Read the CV again."""
        self._read_cv(refresh=True)

    @work(exclusive=True, group="cv-profile")
    async def _read_cv(self, *, refresh: bool) -> None:
        """Build the profile of the stored CV, or say why not."""
        note = self.query_one("#cv-note", Static)
        note.update(Text("Reading your CV. Everything else still works.", style=AMBER))
        try:
            await preload_models()
            _, report = await self._host.service.build_profile(refresh=refresh)
        except MissingModelError:
            note.update(_NO_MODEL)
            return
        except (LlmError, InvalidValueError, sqlite3.Error) as error:
            note.update(Text(str(error), style=CORAL))
            return
        note.update(Text(f"✓ Read, ${report.spent:.4f}", style=MINT))
        self._draw_cv()

    @on(Button.Pressed, "#rules-read")
    def _read_rules(self) -> None:
        """Have a model read the text, in the background."""
        self._propose(self.query_one("#rules-text", TextArea).text)

    @work(exclusive=True, group="rules")
    async def _propose(self, text: str) -> None:
        """Show what the text would set, ready to save, or say why not."""
        note = self.query_one("#rules-note", Static)
        note.update(Text("Reading it. Everything else still works.", style=AMBER))
        try:
            await preload_models()
            proposal = await self._host.service.propose_instructions(
                self._host.layers, text
            )
        except (InvalidValueError, LlmError, sqlite3.Error) as error:
            note.update(Text(str(error), style=CORAL))
            return
        self._proposal = proposal
        note.update(Text("Read. Save it to use it.", style=MINT))
        warnings = [
            Text(f"! {clean(shadow_line(item))}", style=AMBER)
            for item in proposal.shadowed
        ]
        self.query_one("#rules-proposal", Static).update(
            Group(
                instructions_body(
                    filter_items(proposal.instruction_filters), proposal.rules
                ),
                *warnings,
            )
        )
        save = self.query_one("#rules-save", Button)
        save.display = True
        save.focus()

    @on(Button.Pressed, "#rules-save")
    def _save_rules(self) -> None:
        """Store what the model read, and apply its filters."""
        if self._proposal is None:
            return
        note = self.query_one("#rules-note", Static)
        try:
            self._host.service.accept_instructions(self._proposal)
        except (InvalidValueError, LlmError, sqlite3.Error) as error:
            note.update(Text(str(error), style=CORAL))
            return
        self._proposal = None
        self.query_one("#rules-save", Button).display = False
        note.update(Text("✓ Saved. Filters apply now.", style=MINT))
        self._draw_stored_rules()
        self._host.relist()

    @on(Button.Pressed, "#rules-clear")
    def _clear_rules(self) -> None:
        """Remove the stored instructions."""
        self._host.service.clear_instructions()
        self._proposal = None
        self.query_one("#rules-save", Button).display = False
        self.query_one("#rules-text", TextArea).text = ""
        note = self.query_one("#rules-note", Static)
        note.update(Text("Cleared.", style=MUTED))
        self._draw_stored_rules()
        self._host.relist()
