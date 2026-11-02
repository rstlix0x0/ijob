"""The ijob main screen: filters, the stored postings, and the selected posting.

`IjobApp` shows the open postings in a table, one row each, with the user's vote
in the first column, under a row of filter inputs that take the same values as
`ijob search`, and beside a pane with the selected posting's details and
description. A key refreshes the sources (`r`), votes the posting up or down
(`u`, `d`), edits its note (`n`), clears its vote (`x`), opens the sources
screen (`s`) and the CV screen (`c`), scores the posting under the cursor against
the CV (`g`), shows or hides the postings the judge dropped (`v`), and opens the
instructions screen (`i`), whose stored instructions search again.

The table is filled by `Service.search`, so the filters the user's stored
instructions imply apply even when there are no judge rules. When rules are
stored the table gains a verdict column, rows arrive as the judge finishes them,
and a status line shows the running cost against the cap, then the summary.
Dropped postings are hidden until `v` is pressed.

The app reads and writes only through a `Service` that the caller opened on the
thread that runs the app, so the store stays on that thread. Every call that
awaits runs in an async worker on the app's loop, in a group of its own
(`detail`, `label`, `refresh`, `score`, `search`); a new `detail`, `refresh`,
`score` or `search` worker replaces one running in its group, but votes run side
by side, since each is stored. A search or score that is replaced keeps the
verdicts and scores it had stored. The detail pane shows the score and its
reasons of a posting scored with `g`, or why it has none. Nothing
here starts a thread worker. The text of a posting comes from third parties and
the text of a verdict from a model, so the table shows them as plain `Text`, the
detail pane escapes them, the status line and notices are sent without markup,
and a clicked link opens only if it is `http(s)`. A change to a filter reloads
the table once typing pauses.
"""

import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import ClassVar, override
from urllib.parse import urlsplit

from ijob_ai.errors import LlmError, NoCvError
from ijob_core.errors import InvalidValueError
from ijob_core.model import Label, Posting, PostingId, SourceRef, Vote, parse_source_ref
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.notifications import SeverityLevel
from textual.timer import Timer
from textual.validation import ValidationResult, Validator
from textual.widgets import Checkbox, DataTable, Footer, Header, Input, Markdown, Static
from textual.widgets.data_table import ColumnKey

from ijob_app.config import (
    FilterField,
    FilterFlags,
    FilterLayers,
    effective_filters,
    flags_from_spec,
    parse_country_flag,
    parse_min_salary,
    parse_remote,
    parse_since,
    read_filter_layers,
    spec_from_flags,
)
from ijob_app.llm_config import read_llm_settings
from ijob_app.llm_ops import (
    STATUS_TEXT,
    JudgeStatus,
    OperationReport,
    ScoreItem,
    ScoreStatus,
    SearchItem,
)
from ijob_app.llm_text import STOP_TEXT, score_summary, search_summary, verdict_cell
from ijob_app.render import plain_cell, posting_fields
from ijob_app.service import (
    PostingDetail,
    RefreshReport,
    Service,
    UnknownSourceError,
)
from ijob_app.tui.cv import CvScreen
from ijob_app.tui.instructions import InstructionsScreen
from ijob_app.tui.note import NoteScreen
from ijob_app.tui.sources import SourcesScreen

_VOTE_MARKS = {Vote.UP: "👍", Vote.DOWN: "👎"}
_MARKDOWN_PUNCTUATION = re.compile(r"[!-/:-@\[-`{-~]")
_WEB_SCHEMES = frozenset({"http", "https"})
_FILTER_DEBOUNCE = 0.2
_MAIN_ACTIONS = frozenset(
    {
        "refresh",
        "vote_up",
        "vote_down",
        "note",
        "clear_label",
        "sources",
        "cv",
        "toggle_dropped",
        "score",
        "instructions",
    }
)
_BASE_COLUMNS = ("", "title", "company", "location")
_VERDICT_COLUMN = 4


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


def _verdict_cell(item: SearchItem) -> str:
    """Write a search item's verdict as its table cell.

    Args:
        item: The posting and what the judge made of it.

    Returns:
        What `ijob search` prints in its verdict column, or blank for a posting
        that was not judged.
    """
    if item.verdict is None and item.status is JudgeStatus.NOT_RUN:
        return ""
    return verdict_cell(item)


def _dropped(item: SearchItem) -> bool:
    """Tell whether the judge dropped the posting."""
    return item.verdict is not None and not item.verdict.keep


def _score_lines(item: ScoreItem) -> list[str]:
    """Write a scored posting's score and reasons as detail bullets.

    Args:
        item: The posting and what scoring made of it.

    Returns:
        `- Score: N` and one nested bullet per reason, or `- Score:` and the
        status text when the posting carries no score. Reasons are escaped.
    """
    if item.score is None:
        text = STATUS_TEXT[ScoreStatus].get(item.status, item.status.value)
        return [f"- Score: {_escape(text)}"]
    return [
        f"- Score: {item.score.score}",
        *(f"  - {_escape(reason)}" for reason in item.score.reasons),
    ]


def _changed_fields(
    current: FilterFlags, prefill: FilterFlags
) -> frozenset[FilterField]:
    """Name the filters whose input differs from the value it started with."""
    changed: set[FilterField] = set()
    if sorted(current.countries) != sorted(prefill.countries):
        changed.add("countries")
    if sorted(current.remotes) != sorted(prefill.remotes):
        changed.add("remote")
    if current.since != prefill.since:
        changed.add("since")
    if current.companies != prefill.companies:
        changed.add("companies")
    if current.min_salary != prefill.min_salary:
        changed.add("min_salary")
    if bool(current.require_salary) != bool(prefill.require_salary):
        changed.add("require_salary")
    return frozenset(changed)


def _detail_markdown(detail: PostingDetail, notes: Sequence[str] = ()) -> str:
    """Lay a posting's detail out as Markdown, as `ijob show` prints its fields.

    Args:
        detail: The posting with its sources, label and description.
        notes: Bullet lines already escaped, such as the judge's verdict and the
            score, shown after the label.

    Returns:
        The title, company and one bullet per other field, the vote and note
        when labelled, the given notes, then the description or
        `description unavailable:` and the reason. Every value is escaped, so no
        posting or model text becomes markup.
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
    lines.extend(notes)
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
    #status { height: 1; padding: 0 1; }
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
        Binding("g", "score", "score"),
        Binding("i", "instructions", "instructions"),
        Binding("v", "toggle_dropped", "dropped"),
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
                apply. The file in force, `config` or else `default_config`, is
                also the one the status line's spending cap and the CV screen's
                model check read, so it should be the file the service reads.

        Raises:
            InvalidValueError: The config file cannot be used.
        """
        super().__init__()
        self._service = service
        self._llm_config = config if config is not None else default_config
        self._layers = read_filter_layers(config, FilterFlags(), default=default_config)
        self._base_spec, _ = effective_filters(self._layers, None)
        self._prefill = flags_from_spec(self._base_spec)
        self._postings: dict[PostingId, Posting] = {}
        self._items: dict[PostingId, SearchItem] = {}
        self._scores: dict[PostingId, ScoreItem] = {}
        self._selected: PostingId | None = None
        self._restore: PostingId | None = None
        self._refs: list[SourceRef] = []
        self._label_column: ColumnKey | None = None
        self._verdict_column: ColumnKey | None = None
        self._show_dropped = False
        self._reload_timer: Timer | None = None

    @override
    def compose(self) -> ComposeResult:
        """Lay out the filter row, the results, the detail pane and the footer."""
        flags = self._prefill
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
        yield Static(id="status", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        """Fill the table from the stored postings."""
        table = self.query_one("#results", DataTable)
        self._label_column, *_ = table.add_columns(*_BASE_COLUMNS)
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

    def action_toggle_dropped(self) -> None:
        """Show or hide the postings the judge dropped."""
        self._show_dropped = not self._show_dropped
        self._render_items()

    def action_score(self) -> None:
        """Score the posting under the cursor against the CV."""
        posting_id = self._cursor_id()
        if posting_id is not None:
            self._score(posting_id)

    def action_sources(self) -> None:
        """Open the sources screen, and reload the table when it closes."""
        self.push_screen(SourcesScreen(self._service), lambda _: self._apply_filters())

    def action_cv(self) -> None:
        """Open the CV screen."""
        self.push_screen(CvScreen(self._service, config_path=self._llm_config))

    def action_instructions(self) -> None:
        """Open the instructions screen, and search again if it stored new ones."""
        try:
            layers, _ = self._read_filters()
        except InvalidValueError as error:
            self.notify(str(error), severity="error", markup=False)
            return
        before = self._service.instructions()

        def closed(_: None) -> None:
            if self._service.instructions() != before:
                self._apply_filters()

        self.push_screen(InstructionsScreen(self._service, layers), closed)

    @override
    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Switch the main screen's actions off while another screen is open.

        Showing dropped postings is also off while there is no verdict column.
        """
        if action == "toggle_dropped" and self._verdict_column is None:
            return False
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
        await self.query_one("#detail", Markdown).update(
            _detail_markdown(detail, self._notes(posting_id))
        )

    def _notes(self, posting_id: PostingId) -> list[str]:
        """Write the bullets a posting's detail gains from the LLM operations."""
        notes: list[str] = []
        if (judge := self._judge_note(posting_id)) is not None:
            notes.append(judge)
        if (scored := self._scores.get(posting_id)) is not None:
            notes.extend(_score_lines(scored))
        return notes

    def _judge_note(self, posting_id: PostingId) -> str | None:
        """Write the verdict of a posting, with its reason, as a detail bullet.

        Returns:
            The bullet, or `None` when the table has no verdict column or the
            posting was not judged.
        """
        item = self._items.get(posting_id)
        if item is None or self._verdict_column is None:
            return None
        if item.verdict is not None:
            mark = "✔" if item.verdict.keep else "✘"
            return f"- judge: {mark} {_escape(item.verdict.reason)}"
        cell = _verdict_cell(item)
        return f"- judge: {_escape(cell)}" if cell else None

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
            await self.query_one("#detail", Markdown).update(
                _detail_markdown(detail, self._notes(posting_id))
            )

    @work(exclusive=True, group="score")
    async def _score(self, posting_id: PostingId) -> None:
        """Score one posting, show the score in its detail, then report the cost.

        A posting already scored for its current text comes back as cached, with
        no model call.
        """
        self._set_status(f"scoring… cost ? of {self._cap_text()}")
        try:
            async for item in self._service.score([posting_id]):
                self._scores[item.posting.id] = item
                if self._selected == item.posting.id:
                    self._load_detail(item.posting.id)
        except NoCvError:
            self._set_status("")
            self.notify(
                "no CV is stored; store one with `ijob cv set PATH` or press c",
                severity="error",
                markup=False,
            )
            return
        except (InvalidValueError, LlmError, sqlite3.Error) as error:
            self._set_status("")
            self.notify(str(error), severity="error", markup=False)
            return
        self._report(self._service.last_report, score_summary)

    def _set_mark(self, posting_id: PostingId, mark: str) -> None:
        """Show a posting's vote in its row, if the row is in the table."""
        table = self.query_one("#results", DataTable)
        if self._label_column is not None and str(posting_id) in table.rows:
            table.update_cell(str(posting_id), self._label_column, mark)

    @work(exclusive=True, group="refresh")
    async def _refresh(self, refs: list[SourceRef]) -> None:
        """Fetch sources, tell the user what went wrong, and reload the table.

        The scores shown are dropped, since a fetch can change a posting's text.
        """
        try:
            report = await self._service.refresh(refs)
        except UnknownSourceError:
            self.query_one("#source", Input).add_class("-invalid")
            return
        for severity, line in _report_lines(report):
            self.notify(line, severity=severity, markup=False)
        self._scores.clear()
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

    def _read_filters(self) -> tuple[FilterLayers, list[SourceRef]]:
        """Read the filter inputs.

        Only an input whose value differs from the one it started with is a
        filter the user set, so an untouched input leaves a stored instruction's
        filter in force.

        Returns:
            The filter layers with the inputs as the flags layer, and the sources
            named; empty means every source.

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
        layers = replace(
            self._layers,
            flags=spec_from_flags(self._base_spec, flags),
            flags_set=_changed_fields(flags, self._prefill),
        )
        return layers, refs

    def _apply_filters(self) -> None:
        """Search again from the filter inputs, unless one holds a bad value.

        A bad value marks its input and leaves the table as it was. A source the
        table no longer holds marks the `source` input.
        """
        if not all(widget.is_valid for widget in self.query(Input).filter(".filter")):
            return
        try:
            layers, refs = self._read_filters()
        except InvalidValueError as error:
            self.notify(str(error), severity="error", markup=False)
            return
        instructions = self._service.instructions()
        with_verdicts = instructions is not None and bool(instructions.rules)
        self._search(layers, refs, with_verdicts=with_verdicts)

    @work(exclusive=True, group="search")
    async def _search(
        self, layers: FilterLayers, refs: list[SourceRef], *, with_verdicts: bool
    ) -> None:
        """Fill the table from a search, row by row, then report what it did.

        A search the user replaces is cancelled where it waits; the verdicts it
        had stored stay, so the next search does not judge those again.
        """
        self._set_status(
            f"judging… cost ? of {self._cap_text()}" if with_verdicts else ""
        )
        started = False
        try:
            async for item in self._service.search(layers, refs):
                if not started:
                    self._start_results(refs, with_verdicts=with_verdicts)
                    started = True
                self._add_item(item)
            if not started:
                self._start_results(refs, with_verdicts=with_verdicts)
        except UnknownSourceError:
            self._set_status("")
            self.query_one("#source", Input).add_class("-invalid")
            return
        except (InvalidValueError, LlmError, sqlite3.Error) as error:
            self._set_status("")
            self.notify(str(error), severity="error", markup=False)
            return
        self._select(self._cursor_id())
        self._report(self._service.last_report, search_summary)

    def _cap_text(self) -> str:
        """Write the spending cap from the config file the service reads, or `?`."""
        if self._llm_config is None:
            return "?"
        try:
            return f"${read_llm_settings(self._llm_config).max_cost_usd:.2f}"
        except InvalidValueError:
            return "?"

    def _set_status(self, text: str) -> None:
        """Show a line in the status bar."""
        self.query_one("#status", Static).update(text)

    def _report(
        self,
        report: OperationReport | None,
        summarize: Callable[[OperationReport], str],
    ) -> None:
        """Show the summary of an operation and tell what went wrong.

        Tracing warnings, spans the trace store could not keep and a stop other
        than the cap become notifications.
        """
        if report is None:
            self._set_status("")
            return
        self._set_status(summarize(report))
        for warning in report.warnings:
            self.notify(warning, severity="warning", markup=False)
        if report.dropped_spans:
            self.notify(
                f"{report.dropped_spans} spans not stored",
                severity="warning",
                markup=False,
            )
        if report.stopped_by in ("provider", "cost_unknown"):
            self.notify(
                f"stopped: {STOP_TEXT[report.stopped_by]}",
                severity="warning",
                markup=False,
            )

    def _start_results(self, refs: list[SourceRef], *, with_verdicts: bool) -> None:
        """Empty the table for a new search, with a verdict column when judging."""
        self.query_one("#source", Input).remove_class("-invalid")
        self._refs = refs
        table = self.query_one("#results", DataTable)
        if with_verdicts != (self._verdict_column is not None):
            table.clear(columns=True)
            columns = (*_BASE_COLUMNS, "verdict") if with_verdicts else _BASE_COLUMNS
            keys = table.add_columns(*columns)
            self._label_column = keys[0]
            self._verdict_column = keys[_VERDICT_COLUMN] if with_verdicts else None
            self.refresh_bindings()
        else:
            table.clear()
        self._restore = self._selected
        self._items = {}
        self._postings = {}

    def _add_item(self, item: SearchItem) -> None:
        """Take a search item in and show its row unless it is hidden."""
        posting_id = item.posting.id
        self._postings[posting_id] = item.posting
        self._items[posting_id] = item
        if self._shown(item):
            self._add_row(item, self._service.labels([posting_id]).get(posting_id))
            if self._verdict_column is not None and posting_id == self._selected:
                self._load_detail(posting_id)

    def _shown(self, item: SearchItem) -> bool:
        """Tell whether the table lists a search item."""
        return self._show_dropped or not _dropped(item)

    def _add_row(self, item: SearchItem, label: Label | None) -> None:
        """Add the row of a search item, moving the cursor back to its old place."""
        table = self.query_one("#results", DataTable)
        posting = item.posting
        cells = [
            _mark(label),
            plain_cell(posting.title),
            plain_cell(posting.company),
            plain_cell(posting.location or "-"),
        ]
        if self._verdict_column is not None:
            cells.append(plain_cell(_verdict_cell(item)))
        table.add_row(*cells, key=str(posting.id))
        if posting.id == self._restore:
            table.move_cursor(row=table.get_row_index(str(posting.id)))

    def _render_items(self) -> None:
        """List the search items again, after the dropped ones are shown or hidden."""
        table = self.query_one("#results", DataTable)
        labels = self._service.labels(list(self._items))
        self._restore = self._selected
        table.clear()
        for item in self._items.values():
            if self._shown(item):
                self._add_row(item, labels.get(item.posting.id))
        self._select(self._cursor_id())

    def _cursor_id(self) -> PostingId | None:
        """Read the id under the cursor; `None` when the table is empty."""
        table = self.query_one("#results", DataTable)
        if table.row_count == 0:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return None if key is None else PostingId(int(key))
