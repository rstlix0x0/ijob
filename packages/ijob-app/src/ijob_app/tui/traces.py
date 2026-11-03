"""The Traces tab: recent model runs and what each one spent."""

from typing import ClassVar, Final, Protocol, override

from ijob_core.errors import InvalidValueError
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Static

from ijob_app.service import Service
from ijob_app.view import MUTED, TEXT, clean, duration_text, trace_body

_LISTED: Final = 50


class TracesHost(Protocol):
    """What the Traces tab asks of the app that holds it."""

    @property
    def service(self) -> Service:
        """The open service."""
        ...


class TracesPane(Horizontal):
    """Recent model runs on the left, the selected run's calls on the right."""

    BINDINGS: ClassVar[list[BindingType]] = []
    DEFAULT_CSS = """
    TracesPane #trace-list { width: 63%; height: 100%; }
    TracesPane #traces-table { height: 1fr; }
    TracesPane #trace-details-box { width: 1fr; height: 100%; }
    """

    def __init__(self, host: TracesHost, *, id: str) -> None:  # noqa: A002  # Textual's name
        """Build the tab over the app that holds it.

        Args:
            host: The app, which holds the service.
            id: The widget's id.
        """
        super().__init__(id=id)
        self._host = host

    @override
    def compose(self) -> ComposeResult:
        """Lay out the list and the details."""
        with Vertical(id="trace-list", classes="box") as listing:
            listing.border_title = "Recent model runs"
            yield DataTable(id="traces-table", cursor_type="row")
        with VerticalScroll(id="trace-details-box", classes="box") as details:
            details.border_title = "Calls"
            yield Static(id="trace-details", markup=False)

    def on_mount(self) -> None:
        """Set the columns."""
        table = self.query_one("#traces-table", DataTable)
        table.add_column("WHEN", width=16, key="when")
        table.add_column("OPERATION", width=22, key="operation")
        table.add_column("CALLS", width=5, key="calls")
        table.add_column("COST", width=8, key="cost")
        table.add_column("TIME", width=8, key="time")

    def redraw(self) -> None:
        """Read the recorded runs again."""
        table = self.query_one("#traces-table", DataTable)
        table.clear()
        for summary in self._host.service.traces(_LISTED):
            table.add_row(
                Text(summary.started.strftime("%Y-%m-%d %H:%M"), style=MUTED),
                Text(clean(summary.operation), style=TEXT, no_wrap=True),
                Text(str(summary.calls), justify="right"),
                Text(f"${summary.cost_usd:.4f}", justify="right"),
                Text(duration_text(summary.duration_ms), style=MUTED, justify="right"),
                key=summary.trace_id,
            )

    @on(DataTable.RowHighlighted, "#traces-table")
    def _highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Show the calls of the run under the cursor."""
        trace_id = event.row_key.value
        if trace_id is None:
            return
        widget = self.query_one("#trace-details", Static)
        try:
            tree = self._host.service.trace(trace_id)
        except InvalidValueError as error:
            widget.update(Text(str(error), style=MUTED))
            return
        widget.update(trace_body(tree.roots))
