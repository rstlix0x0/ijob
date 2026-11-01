"""The screen that lists the job sources and adds or removes them."""

from typing import ClassVar, override

from ijob_core.errors import InvalidValueError
from ijob_core.model import SourceRef, parse_source_ref
from ijob_sources.errors import SourcesError
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Input, Static

from ijob_app.render import plain_cell
from ijob_app.service import (
    Service,
    SourceCompanyError,
    UnknownSourceError,
)


def _read_company(text: str) -> str:
    """Trim a company name and refuse a blank one.

    Raises:
        InvalidValueError: Nothing is left after trimming.
    """
    company = text.strip()
    if not company:
        raise InvalidValueError("company", "must not be blank")
    return company


def _error_text(error: InvalidValueError | SourcesError) -> str:
    """Say why a source was refused: the bare reason for the service's own refusals."""
    if isinstance(error, UnknownSourceError | SourceCompanyError):
        return error.reason
    return str(error)


class SourcesScreen(Screen[None]):
    """List the sources; add one by reference or by URL, or remove one.

    An error from an action shows under the inputs. Escape returns to the screen
    below.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "back", "back"),
    ]
    DEFAULT_CSS = """
    SourcesScreen .entry { height: auto; }
    SourcesScreen .entry Input { width: 1fr; }
    SourcesScreen #sources { height: 1fr; }
    SourcesScreen #error { color: $error; height: 1; }
    """

    def __init__(self, service: Service) -> None:
        """Build the screen over an open service.

        Args:
            service: The service that lists, adds and removes the sources; it
                must have been opened on the thread that runs the app.
        """
        super().__init__()
        self._service = service

    @override
    def compose(self) -> ComposeResult:
        """Lay out the table, the two entry rows, the remove button and the error."""
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
        """Add the source named in the first row."""
        self._add(
            self.query_one("#ref", Input).value,
            self.query_one("#company", Input).value,
        )

    @on(Button.Pressed, "#add-url")
    @on(Input.Submitted, "#url-company")
    def _add_url_pressed(self) -> None:
        """Add the source the URL in the second row leads to."""
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
        self._show_error("")
        self._reload()

    @work(exclusive=True, group="sources")
    async def _add_url(self, url: str, company_text: str) -> None:
        """Work out what a URL is, verify it and add it."""
        try:
            company = _read_company(company_text)
            await self._service.add_url(url.strip(), company)
        except (InvalidValueError, SourcesError) as error:
            self._show_error(_error_text(error))
            return
        self._show_error("")
        self._reload()

    def _show_error(self, text: str) -> None:
        """Show a message under the inputs; empty clears it."""
        self.query_one("#error", Static).update(text)

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
