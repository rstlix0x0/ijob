"""The Sources tab: every source and how its last fetch went, with a fix for problems.

Sources that need a look sort first. Adding a source, finding one with the
agent and any model analysis run in the background, and everything else keeps
working meanwhile.
"""

import asyncio
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar, Final, Protocol, override

from ijob_ai.errors import CapReachedError, LlmError
from ijob_core.errors import InvalidValueError
from ijob_core.model import SourceRef, parse_source_ref
from ijob_core.store import SourceRow
from ijob_sources.errors import FetchedPage, SourcesError, UnrecognizedPageError
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Input, Static

from ijob_app.agent_ops import SourceResult
from ijob_app.agent_view import shown_steps, step_what
from ijob_app.cli_search import failure_reason
from ijob_app.job_search import JobList
from ijob_app.llm_ops import OperationReport, cost_line
from ijob_app.render import error_notes, error_text
from ijob_app.service import Service
from ijob_app.tui.found import SourceFoundScreen
from ijob_app.tui.recipe import RecipeProposalScreen
from ijob_app.view import (
    AMBER,
    CORAL,
    MINT,
    MUTED,
    PROMPT_MARK,
    TEXT,
    ago_text,
    clean,
)

if TYPE_CHECKING:
    from ijob_ai.agents.react import AgentStep

_BUSY: Final = "An add or find is still running."
_NEEDS_COMPANY: Final = "Give a company, with a URL or kind:key if you have one."
_CAP_STOP: Final = "Stopped at the cost cap."


class SourcesHost(Protocol):
    """What the Sources tab asks of the app that holds it."""

    @property
    def service(self) -> Service:
        """The open service."""
        ...

    @property
    def jobs(self) -> JobList:
        """What the search has found so far."""
        ...

    def refetch(self, refs: list[SourceRef]) -> None:
        """Fetch these sources again, or every source that is due."""
        ...

    def now(self) -> datetime:
        """Read the wall clock."""
        ...


def _error_text(error: Exception) -> str:
    """Say why an action failed, with the notes the error carries.

    The text can hold words from a provider or a web page, so each line is cut
    to one line with control characters dropped; the lines stay apart.
    """
    text = error_text(error) if isinstance(error, InvalidValueError) else str(error)
    return "\n".join(clean(line) for line in [text, *error_notes(error)])


class SourcesPane(Vertical):
    """Every source, how it stands, and how to add or fix one."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("a", "add", "add"),
        Binding("t", "retry", "try again"),
        Binding("x", "remove", "remove"),
    ]
    DEFAULT_CSS = f"""
    SourcesPane #add-box {{ height: 3; }}
    SourcesPane #add-row {{ height: 1; }}
    SourcesPane #add-mark {{ width: 2; color: {AMBER}; text-style: bold; }}
    SourcesPane #add-url {{ width: 3fr; }}
    SourcesPane #add-company {{ width: 1fr; }}
    SourcesPane #source-body {{ height: 1fr; }}
    SourcesPane #source-list {{ width: 63%; height: 100%; }}
    SourcesPane #sources-table {{ height: 1fr; }}
    SourcesPane #source-details-box {{ width: 1fr; height: 100%; }}
    SourcesPane #sources-status {{ height: 1; color: {MUTED}; }}
    """

    def __init__(self, host: SourcesHost, *, id: str) -> None:  # noqa: A002  # Textual's name
        """Build the tab over the app that holds it.

        Args:
            host: The app, which holds the service and the jobs.
            id: The widget's id.
        """
        super().__init__(id=id)
        self._host = host
        self._adding = False
        self._shown: list[tuple[str, str]] = []
        self._selected: str | None = None

    @override
    def compose(self) -> ComposeResult:
        """Lay out the add box, the list and the details."""
        with Vertical(id="add-box", classes="box") as box:
            box.border_title = "Add a source"
            with Horizontal(id="add-row"):
                yield Static(PROMPT_MARK, id="add-mark", markup=False)
                yield Input(
                    placeholder="URL, kind:key, or blank to find it", id="add-url"
                )
                yield Input(placeholder="company", id="add-company")
        with Horizontal(id="source-body"):
            with Vertical(id="source-list", classes="box") as listing:
                listing.border_title = "Sources"
                yield DataTable(id="sources-table", cursor_type="row")
            with VerticalScroll(id="source-details-box", classes="box") as details:
                details.border_title = "Details"
                yield Static(id="source-details", markup=False)
        yield Static(id="sources-status", markup=False)

    def on_mount(self) -> None:
        """Set the columns and fill the list."""
        table = self.query_one("#sources-table", DataTable)
        table.add_column("SOURCE", width=30, key="ref")
        table.add_column("JOBS", width=6, key="jobs")
        table.add_column("STATE", width=11, key="state")
        table.add_column("LAST", width=9, key="last")
        self.redraw()

    def _states(self) -> dict[str, tuple[str, str, str]]:
        """Work out each source's state word, its color and its reason."""
        states: dict[str, tuple[str, str, str]] = {}
        for event in self._host.jobs.events:
            if event.failure is None:
                states[str(event.ref)] = ("✓ ok", MINT, "")
            else:
                reason = failure_reason(event.failure.error)
                states[str(event.ref)] = ("✗ failed", CORAL, reason)
        return states

    def redraw(self) -> None:
        """Bring the list up to date, problems first, keeping the cursor."""
        states = self._states()
        rows = self._host.service.sources()
        counts = self._host.service.source_job_counts()
        running = self._host.jobs.fetch is not None and (
            self._host.jobs.fetch_report is None
        )

        def word(row: SourceRow) -> tuple[str, str, str]:
            known = states.get(str(row.ref))
            if known is not None:
                return known
            if running:
                return ("● fetching", AMBER, "")
            if row.last_fetched_at is None:
                return ("never", MUTED, "")
            return ("✓ ok", MINT, "")

        order = {CORAL: 0, AMBER: 1}
        problem_first = sorted(
            rows, key=lambda row: (order.get(word(row)[1], 2), str(row.ref))
        )
        shown = [(str(row.ref), word(row)[0]) for row in problem_first]
        if shown != self._shown:
            self._shown = shown
            self._fill(problem_first, word, counts)
        self._draw_details(states)
        healthy = sum(1 for _, state in shown if state.startswith("✓"))
        problems = sum(1 for _, state in shown if state.startswith("✗"))
        self.query_one(
            "#source-list"
        ).border_title = f"{len(shown)} sources, {healthy} ok, {problems} failed"

    def _fill(
        self,
        rows: list[SourceRow],
        word: Callable[[SourceRow], tuple[str, str, str]],
        counts: dict[str, int],
    ) -> None:
        """Rebuild the table."""
        table = self.query_one("#sources-table", DataTable)
        table.clear()
        now = self._host.now()
        for row in rows:
            state, color, _ = word(row)
            last = "-"
            if row.last_fetched_at is not None:
                last = ago_text(row.last_fetched_at, now)
            table.add_row(
                Text(clean(str(row.ref)), style=TEXT, no_wrap=True),
                Text(f"{counts.get(str(row.ref), 0):,}", justify="right"),
                Text(state, style=color),
                Text(last, style=MUTED),
                key=str(row.ref),
            )
        if self._selected is not None:
            for number, row in enumerate(rows):
                if str(row.ref) == self._selected:
                    table.move_cursor(row=number, animate=False, scroll=False)
                    break

    def _draw_details(self, states: dict[str, tuple[str, str, str]]) -> None:
        """Draw the selected source and how to fix it."""
        widget = self.query_one("#source-details", Static)
        if self._selected is None:
            widget.update(Text("Select a source to see how it stands.", style=MUTED))
            return
        state = states.get(self._selected)
        lines = Text(clean(self._selected), style=f"bold {TEXT}")
        lines.append("\n\n")
        if state is None or state[1] != CORAL:
            lines.append("Nothing to fix.", style=MUTED)
        else:
            lines.append("✗ Failed\n", style=f"bold {CORAL}")
            lines.append(f"{clean(state[2])}\n\n", style=TEXT)
            lines.append("FIX IT\n", style=f"bold {MUTED}")
            lines.append(" t ", style=f"bold on {AMBER} #0e1117")
            lines.append(" try again now\n", style=TEXT)
            lines.append(" x ", style=f"bold on {AMBER} #0e1117")
            lines.append(" remove this source\n\n", style=TEXT)
            lines.append(
                "Other sources keep fetching. Finished ones already show on the "
                "Jobs tab.",
                style=MUTED,
            )
        widget.update(lines)

    @on(DataTable.RowHighlighted, "#sources-table")
    def _highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Show the source under the cursor."""
        self._selected = event.row_key.value
        self._draw_details(self._states())

    def action_add(self) -> None:
        """Move to the add box."""
        self.query_one("#add-url", Input).focus()

    def action_retry(self) -> None:
        """Fetch the selected source again."""
        if self._selected is not None:
            self._host.refetch([parse_source_ref(self._selected)])
            self.notify(f"Fetching {self._selected}", markup=False)

    def action_remove(self) -> None:
        """Remove the selected source."""
        if self._selected is None:
            return
        try:
            self._host.service.remove_source(parse_source_ref(self._selected))
        except InvalidValueError as error:
            self.notify(_error_text(error), severity="error", markup=False)
            return
        self.notify(f"Removed {self._selected}", markup=False)
        self._selected = None
        self.redraw()

    @on(Input.Submitted, "#add-url, #add-company")
    def _submitted(self) -> None:
        """Add the source in the add box, or find it when the URL is blank.

        Both run in the background and share one guard, so only one runs at a time.
        """
        target = self.query_one("#add-url", Input).value.strip()
        company = self.query_one("#add-company", Input).value.strip()
        if not company:
            self._status(_NEEDS_COMPANY)
            return
        if self._adding:
            self._status(_BUSY)
            return
        self._adding = True
        if not target:
            self.show_progress(company, 0)
            self._find(company)
            return
        self._status(f"Checking {target} in the background.")
        self._add(target, company)

    def _status(self, text: str) -> None:
        """Say how the last action went."""
        self.query_one("#sources-status", Static).update(text)

    def show_progress(
        self, company: str, steps: int, spent: float | None = None
    ) -> None:
        """Say that a find is running, how many steps it has taken and its cost.

        Does nothing when the status line is not on screen, so a callback that
        outlives the tab is harmless.

        Args:
            company: The company being found.
            steps: How many tool calls the agent has finished.
            spent: The money the run has spent so far, in US dollars, or `None`
                before the run has reported any, which leaves the money off.
        """
        name = clean(company)
        text = f"Finding {name} in the background."
        if steps:
            text = f"Finding {name} in the background · step {steps}"
            if spent is not None:
                text += f" · ${spent:.3f}"
        for line in self.query("#sources-status").results(Static):
            line.update(text)

    @work(exclusive=True, group="add-source")
    async def _add(self, target: str, company: str) -> None:
        """Check a source and add it, learning a recipe for a page it cannot read."""
        service = self._host.service
        try:
            if target.startswith(("http://", "https://")):
                try:
                    await service.add_url(target, company)
                except UnrecognizedPageError as unrecognized:
                    await self._learn(target, company, unrecognized.page)
                    return
            else:
                await service.add_source(parse_source_ref(target), company)
        except (InvalidValueError, SourcesError, LlmError, sqlite3.Error) as error:
            self._status("")
            self.notify(_error_text(error), severity="error", markup=False)
            return
        finally:
            self._adding = False
        self._status(f"Added {target}.")
        self.query_one("#add-url", Input).value = ""
        self.query_one("#add-company", Input).value = ""
        self.redraw()

    async def _learn(self, url: str, company: str, page: FetchedPage | None) -> None:
        """Have a model learn a page, and store it if the user accepts."""
        service = self._host.service
        self._status(f"Having a model read {url}.")
        proposal = await service.analyze_url(url, company, page=page)
        self._status(cost_line(proposal.report))
        if await self.app.push_screen_wait(RecipeProposalScreen(proposal)):
            row = service.accept_recipe(proposal)
            self._status(f"Added {row.ref}.")
            self.redraw()
        else:
            self._status("Not stored.")

    @work(exclusive=True, group="add-source")
    async def _find(self, company: str) -> None:
        """Have the agent find a company's source, and add it if the user accepts."""
        service = self._host.service
        here = asyncio.get_running_loop()
        taken = 0
        spent: float | None = None

        def on_cost(money: float) -> None:
            nonlocal spent
            spent = money

        def on_step(_step: "AgentStep") -> None:
            nonlocal taken
            taken += 1
            here.call_soon_threadsafe(self.show_progress, company, taken, spent)

        try:
            result, report = await service.find_source(
                company, on_step=on_step, on_cost=on_cost
            )
            found = result.found
            note = self._report_end(company, result, report)
            if found is None:
                return
            if found.known and found.proposal is not None:
                self._status(f"{found.ref} is already a source. {note}".strip())
                return
            if found.proposal is not None:
                accepted = await self.app.push_screen_wait(
                    RecipeProposalScreen(found.proposal)
                )
            else:
                steps = [step_what(step) for step in shown_steps(result.steps)]
                accepted = await self.app.push_screen_wait(
                    SourceFoundScreen(found, steps, cost_line(report))
                )
            if not accepted:
                self._status(f"Not stored. {note}".strip())
                return
            row = await service.add_found(found)
        except (
            InvalidValueError,
            SourcesError,
            LlmError,
            sqlite3.Error,
            ExceptionGroup,
        ) as error:
            self._status("")
            self.notify(_error_text(error), severity="error", markup=False)
            return
        finally:
            self._adding = False
        self._status(f"Added {row.ref}. {note}".strip())
        self.query_one("#add-company", Input).value = ""
        self.redraw()

    def _report_end(
        self, company: str, result: SourceResult, report: OperationReport
    ) -> str:
        """Say how a find ended; return the text that must outlive a later status.

        A cap goes on the status line, after "No source found" when nothing was
        verified. Any other early stop goes through notify. A run that found
        nothing and stopped for nothing says so.

        Returns:
            The cap's text, or an empty string when the run did not hit the cap.
        """
        cost = cost_line(report)
        stop = result.stop
        name = clean(company)
        if isinstance(stop, CapReachedError):
            note = f"{_CAP_STOP} {cost}. Raise [llm].max_cost_usd to spend more."
            if result.found is None:
                self._status(f"No source found for {name}. {note}")
            else:
                self._status(note)
            return note
        self._status(cost)
        if stop is not None:
            self.notify(_error_text(stop), severity="error", markup=False)
        elif result.found is None:
            self._status(f"No source found for {name}. {cost}. Try its careers URL.")
        return ""
