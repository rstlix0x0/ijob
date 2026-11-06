"""The Jobs tab: a search box, the list of jobs and the selected job's details.

Everything here reads what the app's `JobList` already holds, so it never waits.
Typing filters the jobs in memory. The table holds only what is worth scrolling,
and grows as the cursor reaches its end, so it stays quick with any number of
jobs. A description that was never fetched arrives in the details pane on its
own, while the job's other facts already show.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import ClassVar, Final, Protocol, override

from ijob_core.errors import InvalidValueError
from ijob_core.model import PostingId, Vote
from ijob_sources.errors import SourcesError
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Key, Resize
from textual.message import Message
from textual.widgets import DataTable, Input, Markdown, Static

from ijob_app.config import FilterFlags
from ijob_app.job_search import JobList, JobRow, ScoreState
from ijob_app.llm_text import filter_items
from ijob_app.service import PostingDetail, Service
from ijob_app.tui.note import NoteScreen
from ijob_app.view import (
    AMBER,
    CORAL,
    MINT,
    MUTED,
    PROMPT_MARK,
    SELECTED,
    TEXT,
    age_text,
    clean,
    clean_markdown,
    posting_head,
    posting_link,
    score_text,
    unavailable_text,
    where_text,
)

_PAGE: Final = 200
_SCORE_LIMIT: Final = 50
_SCORE_WIDTH: Final = 9
_COMPANY_WIDTH: Final = 14
_WHERE_WIDTH: Final = 12
_AGE_WIDTH: Final = 4
_CELL_PADDING: Final = 2
_MIN_TITLE: Final = 16
_LIST_CHROME: Final = 4


class JobsHost(Protocol):
    """What the Jobs tab asks of the app that holds it."""

    @property
    def service(self) -> Service:
        """The open service."""
        ...

    @property
    def jobs(self) -> JobList:
        """What the search has found so far."""
        ...

    @property
    def filter_flags(self) -> FilterFlags:
        """The filters in force."""
        ...

    @property
    def source_filter(self) -> str:
        """The words that choose the sources on show."""
        ...

    @property
    def concurrency(self) -> int:
        """How many model calls run at once."""
        ...

    def now(self) -> datetime:
        """Read the wall clock."""
        ...


def _matcher(query: str) -> re.Pattern[str] | None:
    """Build the pattern a search box means: words that start like these, in any order.

    So `go` finds `Golang` and `Google`, but not `Django`.
    """
    words = query.split()
    if not words:
        return None
    ahead = "".join(
        f"(?=.*(?<!\\w){re.escape(word)})"
        if word.isascii()
        else f"(?=.*{re.escape(word)})"
        for word in words
    )
    return re.compile(ahead, re.IGNORECASE | re.DOTALL)


def matches(row: JobRow, pattern: re.Pattern[str] | None) -> bool:
    """Tell whether a job's title or company holds what was typed."""
    if pattern is None:
        return True
    return pattern.match(f"{row.posting.title} {row.posting.company}") is not None


def chips(flags: FilterFlags, source: str = "", saved: Sequence[str] = ()) -> Text:
    """Draw the filters in force as chips.

    Args:
        flags: The filters set on the filter screen.
        source: The words that choose the sources.
        saved: The filters the user's saved words set, each as text.

    Returns:
        One chip each, so nothing filters the list unseen.
    """
    values = [
        ", ".join(flags.countries) or "any country",
        ", ".join(flags.remotes) or "any remote",
        flags.since or "any age",
    ]
    if flags.companies:
        values.append(", ".join(flags.companies))
    if flags.min_salary:
        values.append(f"pays {flags.min_salary}")
    if flags.require_salary:
        values.append("states a salary")
    if source:
        values.append(f"source {source}")
    values += [f"your rules {item}" for item in saved]
    line = Text(no_wrap=True, overflow="ellipsis")
    for value in values:
        line.append(f" {clean(value)} ", style=f"{MUTED} on {SELECTED}")
        line.append(" ")
    return line


def _score_cell(row: JobRow, *, scoring: bool) -> Text:
    """Draw a job's score, or where it stands while it has none."""
    if row.score is not None:
        return score_text(row.score)
    if row.state is ScoreState.QUEUED:
        word, color = ("scoring", AMBER) if scoring else ("queued", MUTED)
        return Text(f" {word}", style=color)
    if row.state is ScoreState.MISSED:
        return Text(" missed", style=CORAL)
    return Text("  -", style=MUTED)


@dataclass(frozen=True, slots=True)
class _Shown:
    """What one table row was last drawn from, to tell when it must change."""

    posting_id: PostingId
    score: int | None
    state: ScoreState
    keep: bool | None
    is_new: bool


class JobsPane(Vertical):
    """The search box, the list of jobs and the selected job's details."""

    class ScoreAsked(Message):
        """The user asked to score the jobs on the list."""

    class JudgeAsked(Message):
        """The user asked to check the jobs on the list against the rules."""

    class FiltersAsked(Message):
        """The user asked to change the filters in force."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("slash", "search", "search"),
        Binding("g", "score", "score these", priority=False),
        Binding("j", "judge", "check rules"),
        Binding("f", "filters", "filters"),
        Binding("o", "open_job", "open"),
        Binding("enter", "open_job", "open", show=False),
        Binding("u", "vote_up", "good"),
        Binding("d", "vote_down", "bad"),
        Binding("n", "note", "note"),
        Binding("x", "clear_label", "clear", show=False),
    ]
    DEFAULT_CSS = f"""
    JobsPane #search-box {{ height: 3; }}
    JobsPane #search-row {{ height: 1; }}
    JobsPane #search-mark {{ width: 2; color: {AMBER}; text-style: bold; }}
    JobsPane #search {{ width: 1fr; }}
    JobsPane #search-chips {{ width: auto; max-width: 60%; }}
    JobsPane #search-count {{
        width: auto; min-width: 14; text-align: right; color: {TEXT}; text-style: bold;
    }}
    JobsPane #body {{ height: 1fr; }}
    JobsPane #list-box {{ width: 68%; height: 100%; }}
    JobsPane #jobs-table {{ height: 1fr; }}
    JobsPane #details-box {{ width: 1fr; height: 100%; }}
    JobsPane #jobs-status {{ height: 1; color: {MUTED}; }}
    """

    def __init__(self, host: JobsHost, *, id: str) -> None:  # noqa: A002  # Textual's name
        """Build the tab over the app that holds it.

        Args:
            host: The app, which holds the service and the jobs.
            id: The widget's id.
        """
        super().__init__(id=id)
        self._host = host
        self._query = ""
        self._limit = _PAGE
        self._matched: list[JobRow] = []
        self._shown: list[_Shown] = []
        self._selected: PostingId | None = None
        self._details: dict[PostingId, PostingDetail] = {}
        self._title_width = 0
        self._drawn: tuple[Text, str | None, Text] | None = None

    @override
    def compose(self) -> ComposeResult:
        """Lay out the search box, the list and the details."""
        with Vertical(id="search-box", classes="box") as box:
            box.border_title = "Search"
            with Horizontal(id="search-row"):
                yield Static(PROMPT_MARK, id="search-mark", markup=False)
                yield Input(placeholder="title or company", id="search")
                yield Static(id="search-chips", markup=False)
                yield Static(id="search-count", markup=False)
        with Horizontal(id="body"):
            with Vertical(id="list-box", classes="box") as jobs_box:
                jobs_box.border_title = "Jobs"
                yield DataTable(id="jobs-table", cursor_type="row", show_cursor=True)
            with VerticalScroll(id="details-box", classes="box") as details_box:
                details_box.border_title = "Details"
                yield Static(id="job-head", markup=False)
                yield Markdown(id="job-text", open_links=False)
                yield Static(id="job-link", markup=False)
        yield Static(id="jobs-status", markup=False)

    def on_mount(self) -> None:
        """Put the keys on the list, and show the filters in force."""
        self.query_one("#jobs-table", DataTable).focus()
        self.show_filters()

    def show_filters(self) -> None:
        """Draw the filters in force."""
        stored = self._host.service.instructions()
        saved = (
            []
            if stored is None
            else [
                f"{name}: {value}"
                for name, value in filter_items(json.loads(stored.filters_json))
            ]
        )
        self.query_one("#search-chips", Static).update(
            chips(self._host.filter_flags, self._host.source_filter, saved)
        )

    def on_resize(self, _event: Resize) -> None:
        """Give the title column whatever the other columns leave."""
        width = self.query_one("#list-box").size.width - _LIST_CHROME
        others = _SCORE_WIDTH + _COMPANY_WIDTH + _WHERE_WIDTH + _AGE_WIDTH
        title = max(width - others - 4 * _CELL_PADDING, _MIN_TITLE)
        if title == self._title_width:
            return
        self._title_width = title
        table = self.query_one("#jobs-table", DataTable)
        table.clear(columns=True)
        table.add_column("SCORE", width=_SCORE_WIDTH, key="score")
        table.add_column("TITLE", width=title, key="title")
        table.add_column("COMPANY", width=_COMPANY_WIDTH, key="company")
        table.add_column("WHERE", width=_WHERE_WIDTH, key="where")
        table.add_column("AGE", width=_AGE_WIDTH, key="age")
        self._shown = []
        self.redraw()

    def redraw(self) -> None:
        """Bring the list up to date, keeping the cursor on its job."""
        jobs = self._host.jobs
        pattern = _matcher(self._query)
        self._matched = [row for row in jobs.ranked() if matches(row, pattern)]
        self._draw_rows()
        self._draw_status()
        self.query_one("#search-count", Static).update(
            f"{len(self._matched):,} of {len(jobs.rows):,}"
        )

    def _draw_rows(self) -> None:
        """Rebuild the table when what it shows has changed."""
        visible = self._matched[: self._limit]
        shown = [
            _Shown(
                row.posting.id,
                None if row.score is None else row.score.score,
                row.state,
                None if row.verdict is None else row.verdict.keep,
                row.is_new,
            )
            for row in visible
        ]
        if shown == self._shown or self._title_width == 0:
            return
        self._shown = shown
        table = self.query_one("#jobs-table", DataTable)
        now = self._host.now()
        table.clear()
        scoring_left = self._host.concurrency
        for row in visible:
            is_scoring = row.state is ScoreState.QUEUED and scoring_left > 0
            scoring_left -= is_scoring
            self._add_row(table, row, now, scoring=is_scoring)
        self._restore_cursor(table, visible)

    def _add_row(
        self, table: DataTable[Text], row: JobRow, now: datetime, *, scoring: bool
    ) -> None:
        """Add one job to the table."""
        posting = row.posting
        dropped = row.verdict is not None and not row.verdict.keep
        title_style = MUTED if dropped else TEXT
        flag = "? " if posting.low_confidence else ""
        table.add_row(
            _score_cell(row, scoring=scoring),
            Text(f"{flag}{clean(posting.title)}", style=title_style, no_wrap=True),
            Text(clean(posting.company), style=MUTED, no_wrap=True),
            Text(clean(where_text(posting)), style=MUTED, no_wrap=True),
            (
                Text("new", style=f"bold {MINT}", justify="right")
                if row.is_new
                else Text(age_text(posting, now), style=MUTED, justify="right")
            ),
            key=str(posting.id),
        )

    def _restore_cursor(self, table: DataTable[Text], visible: list[JobRow]) -> None:
        """Keep the cursor on the selected job, wherever it moved to."""
        for number, row in enumerate(visible):
            if row.posting.id == self._selected:
                table.move_cursor(row=number, animate=False, scroll=False)
                return
        if visible:
            table.move_cursor(row=0, animate=False)
            self._select(visible[0].posting.id)

    def _draw_status(self) -> None:
        """Say how scoring stands, or remind what the list is."""
        jobs = self._host.jobs
        text = Text(no_wrap=True, overflow="ellipsis")
        if jobs.score_error is not None:
            text.append("Scoring did not run: ", style=CORAL)
            text.append(clean(str(jobs.score_error)), style=MUTED)
        elif (queued := jobs.queued) > 0:
            text.append(f"scoring, {queued} left", style=AMBER)
            text.append("  best first as they arrive. Keep working.", style=MUTED)
        elif jobs.score_report is not None:
            report = jobs.score_report
            text.append("scored ", style=MINT)
            text.append(
                f"{report.done + report.cached} jobs, ${report.spent:.4f} of "
                f"${report.cap:.2f}, saved for next time",
                style=MUTED,
            )
        else:
            text.append(
                "Results come from saved jobs. Fetching and scoring never block you.",
                style=MUTED,
            )
        self.query_one("#jobs-status", Static).update(text)

    @on(Input.Changed, "#search")
    def _typed(self, event: Input.Changed) -> None:
        """Filter the list as the user types."""
        self._query = event.value
        self._limit = _PAGE
        self.redraw()

    @on(Input.Submitted, "#search")
    def _submitted(self) -> None:
        """Move from the search box to the list."""
        self.query_one("#jobs-table", DataTable).focus()

    def on_key(self, event: Key) -> None:
        """Let the arrow keys and escape leave the search box for the list."""
        search = self.query_one("#search", Input)
        if search.has_focus and event.key in {"down", "escape"}:
            self.query_one("#jobs-table", DataTable).focus()
            event.stop()

    @on(DataTable.RowHighlighted, "#jobs-table")
    def _highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Show the job under the cursor, and grow the list at its end."""
        key = event.row_key.value
        if key is None:
            return
        self._select(PostingId(int(key)))
        at_end = event.cursor_row >= len(self._shown) - 1
        if at_end and len(self._matched) > self._limit:
            self._limit += _PAGE
            self.redraw()

    def _select(self, posting_id: PostingId) -> None:
        """Show a job now, and fetch its description if it has none."""
        self._selected = posting_id
        self._draw_details()
        if posting_id not in self._details:
            detail = self._host.service.saved_detail(posting_id)
            self._details[posting_id] = detail
            self._draw_details()
            if detail.description is None:
                self._fetch_description(posting_id)

    def _draw_details(self) -> None:
        """Draw the selected job, as text the mouse can select and copy.

        Each part is redrawn only when it changes, so a selection survives the
        redraws that fetching and scoring cause.
        """
        selected = self._selected
        if selected is None or selected not in self._details:
            self._show_details(Text("Nothing selected", style=MUTED), None, Text())
            return
        row = self._host.jobs.rows.get(selected)
        detail = self._details[selected]
        note = "Not scored yet. Press g."
        if row is not None and row.state is ScoreState.QUEUED:
            note = "Scoring. Everything else still works."
        elif row is not None and row.state is ScoreState.MISSED:
            note = f"No score: {row.missed}"
        head = posting_head(
            detail,
            self._host.now(),
            score=None if row is None else row.score,
            verdict=None if row is None else row.verdict,
            note=note,
        )
        description = None
        if detail.description is None:
            head = Text("\n").join([head, unavailable_text(detail)])
        else:
            description = clean_markdown(detail.description)
        self._show_details(head, description, posting_link(detail))

    def _show_details(self, head: Text, description: str | None, link: Text) -> None:
        """Put a job's parts on screen, touching only the ones that changed."""
        drawn = self._drawn
        if drawn is None or drawn[0] != head:
            self.query_one("#job-head", Static).update(head)
        if drawn is None or drawn[1] != description:
            text = self.query_one("#job-text", Markdown)
            text.display = description is not None
            text.update(description or "")
        if drawn is None or drawn[2] != link:
            self.query_one("#job-link", Static).update(link)
        self._drawn = (head, description, link)

    @work(exclusive=True, group="description")
    async def _fetch_description(self, posting_id: PostingId) -> None:
        """Fetch a job's description, and show it if the job is still selected."""
        saved = self._details[posting_id]
        self._details[posting_id] = replace(saved, unavailable_reason="fetching…")
        self._draw_details()
        try:
            detail = await self._host.service.detail(posting_id)
        except (InvalidValueError, SourcesError) as error:
            detail = replace(saved, unavailable_reason=str(error))
        self._details[posting_id] = detail
        if self._selected == posting_id:
            self._draw_details()

    def refresh_details(self) -> None:
        """Redraw the selected job, whose score or verdict may have arrived."""
        self._draw_details()

    def action_search(self) -> None:
        """Move to the search box."""
        self.query_one("#search", Input).focus()

    def visible_ids(self, limit: int = _SCORE_LIMIT) -> list[PostingId]:
        """List the jobs on the list that have no score yet, newest first."""
        waiting = [row for row in self._matched if row.score is None]
        return [row.posting.id for row in waiting[:limit]]

    def checkable_ids(self, limit: int = _SCORE_LIMIT) -> list[PostingId]:
        """List the jobs on the list that have no verdict yet."""
        waiting = [row for row in self._matched if row.verdict is None]
        return [row.posting.id for row in waiting[:limit]]

    def action_score(self) -> None:
        """Score the jobs on the list."""
        self.post_message(self.ScoreAsked())

    def action_judge(self) -> None:
        """Check the jobs on the list against the rules."""
        self.post_message(self.JudgeAsked())

    def action_filters(self) -> None:
        """Change the filters in force."""
        self.post_message(self.FiltersAsked())

    def action_open_job(self) -> None:
        """Open the selected job's page in the browser."""
        row = self._host.jobs.rows.get(self._selected or PostingId(0))
        if row is not None and row.posting.url.startswith(("http://", "https://")):
            self.app.open_url(row.posting.url)
            self.notify(f"Opened {clean(row.posting.title)}", markup=False)

    def action_vote_up(self) -> None:
        """Label the selected job good."""
        self._vote(Vote.UP)

    def action_vote_down(self) -> None:
        """Label the selected job bad."""
        self._vote(Vote.DOWN)

    def action_clear_label(self) -> None:
        """Remove the label on the selected job."""
        if self._selected is None:
            return
        self._host.service.clear_label(self._selected)
        self._details.pop(self._selected, None)
        self._select(self._selected)
        self.notify("Label cleared", markup=False)

    def action_note(self) -> None:
        """Write a note on the selected job."""
        selected = self._selected
        if selected is None or selected not in self._details:
            return
        label = self._details[selected].label
        current = "" if label is None or label.note is None else label.note

        def saved(note: str | None) -> None:
            if note is None:
                return
            vote = Vote.UP if label is None else label.vote
            self._save_label(selected, vote, note or None)

        self.app.push_screen(NoteScreen(current), saved)

    def _vote(self, vote: Vote) -> None:
        """Label the selected job, keeping any note it has."""
        selected = self._selected
        if selected is None:
            return
        detail = self._details.get(selected)
        note = None if detail is None or detail.label is None else detail.label.note
        self._save_label(selected, vote, note)

    @work(group="label")
    async def _save_label(
        self, posting_id: PostingId, vote: Vote, note: str | None
    ) -> None:
        """Store a label, and show it."""
        try:
            detail = await self._host.service.label(posting_id, vote, note)
        except (InvalidValueError, SourcesError) as error:
            self.notify(str(error), severity="error", markup=False)
            return
        self._details[posting_id] = detail
        word = "good" if vote is Vote.UP else "bad"
        self.notify(f"Labeled {word}", markup=False)
        if self._selected == posting_id:
            self._draw_details()
