"""The ijob main screen: filters, the stored postings, and the selected posting.

`IjobApp` shows the open postings in a table, one row each, with the user's vote
in the first column, under a row of filter inputs that take the same values as
`ijob search`, and beside a pane with the selected posting's details and
description. A key refreshes the sources (`r`), votes the posting up or down
(`u`, `d`), edits its note (`n`), clears its vote (`x`), and opens the sources
screen (`s`) and the CV screen (`c`).

The app reads and writes only through a `Service` that the caller opened on the
thread that runs the app, so the store stays on that thread. Every call that
awaits runs in an async worker on the app's loop, in a group of its own
(`detail`, `label`, `refresh`); a new `detail` or `refresh` worker replaces one
running in its group, but votes run side by side, since each is stored. Nothing
here starts a thread worker. The text of a posting comes from third parties, so
the table shows it as plain `Text`, the detail pane escapes it, notices are sent
without markup, and a clicked link opens only if it is `http(s)`. A change to a
filter reloads the table once typing pauses.
"""

import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import ClassVar, override
from urllib.parse import urlsplit

from ijob_core.errors import InvalidValueError
from ijob_core.filters import FilterSpec
from ijob_core.model import Label, Posting, PostingId, SourceRef, Vote, parse_source_ref
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.notifications import SeverityLevel
from textual.timer import Timer
from textual.validation import ValidationResult, Validator
from textual.widgets import Checkbox, DataTable, Footer, Header, Input, Markdown
from textual.widgets.data_table import ColumnKey

from ijob_app.config import (
    FilterFlags,
    flags_from_spec,
    parse_country_flag,
    parse_min_salary,
    parse_remote,
    parse_since,
    read_filters,
    spec_from_flags,
)
from ijob_app.render import plain_cell, posting_fields
from ijob_app.service import (
    PostingDetail,
    RefreshReport,
    Service,
    UnknownSourceError,
)
from ijob_app.tui.cv import CvScreen
from ijob_app.tui.note import NoteScreen
from ijob_app.tui.sources import SourcesScreen

_VOTE_MARKS = {Vote.UP: "👍", Vote.DOWN: "👎"}
_MARKDOWN_PUNCTUATION = re.compile(r"[!-/:-@\[-`{-~]")
_WEB_SCHEMES = frozenset({"http", "https"})
_FILTER_DEBOUNCE = 0.2
_MAIN_ACTIONS = frozenset(
    {"refresh", "vote_up", "vote_down", "note", "clear_label", "sources", "cv"}
)


def _mark(label: Label | None) -> str:
    """Show a vote as the mark the label column holds; blank for none."""
    return "" if label is None else _VOTE_MARKS[label.vote]


def _escape(text: str) -> str:
    """Make third-party text plain Markdown: one line, no markup.

    Every ASCII punctuation mark is backslash-escaped, which CommonMark reads as
    the literal character.
    """
    return _MARKDOWN_PUNCTUATION.sub(r"\\\g<0>", " ".join(text.split()))


def _url_text(url: str) -> str:
    """Write a URL as a Markdown autolink, or as plain text if it cannot be one."""
    if any(character in url for character in " <>") or not url.isprintable():
        return _escape(url)
    return f"<{url}>"


def _detail_markdown(detail: PostingDetail) -> str:
    """Lay a posting's detail out as Markdown, as `ijob show` prints its fields.

    Args:
        detail: The posting with its sources, label and description.

    Returns:
        The title, company and one bullet per other field, the vote and note
        when labelled, then the description or `description unavailable:` and
        the reason. Every value is escaped, so no posting text becomes markup.
    """
    posting = detail.posting
    lines = [f"# {_escape(posting.title)}", "", _escape(posting.company), ""]
    lines.extend(
        f"- {name}: {_escape(value)}" for name, value in posting_fields(detail)
    )
    lines.append(f"- url: {_url_text(posting.url)}")
    if detail.label is not None:
        lines.append(f"- label: {_VOTE_MARKS[detail.label.vote]} {detail.label.vote}")
        if detail.label.note is not None:
            lines.append(f"- note: {_escape(detail.label.note)}")
    lines.append("")
    if detail.description is not None:
        lines.append(detail.description)
    else:
        reason = detail.unavailable_reason or "no description published"
        lines.append(f"description unavailable: {_escape(reason)}")
    return "\n".join(lines)


def _report_lines(report: RefreshReport) -> list[tuple[SeverityLevel, str]]:
    """Describe what a refresh could not do, one line per source and problem.

    Args:
        report: What the refresh did.

    Returns:
        Each source that failed or was refused, with severity `warning`, then
        each source that dropped unusable jobs and each stale source, with
        severity `information`; a note when every chosen source was too recently
        fetched to fetch again.
    """
    lines: list[tuple[SeverityLevel, str]] = [
        ("warning", f"failed {failure.ref}: {failure.error}")
        for failure in report.failures
    ]
    lines.extend(
        ("warning", f"failed {ref}: {error}") for ref, error in report.refused.items()
    )
    lines.extend(
        ("information", f"{ref}: skipped {count} invalid jobs")
        for ref, count in report.rejected.items()
    )
    lines.extend(
        ("information", f"stale {row.ref}: last fetched {row.last_fetched_at:%Y-%m-%d}")
        for row in report.stale
        if row.last_fetched_at is not None
    )
    if report.kept and not report.due:
        lines.append(("information", "all chosen sources were fetched recently"))
    return lines


def _items(text: str) -> tuple[str, ...]:
    """Split comma-separated input into its trimmed, non-empty items."""
    return tuple(item.strip() for item in text.split(",") if item.strip())


def _check_each(parse: Callable[[str], object]) -> Callable[[str], None]:
    """Build a check that every comma-separated item of the input parses."""

    def check(text: str) -> None:
        for item in _items(text):
            parse(item)

    return check


def _check_single(parse: Callable[[str], object]) -> Callable[[str], None]:
    """Build a check that the input, when not blank, parses as one value."""

    def check(text: str) -> None:
        if text.strip():
            parse(text.strip())

    return check


class _Check(Validator):
    """Validate an input with a function that raises `InvalidValueError`."""

    def __init__(self, check: Callable[[str], None]) -> None:
        """Build the validator.

        Args:
            check: Raises `InvalidValueError` for a value that cannot be used.
        """
        super().__init__()
        self._check = check

    @override
    def validate(self, value: str) -> ValidationResult:
        """Run the check, failing with the error's reason."""
        try:
            self._check(value)
        except InvalidValueError as error:
            return self.failure(error.reason)
        return self.success()


def _filter_input(name: str, value: str, check: Callable[[str], None] | None) -> Input:
    """Build one filter input, titled with its name."""
    widget = Input(
        value,
        id=name,
        classes="filter",
        validators=None if check is None else _Check(check),
    )
    widget.border_title = name.replace("-", " ")
    return widget


class IjobApp(App[None]):
    """Browse the stored postings.

    The starting filters are read from the config file when the app is built,
    so an unusable config fails before the screen opens.
    """

    TITLE = "ijob"
    CSS = """
    #filters { height: auto; }
    #filters Input { width: 1fr; }
    #filters Checkbox { width: auto; }
    #body { height: 1fr; }
    #results { width: 1fr; }
    #detail-scroll { width: 1fr; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "quit"),
        Binding("r", "refresh", "refresh"),
        Binding("u", "vote_up", "👍"),
        Binding("d", "vote_down", "👎"),
        Binding("n", "note", "note"),
        Binding("x", "clear_label", "clear"),
        Binding("s", "sources", "sources"),
        Binding("c", "cv", "CV"),
        Binding("escape", "focus_results", "results", show=False),
    ]

    def __init__(
        self,
        service: Service,
        config: Path | None,
        *,
        default_config: Path | None = None,
    ) -> None:
        """Build the app over an open service.

        Args:
            service: The service every read and write goes through; it must have
                been opened on the thread that runs this app.
            config: The config file the starting filters come from, or `None`
                to use `default_config`.
            default_config: The file read when `config` is `None`, skipped if
                it does not exist; with neither given, the packaged defaults
                apply.

        Raises:
            InvalidValueError: The config file cannot be used.
        """
        super().__init__()
        self._service = service
        self._base_spec = read_filters(config, FilterFlags(), default=default_config)
        self._postings: dict[PostingId, Posting] = {}
        self._selected: PostingId | None = None
        self._refs: list[SourceRef] = []
        self._label_column: ColumnKey | None = None
        self._reload_timer: Timer | None = None

    @override
    def compose(self) -> ComposeResult:
        """Lay out the filter row, the results, the detail pane and the footer."""
        flags = flags_from_spec(self._base_spec)
        yield Header()
        with Horizontal(id="filters"):
            yield _filter_input("source", "", self._check_sources)
            yield _filter_input(
                "country",
                ", ".join(flags.countries),
                _check_each(parse_country_flag),
            )
            yield _filter_input(
                "remote", ", ".join(flags.remotes), _check_each(parse_remote)
            )
            yield _filter_input("since", flags.since or "", _check_single(parse_since))
            yield _filter_input("company", ", ".join(flags.companies), None)
            yield _filter_input(
                "min-salary",
                flags.min_salary or "",
                _check_single(parse_min_salary),
            )
            yield Checkbox(
                "salary required", bool(flags.require_salary), id="require-salary"
            )
        with Horizontal(id="body"):
            yield DataTable(id="results", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="detail-scroll"):
                yield Markdown(id="detail", open_links=False)
        yield Footer()

    def on_mount(self) -> None:
        """Fill the table from the stored postings."""
        table = self.query_one("#results", DataTable)
        self._label_column, *_ = table.add_columns("", "title", "company", "location")
        self._apply_filters()
        table.focus()

    def action_focus_results(self) -> None:
        """Move focus from a filter back to the results."""
        self.query_one("#results", DataTable).focus()

    def action_refresh(self) -> None:
        """Fetch the sources named in the `source` filter, or every source."""
        self._refresh(self._refs)

    def action_vote_up(self) -> None:
        """Vote the posting under the cursor up, keeping its note."""
        self._vote_current(Vote.UP)

    def action_vote_down(self) -> None:
        """Vote the posting under the cursor down, keeping its note."""
        self._vote_current(Vote.DOWN)

    def action_clear_label(self) -> None:
        """Remove the vote and note on the posting under the cursor."""
        posting_id = self._cursor_id()
        if posting_id is None:
            return
        try:
            self._service.clear_label(posting_id)
        except InvalidValueError as error:
            self.notify(str(error), severity="error", markup=False)
            return
        self._set_mark(posting_id, "")
        self._load_detail(posting_id)

    def action_sources(self) -> None:
        """Open the sources screen, and reload the table when it closes."""
        self.push_screen(SourcesScreen(self._service), lambda _: self._apply_filters())

    def action_cv(self) -> None:
        """Open the CV screen."""
        self.push_screen(CvScreen(self._service))

    @override
    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Switch the main screen's actions off while another screen is open."""
        return not (action in _MAIN_ACTIONS and len(self.screen_stack) > 1)

    def action_note(self) -> None:
        """Ask for a note on the posting under the cursor, if it has a vote."""
        posting_id = self._cursor_id()
        if posting_id is None:
            return
        label = self._service.labels([posting_id]).get(posting_id)
        if label is None:
            self.notify("label it first with u or d", severity="warning", markup=False)
            return

        def save(note: str | None) -> None:
            current = self._service.labels([posting_id]).get(posting_id)
            if note is not None and current is not None:
                self._vote(posting_id, current.vote, note or None)

        self.push_screen(NoteScreen(label.note), save)

    @on(Input.Changed, ".filter")
    def _input_changed(self) -> None:
        """Reload the table once typing in a filter pauses.

        Each change restarts the wait, so a burst of keystrokes reloads once.
        """
        if self._reload_timer is not None:
            self._reload_timer.stop()
        self._reload_timer = self.set_timer(_FILTER_DEBOUNCE, self._apply_filters)

    @on(Checkbox.Changed, "#require-salary")
    def _checkbox_changed(self) -> None:
        """Reload the table when the salary requirement is toggled."""
        self._apply_filters()

    @on(DataTable.RowHighlighted, "#results")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Show the posting the cursor is on."""
        event.stop()
        self._select(self._cursor_id())

    @on(Markdown.LinkClicked)
    def _link_clicked(self, event: Markdown.LinkClicked) -> None:
        """Open a clicked link in the browser if it is a web address, else ignore it.

        The detail text comes from third parties, so only `http` and `https`
        links with a host are followed.
        """
        parts = urlsplit(event.href)
        if parts.scheme.lower() in _WEB_SCHEMES and parts.netloc:
            self.open_url(event.href)

    def _select(self, posting_id: PostingId | None) -> None:
        """Make a posting the one the detail pane shows, loading it if it changed."""
        if posting_id == self._selected:
            return
        self._selected = posting_id
        pane = self.query_one("#detail", Markdown)
        if posting_id is None:
            pane.update("")
            return
        posting = self._postings[posting_id]
        if posting.description is None:
            pane.update(f"# {_escape(posting.title)}\n\n_loading description…_")
        self._load_detail(posting_id)

    @work(exclusive=True, group="detail")
    async def _load_detail(self, posting_id: PostingId) -> None:
        """Read a posting's detail, fetching its description, and show it."""
        try:
            detail = await self._service.detail(posting_id)
        except InvalidValueError as error:
            self.notify(str(error), severity="error", markup=False)
            return
        await self.query_one("#detail", Markdown).update(_detail_markdown(detail))

    def _vote_current(self, vote: Vote) -> None:
        """Vote the posting under the cursor, carrying over its current note."""
        posting_id = self._cursor_id()
        if posting_id is None:
            return
        label = self._service.labels([posting_id]).get(posting_id)
        self._vote(posting_id, vote, None if label is None else label.note)

    @work(group="label")
    async def _vote(self, posting_id: PostingId, vote: Vote, note: str | None) -> None:
        """Record a vote, then update the row and the detail pane.

        Votes are not exclusive, so a later vote never drops an earlier one's
        row update; the row shows the label as stored when this one finishes.
        """
        try:
            detail = await self._service.label(posting_id, vote, note)
        except InvalidValueError as error:
            self.notify(str(error), severity="error", markup=False)
            return
        stored = self._service.labels([posting_id]).get(posting_id)
        self._set_mark(posting_id, _mark(stored))
        if self._selected == posting_id:
            await self.query_one("#detail", Markdown).update(_detail_markdown(detail))

    def _set_mark(self, posting_id: PostingId, mark: str) -> None:
        """Show a posting's vote in its row, if the row is in the table."""
        table = self.query_one("#results", DataTable)
        if self._label_column is not None and str(posting_id) in table.rows:
            table.update_cell(str(posting_id), self._label_column, mark)

    @work(exclusive=True, group="refresh")
    async def _refresh(self, refs: list[SourceRef]) -> None:
        """Fetch sources, tell the user what went wrong, and reload the table."""
        try:
            report = await self._service.refresh(refs)
        except UnknownSourceError:
            self.query_one("#source", Input).add_class("-invalid")
            return
        for severity, line in _report_lines(report):
            self.notify(line, severity=severity, markup=False)
        self._apply_filters()

    def _check_sources(self, text: str) -> None:
        """Check that every named source is a reference to a source in the table.

        Raises:
            InvalidValueError: An item is not a reference, or names no source.
        """
        known = {row.ref for row in self._service.sources()}
        for item in _items(text):
            ref = parse_source_ref(item)
            if ref not in known:
                reason = f"unknown source {ref}"
                raise UnknownSourceError(reason)

    def _read_filters(self) -> tuple[FilterSpec, list[SourceRef]]:
        """Read the filter inputs.

        Returns:
            The filters, and the sources named; empty means every source.

        Raises:
            InvalidValueError: An input holds a value that cannot be used.
        """

        def text(name: str) -> str:
            return self.query_one(f"#{name}", Input).value

        flags = FilterFlags(
            countries=_items(text("country")),
            remotes=_items(text("remote")),
            since=text("since").strip() or None,
            companies=_items(text("company")),
            min_salary=text("min-salary").strip() or None,
            require_salary=self.query_one("#require-salary", Checkbox).value,
        )
        refs = list(dict.fromkeys(map(parse_source_ref, _items(text("source")))))
        return spec_from_flags(self._base_spec, flags), refs

    def _apply_filters(self) -> None:
        """Reload the table from the filter inputs, unless one holds a bad value.

        A bad value marks its input and leaves the table as it was. A source the
        table no longer holds marks the `source` input.
        """
        if not all(widget.is_valid for widget in self.query(Input).filter(".filter")):
            return
        try:
            spec, refs = self._read_filters()
            postings = self._service.open_postings(spec, refs)
        except UnknownSourceError:
            self.query_one("#source", Input).add_class("-invalid")
            return
        except InvalidValueError as error:
            self.notify(str(error), severity="error", markup=False)
            return
        self.query_one("#source", Input).remove_class("-invalid")
        self._refs = refs
        self._show(postings)

    def _show(self, postings: Iterable[Posting]) -> None:
        """Fill the table with postings, labels from the service."""
        rows = list(postings)
        labels = self._service.labels([posting.id for posting in rows])
        table = self.query_one("#results", DataTable)
        previous = self._selected
        table.clear()
        self._postings = {posting.id: posting for posting in rows}
        for posting in rows:
            table.add_row(
                _mark(labels.get(posting.id)),
                plain_cell(posting.title),
                plain_cell(posting.company),
                plain_cell(posting.location or "-"),
                key=str(posting.id),
            )
        if previous in self._postings:
            table.move_cursor(row=table.get_row_index(str(previous)))
        self._select(self._cursor_id())

    def _cursor_id(self) -> PostingId | None:
        """Read the id under the cursor; `None` when the table is empty."""
        table = self.query_one("#results", DataTable)
        if table.row_count == 0:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return None if key is None else PostingId(int(key))
