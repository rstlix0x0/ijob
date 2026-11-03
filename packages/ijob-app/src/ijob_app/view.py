"""How ijob looks on a terminal: the colors, symbols and parts every command shares.

Each function builds a Rich renderable from plain data, and prints nothing, so
the command line and its tests decide where it goes. Text that comes from job
sources, models or the user reaches the screen only as `Text`, after `clean`,
so it cannot carry markup or terminal escape sequences.

A command uses these only when its output is a terminal. In a pipe it prints the
plain tab-separated lines scripts rely on, so `is_terminal` is the switch.
"""

import sys
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Final

from ijob_core.model import Posting, ResumeProfile, Score, SightingDraft, Verdict
from ijob_core.money import money_text
from ijob_core.store import SourceRow
from ijob_core.traces import SpanNode, TraceSummary
from rich import box
from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from typer import rich_utils

from ijob_app.job_search import JobList, JobRow, ScoreState
from ijob_app.service import FetchProgress, PostingDetail, SourceEvent

GROUND: Final = "#0e1117"
SELECTED: Final = "#232c3b"
BORDER: Final = "#323c4d"
TEXT: Final = "#dde2ea"
MUTED: Final = "#98a2b3"
AMBER: Final = "#f2b24d"
MINT: Final = "#6fe0b2"
SKY: Final = "#86b6ff"
CORAL: Final = "#ff9d90"

PROMPT_MARK: Final = "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK} "

STRONG_FIT: Final = 80
GOOD_FIT: Final = 60
MAYBE_FIT: Final = 40

_SCORE_BAR: Final = 5
_WIDE_BAR: Final = 20
_PROGRESS_BAR: Final = 8
_SECONDS_PER_HOUR: Final = 3600
_HOURS_PER_DAY: Final = 24
_NS_PER_MS: Final = 1_000_000
_CONTROL_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})
_REASONS_SHOWN: Final = 5
_REASONS_IN_LIST: Final = 2
_RECIPE_JOBS_SHOWN: Final = 5
_REASON_INDENT: Final = 17


def _stdout_is_terminal() -> bool:
    """Ask the process whether standard output is a terminal."""
    return sys.stdout is not None and sys.stdout.isatty()


def is_terminal() -> bool:
    """Say whether standard output is a terminal, so a command can draw on it."""
    return _stdout_is_terminal()


def theme_help() -> None:
    """Draw `--help` and usage errors in ijob's colors."""
    rich_utils.STYLE_OPTION = f"bold {AMBER}"
    rich_utils.STYLE_SWITCH = f"bold {SKY}"
    rich_utils.STYLE_NEGATIVE_OPTION = f"bold {MUTED}"
    rich_utils.STYLE_NEGATIVE_SWITCH = f"bold {MUTED}"
    rich_utils.STYLE_TYPES = MUTED
    rich_utils.STYLE_USAGE = AMBER
    rich_utils.STYLE_HELPTEXT = MUTED
    rich_utils.STYLE_OPTION_DEFAULT = MUTED
    rich_utils.STYLE_REQUIRED_SHORT = CORAL
    rich_utils.STYLE_REQUIRED_LONG = CORAL
    rich_utils.STYLE_OPTIONS_PANEL_BORDER = BORDER
    rich_utils.STYLE_COMMANDS_PANEL_BORDER = BORDER
    rich_utils.STYLE_COMMANDS_TABLE_FIRST_COLUMN = f"bold {AMBER}"
    rich_utils.STYLE_ERRORS_PANEL_BORDER = CORAL
    rich_utils.STYLE_ERRORS_SUGGESTION = MUTED
    rich_utils.STYLE_ABORTED = CORAL


def make_console(*, stderr: bool = False) -> Console:
    """Build the console a command draws on.

    Args:
        stderr: Whether to draw on standard error instead of standard output.
    """
    return Console(stderr=stderr, highlight=False)


def clean(text: str) -> str:
    """Make outside text safe to draw: one line, with no control characters."""
    joined = " ".join(text.split())
    return "".join(
        char for char in joined if unicodedata.category(char) not in _CONTROL_CATEGORIES
    )


def _plain(text: str, style: str = TEXT) -> Text:
    """Wrap outside text so it is drawn literally."""
    return Text(clean(text), style=style, no_wrap=True, overflow="ellipsis")


def score_color(score: int) -> str:
    """Pick the color of a score: the stronger the fit, the brighter."""
    if score >= STRONG_FIT:
        return MINT
    if score >= GOOD_FIT:
        return SKY
    if score >= MAYBE_FIT:
        return TEXT
    return MUTED


def bar(value: float, width: int) -> str:
    """Draw a value from 0 to 100 as a bar of a given width."""
    filled = round(max(0.0, min(100.0, value)) / 100 * width)
    return "█" * filled + "░" * (width - filled)


def score_text(score: Score) -> Text:
    """Draw a score as its number and a bar, so it reads without color."""
    style = f"bold {score_color(score.score)}"
    if score.score < STRONG_FIT:
        style = score_color(score.score)
    return Text(f"{score.score:>3} {bar(score.score, _SCORE_BAR)}", style=style)


def age_text(posting: Posting, now: datetime) -> str:
    """Write how long ago a job was posted, or first seen when no date is known."""
    moment = posting.posted_at or posting.first_seen
    seconds = (now - moment).total_seconds()
    hours = max(int(seconds // _SECONDS_PER_HOUR), 0)
    if hours < _HOURS_PER_DAY:
        return f"{hours}h"
    return f"{hours // _HOURS_PER_DAY}d"


def ago_text(moment: datetime, now: datetime) -> str:
    """Write how long ago something happened, for a list of sources."""
    seconds = (now - moment).total_seconds()
    hours = int(seconds // _SECONDS_PER_HOUR)
    if hours >= _HOURS_PER_DAY:
        return f"{hours // _HOURS_PER_DAY}d ago"
    if hours:
        return f"{hours}h ago"
    minutes = int(seconds // 60)
    return f"{minutes}m ago" if minutes else "now"


def where_text(posting: Posting) -> str:
    """Write where a job is, or a dash when it does not say."""
    return posting.location or "-"


def boxed(title: str, body: RenderableType, *, accent: bool = False) -> Panel:
    """Put something in the rounded box every list and detail view uses."""
    title_text = Text(clean(title), style=f"bold {AMBER if accent else TEXT}")
    return Panel(
        body,
        title=title_text,
        title_align="left",
        border_style=BORDER,
        box=box.ROUNDED,
        padding=(0, 1),
    )


def ok_line(text: str, detail: str = "") -> Text:
    """Draw a finished result: a tick, what was done and any detail."""
    line = Text(" ✓ ", style=f"bold {MINT}")
    line.append(clean(text), style=TEXT)
    if detail:
        line.append(f"  {clean(detail)}", style=MUTED)
    return line


def failed_line(text: str, detail: str = "") -> Text:
    """Draw a failure: a cross, what failed and why."""
    line = Text(" ✗ ", style=f"bold {CORAL}")
    line.append(clean(text), style=TEXT)
    if detail:
        line.append(f"  {clean(detail)}", style=MUTED)
    return line


def notice_line(text: str, detail: str = "") -> Text:
    """Draw something that needs the user's attention."""
    line = Text(" ! ", style=f"bold {AMBER}")
    line.append(clean(text), style=TEXT)
    if detail:
        line.append(f"  {clean(detail)}", style=MUTED)
    return line


def fix_line(command: str) -> Text:
    """Draw the command that fixes a problem shown just above."""
    line = Text("   fix  ", style=f"bold {AMBER}")
    line.append(command, style=SKY)
    return line


def next_line(*commands: str) -> Text:
    """Draw the commands that usually come next."""
    line = Text(" next  ", style=f"bold {MUTED}")
    line.append("   ".join(commands), style=SKY)
    return line


def error_lines(message: str, fix: str | None = None) -> Group:
    """Draw an error and, when there is one, its fix."""
    lines: list[RenderableType] = [failed_line(message)]
    if fix is not None:
        lines.append(fix_line(fix))
    return Group(*lines)


def progress_text(label: str, done: int, total: int, *, failed: int = 0) -> Text:
    """Draw running work: what it is, how many are done and how far that is."""
    share = 100 * done / total if total else 100
    line = Text(" ● ", style=AMBER)
    line.append(f"{label} {done}/{total} ", style=TEXT)
    line.append(bar(share, _PROGRESS_BAR), style=AMBER)
    if failed:
        line.append(f"  {failed} failed", style=CORAL)
    return line


def _score_cell(row: JobRow, *, scoring: bool) -> Text:
    """Draw a job's score, or where it stands while it has none."""
    if row.score is not None:
        return score_text(row.score)
    match row.state:
        case ScoreState.QUEUED:
            word = "scoring" if scoring else "queued"
            return Text(f" {word}", style=AMBER if scoring else MUTED)
        case ScoreState.MISSED:
            return Text(" missed", style=CORAL)
        case ScoreState.NONE | ScoreState.SCORED:
            return Text("  -", style=MUTED)


def _rules_cell(row: JobRow) -> Text:
    """Draw what a model made of a job against the user's rules."""
    if row.verdict is None:
        return Text(" ·", style=MUTED)
    if row.verdict.keep:
        return Text(" ✓", style=f"bold {MINT}")
    return Text(" ✗", style=f"bold {CORAL}")


def _jobs_table(
    *, with_score: bool, with_rules: bool = False, header: bool = True
) -> Table:
    """Build an empty table of jobs, with the columns every job list shares."""
    table = Table(
        box=None,
        expand=True,
        show_header=header,
        pad_edge=False,
        padding=(0, 1, 0, 0),
        header_style=f"bold {MUTED}",
    )
    if with_score:
        table.add_column("SCORE", width=9, no_wrap=True)
    if with_rules:
        table.add_column("RULES", width=5, no_wrap=True)
    table.add_column("ID", width=6, no_wrap=True, justify="right", style=MUTED)
    table.add_column("TITLE", ratio=5, no_wrap=True, overflow="ellipsis", style=TEXT)
    table.add_column("COMPANY", ratio=2, no_wrap=True, overflow="ellipsis", style=MUTED)
    table.add_column("WHERE", ratio=2, no_wrap=True, overflow="ellipsis", style=MUTED)
    table.add_column("AGE", width=4, no_wrap=True, justify="right")
    return table


def _add_job(  # noqa: PLR0913  # what a row shows, one each
    table: Table,
    row: JobRow,
    now: datetime,
    *,
    with_score: bool,
    scoring: bool,
    with_rules: bool = False,
) -> None:
    """Add one job to a table of jobs."""
    posting = row.posting
    age = (
        Text("new", style=f"bold {MINT}")
        if row.is_new
        else Text(age_text(posting, now), style=MUTED)
    )
    cells: list[RenderableType] = []
    if with_score:
        cells.append(_score_cell(row, scoring=scoring))
    if with_rules:
        cells.append(_rules_cell(row))
    flag = "? " if posting.low_confidence else ""
    cells += [
        str(posting.id),
        _plain(f"{flag}{posting.title}"),
        _plain(posting.company, MUTED),
        _plain(where_text(posting), MUTED),
        age,
    ]
    table.add_row(*cells)


def search_title(title: str | None, count: int, total: int | None = None) -> str:
    """Write the heading of a job list."""
    head = title or "all jobs"
    of = f" of {total:,}" if total is not None else ""
    noun = "job" if count == 1 and total is None else "jobs"
    return f"{head}  ·  {count:,}{of} {noun}"


def live_search(  # noqa: PLR0913  # what the view shows, one each
    jobs: JobList,
    now: datetime,
    *,
    title: str | None,
    with_score: bool,
    room: int,
    concurrency: int,
    spent_note: str = "",
    with_rules: bool = False,
) -> Group:
    """Draw a search while it runs.

    Args:
        jobs: What the search has found so far.
        now: The current time, for each job's age.
        title: What the user searched for.
        with_score: Whether jobs are being scored.
        room: How many jobs fit on the screen.
        concurrency: How many scores run at once.
        spent_note: Text for the end of the status line.
        with_rules: Whether jobs are being checked against the user's rules.

    Returns:
        The box of jobs and the status line below it.
    """
    ranked = jobs.ranked()
    table = _jobs_table(with_score=with_score, with_rules=with_rules)
    shown = 0
    scoring_left = concurrency
    for row in ranked[:room]:
        is_scoring = row.state is ScoreState.QUEUED and scoring_left > 0
        scoring_left -= is_scoring
        _add_job(
            table,
            row,
            now,
            with_score=with_score,
            scoring=is_scoring,
            with_rules=with_rules,
        )
        shown += 1
    body: list[RenderableType] = [table]
    if (left := len(ranked) - shown) > 0:
        body.append(Text(f"+ {left:,} more when this ends", style=MUTED))
    if not ranked and jobs.loaded:
        body.append(Text("No saved job matches yet.", style=MUTED))
    panel = boxed(search_title(title, len(ranked)), Group(*body), accent=True)
    status = _live_status(
        jobs, with_score=with_score, note=spent_note, with_rules=with_rules
    )
    return Group(panel, status)


def _live_status(
    jobs: JobList, *, with_score: bool, note: str, with_rules: bool = False
) -> Text:
    """Draw how far the running parts of a search have got."""
    line = Text()
    if jobs.fetch is not None:
        line.append_text(
            progress_text(
                "fetching",
                jobs.fetch.done,
                jobs.fetch.total,
                failed=jobs.fetch.failed,
            )
        )
        if jobs.new_jobs:
            line.append(f"  +{jobs.new_jobs:,} new", style=MINT)
    if with_rules and jobs.checking:
        checked = sum(row.verdict is not None for row in jobs.rows.values())
        line.append_text(progress_text("checking", checked, checked + jobs.checking))
    if with_score and (queued := jobs.queued):
        total = queued + sum(
            row.state is ScoreState.SCORED for row in jobs.rows.values()
        )
        line.append_text(
            progress_text("scoring", total - queued, total),
        )
    if note:
        line.append(f"  {note}", style=MUTED)
    line.append("  ctrl+c stops, all saved", style=MUTED)
    return line


def final_search(  # noqa: PLR0913  # what the view shows, one each
    jobs: JobList,
    now: datetime,
    *,
    title: str | None,
    with_score: bool,
    limit: int | None,
    with_rules: bool = False,
    show_dropped: bool = False,
) -> Panel:
    """Draw a search's result, which stays in the scrollback.

    Args:
        jobs: What the search found.
        now: The current time, for each job's age.
        title: What the user searched for.
        with_score: Whether to show scores and their reasons.
        limit: The most jobs to show, or `None` for all of them.
        with_rules: Whether to show what a model made of them against the rules.
        show_dropped: Whether to keep the jobs that break a rule on the list.

    Returns:
        The box of jobs, best first.
    """
    ranked = [
        row
        for row in jobs.ranked()
        if show_dropped or row.verdict is None or row.verdict.keep
    ]
    shown = ranked if limit is None else ranked[:limit]
    body: list[RenderableType] = []
    table = _jobs_table(with_score=with_score, with_rules=with_rules)
    for row in shown:
        _add_job(
            table,
            row,
            now,
            with_score=with_score,
            scoring=False,
            with_rules=with_rules,
        )
        if with_score and row.score is not None:
            body += [table, *_indented_reasons(row.score)]
            table = _jobs_table(
                with_score=with_score, with_rules=with_rules, header=False
            )
        elif with_rules and row.verdict is not None and not row.verdict.keep:
            body += [table, _dropped_reason(row.verdict.reason)]
            table = _jobs_table(
                with_score=with_score, with_rules=with_rules, header=False
            )
    if table.row_count or not body:
        body.append(table)
    if (left := len(ranked) - len(shown)) > 0:
        body.append(Text(f"+ {left:,} more, add --all to list them", style=MUTED))
    if not ranked:
        body.append(Text("No job matches.", style=MUTED))
    order = "best first" if with_score else "newest first"
    return boxed(f"{search_title(title, len(ranked))}  ·  {order}", Group(*body))


def _dropped_reason(reason: str) -> Text:
    """Draw why a job breaks a rule, under its row."""
    line = Text(" " * _REASON_INDENT, no_wrap=True, overflow="ellipsis")
    line.append(clean(reason), style=MUTED)
    return line


def _indented_reasons(score: Score) -> list[Text]:
    """Draw a job's reasons under its row, using the whole width of the box."""
    return [
        Text.assemble(" " * _REASON_INDENT, _reason(reason), no_wrap=True)
        for reason in score.reasons[:_REASONS_IN_LIST]
    ]


def _reason(text: str) -> Text:
    """Draw one reason for a score."""
    line = Text("+ ", style=MINT, no_wrap=True, overflow="ellipsis")
    line.append(clean(text), style=MUTED)
    return line


def recipe_view(
    recipe_format: str, url: str, invalid: int, jobs: Sequence[SightingDraft]
) -> Panel:
    """Draw what a learned recipe reads, so the user can decide whether to keep it.

    Args:
        recipe_format: The kind of document the recipe reads.
        url: The address it reads.
        invalid: How many entries it selected that were not jobs.
        jobs: The jobs it read.

    Returns:
        The box holding a heading and the first jobs.
    """
    heading = Text.assemble(
        (f"{len(jobs)} jobs read", f"bold {TEXT}"),
        (f", {invalid} invalid  ·  {clean(recipe_format)}  ·  ", MUTED),
        (clean(url), SKY),
    )
    table = Table(
        box=None,
        expand=True,
        pad_edge=False,
        padding=(0, 1, 0, 0),
        header_style=f"bold {MUTED}",
    )
    table.add_column("TITLE", ratio=4, no_wrap=True, overflow="ellipsis")
    table.add_column("COMPANY", ratio=2, no_wrap=True, overflow="ellipsis")
    table.add_column("WHERE", ratio=2, no_wrap=True, overflow="ellipsis")
    table.add_column("SALARY", ratio=2, no_wrap=True, overflow="ellipsis")
    for job in jobs[:_RECIPE_JOBS_SHOWN]:
        salary = "; ".join(money_text(money) for money in job.salaries)
        table.add_row(
            _plain(job.title),
            _plain(job.company, MUTED),
            _plain(job.location or "-", MUTED),
            _plain(salary or "-", MUTED),
        )
    return boxed("What this recipe reads", Group(heading, Text(), table), accent=True)


def unread_table(rows: Iterable[tuple[str, str, str]]) -> Panel:
    """Draw the items a model could not read, each with why.

    Args:
        rows: For each item, its source, the start of its text and the reason.

    Returns:
        The box of items.
    """
    table = Table(
        box=None,
        expand=True,
        pad_edge=False,
        padding=(0, 1, 0, 0),
        header_style=f"bold {MUTED}",
    )
    table.add_column("SOURCE", ratio=2, no_wrap=True, overflow="ellipsis")
    table.add_column("ITEM", ratio=4, no_wrap=True, overflow="ellipsis")
    table.add_column("WHY", ratio=4, no_wrap=True, overflow="ellipsis")
    for source, preview, reason in rows:
        table.add_row(_plain(source), _plain(preview, MUTED), _plain(reason, CORAL))
    return boxed("Still unread", table)


def extract_status(total: int, *, read: int, added: int, not_jobs: int) -> Text:
    """Draw how far reading skipped jobs has got."""
    if not total:
        return Text(" ● Looking for skipped jobs", style=AMBER)
    line = progress_text("reading", read, total)
    line.append(f"  {added} jobs, {not_jobs} not jobs", style=MUTED)
    line.append("  ctrl+c stops, all saved", style=MUTED)
    return line


def source_event_line(event: SourceEvent, failure_text: str | None) -> Text:
    """Draw how one source's fetch ended.

    Args:
        event: How it ended.
        failure_text: The reason, when it failed.
    """
    if event.failure is not None:
        return failed_line(str(event.ref), failure_text or "failed")
    detail = f"+{event.new} new, {event.listed:,} open"
    if event.rejected:
        detail += f", {event.rejected} skipped"
    return ok_line(str(event.ref), detail)


def fetch_status(progress: FetchProgress, new_jobs: int) -> Text:
    """Draw the running status of a fetch."""
    line = progress_text(
        "fetching", progress.done, progress.total, failed=progress.failed
    )
    line.append(f"  +{new_jobs:,} new so far", style=MUTED)
    line.append("  ctrl+c stops, all saved", style=MUTED)
    return line


def posting_detail(
    detail: PostingDetail,
    now: datetime,
    *,
    score: Score | None,
) -> Panel:
    """Draw one job in a box of its own, as `ijob show` prints it."""
    body = posting_body(detail, now, score=score)
    return boxed(str(detail.posting.id), body)


def posting_body(
    detail: PostingDetail,
    now: datetime,
    *,
    score: Score | None,
    verdict: Verdict | None = None,
    note: str = "",
) -> Group:
    """Draw one job: its facts, its score and reasons, its text and its link.

    Args:
        detail: The posting with its sources, label and description.
        now: The current time, for the job's age.
        score: Its score, if it has one.
        verdict: What a model made of it against the user's rules, if anything.
        note: A line about its score when there is none yet.

    Returns:
        The lines, without a box.
    """
    posting = detail.posting
    salary = ", ".join(money_text(money) for money in posting.salaries)
    facts = "  ·  ".join(
        [
            clean(posting.company),
            clean(where_text(posting)),
            clean(salary) or "no salary stated",
            f"posted {age_text(posting, now)} ago",
        ]
    )
    lines: list[RenderableType] = [
        Text(clean(posting.title), style=f"bold {TEXT}"),
        Text(facts, style=MUTED),
    ]
    if detail.label is not None:
        vote = "good" if str(detail.label.vote) == "up" else "bad"
        note = f": {clean(detail.label.note)}" if detail.label.note else ""
        lines.append(Text(f"You labeled this {vote}{note}", style=SKY))
    if score is not None:
        lines += [Text(), _wide_score(score)]
        lines += [_full_reason(reason) for reason in score.reasons]
    elif note:
        lines += [Text(), Text(note, style=MUTED)]
    if verdict is not None:
        word, color = (
            ("Fits your rules", MINT) if verdict.keep else ("Breaks a rule", CORAL)
        )
        lines += [
            Text(),
            Text.assemble(
                (word, f"bold {color}"), (f": {clean(verdict.reason)}", MUTED)
            ),
        ]
    lines += [Text(), Text("POSTING", style=f"bold {MUTED}")]
    if detail.description is not None:
        lines.append(_body_text(detail.description))
    else:
        reason = detail.unavailable_reason or "no description published"
        lines.append(Text(f"Description unavailable: {clean(reason)}", style=MUTED))
    lines += [Text(), Text(clean(posting.url), style=SKY, overflow="fold")]
    return Group(*lines)


def _full_reason(text: str) -> Text:
    """Draw one reason for a score, wrapping rather than cutting it."""
    line = Text("+ ", style=MINT)
    line.append(clean(text), style=TEXT)
    return line


def _wide_score(score: Score) -> Text:
    """Draw a score with its number and a long bar."""
    color = score_color(score.score)
    line = Text(f"{score.score}", style=f"bold {color}")
    line.append(" / 100  ", style=MUTED)
    line.append(bar(score.score, _WIDE_BAR), style=color)
    return line


def _body_text(text: str) -> Markdown:
    """Draw a description, which is Markdown, with none of its control characters.

    Its links do not become terminal hyperlinks, since the text comes from a
    third party.
    """
    kept = "".join(
        char
        for char in text
        if char == "\n" or unicodedata.category(char) not in _CONTROL_CATEGORIES
    )
    return Markdown(kept, hyperlinks=False)


def sources_table(
    rows: Sequence[SourceRow],
    jobs_by_source: Mapping[str, int],
    now: datetime,
    *,
    limit: int | None = None,
) -> Panel:
    """Draw the sources, biggest first, each with its job count and last fetch.

    Args:
        rows: The sources.
        jobs_by_source: How many jobs each source shows, by reference text.
        now: The current time, for each fetch's age.
        limit: The most sources to show, or `None` for all of them.

    Returns:
        The box of sources.
    """
    ordered = sorted(rows, key=lambda row: -jobs_by_source.get(str(row.ref), 0))
    shown = ordered if limit is None else ordered[:limit]
    table = Table(
        box=None,
        expand=True,
        pad_edge=False,
        padding=(0, 1, 0, 0),
        header_style=f"bold {MUTED}",
    )
    table.add_column("SOURCE", ratio=3, no_wrap=True, overflow="ellipsis", style=TEXT)
    table.add_column("COMPANY", ratio=2, no_wrap=True, overflow="ellipsis", style=MUTED)
    table.add_column("JOBS", width=6, justify="right")
    table.add_column("LAST FETCH", width=11, style=MUTED)
    table.add_column("ADDED BY", width=8, style=MUTED)
    for row in shown:
        fetched = "never"
        if row.last_fetched_at is not None:
            fetched = ago_text(row.last_fetched_at, now)
        table.add_row(
            _plain(str(row.ref)),
            _plain(row.company, MUTED),
            f"{jobs_by_source.get(str(row.ref), 0):,}",
            fetched,
            str(row.origin),
        )
    body: list[RenderableType] = [table]
    if (left := len(rows) - len(shown)) > 0:
        body.append(Text(f"+ {left:,} more, add --all to list them", style=MUTED))
    return boxed(f"{len(rows)} sources", Group(*body))


def profile_view(profile: ResumeProfile) -> Panel:
    """Draw the profile read from a CV in a box of its own."""
    return boxed("Profile from your CV", profile_body(profile))


def profile_body(profile: ResumeProfile) -> Group:
    """Draw the profile read from a CV."""
    years = (
        "years not stated"
        if profile.years_experience is None
        else f"{profile.years_experience} years"
    )
    lines: list[RenderableType] = [
        Text(clean(profile.summary), style=TEXT),
        Text(),
        Text.assemble(
            ("LEVEL  ", f"bold {MUTED}"),
            (f"{clean(str(profile.seniority))}, {years}", TEXT),
        ),
    ]
    for name, values in (
        ("ROLES", profile.roles),
        ("SKILLS", profile.skills),
        ("LANGUAGES", profile.spoken_languages),
        ("LOCATIONS", profile.locations),
        ("WORK AUTHORIZATION", profile.work_authorization),
    ):
        lines += [Text(), Text(name, style=f"bold {MUTED}"), _chips(values)]
    return Group(*lines)


def _chips(values: Iterable[str]) -> Text:
    """Draw a list of short values as chips."""
    line = Text(overflow="fold")
    for value in values:
        line.append(f" {clean(value)} ", style=f"{TEXT} on {SELECTED}")
        line.append(" ")
    if not line:
        line.append("none stated", style=MUTED)
    return line


def instructions_view(
    filter_lines: Iterable[tuple[str, str]],
    rules: Sequence[str],
    *,
    text: str | None = None,
) -> Panel:
    """Draw what the user's words became, in a box of its own."""
    body = instructions_body(filter_lines, rules, text=text)
    return boxed("What you asked for", body, accent=True)


def instructions_body(
    filter_lines: Iterable[tuple[str, str]],
    rules: Sequence[str],
    *,
    text: str | None = None,
) -> Group:
    """Draw what the user's words became: filters and rules.

    Args:
        filter_lines: Each filter's name and value.
        rules: The rules a model checks.
        text: The user's own words, when they are worth repeating.

    Returns:
        The lines, without a box.
    """
    lines: list[RenderableType] = []
    if text is not None:
        lines += [Text(clean(text), style=TEXT), Text()]
    lines.append(
        Text.assemble(
            ("FILTERS", f"bold {MUTED}"),
            ("  free and instant", MUTED),
        )
    )
    filters = [f"{name}: {value}" for name, value in filter_lines]
    lines.append(_chips(filters))
    lines += [
        Text(),
        Text.assemble(
            ("RULES", f"bold {MUTED}"),
            ("  a model checks these when you ask", MUTED),
        ),
    ]
    if not rules:
        lines.append(Text("none", style=MUTED))
    for number, rule in enumerate(rules, start=1):
        lines.append(
            Text.assemble((f"{number} ", f"bold {AMBER}"), (clean(rule), TEXT))
        )
    return Group(*lines)


def traces_table(summaries: Sequence[TraceSummary]) -> Panel:
    """Draw the recorded runs of models, newest first."""
    table = Table(
        box=None,
        expand=True,
        pad_edge=False,
        padding=(0, 1, 0, 0),
        header_style=f"bold {MUTED}",
    )
    table.add_column("TRACE", width=34, no_wrap=True, style=MUTED)
    table.add_column("WHEN", width=16, no_wrap=True, style=MUTED)
    table.add_column("OPERATION", ratio=1, no_wrap=True, overflow="ellipsis")
    table.add_column("CALLS", width=5, justify="right")
    table.add_column("COST", width=8, justify="right")
    table.add_column("TIME", width=8, justify="right", style=MUTED)
    for summary in summaries:
        table.add_row(
            summary.trace_id,
            summary.started.strftime("%Y-%m-%d %H:%M"),
            _plain(summary.operation),
            str(summary.calls),
            f"${summary.cost_usd:.4f}",
            duration_text(summary.duration_ms),
        )
    return boxed(f"{len(summaries)} recent model runs", table)


def duration_text(milliseconds: float) -> str:
    """Write a duration for a person: seconds, or minutes and seconds."""
    seconds = round(milliseconds / 1000)
    minutes, rest = divmod(seconds, 60)
    return f"{minutes}m {rest:02d}s" if minutes else f"{rest}s"


def trace_tree(roots: Iterable[SpanNode]) -> Panel:
    """Draw a trace as a tree of spans, with each model call's tokens and cost."""
    lines: list[RenderableType] = []
    for root in roots:
        lines += _span_lines(root, 0)
    return boxed("Trace", Group(*lines))


def trace_body(roots: Iterable[SpanNode]) -> Group:
    """Draw a trace's spans, without a box."""
    lines: list[RenderableType] = []
    for root in roots:
        lines += _span_lines(root, 0)
    return Group(*lines)


def _span_lines(node: SpanNode, depth: int) -> list[Text]:
    """Draw a span and the spans under it."""
    span = node.span
    attributes = span.attributes
    took = duration_text((span.end_ns - span.start_ns) / _NS_PER_MS)
    line = Text("  " * depth, no_wrap=True, overflow="ellipsis")
    line.append(clean(span.name), style=TEXT)
    if "gen_ai.request.model" in attributes:
        cost = attributes.get("gen_ai.usage.cost")
        cost_text = f"${cost:.4f}" if isinstance(cost, int | float) else "-"
        tokens_in = attributes.get("gen_ai.usage.input_tokens", "-")
        tokens_out = attributes.get("gen_ai.usage.output_tokens", "-")
        line.append(
            f"  in {tokens_in}  out {tokens_out}  {cost_text}  {took}", style=MUTED
        )
    else:
        line.append(f"  {took}", style=MUTED)
    lines = [line]
    for child in node.children:
        lines += _span_lines(child, depth + 1)
    return lines


def score_rows(items: Iterable[tuple[int, str, str, Score | None, str]]) -> Table:
    """Draw scored jobs with their reasons.

    Args:
        items: For each job, its id, title, company, score, and why it has none.

    Returns:
        The table.
    """
    table = Table(
        box=None,
        expand=True,
        pad_edge=False,
        padding=(0, 1, 0, 0),
        header_style=f"bold {MUTED}",
    )
    table.add_column("SCORE", width=9, no_wrap=True)
    table.add_column("ID", width=6, justify="right", style=MUTED)
    table.add_column("TITLE", ratio=5, no_wrap=True, overflow="ellipsis", style=TEXT)
    table.add_column("COMPANY", ratio=2, no_wrap=True, overflow="ellipsis", style=MUTED)
    for posting_id, title, company, score, why_not in items:
        cell = (
            score_text(score)
            if score is not None
            else Text(f" {clean(why_not)}", style=CORAL, no_wrap=True)
        )
        table.add_row(cell, str(posting_id), _plain(title), _plain(company, MUTED))
        if score is not None:
            for reason in score.reasons[:_REASONS_SHOWN]:
                table.add_row("", "", _reason(reason), "")
    return table
