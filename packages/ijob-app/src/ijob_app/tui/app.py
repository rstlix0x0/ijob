"""The ijob terminal UI: tabs over the saved jobs, with everything else behind them.

`IjobApp` opens at once on the saved jobs. Fetching and scoring run in the
background and land on screen as they arrive; `run_search` does the work and a
short timer draws what changed, so nothing the user does ever waits for them.

The app reads and writes only through a `Service` that the caller opened on the
thread that runs the app, so the store stays on that thread. Reads of the whole
job list and saves of a fetched source run off the event loop on connections of
their own. Text from job sources and models reaches the screen only as `Text`,
which carries no markup, and only an `http(s)` address is ever opened.
"""

import sqlite3
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Final, override

from ijob_core.errors import InvalidValueError
from ijob_core.model import PostingId, SourceRef
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import ContentSwitcher, Footer, Static

from ijob_app.config import FilterFlags, FilterLayers, read_filter_layers
from ijob_app.job_search import (
    JobList,
    JobView,
    judge_jobs,
    run_search,
    score_jobs,
)
from ijob_app.llm_config import read_llm_settings
from ijob_app.service import Service
from ijob_app.tui.filters import FilterChoice, FilterScreen, matching_sources
from ijob_app.tui.jobs import JobsPane
from ijob_app.tui.profile import ProfilePane
from ijob_app.tui.sources import SourcesPane
from ijob_app.tui.theme import APP_CSS, IJOB_THEME
from ijob_app.tui.traces import TracesPane
from ijob_app.view import AMBER, CORAL, GROUND, MINT, MUTED, SELECTED, TEXT, bar

_DRAW_EVERY_S: Final = 0.2
_DEFAULT_CONCURRENCY: Final = 4
_PROGRESS_BAR: Final = 8
_TABS: Final = (
    ("jobs", "Jobs", "1", "jobs"),
    ("sources", "Sources", "2", "sources"),
    ("cv", "CV", "3", "profile"),
    ("rules", "Rules", "4", "profile"),
    ("traces", "Traces", "5", "traces"),
)


def _utc_now() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(UTC)


def tab_bar(active: str) -> str:
    """Write the tab bar, with the tabs that show the same pane lit together.

    Args:
        active: The pane on show.

    Returns:
        Textual markup: every word in it is one of ijob's own.
    """
    parts = [f"[bold {GROUND} on {AMBER}] ijob [/]  "]
    for name, label, _, pane in _TABS:
        style = f"bold {TEXT} on {SELECTED}" if pane == active else MUTED
        parts.append(f"[@click=app.show_tab('{name}')][{style}] {label} [/][/]")
    return "".join(parts)


class IjobApp(App[None]):
    """Browse, search, score and label the saved jobs."""

    TITLE = "ijob"
    CSS = APP_CSS
    ENABLE_COMMAND_PALETTE = True
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "quit"),
        Binding("r", "fetch", "fetch"),
        Binding("1", "show_tab('jobs')", "Jobs", show=False),
        Binding("2", "show_tab('sources')", "Sources", show=False),
        Binding("3", "show_tab('cv')", "CV", show=False),
        Binding("4", "show_tab('rules')", "Rules", show=False),
        Binding("5", "show_tab('traces')", "Traces", show=False),
    ]

    def __init__(
        self,
        service: Service,
        config: Path | None,
        *,
        default_config: Path | None = None,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Build the app over an open service.

        Args:
            service: The service every read and write goes through; it must have
                been opened on the thread that runs this app.
            config: The config file the filters come from, or `None` to use
                `default_config`.
            default_config: The file read when `config` is `None`, skipped if it
                does not exist.
            now: The wall clock, returning timezone-aware times.

        Raises:
            InvalidValueError: The config file cannot be used.
        """
        super().__init__()
        self.service = service
        self.jobs = JobList()
        self.now = now
        self.filter_flags = FilterFlags()
        self._config = config
        self._default_config = default_config
        self.layers: FilterLayers = read_filter_layers(
            config, self.filter_flags, default=default_config
        )
        config_path = config if config is not None else default_config
        self.concurrency = _DEFAULT_CONCURRENCY
        if config_path is not None and config_path.exists():
            self.concurrency = read_llm_settings(config_path).concurrency
        self.source_filter = ""
        self._view: JobView = (self.layers, ())
        self._dirty = True
        self._active = "jobs"
        self._searching = False

    @override
    def compose(self) -> ComposeResult:
        """Lay out the tab bar, the panes and the keys."""
        with Horizontal(id="top"):
            yield Static(tab_bar(self._active), id="tabs")
            yield Static(id="activity", markup=False)
        with ContentSwitcher(initial="jobs", id="panes"):
            yield JobsPane(self, id="jobs")
            yield SourcesPane(self, id="sources")
            yield ProfilePane(self, id="profile")
            yield TracesPane(self, id="traces")
        yield Footer()

    def on_mount(self) -> None:
        """Start from the saved jobs, then fetch what is due behind them."""
        self.register_theme(IJOB_THEME)
        self.theme = IJOB_THEME.name
        self.set_interval(_DRAW_EVERY_S, self._draw)
        self._search(fetch=True)

    @override
    def get_system_commands(self, screen: Screen[object]) -> Iterable[SystemCommand]:
        """Offer ijob's own actions in the command palette."""
        yield SystemCommand(
            "Fetch", "Fetch the sources that are due", self.action_fetch
        )
        for name, label, _, _ in _TABS:
            yield SystemCommand(
                f"Go to {label}",
                f"Show the {label} tab",
                lambda name=name: self.action_show_tab(name),
            )
        yield from super().get_system_commands(screen)

    def changed(self) -> None:
        """Note that something on screen is out of date; the timer draws it."""
        self._dirty = True

    def _draw(self) -> None:
        """Draw whatever changed since the last tick.

        On a busy machine the timer can tick before the screen is composed; the
        change stays pending for the next tick.
        """
        if not self._dirty or not (self.query("#activity") and self.query("#panes")):
            return
        self._dirty = False
        self.query_one("#activity", Static).update(self._activity())
        panes = self.query_one("#panes", ContentSwitcher)
        if panes.current == "jobs":
            jobs = self.query_one("#jobs", JobsPane)
            jobs.redraw()
            jobs.refresh_details()
        elif panes.current == "sources":
            self.query_one("#sources", SourcesPane).redraw()

    def _activity(self) -> Text:
        """Draw what is running at the right of the tab bar."""
        jobs = self.jobs
        line = Text(no_wrap=True, justify="right")
        fetch = jobs.fetch
        if fetch is not None and fetch.done < fetch.total:
            share = 100 * fetch.done / fetch.total if fetch.total else 0
            line.append(f"{bar(share, _PROGRESS_BAR)} ", style=AMBER)
            line.append(f"fetching {fetch.done}/{fetch.total}", style=AMBER)
            if fetch.failed:
                line.append(f"  {fetch.failed} failed", style=CORAL)
        elif self._searching:
            line.append("loading", style=AMBER)
        else:
            line.append("up to date", style=MINT)
            if fetch is not None and fetch.failed:
                line.append(f"  {fetch.failed} failed", style=CORAL)
        if jobs.new_jobs:
            line.append(f"  +{jobs.new_jobs:,} new", style=MINT)
        return line

    def action_show_tab(self, name: str) -> None:
        """Show a tab.

        Args:
            name: The tab: `jobs`, `sources`, `cv`, `rules` or `traces`.
        """
        pane = next(entry[3] for entry in _TABS if entry[0] == name)
        self._active = pane
        self.query_one("#tabs", Static).update(tab_bar(pane))
        self.query_one("#panes", ContentSwitcher).current = pane
        focus = {
            "jobs": "#jobs-table",
            "sources": "#sources-table",
            "cv": "#cv-path",
            "rules": "#rules-text",
            "traces": "#traces-table",
        }[name]
        self.query_one(focus).focus()
        if pane == "traces":
            self.query_one("#traces", TracesPane).redraw()
        self.changed()

    def action_fetch(self) -> None:
        """Fetch the sources that are due, behind whatever is on screen."""
        if self._searching:
            self.notify("Already fetching.", markup=False)
            return
        self._search(fetch=True)

    def refetch(self, refs: list[SourceRef]) -> None:
        """Fetch chosen sources, behind whatever is on screen.

        Args:
            refs: The sources to fetch.
        """
        self.jobs.events = []
        self._search(fetch=True, refs=refs)

    def relist(self) -> None:
        """Read the jobs again at once, since the filters in force changed."""
        self.query_one("#jobs", JobsPane).show_filters()
        self._view = (self.layers, self._view[1])
        self._relist()

    @work(exclusive=True, group="relist")
    async def _relist(self) -> None:
        """Show the jobs the filters in force leave, even while a fetch runs."""
        try:
            postings = await self.service.list_jobs(*self._view)
        except (InvalidValueError, sqlite3.Error) as error:
            self.notify(str(error), severity="error", markup=False)
            return
        self.jobs.set_jobs(postings, mark_new=False)
        self.jobs.set_saved_scores(self.service.saved_scores())
        self.changed()

    @work(exclusive=True, group="search")
    async def _search(
        self, *, fetch: bool, refs: list[SourceRef] | None = None
    ) -> None:
        """Run a search, landing each result on screen as it arrives."""
        self._searching = True
        self.changed()
        try:
            await run_search(
                self.service,
                self.layers,
                refs or [],
                self.jobs,
                self.changed,
                fetch=fetch,
                latest_view=lambda: self._view,
            )
        except (InvalidValueError, sqlite3.Error, ExceptionGroup) as error:
            self.notify(str(error), severity="error", markup=False)
        finally:
            self._searching = False
            self.changed()

    @on(JobsPane.ScoreAsked)
    def _score_asked(self) -> None:
        """Score the jobs on the list."""
        ids = self.query_one("#jobs", JobsPane).visible_ids()
        if not ids:
            self.notify("Every job on the list has a score.", markup=False)
            return
        self._score(ids)

    @work(exclusive=True, group="score")
    async def _score(self, ids: list[PostingId]) -> None:
        """Score jobs in the background."""
        await score_jobs(self.service, self.jobs, ids, self.changed)

    @on(JobsPane.JudgeAsked)
    def _judge_asked(self) -> None:
        """Check the jobs on the list against the rules."""
        if self.service.instructions() is None:
            self.notify("Write what you want on the Rules tab first.", markup=False)
            return
        self._judge(self.query_one("#jobs", JobsPane).checkable_ids())

    @work(exclusive=True, group="judge")
    async def _judge(self, ids: list[PostingId]) -> None:
        """Check jobs against the rules in the background."""
        await judge_jobs(self.service, self.jobs, ids, self.changed)

    @on(JobsPane.FiltersAsked)
    def _filters_asked(self) -> None:
        """Ask for new filters, and apply them when given."""

        def apply(choice: FilterChoice | None) -> None:
            if choice is None:
                return
            try:
                layers = read_filter_layers(
                    self._config, choice.flags, default=self._default_config
                )
            except InvalidValueError as error:
                self.notify(str(error), severity="error", markup=False)
                return
            self.layers = layers
            self.filter_flags = choice.flags
            self.source_filter = choice.source
            refs = matching_sources(choice.source, self.service.sources())
            self._view = (layers, refs)
            self.relist()

        choice = FilterChoice(self.filter_flags, self.source_filter)
        self.push_screen(FilterScreen(choice, self.service.sources()), apply)
