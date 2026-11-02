"""The screen that lists the job sources and adds or removes them."""

import sqlite3
from typing import ClassVar, Final, override

from ijob_ai.errors import LlmError
from ijob_core.errors import InvalidValueError
from ijob_core.model import SourceRef, parse_source_ref
from ijob_sources.errors import FetchedPage, SourcesError, UnrecognizedPageError
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Input, Static

from ijob_app.llm_ops import cost_line
from ijob_app.render import error_notes, error_text, plain_cell
from ijob_app.service import Service
from ijob_app.tui.recipe import RecipeProposalScreen


def _read_company(text: str) -> str:
    """Trim a company name and refuse a blank one.

    Raises:
        InvalidValueError: Nothing is left after trimming.
    """
    company = text.strip()
    if not company:
        raise InvalidValueError("company", "must not be blank")
    return company


_BUSY_TEXT: Final = "an analysis is running; wait for it to finish"


def _error_text(
    error: InvalidValueError | SourcesError | LlmError | sqlite3.Error,
) -> str:
    """Say why an action failed, with the notes the error carries.

    The text is the bare reason for the service's own refusals, then one line
    for each note, such as what a failed analysis cost.
    """
    text = error_text(error) if isinstance(error, InvalidValueError) else str(error)
    return "\n".join([text, *error_notes(error)])


class SourcesScreen(Screen[None]):
    """List the sources; add one by reference or by URL, or remove one.

    A URL no source kind recognizes is offered to the model: it learns a recipe
    from the page that was already read, and the user accepts or cancels it in a
    modal. Progress, cost and the outcome show on the status line; an error from
    an action shows under the inputs, followed by the cost when a paid analysis
    failed. While an add runs, or the modal is open, a further add is refused so
    a paid analysis is never started twice. Escape returns to the screen below.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "back", "back"),
    ]
    DEFAULT_CSS = """
    SourcesScreen .entry { height: auto; }
    SourcesScreen .entry Input { width: 1fr; }
    SourcesScreen #sources { height: 1fr; }
    SourcesScreen #error { color: $error; width: 1fr; height: auto; }
    SourcesScreen #status { height: 1; }
    """

    def __init__(self, service: Service) -> None:
        """Build the screen over an open service.

        Args:
            service: The service that lists, adds and removes the sources; it
                must have been opened on the thread that runs the app.
        """
        super().__init__()
        self._service = service
        self._adding = False

    @override
    def compose(self) -> ComposeResult:
        """Lay out the table, the entry rows, the remove button, error and status."""
        yield DataTable(id="sources", cursor_type="row")
        with Horizontal(classes="entry"):
            yield Input(placeholder="kind:key", id="ref")
            yield Input(placeholder="company", id="company")
            yield Button("Add", id="add")
        with Horizontal(classes="entry"):
            yield Input(placeholder="url", id="url")
            yield Input(placeholder="company", id="url-company")
            yield Button("Add URL", id="add-url")
        with Horizontal(classes="entry"):
            yield Button("Remove", id="remove")
            yield Static("", id="error", markup=False)
        yield Static("", id="status", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        """Fill the table and put the cursor in it."""
        table = self.query_one("#sources", DataTable)
        table.add_columns("source", "company", "origin", "last fetched")
        self._reload()
        table.focus()

    def action_back(self) -> None:
        """Return to the screen below."""
        self.dismiss(None)

    @on(Button.Pressed, "#add")
    @on(Input.Submitted, "#company")
    def _add_pressed(self) -> None:
        """Add the source named in the first row, unless an add is running."""
        if not self._begin_add():
            return
        self._add(
            self.query_one("#ref", Input).value,
            self.query_one("#company", Input).value,
        )

    @on(Button.Pressed, "#add-url")
    @on(Input.Submitted, "#url-company")
    def _add_url_pressed(self) -> None:
        """Add the source the URL in the second row leads to, if no add is running."""
        if not self._begin_add():
            return
        self._add_url(
            self.query_one("#url", Input).value,
            self.query_one("#url-company", Input).value,
        )

    @on(Button.Pressed, "#remove")
    def _remove_pressed(self) -> None:
        """Remove the source under the cursor."""
        table = self.query_one("#sources", DataTable)
        if table.row_count == 0:
            return
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        if key is None:
            return
        try:
            self._service.remove_source(parse_source_ref(key))
        except InvalidValueError as error:
            self._show_error(_error_text(error))
            return
        self._show_error("")
        self._reload()

    def _begin_add(self) -> bool:
        """Claim the screen for an add, or say that one is running.

        Returns:
            Whether the caller may start its add; it must start one, and the
            add releases the claim when it ends.
        """
        if self._adding:
            self._show_status(_BUSY_TEXT)
            return False
        self._adding = True
        return True

    @work(exclusive=True, group="sources")
    async def _add(self, ref_text: str, company_text: str) -> None:
        """Verify a source with one fetch and add it."""
        try:
            ref: SourceRef = parse_source_ref(ref_text.strip())
            company = _read_company(company_text)
            await self._service.add_source(ref, company)
        except (InvalidValueError, SourcesError) as error:
            self._show_error(_error_text(error))
            return
        finally:
            self._adding = False
        self._show_error("")
        self._reload()

    @work(exclusive=True, group="sources")
    async def _add_url(self, url: str, company_text: str) -> None:
        """Work out what a URL is, verify it and add it.

        A page no source kind recognizes goes to `_learn_recipe` with the page
        that was read.
        """
        self._show_status("")
        try:
            company = _read_company(company_text)
            try:
                await self._service.add_url(url.strip(), company)
            except UnrecognizedPageError as unrecognized:
                await self._learn_recipe(url.strip(), company, unrecognized.page)
                return
        except (InvalidValueError, SourcesError, LlmError, sqlite3.Error) as error:
            self._show_status("")
            self._show_error(_error_text(error))
            return
        finally:
            self._adding = False
        self._show_error("")
        self._reload()

    async def _learn_recipe(
        self, url: str, company: str, page: FetchedPage | None
    ) -> None:
        """Learn a recipe for a page, show it and store it if the user accepts.

        Runs inside the `sources` worker, which `push_screen_wait` needs.
        """
        self._show_error("")
        self._show_status(f"analyzing {url}\N{HORIZONTAL ELLIPSIS}")
        proposal = await self._service.analyze_url(url, company, page=page)
        self._show_status(cost_line(proposal.report))
        for warning in proposal.report.warnings:
            self.notify(warning, severity="warning", markup=False)
        if proposal.report.dropped_spans:
            self.notify(
                f"{proposal.report.dropped_spans} spans not stored",
                severity="warning",
                markup=False,
            )
        if await self.app.push_screen_wait(RecipeProposalScreen(proposal)):
            row = self._service.accept_recipe(proposal)
            self._reload()
            self._show_status(f"added {row.ref}")
        else:
            self._show_status("not stored")

    def _show_error(self, text: str) -> None:
        """Show a message under the inputs; empty clears it."""
        self.query_one("#error", Static).update(text)

    def _show_status(self, text: str) -> None:
        """Show progress, cost or the outcome above the footer; empty clears it."""
        self.query_one("#status", Static).update(text)

    def _reload(self) -> None:
        """Fill the table with the sources that were not removed."""
        table = self.query_one("#sources", DataTable)
        table.clear()
        for row in self._service.sources():
            fetched = row.last_fetched_at.isoformat() if row.last_fetched_at else "-"
            table.add_row(
                plain_cell(str(row.ref)),
                plain_cell(row.company),
                plain_cell(str(row.origin)),
                fetched,
                key=str(row.ref),
            )
